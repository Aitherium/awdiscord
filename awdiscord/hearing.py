"""What Aither hears and answers in one server -- every switch, read from where it lives.

The switches already exist, scattered: env lines the operator sets, per-server rows in
the ``GuildStore`` (``core/voice_calls``, ``core/reply_visibility``,
``moderation/ignore:*``, plugin state). This module adds NO setting and NO store. It
reads each switch through the function that enforces it, so the view cannot disagree
with the behaviour, and it changes the two a server may change through their existing
setters, writing who/when/what to an audit trail in the same store.

    rows = snapshot(guild, store, host=host)        # every switch, most important first
    view(rows)                                      # {"shown", "more", "total"} (cap 5)
    change(store, guild, user, "calls", "off", ...) # -> (ok, text); audited

Privacy direction: anyone who manages the server may make Aither hear LESS. Only the
bot's owner may make her hear MORE (the same rule ``/calls mode:on`` already has), so
no button here can start listening on a server manager's say-so alone.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from awdiscord import callnotes

log = logging.getLogger("awdiscord.hearing")

#: Rows shown before "+N more" (.claude/rules/ux-shape.md law 4).
VISIBLE_CAP = 5
AUDIT_PLUGIN = "hearing"
AUDIT_PREFIX = "audit:"
AUDIT_KEEP = 200
_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


def _env(env: Mapping[str, str], name: str, default: str = "") -> str:
    return str(env.get("AWDISCORD_" + name) or default).strip()


def wake_mode(env: Mapping[str, str]) -> str:
    """``AWDISCORD_VOICE_WAKE`` as ``voice._wake_mode`` reads it (pinned by a test)."""
    v = _env(env, "VOICE_WAKE", "auto").lower()
    if v in ("1", "true", "yes", "on", "always", "required"):
        return "on"
    if v in ("0", "false", "no", "off", "never", "none"):
        return "off"
    return "auto"


def owners_from_env(env: Optional[Mapping[str, str]] = None) -> set:
    """The bot's owners, as the gateway derives them: OWNERS, else OPERATORS."""
    env = os.environ if env is None else env
    for name in ("OWNERS", "OPERATORS"):
        got = {o for o in _env(env, name).replace(" ", "").split(",") if o}
        if got:
            return got
    return set()


def _served(guild: str) -> Optional[bool]:
    try:
        from awdiscord.served import ServedGuilds
        return bool(ServedGuilds.from_env().serves_guild(guild))
    except Exception:  # noqa: BLE001 - an unreadable allowlist is "not known", said so
        return None


def _row(key: str, label: str, value: str, detail: str, *, listening: bool,
         stored: str, who: str, change: str = "") -> Dict[str, Any]:
    return {"key": key, "label": label, "value": value, "detail": detail,
            "listening": bool(listening), "stored": stored, "who": who,
            "change": change}


