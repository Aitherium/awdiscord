"""Orientation: a short welcome post in each server's entrance channel, on a timer.

New members land in a server with no idea what it is or how to talk to Aither.
This plugin posts one plain message in a channel an admin names, no more often
than every six hours, and skips the post when its previous one is still among
the channel's last twenty messages (a quiet server does not get a wall).

    /orientation set channel:#welcome every:24
    /orientation post
    /orientation off

The server name in the text comes from Discord (``GET /guilds/<id>``), never a
hardcoded list. The resident daemon (§ ``on_ready``) sweeps every known guild
and is inert until a channel is configured. ``AWDISCORD_ORIENTATION=0`` turns
it off for a deployment that does not want it.
"""
from __future__ import annotations

import logging
import os
import threading
import time

from awdiscord.plugins import Command, Option, Plugin, PluginError, ScopedStore
from awdiscord.served import SETUP_URL

log = logging.getLogger("awdiscord.orientation")

NAME = "orientation"
CONFIG_KEY = "orientation"
DEFAULT_HOURS = 24.0
MIN_HOURS = 6.0
MAX_RECENT = 20
SWEEP_S = 60.0
ENV_OFF = "AWDISCORD_ORIENTATION"

_lock = threading.Lock()
_daemons: dict[int, threading.Thread] = {}


# ── configuration ─────────────────────────────────────────────────────────────

def normalize_hours(value) -> float:
    """The interval in hours. Default 24; below the 6-hour floor is refused."""
    if value in (None, ""):
        return DEFAULT_HOURS
    try:
        hours = float(value)
    except (TypeError, ValueError):
        raise PluginError("`every` must be a whole number of hours.")
    if hours < MIN_HOURS:
        raise PluginError(f"Orientation can post at most once every "
                          f"{int(MIN_HOURS)} hours.")
    return hours


def due(config: dict, now: float) -> bool:
    """True when an enabled, configured guild is due for another post."""
    config = config or {}
    if not config.get("enabled", True) or not config.get("channel"):
        return False
    every = max(MIN_HOURS, float(config.get("every_hours") or DEFAULT_HOURS))
    last = float(config.get("last_at") or 0.0)
    return now - last >= every * 3600.0


# ── the post ──────────────────────────────────────────────────────────────────

def orientation_text(server: str, url: str = SETUP_URL) -> str:
    """The whole message. Short, plain, and true of every server."""
    server = str(server or "").strip() or "this server"
    return (
        f"**Welcome to {server}.**\n"
        f"This is {server}'s space on Aither.\n\n"
        "Talk to Aither by mentioning @Aither, or with `/ask`.\n"
        "Want to run your own model? `/selfhost` sets it up and `/model` manages it.\n"
        f"More: {url}")


def server_name(rest, guild: str) -> str:
    """The guild's current name from Discord. Never a hardcoded customer name."""
    if rest is None or not guild:
        return "this server"
    try:
        out = rest.request("GET", f"/guilds/{guild}") or {}
    except Exception as exc:          # a names failure must not stop the post
        log.warning("orientation: could not read guild %s: %s", guild, exc)
        return "this server"
    return str((out or {}).get("name") or "").strip() or "this server"


def recent_message_ids(rest, channel: str, limit: int = MAX_RECENT) -> list[str]:
    """The ids of the channel's newest ``limit`` messages, newest first."""
    out = rest.request("GET", f"/channels/{channel}/messages?limit={int(limit)}") or []
    return [str(m.get("id") or "") for m in out if isinstance(m, dict)]


def post_orientation(host, guild: str, config: dict, *, force: bool = False) -> dict:
    """Post once unless our previous orientation post is still recent.

    ``force`` is the admin's explicit ``/orientation post``: it always posts.
    Returns ``{"posted": bool, "reason": str, "message_id": str}``.
    """
    rest = getattr(host, "rest", None)
    if rest is None:
        return {"posted": False, "reason": "no_rest"}
    channel = str((config or {}).get("channel") or "")
    if not channel:
        return {"posted": False, "reason": "no_channel"}
    last_id = str((config or {}).get("last_message_id") or "")
    try:
        recent = recent_message_ids(rest, channel)
    except Exception as exc:          # cannot read history: do not risk a second post
        log.warning("orientation: could not read channel %s: %s", channel, exc)
        return {"posted": False, "reason": "history_unreadable"}
    if not force and last_id and last_id in recent:
        return {"posted": False, "reason": "recent"}
    message = rest.create_message(channel, {"content": orientation_text(
        server_name(rest, guild))}) or {}
    return {"posted": True, "reason": "posted",
            "message_id": str(message.get("id") or "")}


# ── the sweep ─────────────────────────────────────────────────────────────────

