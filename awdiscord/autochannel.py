"""Create the #bot channel the bot talks in, when a server has none.

Containment (``awdiscord.containment``) keeps the bot's output in one channel per
server; a server with none gets DMs. This module creates that channel, once, so
a fresh install works without anyone reading a setup guide:

  * at startup for every guild the bot is in (off the event loop), and
  * on ``GUILD_CREATE`` for a new one,

when containment resolves no bot channel for that server.

WHAT AND WHERE. A text channel named ``bot`` (topic: "Aither lives here. Ask with
@Aither or /ask; answers elsewhere stay private."), under the first category
whose name holds "bot" or "aither", otherwise at the top level. Then the
containment cache is cleared for that server, so the answer is resolved at once.

WHEN IT DOES NOT. Not for a server the owner does not own (``owner_id`` is not
the owner's Discord id) unless that server's own config opted in with
``auto_bot_channel: true``; not when ``AWDISCORD_AUTO_BOT_CHANNEL=0``; not when
the guild config says ``false``. With Manage Channels missing it never fails
silently: it posts the setup card's one line to the system channel and logs
``auto_bot_channel_no_permission``.

IT NEVER renames, moves or deletes an existing channel, and it is idempotent: if
a channel appeared between the check and the create, the server is re-resolved
and nothing is posted twice.
"""
from __future__ import annotations

import logging
import os
from typing import Callable

log = logging.getLogger("awdiscord.autochannel")

CATEGORY = 4
TEXT_TYPES = (0, 5)
NAME = "bot"
TOPIC = "Aither lives here. Ask with @Aither or /ask; answers elsewhere stay private."
NO_PERMISSION = ("Create a #bot channel (or grant Manage Channels and I'll make it)")
MANAGE_CHANNELS = 1 << 4
ADMINISTRATOR = 1 << 3


def enabled_for(guild: str, setting: Callable[[str], str | None] | None = None) -> bool:
    """The env switch, then the server's own ``auto_bot_channel`` setting."""
    env = (os.getenv("AWDISCORD_AUTO_BOT_CHANNEL") or "1").strip().lower()
    if env in ("0", "false", "no", "off"):
        return False
    if setting is not None:
        try:
            own = setting(guild)
        except Exception as exc:                              # noqa: BLE001
            log.warning("auto_bot_channel setting unreadable for %s: %s", guild, exc)
            own = None
        if own is not None:
            return bool(own)
    return True


def owner_owned(owner_id: str, owners: set[str]) -> bool:
    return bool(owner_id) and str(owner_id) in {str(o) for o in owners or ()}


def _parent(channels: list[dict]) -> str | None:
    for c in channels:
        if int(c.get("type") or 0) != CATEGORY:
            continue
        name = str(c.get("name") or "").lower()
        if "bot" in name or "aither" in name:
            return str(c.get("id"))
    return None


def ensure(rest, guild: str, channels: Callable[[str], list],
           resolve: Callable[[str], str | None],
           bot_bits: Callable[[str], int | None] | None = None,
           system_channel: str = "") -> str:
    """Make sure ``guild`` has a bot channel. Returns what happened:

    ``"present"`` | ``"created:<id>"`` | ``"no-permission"`` | ``"unknown"`` |
    ``"failed:<reason>"``. Never raises for a Discord answer.
    """
    try:
        found = resolve(guild)
    except Exception as exc:                                  # noqa: BLE001
        return f"failed:{type(exc).__name__}"
    if found is None:
        return "unknown"                     # the channel list could not be read
    if found:
        return "present"
    bits = None if bot_bits is None else bot_bits(guild)
    if bits is not None and not (bits & (MANAGE_CHANNELS | ADMINISTRATOR)):
        if system_channel:
            try:
                rest.create_message(system_channel, {"content": NO_PERMISSION,
                                                     "allowed_mentions": {"parse": []}})
            except Exception as exc:                          # noqa: BLE001
                log.warning("auto_bot_channel: setup line failed: %s", exc)
        log.warning("auto_bot_channel_no_permission guild=%s", guild)
        return "no-permission"
    try:
        listed = channels(guild)
    except Exception as exc:                                  # noqa: BLE001
        return f"failed:{type(exc).__name__}"
    text = [c for c in listed if isinstance(c, dict) and int(c.get("type") or 0) in TEXT_TYPES]
    if any(str(c.get("name") or "").strip().lower() == NAME for c in text):
        return "present"                     # appeared since the resolver's answer
    body = {"name": NAME, "type": 0, "topic": TOPIC}
    parent = _parent([c for c in listed if isinstance(c, dict)])
    if parent:
        body["parent_id"] = parent
    try:
        made = rest.request("POST", f"/guilds/{guild}/channels", body) or {}
    except Exception as exc:                                  # noqa: BLE001
        return f"failed:{type(exc).__name__}"
    made_id = str(made.get("id") or "")
    after = resolve(guild)                   # a channel may have appeared meanwhile
    if not made_id and not after:
        return "failed:no-id"
    return f"created:{after or made_id}"