def snapshot(guild: str, store: Any, *, host: Any = None,
             env: Optional[Mapping[str, str]] = None,
             now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every switch that decides what Aither hears/answers in ``guild``, ranked.

    ``listening`` marks a switch whose current value has her taking something in
    without being addressed. A value that cannot be read is reported as unknown,
    never guessed.
    """
    from awdiscord import containment, dispatch
    env = os.environ if env is None else env
    gid = str(guild or "")
    now = time.time() if now is None else now
    rows: List[Dict[str, Any]] = []

    served = _served(gid)
    mentions = _env(env, "MENTIONS", "1").lower() not in ("0", "false", "no")
    rows.append(_row(
        "answers", "Answers here",
        "unknown" if served is None else ("yes" if served else "no"),
        ("when she is mentioned or asked with /ask" if mentions
         else "only to slash commands (mentions are off)") if served
        else "this server is not connected to a model",
        listening=False, stored="env AWDISCORD_SERVED_GUILDS, AWDISCORD_MENTIONS",
        who="the bot's operator"))

    voice_on = _env(env, "VOICE").lower() in _TRUE
    env_guilds = callnotes.auto_guilds()
    forced = gid in {str(g) for g in env_guilds}
    calls = voice_on and callnotes.voice_calls_on(gid, store, env_guilds)
    rows.append(_row(
        "calls", "Joins voice calls on her own",
        "on" if calls else "off",
        ("joins when a person joins, takes notes for the whole call, posts them "
         "when it ends" + (" (set for this server by the operator)" if forced else ""))
        if calls else
        ("voice is not enabled on this bot" if not voice_on
         else "`/join` brings her in; notes only after \"Aither, take notes\""),
        listening=calls,
        stored="server setting core/voice_calls"
               + (", env AWDISCORD_VOICE_AUTO_GUILDS" if forced else ""),
        who="off: server managers. on: the bot's owner",
        change="" if (forced or not voice_on) else ("off" if calls else "on")))

    content = mentions and _env(env, "MESSAGE_CONTENT").lower() in _TRUE
    watch = [c for c in _env(env, "WATCH_CHANNELS").replace(" ", "").split(",") if c]
    hooks = 0
    if host is not None:
        try:
            hooks = sum(1 for p in host.active(gid) if getattr(p, "on_message", None))
        except Exception:  # noqa: BLE001 - count unknown, the switch itself is still shown
            hooks = 0
    rows.append(_row(
        "messages", "Reads messages that do not mention her",
        "on" if content else "off",
        (f"{hooks} plugin(s) on here may read channel messages"
         + (f"; {len(watch)} operator channel(s) are triaged" if watch else ""))
        if content else "she sees a message only when it mentions her",
        listening=content and (hooks > 0 or bool(watch)),
        stored="env AWDISCORD_MESSAGE_CONTENT, AWDISCORD_WATCH_CHANNELS",
        who="the bot's operator (plugins: `/plugins disable <name>`)"))

    wake = wake_mode(env)
    rows.append(_row(
        "wake", "In voice, answers",
        {"on": "only a sentence that starts with \"Aither\"",
         "off": "every sentence",
         "auto": "every sentence when alone with you; with 2+ people only "
                 "\"Aither, ...\""}[wake],
        "\"Aither, off the record\" pauses notes; \"Aither, stop\" ends them",
        listening=voice_on and wake == "off",
        stored="env AWDISCORD_VOICE_WAKE", who="the bot's operator"))

    vis = dispatch.reply_visibility(store, gid)
    rows.append(_row(
        "visibility", "Who sees her answers", vis,
        {"public": "everyone, where it was asked",
         "private": "only the person who asked",
         "bot_channel": "everyone, in the bot channel"}[vis],
        listening=False, stored="server setting core/reply_visibility",
        who="server managers", change="visibility"))

    contained = containment.enabled()
    try:
        chan = str(store.get(gid, "core", "bot_channel", "") or "") if store else ""
    except Exception:  # noqa: BLE001
        chan = ""
    chan = chan or _env(env, "BOT_CHANNEL") or "bot"
    rows.append(_row(
        "containment", "Kept to one channel",
        "on" if contained else "off",
        f"her own posts go to #{chan}" if contained else "she posts where she is asked",
        listening=False, stored="env AWDISCORD_CONTAIN, server setting core/bot_channel",
        who="the bot's operator"))

    ignored = 0
    try:
        for key in (store.keys(gid, dispatch.MODERATION_PLUGIN, dispatch.IGNORE_KEY)
                    if store else []):
            entry = store.get(gid, dispatch.MODERATION_PLUGIN, key, None)
            if isinstance(entry, dict) and float(entry.get("until") or 0) > now:
                ignored += 1
    except Exception:  # noqa: BLE001
        ignored = 0
    rows.append(_row(
        "ignored", "Members she ignores", str(ignored),
        "`/aither-ignore` and `/aither-unignore` change this",
        listening=False, stored="server setting moderation/ignore:<member>",
        who="server moderators"))

    if host is not None:
        try:
            active = [p.name for p in host.active(gid)]
            total = len(host.plugins)
        except Exception:  # noqa: BLE001
            active, total = [], 0
        rows.append(_row(
            "plugins", "Plugins on here", f"{len(active)} of {total}",
            "`/plugins list` shows them; each adds commands and may add tools",
            listening=False, stored="server plugin state", who="server managers"))
    return rows


def view(rows: List[Dict[str, Any]], expanded: bool = False,
         cap: int = VISIBLE_CAP) -> Dict[str, Any]:
    """Cap presentation, never data: ``more`` is what ``expanded`` reveals."""
    shown = rows if expanded else rows[:cap]
    return {"shown": shown, "more": len(rows) - len(shown), "total": len(rows)}


def headline(rows: List[Dict[str, Any]]) -> str:
    on = [r for r in rows if r["listening"]]
    if not on:
        return ("**What Aither hears in this server**: only what is addressed to her "
                f"(0 of {len(rows)} switches have her listening on her own).")
    return ("**What Aither hears in this server**: "
            f"{len(on)} of {len(rows)} switches have her listening on her own - "
            + "; ".join(r["label"].lower() for r in on) + ".")


def next_action(rows: List[Dict[str, Any]], can_manage: bool) -> str:
    """Exactly one concrete next action (ux-shape law 2), from the rows already read."""
    by = {r["key"]: r for r in rows}
    if not can_manage:
        return "Next: ask a server manager to change any of this; `/calls` shows voice."
    if by.get("calls", {}).get("change") == "off":
        return "Next: press **Calls: turn off** to stop her joining calls on her own."
    if by.get("visibility", {}).get("value") == "public":
        return ("Next: nothing needs changing. Press **Replies: private** if answers "
                "should be seen only by the person who asked.")
    return "Next: nothing needs changing. **History** shows who changed what."


# -- audit: who, when, what ---------------------------------------------------------------

def audit(store: Any, guild: str, by: str, what: str, old: str, new: str) -> Dict[str, Any]:
    """Append one change to the server's own trail; trimmed to the newest AUDIT_KEEP."""
    entry = {"at": int(time.time()), "by": str(by), "what": what, "old": str(old),
             "new": str(new)}
    store.set(str(guild), AUDIT_PLUGIN,
              f"{AUDIT_PREFIX}{entry['at']:012d}:{secrets.token_hex(3)}", entry)
    try:
        keys = sorted(store.keys(str(guild), AUDIT_PLUGIN, AUDIT_PREFIX))
        for stale in keys[:-AUDIT_KEEP]:
            store.delete(str(guild), AUDIT_PLUGIN, stale)
    except Exception as exc:  # noqa: BLE001 - trimming is housekeeping; the entry is written
        log.debug("audit trim skipped: %s", type(exc).__name__)
    print(json.dumps({"event": "hearing_setting", "guild": str(guild), **entry}),
          flush=True)
    return entry


def audit_log(store: Any, guild: str, limit: int = VISIBLE_CAP) -> Tuple[List[dict], int]:
    """The newest ``limit`` changes (newest first) and the real total."""
    keys = sorted(store.keys(str(guild), AUDIT_PLUGIN, AUDIT_PREFIX))
    got = [store.get(str(guild), AUDIT_PLUGIN, k, None) for k in reversed(keys[-limit:])]
    return [e for e in got if isinstance(e, dict)], len(keys)


# -- the two switches a server may change ------------------------------------------------

def change(store: Any, guild: str, user: str, key: str, value: str, *,
           is_manager: bool, owners: Iterable[str] = (),
           env: Optional[Mapping[str, str]] = None) -> Tuple[bool, str]:
    """Apply one change through the existing setter. Returns ``(changed, text)``.

    Refusals name the remedy. Nothing here can turn listening on for anyone but the
    bot's owner.
    """
    from awdiscord import dispatch
    env = os.environ if env is None else env
    gid, user, value = str(guild or ""), str(user), str(value or "").strip().lower()
    is_owner = user in {str(o) for o in owners}
    if store is None or not gid:
        return False, "This bot has no per-server store, so nothing can be saved."
    if not (is_manager or is_owner):
        return False, "Only server managers can change this. The view is open to all."
    if key == "visibility":
        if value not in dispatch.VISIBILITY_MODES:
            return False, "Replies are one of " + ", ".join(dispatch.VISIBILITY_MODES) + "."
        old = dispatch.reply_visibility(store, gid)
        if old == value:
            return False, f"Replies are already `{value}`."
        store.set(gid, "core", dispatch.REPLY_VISIBILITY_KEY, value)
        audit(store, gid, user, "visibility", old, value)
        return True, f"Saved: replies are now `{value}` (were `{old}`)."
    if key == "calls":
        if value not in ("on", "off"):
            return False, "Calls are `on` or `off`."
        env_guilds = callnotes.auto_guilds()
        if gid in {str(g) for g in env_guilds}:
            return False, ("Calls are set for this server by the operator "
                           "(AWDISCORD_VOICE_AUTO_GUILDS); ask them to remove it.")
        if value == "on" and not is_owner:
            return False, ("Only the bot's owner can turn this on - a server's own "
                           "managers cannot start recording. They can turn it off.")
        old = "on" if callnotes.voice_calls_on(gid, store, env_guilds) else "off"
        if old == value:
            return False, f"Calls are already `{value}`."
        callnotes.set_voice_calls(store, gid, value == "on")
        audit(store, gid, user, "calls", old, value)
        return True, (f"Saved: she {'now joins' if value == 'on' else 'no longer joins'} "
                      "voice calls on her own.")
    return False, f"`{key}` is set by the bot's operator, not from Discord."
