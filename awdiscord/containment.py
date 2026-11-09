"""Bot output containment: the bot talks in ONE channel per server.

A bot that answers wherever it is invoked spams every channel it can see. This
is the technical control (a persona prompt cannot hold it): each server has a
*bot channel*, and the gateway keeps the bot's output there.

  slash command outside the bot channel   the reply is ephemeral (only the
                                          invoker sees it)
  mention / reply outside it              the answer goes to the bot channel,
                                          mentioning the asker with a jump link;
                                          the original message gets a reaction
  server with no bot channel              the answer is DMed to the asker; DMs
                                          closed = the original channel
  the bot's own unsolicited posts         the bot channel (an admin-configured
                                          feed channel keeps working)

Which channel is the bot channel, per server, first match wins:

  1. the server's ``core``/``bot_channel`` setting in the guild store (id or name)
  2. ``AWDISCORD_BOT_CHANNEL`` (a channel id, or a name; ``#`` optional) -- an id
     only counts in the server that owns it, other servers fall through
  3. the text channel named ``bot``

A name matches tolerantly: case, a leading ``#``, emoji and punctuation are
ignored (``🤖-bot`` and ``Bot`` are ``bot``); failing an exact match, a channel
whose name holds the word (``bot-chat``, ``aither-bot``) counts, the shortest
first. Categories and voice channels never count. Names are resolved against
the server's channel list at runtime and the id is cached (``CACHE_SECONDS``);
the gateway forgets a server's entry when one of its channels is created,
renamed or deleted. A thread inside the bot channel counts as the bot
channel. When the channel list cannot be read at all the answer is UNKNOWN and
nothing is moved (a REST hiccup must not turn every answer into a DM).
``AWDISCORD_CONTAIN=0`` turns containment off (the old behaviour).
"""
from __future__ import annotations

import logging
import os
import re
import threading
import time
from typing import Callable, Iterable

_log = logging.getLogger(__name__)

DEFAULT_NAME = "bot"
CACHE_SECONDS = 3600
MISS_SECONDS = 600            # a server with no bot channel is re-checked sooner
UNKNOWN_SECONDS = 60          # the channel list could not be read: retry soon
TEXT_TYPES = (0, 5)           # GUILD_TEXT, GUILD_ANNOUNCEMENT
EYES = "\N{EYES}"

#: ``(guild_id) -> [{"id", "name", "type"}, ...]`` -- the server's channels
Fetch = Callable[[str], Iterable[dict]]


def enabled() -> bool:
    """Containment is on unless ``AWDISCORD_CONTAIN`` says off."""
    return (os.getenv("AWDISCORD_CONTAIN") or "1").strip().lower() not in (
        "0", "false", "no", "off")


_NON_WORD = re.compile(r"[^0-9a-z]+")


def _words(name: str) -> list[str]:
    """``name`` as lowercase words: emoji, ``#`` and punctuation dropped."""
    return [w for w in _NON_WORD.split(str(name or "").lower()) if w]


def _norm(name: str) -> str:
    return "-".join(_words(name))


def match_channel(text_channels: list[dict], want: str) -> str:
    """The id of the channel ``want`` names: exact (normalised) first, then the
    shortest channel whose name contains all of ``want``'s words in order."""
    target = _norm(want)
    if not target:
        return ""
    for c in text_channels:
        if _norm(c.get("name", "")) == target:
            return str(c.get("id"))
    words = target.split("-")
    hits = []
    for c in text_channels:
        have = _words(c.get("name", ""))
        for i in range(len(have) - len(words) + 1):
            if have[i:i + len(words)] == words:
                hits.append((len(_norm(c.get("name", ""))), str(c.get("id"))))
                break
    return min(hits)[1] if hits else ""


class BotChannels:
    """Per-server bot channel resolution, cached.

    Args:
        fetch: returns a server's channels (the REST ``GET /guilds/{id}/channels``
            shape); called at most once per server per cache period.
        setting: ``(guild_id) -> str`` the server's own configured channel (id or
            name), or ``""``.
        clock: monotonic seconds (tests pass a fake).
    """

    def __init__(self, fetch: Fetch, setting: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self._fetch = fetch
        self._setting = setting or (lambda guild: "")
        self._clock = clock
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[str | None, float]] = {}

    def _wanted(self, guild: str) -> list[str]:
        """The configured choices for ``guild``, most specific first."""
        out = []
        try:
            own = str(self._setting(guild) or "").strip()
        except Exception as exc:                                  # noqa: BLE001
            _log.warning("bot channel setting unreadable for %s: %s", guild, exc)
            own = ""
        if own:
            out.append(own)
        env = (os.getenv("AWDISCORD_BOT_CHANNEL") or "").strip()
        if env:
            out.append(env)
        out.append(DEFAULT_NAME)
        return out

    def _resolve(self, guild: str) -> str | None:
        try:
            listed = self._fetch(guild)
        except Exception as exc:                                  # noqa: BLE001
            _log.warning("cannot list channels of %s: %s", guild, exc)
            return None
        if not isinstance(listed, list):
            return None
        channels = [c for c in listed if isinstance(c, dict)]
        text = [c for c in channels if int(c.get("type") or 0) in TEXT_TYPES]
        for want in self._wanted(guild):
            if want.isdigit():
                if any(str(c.get("id")) == want for c in text):
                    return want
                continue                  # an id from another server: fall through
            found = match_channel(text, want)
            if found:
                return found
        return ""

    def for_guild(self, guild: str | None) -> str | None:
        """The bot channel id of ``guild``; ``""`` when it has none (or there is no
        guild); ``None`` when its channels could not be read (unknown)."""
        guild = str(guild or "")
        if not guild:
            return ""
        now = self._clock()
        with self._lock:
            hit = self._cache.get(guild)
        if hit is not None and hit[1] > now:
            return hit[0]
        found = self._resolve(guild)
        with self._lock:
            ttl = UNKNOWN_SECONDS if found is None else (
                CACHE_SECONDS if found else MISS_SECONDS)
            self._cache[guild] = (found, now + ttl)
        return found

    def forget(self, guild: str) -> None:
        """Drop the cached answer for ``guild`` (a channel was renamed or deleted)."""
        with self._lock:
            self._cache.pop(str(guild), None)

    def is_bot_channel(self, guild: str | None, channel: str | None,
                       parent: str | None = None) -> bool:
        """True when ``channel`` (or the channel a thread sits in) is the bot channel.

        Outside a server (a DM) there is nothing to contain: always True. So is a
        server whose channel list could not be read (unknown: nothing is moved).
        """
        if not guild:
            return True
        bot = self.for_guild(guild)
        if bot is None:
            return True
        return bool(bot) and bot in (str(channel or ""), str(parent or ""))


def jump_link(guild: str, channel: str, message: str = "") -> str:
    """A Discord link to a channel, or to one message in it."""
    base = f"https://discord.com/channels/{guild}/{channel}"
    return f"{base}/{message}" if message else base


def relayed_header(user: str, guild: str, channel: str, message: str) -> str:
    """The first line of an answer moved to the bot channel."""
    return f"<@{user}> asked in {jump_link(guild, channel, message)}"
