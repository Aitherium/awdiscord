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

ACCOUNT-LINKED SERVING. A server may also be served because a human BOUND it to an
Aitherium account (``/aitherium setup`` -> Genesis ``POST /bot-claim``). That is the
second, dynamic source: ``binding(guild_id) -> bool``, consulted ONLY after the env
list misses, through a bounded TTL cache (``AWDISCORD_BINDING_TTL``, 60 s). The env
list stays first and does no I/O; an exception, a non-bool answer or an unconfigured
binding is "not served" and is never cached as a positive. ``invalidate`` clears one
server so a claim or a leave takes effect on the next call rather than after the TTL.

DMs are not a server, so they are served only to the bot's owners
(``Dispatcher.owners`` / ``AWDISCORD_OWNERS``). With no owner configured, no DM is
answered.
"""
from __future__ import annotations

import copy
import logging
import os
import threading
import time

HOME_ENV = "AITHER_DISCORD_HOME_GUILD_ID"
HOME_ENV_ALT = "AWDISCORD_HOME_GUILD_ID"
SERVED_ENV = "AWDISCORD_SERVED_GUILDS"
WINDOW_ENV = "AWDISCORD_SETUP_REPLY_MINUTES"
DEFAULT_WINDOW_MINUTES = 10.0

#: The bot's own credential for the account-linked read. Kept as a plain env NAME
#: here so this module never imports ``awdiscord.brain`` at import time; the client
#: built in :meth:`ServedGuilds.from_env` already resolves ``BOT_KEY_ENV`` and this
#: is only an override for deployments that keep the claim read on a separate key.
CLAIM_KEY_ENV = "AWDISCORD_CLAIM_KEY"
BINDING_TTL_ENV = "AWDISCORD_BINDING_TTL"
DEFAULT_BINDING_TTL = 60.0
#: How many guild verdicts (and how many "binding failed" log lines) are kept.
BINDING_CACHE_MAX = 2000

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


def home_guild_id() -> str:
    """The owner's own server, or "" when unset/unparseable.

    Same resolution ``ServedGuilds.from_env`` uses, so "home" means one thing
    everywhere. An invalid value resolves to "" (fail closed: no server can
    then prove it is home).
    """
    home = (os.getenv(HOME_ENV) or os.getenv(HOME_ENV_ALT) or "").strip()
    return home if _is_guild_id(home) else ""


def surface_for(guild_id) -> str:
    """The Genesis ``/chat`` surface for a Discord guild: home, dm, or partner.

    A DM is the owner's private surface. A server is ``home`` only when its id
    matches the configured home guild; an unset home means no server qualifies,
    so every server is a partner (fail closed).
    """
    if not guild_id:
        return "dm"
    return "home" if str(guild_id) == home_guild_id() else "partner"


def _window_seconds() -> float:
    raw = (os.getenv(WINDOW_ENV) or "").strip()
    if not raw:
        return DEFAULT_WINDOW_MINUTES * 60.0
    try:
        return max(0.0, float(raw) * 60.0)
    except ValueError:
        return DEFAULT_WINDOW_MINUTES * 60.0


def _binding_ttl() -> float:
    raw = (os.getenv(BINDING_TTL_ENV) or "").strip()
    if not raw:
        return DEFAULT_BINDING_TTL
    try:
        return max(0.0, float(raw))
    except ValueError:
        return DEFAULT_BINDING_TTL


class ServedGuilds:
    """The allowlist (env + home) plus the account-linked binding, and the throttle.

    ``served=None`` on a Dispatcher/Bot means "no gate" (the library embedding
    path, and every test that builds the pieces directly). The deployed bot
    installs the gate with :meth:`from_env`.

    ``binding`` is ``callable(guild_id) -> bool``: it asks Genesis whether a human
    bound that server to an Aitherium account. It is consulted ONLY after the env
    and home sets miss, so the allowlist path stays I/O-free and unchanged.
    """

    def __init__(self, home: str = "", guilds=(), window: float | None = None,
                 clock=None, problem: str = "", binding=None,
                 binding_ttl: float | None = None):
        self.home = str(home or "")
        self._guilds = frozenset(str(g) for g in guilds if str(g))
        if self.home:
            self._guilds = self._guilds | {self.home}
        self.window = _window_seconds() if window is None else float(window)
        self._clock = clock or time.monotonic
        self._lock = threading.Lock()
        self._last: dict[str, float] = {}
        self.problem = problem
        #: callable(guild_id) -> bool, or None when the deployment has no bot key.
        self.binding = binding
        self.binding_ttl = _binding_ttl() if binding_ttl is None else float(binding_ttl)
        #: guild -> (served, expires_at). Negatives expire on the same TTL as
        #: positives: a fresh claim must not read as broken for longer than that.
        self._binding_cache: dict[str, tuple[bool, float]] = {}
        #: guilds whose binding already logged a failure, so a flapping read is
        #: one line per server and not one line per interaction.
        self._binding_warned: set[str] = set()

    @classmethod
    def from_env(cls, clock=None, client=None) -> "ServedGuilds":
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
        binding = None
        bind_problem = ""
        if client is None:
            try:
                from awdiscord.brain import GenesisInferenceClient
                client = GenesisInferenceClient.from_env()
            except Exception:          # noqa: BLE001 - absent config is no binding
                client = None
        if client is not None:
            claim_key = (os.getenv(CLAIM_KEY_ENV) or "").strip()
            if claim_key and claim_key != str(getattr(client, "bot_key", "") or ""):
                # Never mutate the caller's client: it is the SAME object the
                # brain resolver uses for /inference, so a claim-only key must
                # not silently replace the bot key there. A copy carries it for
                # the binding read only.
                try:
                    client = copy.copy(client)
                    client.bot_key = claim_key
                except Exception as exc:   # noqa: BLE001 - uncopyable: keep its own key
                    _log.warning(
                        "awdiscord served guilds: client is uncopyable, "
                        "claim key ignored (%s)", type(exc).__name__)
            if str(getattr(client, "bot_key", "") or "").strip():
                binding = client.served
            else:
                bind_problem = ("account-linked serving is off: the bot has no "
                                f"{CLAIM_KEY_ENV} and no bot key")
        if problem:
            _log.warning("awdiscord served guilds: %s -- serving only %s",
                         problem, home or "no guild")
        if bind_problem:
            # Not a warning: a bot that never opted in to account-linked serving
            # is the normal case, and this must not drown the parse warning above.
            _log.info("awdiscord served guilds: %s", bind_problem)
            problem = f"{problem}; {bind_problem}" if problem else bind_problem
        return cls(home=home, guilds=tokens, window=_window_seconds(),
                   clock=clock, problem=problem, binding=binding,
                   binding_ttl=_binding_ttl())

    @property
    def binding_live(self) -> bool:
        """Is the account-linked binding configured (a client with a bot key)?"""
        return self.binding is not None

    def serves_guild(self, guild_id) -> bool:
        """Env/home membership first (no I/O), then the account-linked binding."""
        if not guild_id:
            return False
        guild = str(guild_id)
        if guild in self._guilds:
            return True
        return self._serves_by_binding(guild)

    def _serves_by_binding(self, guild: str) -> bool:
        """The TTL-cached Genesis verdict. Any problem is "not served"."""
        binding = self.binding
        if binding is None:
            return False
        now = self._clock()
        with self._lock:
            cached = self._binding_cache.get(guild)
            if cached is not None and cached[1] > now:
                return cached[0]
        try:
            verdict = binding(guild)
        except Exception as exc:       # noqa: BLE001 - a failed read is a refusal
            self._warn_binding(guild, f"binding read failed ({type(exc).__name__})")
            # Never cached: the next call retries (bounded by the same TTL path),
            # and a genuine claim must not be hidden behind a stale negative.
            return False
        if not isinstance(verdict, bool):
            self._warn_binding(guild, "binding answered a non-bool")
            return False
        with self._lock:
            self._binding_cache[guild] = (verdict, now + self.binding_ttl)
            if len(self._binding_cache) > BINDING_CACHE_MAX:
                self._binding_cache.pop(next(iter(self._binding_cache)))
        return verdict

    def _warn_binding(self, guild: str, reason: str) -> None:
        """One line per server, not one per interaction."""
        with self._lock:
            if guild in self._binding_warned:
                return
            self._binding_warned.add(guild)
            if len(self._binding_warned) > BINDING_CACHE_MAX:
                self._binding_warned.pop()
        _log.warning("awdiscord served guilds: %s: %s", guild, reason)

    def invalidate(self, guild_id) -> None:
        """Forget one server's binding verdict, so the next call re-asks Genesis.

        Called by the gateway when a claim's deferred work finishes: a positive
        or a negative is otherwise reused for ``binding_ttl`` seconds, and the
        server an admin just claimed must not keep reading as unserved.
        """
        guild = str(guild_id or "")
        if not guild:
            return
        with self._lock:
            self._binding_cache.pop(guild, None)
            self._binding_warned.discard(guild)

    @property
    def guilds(self) -> frozenset[str]:
        """The EXPLICITLY served guild ids (home included); never the binding.

        A caller that needs "is this guild on the owner's lane" (rather than "may
        it use the bot at all") must ask this, not ``serves_guild``: a guild the
        binding serves runs on its own hosted inference, not the owner's.
        """
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