def guild_setting(bot, guild: str) -> bool | None:
    """The server's own ``core``/``auto_bot_channel`` setting; None when unset."""
    store = getattr(getattr(bot, "host", None), "store", None)
    if store is None:
        return None
    value = store.get(guild, "core", "auto_bot_channel", None)
    return None if value is None else bool(value)


def ensure_guild(bot, guild: dict) -> str:
    """The policy, then ``ensure``. ``guild``: ``{"id", "owner_id"?,
    "system_channel_id"?}``; a missing owner_id is read from Discord.

    Returns ``"off"`` | ``"skipped:not-owner"`` | an ``ensure`` outcome.
    """
    rest = getattr(bot, "rest", None)
    gid = str((guild or {}).get("id") or "")
    if rest is None or not gid:
        return "failed:no-transport"
    own = guild_setting(bot, gid)
    if not enabled_for(gid, lambda _g: own):
        return "off"
    owner_id = str(guild.get("owner_id") or "")
    system = str(guild.get("system_channel_id") or "")
    if not owner_id:
        try:
            full = rest.request("GET", f"/guilds/{gid}") or {}
            owner_id = str(full.get("owner_id") or "")
            system = system or str(full.get("system_channel_id") or "")
        except Exception as exc:                              # noqa: BLE001
            return f"failed:{type(exc).__name__}"
    owners = set(getattr(bot, "owners", ()) or ())
    if not owner_owned(owner_id, owners) and own is not True:
        return "skipped:not-owner"
    resolver = bot._channels()
    out = ensure(rest, gid, lambda g_: rest.request("GET", f"/guilds/{g_}/channels"),
                 resolver.for_guild, bot_bits=lambda g_: _bot_bits(rest, g_),
                 system_channel=system)
    if out.startswith("created"):
        resolver.forget(gid)
    log.info("auto_bot_channel guild=%s outcome=%s", gid, out)
    return out


def sweep(bot) -> dict[str, str]:
    """``ensure_guild`` for every guild the bot is in; guild -> outcome.

    The gateway calls this once at startup, off the event loop.
    """
    rest = getattr(bot, "rest", None)
    if rest is None:
        return {}
    try:
        guilds = rest.guilds()
    except Exception as exc:                                  # noqa: BLE001
        log.warning("auto_bot_channel: cannot list guilds: %s", exc)
        return {"*": f"failed:{type(exc).__name__}"}
    out: dict[str, str] = {}
    for g in guilds or []:
        if isinstance(g, dict) and g.get("id"):
            # /users/@me/guilds carries no owner_id: ensure_guild reads it.
            out[str(g["id"])] = ensure_guild(bot, {"id": str(g["id"])})
    return out


def _bot_bits(rest, guild: str) -> int | None:
    """The bot's permission bits in ``guild``; None when unreadable."""
    try:
        me = rest.request("GET", "/users/@me") or {}
        bot_id = str(me.get("id") or "")
        roles = rest.request("GET", f"/guilds/{guild}/roles") or []
        member = rest.request("GET", f"/guilds/{guild}/members/{bot_id}") or {}
        by_id = {str(r.get("id")): r for r in roles if isinstance(r, dict)}
        bits = 0
        for rid in [guild, *[str(x) for x in (member.get("roles") or [])]]:
            value = by_id.get(rid, {}).get("permissions", "0")
            try:
                bits |= int(str(value), 10)
            except (TypeError, ValueError):
                return None
        return bits
    except Exception:                                         # noqa: BLE001
        return None