def sweep(host, now: float | None = None) -> dict[str, str]:
    """One pass over every guild the store knows. Returns guild -> outcome."""
    out: dict[str, str] = {}
    now = float(time.time() if now is None else now)
    store = getattr(host, "store", None)
    listing = getattr(store, "guilds", None)
    if not callable(listing):
        return out
    try:
        guilds = [str(g) for g in listing() if str(g)]
    except Exception as exc:          # a store outage skips one sweep, never the daemon
        log.warning("orientation: could not list guilds: %s", exc)
        return out
    for guild in guilds:
        try:
            if not host.enabled(guild, NAME):
                continue
            scoped = ScopedStore(store, guild, NAME)
            config = scoped.get(CONFIG_KEY) or {}
            if not due(config, now):
                continue
            result = post_orientation(host, guild, config)
            out[guild] = str(result.get("reason") or "")
            if result.get("posted"):
                scoped.set(CONFIG_KEY, {**config, "enabled": True, "last_at": now,
                                        "last_message_id": result.get("message_id", "")})
            elif result.get("reason") == "recent":
                # Our last post is still visible: record the check so the daemon
                # does not re-read the channel every minute. The next interval
                # re-checks; skipping again is harmless.
                scoped.set(CONFIG_KEY, {**config, "last_at": now})
        except Exception as exc:      # one guild's failure never stops the sweep
            log.warning("orientation: guild %s failed: %s", guild, exc)
            out[guild] = type(exc).__name__
    return out


def _loop(host) -> None:
    while True:
        try:
            sweep(host)
        except Exception as exc:      # noqa: BLE001 - the daemon never dies
            log.warning("orientation sweep failed: %s", exc)
        time.sleep(SWEEP_S)


def _on_ready(host) -> None:
    """Start the one sweep daemon for this host (idempotent, off by env)."""
    if os.getenv(ENV_OFF, "1").strip() == "0":
        return None
    with _lock:
        thread = _daemons.get(id(host))
        if thread is not None and thread.is_alive():
            return None
        thread = threading.Thread(target=_loop, args=(host,), name="awdiscord-orientation",
                                  daemon=True)
        _daemons[id(host)] = thread
        thread.start()
    return None


# ── commands ──────────────────────────────────────────────────────────────────

def _set(ctx):
    channel = str(ctx.opt("channel") or "").strip()
    if not channel.isdigit():
        raise PluginError("Pick the entrance channel: `/orientation set channel:#welcome`.")
    config = ctx.store.get(CONFIG_KEY) or {}
    raw_every = ctx.opt("every")
    hours = (normalize_hours(raw_every) if raw_every not in (None, "")
             else float(config.get("every_hours") or DEFAULT_HOURS))
    ctx.store.set(CONFIG_KEY, {**config, "channel": channel, "every_hours": hours,
                               "enabled": True})
    return ctx.reply(f"Orientation will post in <#{channel}> every {int(hours)}h "
                     f"(never more than once every {int(MIN_HOURS)}h).", private=True)


def _post(ctx):
    config = ctx.store.get(CONFIG_KEY) or {}
    if not config.get("channel"):
        raise PluginError("Set an entrance channel first: "
                          "`/orientation set channel:#welcome`.")
    result = post_orientation(ctx.host, ctx.guild, config, force=True)
    if not result.get("posted"):
        raise PluginError("Could not post the orientation: "
                          + str(result.get("reason") or "unknown"))
    ctx.store.set(CONFIG_KEY, {**config, "enabled": True, "last_at": time.time(),
                               "last_message_id": result.get("message_id", "")})
    return ctx.reply(f"Posted the orientation in <#{config['channel']}>.", private=True)


def _off(ctx):
    config = ctx.store.get(CONFIG_KEY) or {}
    if not config.get("enabled", True) or not config.get("channel"):
        return ctx.reply("Orientation is already off.", private=True)
    ctx.store.set(CONFIG_KEY, {**config, "enabled": False})
    return ctx.reply("Orientation is off. Turn it back on with "
                     "`/orientation set channel:#welcome`.", private=True)


PLUGIN = Plugin(
    name=NAME,
    version="0.1.0",
    description="A periodic orientation post in each server's entrance channel",
    default_enabled=True,
    on_ready=[_on_ready],
    commands=[
        Command("orientation", "The periodic welcome post for new members", subcommands=[
            Command("set", "Choose the entrance channel and interval", _set, admin=True,
                    options=[
                        Option("channel", "The entrance channel", type="channel",
                               required=True),
                        Option("every", "Hours between posts (default 24, minimum 6)",
                               type="integer")]),
            Command("post", "Post the orientation now", _post, admin=True),
            Command("off", "Stop the periodic orientation post", _off, admin=True),
        ]),
    ],
)
