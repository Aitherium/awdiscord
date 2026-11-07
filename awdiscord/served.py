"""Which Discord servers may use the owner's model, and the answer for the rest.

A public bot is installed by strangers. The deployed "Aither" answers with the
OWNER's inference (``AWDISCORD_AGENT_URL`` / ``AWDISCORD_OWNER_AGENT_URL``), so a
server that is not explicitly served must never reach a model. It gets one short
card explaining how to run the bot on its own inference instead.

Fail closed. The home guild (``AITHER_DISCORD_HOME_GUILD_ID``) is always served;
every other server must be named in ``AWDISCORD_SERVED_GUILDS``. An unset or empty
value therefore serves the home guild only, and a value that contains anything that
is not a guild id is treated as unparseable: the whole list is dropped (home still
served) and the mistake is logged loudly.

DMs are not a server, so they are served only to the bot's owners
(``Dispatcher.owners`` / ``AWDISCORD_OWNERS``). With no owner configured, no DM is
answered.
"""
from __future__ import annotations

import logging
import os
import threading
import time

HOME_ENV = "AITHER_DISCORD_HOME_GUILD_ID"
HOME_ENV_ALT = "AWDISCORD_HOME_GUILD_ID"
SERVED_ENV = "AWDISCORD_SERVED_GUILDS"
WINDOW_ENV = "AWDISCORD_SETUP_REPLY_MINUTES"
DEFAULT_WINDOW_MINUTES = 10.0

SETUP_URL = "https://aitherium.com/discord/"

#: The one answer an unserved server ever gets. Short enough for a Discord
#: message, specific enough to be actionable without a conversation.
SETUP_TEXT = (
    "Hi! I'm Aither. I answer with the server owner's own model, so I can't think "
    "here - not until this server brings its own inference.\n\n"
    "Run this bot on YOUR model:\n"
    "1. `pip install awdiscord`\n"
    "2. `awdiscord setup` - it configures an OpenAI-compatible server (llama.cpp, "
    "Ollama, LM Studio, vLLM) or an `adk serve` agent from awdk.\n"
    "3. Start the bot with the token and agent URL it wrote for you.\n\n"
    "Everything it needs: " + SETUP_URL
)

#: The repeat answer. Same point, one line, for a server that has already seen
#: the card inside the rate-limit window.
SETUP_SHORT = (
    "I only answer with the server owner's model. To bring your own: "
    "`pip install awdiscord` then `awdiscord setup` - " + SETUP_URL
)

_log = logging.getLogger("awdiscord.served")


def _is_guild_id(token: str) -> bool:
    """A Discord snowflake. Deliberately loose on length (tests and old ids),
    strict on shape: digits only, so a pasted name or URL cannot pass."""
    return bool(token) and token.isascii() and token.isdigit() and 5 <= len(token) <= 25


def _window_seconds() -> float:
    raw = (os.getenv(WINDOW_ENV) or "").strip()
    if not raw:
        return DEFAULT_WINDOW_MINUTES * 60.0
    try:
        return max(0.0, float(raw) * 60.0)
    except ValueError:
        return DEFAULT_WINDOW_MINUTES * 60.0


class ServedGuilds:
    """The allowlist, plus the per-server throttle for the setup card.

    ``served=None`` on a Dispatcher/Bot means "no gate" (the library embedding
    path, and every test that builds the pieces directly). The deployed bot
    installs the gate with :meth:`from_env`.
    """

    def __init__(self, home: str = "", guilds=(), window: float | None = None,
                 clock=None, problem: str = ""):
        self.home = str(home or "")
        self._guilds = frozenset(str(g) for g in guilds if str(g))
        if self.home:
            self._guilds = self._guilds | {self.home}
        self.window = _window_seconds() if window is None else float(window)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self.problem = problem

    @classmethod
    def from_env(cls, clock=None) -> "ServedGuilds":
        home = (os.getenv(HOME_ENV) or os.getenv(HOME_ENV_ALT) or "").strip()
        raw = (os.getenv(SERVED_ENV) or "").strip()
        problem = ""
        if home and not _is_guild_id(home):
            problem = f"{HOME_ENV}={home!r} is not a guild id"
            home = ""
        tokens: list[str] = []
        if raw:
            tokens = [t for t in raw.replace(",", " ").split() if t]
            bad = [t for t in tokens if not _is_guild_id(t)]
            if bad:
                reason = f"{SERVED_ENV} has an unparseable entry ({', '.join(bad[:3])})"
                problem = f"{problem}; {reason}" if problem else reason
                tokens = []          # the whole value is untrusted: fail closed
        if problem:
            _log.warning("awdiscord served guilds: %s -- serving only %s",
                         problem, home or "no guild")
        return cls(home=home, guilds=tokens, window=_window_seconds(),
                   clock=clock, problem=problem)

    def serves_guild(self, guild_id) -> bool:
        return bool(guild_id) and str(guild_id) in self._guilds

    @property
    def guilds(self) -> frozenset[str]:
        """Every served guild id, the home guild included."""
        return self._guilds

    def serves_dm(self, user_id, owners=()) -> bool:
        """A DM is served only to an owner; no owner configured means no DM."""
        return bool(user_id) and str(user_id) in {str(o) for o in owners if str(o)}

    def allows(self, guild_id, user_id, owners=()) -> bool:
        if guild_id:
            return self.serves_guild(guild_id)
        return self.serves_dm(user_id, owners)

    @staticmethod
    def key(guild_id, user_id) -> str:
        return f"g:{guild_id}" if guild_id else f"u:{user_id}"

    def allow_prompt(self, key: str) -> bool:
        """True once per ``window`` seconds per key; stamps the attempt."""
        now = self._clock()
        with self._lock:
            last = self._last.get(key)
            if last is not None and now - last < self.window:
                return False
            self._last[key] = now
            if len(self._last) > 2000:        # bounded memory across many servers
                self._last.pop(next(iter(self._last)))
            return True
