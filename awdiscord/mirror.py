"""mirror -- feed every Company-category Discord event to the Relay bridge.

The owner's ask (2026-11-01): Discord stays the live surface, Aitherium's workspace
becomes the durable, graphable record. The ONE gateway session is the only place
that sees every event, so it is the only place that can hand them over.

    mirror.feed("message", message)               # from on_message
    mirror.feed("message_edit", raw_event)        # on_raw_message_edit
    ...

This module is deliberately part of THIS package, not a lift from `lib/`: the
awdiscord image is built from `packages/awdiscord` alone, so an import of
`lib.integrations.discord_mirror_feed` would be a ModuleNotFoundError inside the
container that looked fine on a host with the repo checked out.

Fire-and-forget: it never raises, never blocks the gateway's event loop (one
daemon thread drains a bounded queue), and never logs a token or a message body.
The receiver is the DiscordRelayBridge, authenticated with the fleet's internal
secret (`X-Internal-Key` + `X-Caller-Type: platform`); the caller's workspace and
audience are NEVER taken from the payload -- the bridge derives them from the
authenticated caller and the guild binding.
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import urllib.request
from typing import Any, Dict, Optional

log = logging.getLogger("awdiscord.mirror")

KINDS = frozenset({"message", "message_edit", "message_delete", "bulk_message_delete",
                   "reaction_add", "reaction_remove", "thread_create", "thread_update",
                   "thread_delete", "channel_update"})

#: Default receiver. The quadlet's NetworkAlias, not the compose container name:
#: inside `aither.network` both resolve, and this one is the one the unit publishes.
DEFAULT_URL = "https://aitheros-discordrelaybridge:8228"
_QUEUE_MAX = 2000
_queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=_QUEUE_MAX)
_started = False
_lock = threading.Lock()
_dropped = 0
#: Events NOT sent because their guild is not mirrored, per guild id ("dm" = no guild).
_refused_guilds: Dict[str, int] = {}


def _g(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _sid(v: Any) -> str:
    return "" if v is None else str(v)


def _author(a: Any) -> Dict[str, Any]:
    if a is None:
        return {}
    return {"id": _sid(_g(a, "id")), "username": _sid(_g(a, "name") or _g(a, "username")),
            "global_name": _sid(_g(a, "global_name") or _g(a, "display_name")),
            "bot": bool(_g(a, "bot", False))}


def _channel(ch: Any) -> Dict[str, Any]:
    if ch is None:
        return {}
    ctype = getattr(_g(ch, "type"), "value", _g(ch, "type"))
    out: Dict[str, Any] = {"id": _sid(_g(ch, "id")), "name": _sid(_g(ch, "name"))}
    if ctype is not None:
        try:
            out["type"] = int(ctype)
        except (TypeError, ValueError):
            out["type_raw"] = str(ctype)      # the bridge re-reads it over REST
    if _g(ch, "parent_id") is not None:
        out["parent_id"] = _sid(_g(ch, "parent_id"))
    return out


def _attachment(a: Any) -> Dict[str, Any]:
    return {"id": _sid(_g(a, "id")), "filename": _sid(_g(a, "filename")),
            "url": _sid(_g(a, "url")), "size": _g(a, "size"),
            "content_type": _sid(_g(a, "content_type"))}


def to_payload(kind: str, obj: Any) -> Dict[str, Any]:
    """A discord.py object or a raw gateway dict -> a plain JSON-able payload."""
    if isinstance(obj, dict) and obj.get("_normalized"):
        return dict(obj)
    if kind == "message_edit":
        data = dict(_g(obj, "data") or (obj if isinstance(obj, dict) else {}))
        data.setdefault("id", _sid(_g(obj, "message_id")))
        data.setdefault("channel_id", _sid(_g(obj, "channel_id")))
        if not data.get("guild_id") and _g(obj, "guild_id") is not None:
            data["guild_id"] = _sid(_g(obj, "guild_id"))
        data["_normalized"] = True
        return data
    if kind in ("message_delete", "bulk_message_delete"):
        return {"id": _sid(_g(obj, "message_id")), "channel_id": _sid(_g(obj, "channel_id")),
                "guild_id": _sid(_g(obj, "guild_id")), "ids": list(_g(obj, "message_ids") or []),
                "_normalized": True}
    if kind in ("reaction_add", "reaction_remove"):
        emoji = _g(obj, "emoji")
        return {"message_id": _sid(_g(obj, "message_id")),
                "channel_id": _sid(_g(obj, "channel_id")),
                "guild_id": _sid(_g(obj, "guild_id")),
                "user_id": _sid(_g(obj, "user_id")),
                "emoji": _sid(_g(emoji, "name") or emoji) if emoji is not None else "",
                "_normalized": True}
    if kind in ("thread_create", "thread_update", "thread_delete"):
        return {"id": _sid(_g(obj, "id")), "name": _sid(_g(obj, "name")),
                "parent_id": _sid(_g(obj, "parent_id")),
                "guild_id": _sid(_g(_g(obj, "guild"), "id") or _g(obj, "guild_id")),
                "type": _g(obj, "type"), "_normalized": True}
    if kind == "channel_update":
        out = _channel(obj)
        out.update({"guild_id": _sid(_g(_g(obj, "guild"), "id") or _g(obj, "guild_id")),
                    "_normalized": True})
        return out
    if kind == "message":
        msg = {"id": _sid(_g(obj, "id")), "channel_id": _sid(_g(obj, "channel_id")),
               "guild_id": _sid(_g(_g(obj, "guild"), "id") or _g(obj, "guild_id")),
               "content": _sid(_g(obj, "content")), "author": _author(_g(obj, "author")),
               "timestamp": _sid(_g(obj, "created_at") or _g(obj, "timestamp")),
               "attachments": [_attachment(a) for a in (_g(obj, "attachments") or [])],
               "_normalized": True}
        ref = _g(obj, "reference")
        if ref is not None:
            msg["reference"] = {"message_id": _sid(_g(ref, "message_id")),
                                "channel_id": _sid(_g(ref, "channel_id"))}
        return msg
    return json.loads(json.dumps(obj, default=str)) if not isinstance(obj, dict) else dict(obj)


def _target() -> str:
    return (os.getenv("AWDISCORD_MIRROR_URL") or DEFAULT_URL).rstrip("/")


def _headers() -> Dict[str, str]:
    h = {"Content-Type": "application/json", "X-Caller-Type": "platform",
         "X-Aither-Caller": "awdiscord"}
    key = os.getenv("AITHER_INTERNAL_SECRET") or os.getenv("AITHER_MASTER_KEY") or ""
    if key:
        h["X-Internal-Key"] = key
    return h


def _post(url: str, body: Dict[str, Any], timeout: float = 20.0) -> int:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers=_headers())
    ctx = None
    bundle = os.getenv("AWDISCORD_CA_BUNDLE") or ""
    if url.startswith("https://") and bundle and os.path.exists(bundle):
        import ssl

        ctx = ssl.create_default_context(cafile=bundle)
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return int(getattr(resp, "status", 0) or 0)


def _drain() -> None:
    """One daemon thread: never raises, never logs a body, drops under backpressure."""
    global _dropped
    url = _target() + "/bridge/discord/event"
    while True:
        item = _queue.get()
        if item is None:
            return
        try:
            _post(url, item)
        except Exception as exc:  # noqa: BLE001 - the mirror must never kill the bot
            log.warning("mirror feed failed: %s", type(exc).__name__)
        finally:
            _queue.task_done()


def _ensure_worker() -> None:
    global _started
    with _lock:
        if _started:
            return
        t = threading.Thread(target=_drain, name="awdiscord-mirror", daemon=True)
        t.start()
        _started = True


def feed(kind: str, obj: Any) -> bool:
    """Queue one event for the bridge. False = not sent (bad kind, no body, full queue).

    Never raises and never blocks: the caller is a discord.py event handler and a
    slow mirror must not delay a reply.
    """
    global _dropped
    if kind not in KINDS or obj is None:
        return False
    try:
        payload = to_payload(kind, obj)
    except Exception as exc:  # noqa: BLE001 - a shape we do not understand is skipped
        log.warning("mirror payload failed: %s", type(exc).__name__)
        return False
    if not payload.get("id") and not payload.get("message_id"):
        return False
    # THE GUILD GATE LIVES HERE, not only in feed_guild(): every caller -- including an
    # older gateway that calls feed() directly -- is held to the allowlist. Measured
    # 2026-11-13: the live bot fed EVERY guild it sits in (partner servers too) to the
    # bridge; the bridge refused them, but their messages had already left the bot.
    gid = _sid(payload.get("guild_id"))
    if not gid or gid not in mirror_guilds():
        key = gid or "dm"
        _refused_guilds[key] = _refused_guilds.get(key, 0) + 1
        return False
    _ensure_worker()
    try:
        _queue.put_nowait({"kind": kind, "payload": payload})
        return True
    except queue.Full:
        _dropped += 1
        if _dropped % 100 == 1:
            log.warning("mirror queue full (%d dropped)", _dropped)
        return False


def mirror_guilds() -> frozenset:
    """Guild ids whose events cross to the bridge. Empty = the mirror is off.

    ``AWDISCORD_MIRROR_GUILDS`` (comma ids) wins; otherwise the home guild
    (``AITHER_DISCORD_HOME_GUILD_ID``). A partner server is never fed unless an
    operator names it here -- the bridge's privacy policy is the second gate, not
    the only one.
    """
    raw = os.getenv("AWDISCORD_MIRROR_GUILDS")
    if raw is None:
        raw = os.getenv("AITHER_DISCORD_HOME_GUILD_ID") or ""
    return frozenset(g.strip() for g in raw.split(",") if g.strip()
                     and g.strip().lower() not in ("0", "off", "none"))


def feed_guild(kind: str, obj: Any, guild_id: Any) -> bool:
    """:func:`feed`, but only for a guild in :func:`mirror_guilds`. DMs never cross."""
    gid = _sid(guild_id)
    if not gid or gid not in mirror_guilds():
        return False
    return feed(kind, obj)


def stats() -> Dict[str, Any]:
    return {"queued": _queue.qsize(), "dropped": _dropped, "target": _target(),
            "guilds": sorted(mirror_guilds()), "refused_guilds": dict(_refused_guilds),
            "internal_key": bool(os.getenv("AITHER_INTERNAL_SECRET"))}


__all__ = ["DEFAULT_URL", "KINDS", "feed", "feed_guild", "mirror_guilds", "stats",
           "to_payload"]
