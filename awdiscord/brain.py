"""One server, one brain: the single place a Discord guild becomes a model.

A public bot answers with the OWNER's inference unless a server brings its own.
That decision used to live in several places -- the served-guild allowlist, the
dispatcher's ``agent_for(user)``, the image seer, the plugins' ``Ctx.agent`` --
and a server could fall between them. This module is the one predicate:

    brain = resolver.resolve(guild_id, user_id)
    if brain.mode == "none": ...   # the setup card, never a model call

Modes in this slice: ``owner`` (the deployed bot's own model, only for guilds the
allowlist names), ``byo`` (a server's own OpenAI-compatible endpoint), ``hosted``
(a server buying inference from the platform) and ``none`` (fail closed). Hosted
is OFF unless ``AWDISCORD_HOSTED=1``; with it unset a hosted server resolves to
``none`` exactly as before. A paid guild never falls back to the owner's model.

Fail closed, everywhere: an unknown guild, an unparseable allowlist, a missing
BYO key, a failed probe or any exception inside the resolver is ``none`` with a
reason. There is no "serve everyone" default and no fallback to the owner's
agent on error.
"""
from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import os
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from awdiscord.agent import AgentError

_log = logging.getLogger("awdiscord.brain")

#: GuildStore plugin name holding this server's brain rows.
BRAIN_PLUGIN = "brain"
MODE_KEY = "mode"
BYO_KEY = "byo"
GEN_KEY = "byo_gen"

OWNER, BYO, NONE = "owner", "byo", "none"
HOSTED = "hosted"

#: The modes that may run anything that can reach a model (message hooks, joins,
#: triage candidates, voice). Every other mode -- in practice only ``none`` --
#: is skipped.
SERVED_MODES = frozenset({OWNER, BYO, HOSTED})


def served_by_brain(brains, guild_id, fallback=None) -> bool:
    """The ONE gate for behaviour that can reach a model in a guild.

    A guild is served when ``brains.resolve(guild_id).mode`` is ``owner``,
    ``byo`` or ``hosted``; only ``none`` is skipped. Resolution is fail-closed:
    an exception is "not served", never "serve it".

    ``brains is None`` means no resolver was configured (the library embedding
    and ``Bot.__new__`` tests). ``fallback`` -- the legacy ``ServedGuilds``
    allowlist -- is consulted then, and an absent one leaves the behaviour
    ungated exactly as it was before the resolver existed. An empty
    ``guild_id`` (a DM, or a call with no guild context) is not gated here.
    """
    if not guild_id:
        return True
    if brains is None:
        return True if fallback is None else bool(fallback.serves_guild(str(guild_id)))
    try:
        return getattr(brains.resolve(str(guild_id)), "mode", NONE) in SERVED_MODES
    except Exception:                      # noqa: BLE001 - resolver bug = refuse
        return False

#: Hosted inference is OFF unless this is set to a truthy value. Unset keeps
#: every existing deployment answering exactly as it does today.
HOSTED_ENV = "AWDISCORD_HOSTED"
#: The gateway's hosted OpenAI-compatible base. The agent posts to
#: ``{base}/chat/completions``, so this ends in ``/v1/hosted``. Never a plain
#: ``http://`` name for a deployed bot: that is an internal address, and the
#: public gateway is reached over TLS.
HOSTED_BASE_ENV = "AWDISCORD_HOSTED_BASE"
HOSTED_MODEL_ENV = "AWDISCORD_HOSTED_MODEL"
DEFAULT_HOSTED_BASE = "https://mcp.aitherium.com/v1/hosted"
DEFAULT_HOSTED_MODEL = "aither-bonsai"

#: Cloudflare fronts the public gateway and answers a request that carries no
#: User-Agent (urllib's default ``Python-urllib/3.x``) with HTTP 403
#: ``error code: 1010`` before it reaches the tunnel -- measured 2026-11-07.
#: The hosted call therefore names itself.
HOSTED_USER_AGENT = "awdiscord-hosted/1.0 (+https://aitherium.com)"

#: How long a positive hosted record is reused, and how long a gateway auth
#: failure keeps a guild at ``none`` (no retry storm against a key that was
#: already refused).
HOSTED_TTL = 60.0
HOSTED_DENY_TTL = 60.0

#: Genesis is authoritative for a guild's hosted record. The bot key is sent in
#: this header; a guild's own gateway key never leaves the per-guild secret file.
GENESIS_INFERENCE_PATH = "/v1/workspace/discord-guild-binding/inference"
BOT_KEY_ENV = "AITHER_DISCORD_BOT_KEY"
BOT_KEY_HEADER = "X-Aither-Bot-Key"
GUILD_HEADER = "X-Aither-Guild-Id"

HOSTED_QUOTA_TEXT = ("this server's daily allowance is used up -- top up or "
                     "bring your own model with /model")

#: The custom_id of the core /model setup modal (gateway routes MODAL_SUBMIT on it).
BRAIN_MODAL_ID = "core:brain:setup"

DEFAULT_TIMEOUT = 60.0
PROBE_TIMEOUT = 10.0

#: Hosts that are never a legitimate public BYO endpoint, checked before DNS.
_BAD_HOST_SUFFIXES = (".local", ".internal", ".lan", ".home", ".localhost",
                      ".aither", ".aitherium")
_BAD_HOST_NAMES = {"localhost", "metadata", "metadata.google.internal",
                   "instance-data", "host.docker.internal"}
_BAD_HOST_PARTS = ("aitheros-", "microscheduler", "genesis", "awdiscord-")


class BrainError(Exception):
    """A BYO endpoint or key that cannot be used. The message is admin-safe."""


# -- endpoint validation (the SSRF boundary) ---------------------------------------

def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    out: list[str] = []
    for info in infos:
        ip = info[4][0]
        if ip not in out:
            out.append(ip)
    return out


def _public_ip(raw: str) -> bool:
    ip = ipaddress.ip_address(raw)
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified)


def _bad_hostname(host: str) -> bool:
    h = host.lower().rstrip(".")
    if h in _BAD_HOST_NAMES:
        return True
    if any(h.endswith(suffix) for suffix in _BAD_HOST_SUFFIXES):
        return True
    if any(part in h for part in _BAD_HOST_PARTS):
        return True
    # A single-label name resolves through search domains / mDNS, not public DNS.
    return "." not in h and not _is_ip(h)


def _split_endpoint(url: str) -> urlsplit:
    raw = str(url or "").strip()
    if not raw:
        raise BrainError("an endpoint URL is required")
    if any(ch in raw for ch in " \t\r\n"):
        raise BrainError("the endpoint URL contains whitespace")
    parts = urlsplit(raw)
    if "@" in parts.netloc:
        raise BrainError("credentials in the endpoint URL are not allowed")
    if not parts.hostname:
        raise BrainError("the endpoint URL has no host")
    if parts.query or parts.fragment:
        raise BrainError("the endpoint URL may not carry a query or fragment")
    return parts


def _checked_endpoint(url: str, *, hosted: bool = True,
                      resolver=None) -> tuple[str, list[str]]:
    """Validate ``url`` and return it cleaned plus the checked IP list.

    The returned ``ips`` are the exact addresses this validation inspected;
    callers that dial the host MUST use them rather than resolving a second
    time (see ``PinnedTransport._connect``). A second lookup is a rebinding
    window even inside one request.
    """
    parts = _split_endpoint(url)
    scheme = (parts.scheme or "").lower()
    ips: list[str] = []
    if hosted:
        if scheme != "https":
            raise BrainError("the endpoint must be https://")
        port = parts.port or 443
        if port != 443:
            raise BrainError("the endpoint must use port 443")
        if _bad_hostname(parts.hostname):
            raise BrainError("the endpoint host is not a public address")
        host = parts.hostname
        if _is_ip(host):
            ips = [host]
        else:
            try:
                ips = (resolver or _default_resolver)(host, port)
            except OSError as exc:
                raise BrainError(
                    f"the endpoint host does not resolve ({type(exc).__name__})")
        if not ips:
            raise BrainError("the endpoint host does not resolve")
        for ip in ips:
            if not _public_ip(ip):
                raise BrainError("the endpoint resolves to a private address")
    else:
        if scheme not in ("http", "https"):
            raise BrainError("the endpoint must be http:// or https://")
        _split_endpoint(url)               # shape only; a self-hoster owns the LAN
    return url.strip().rstrip("/"), ips


def validate_endpoint(url: str, *, hosted: bool = True, resolver=None,
                      timeout: float = PROBE_TIMEOUT) -> str:
    """Return ``url`` unchanged when it is a legitimate BYO endpoint, else raise.

    ``hosted=True`` is the public bot's policy: HTTPS only, public DNS only,
    port 443 only, no private/loopback/link-local/metadata address and no
    internal fleet name. ``hosted=False`` is a self-hosted bot: loopback, LAN
    and plain HTTP are exactly what llama.cpp / Ollama / LM Studio expose, so
    they are allowed. Every resolved address is checked, not just the first.
    """
    return _checked_endpoint(url, hosted=hosted, resolver=resolver)[0]


# -- HTTP transports ----------------------------------------------------------------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A 3xx is returned to the caller, never followed."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        return None


class PlainTransport:
    """A self-hosted bot's transport: ordinary urllib (redirects allowed)."""

    def __init__(self, base_url: str, timeout: float = DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _open(self, method: str, path: str, headers: dict, body=None, timeout=None):
        req = urllib.request.Request(self.base_url + path, data=body,
                                     headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return r.status, r.read()

    def get(self, path: str, headers: dict, timeout: float = DEFAULT_TIMEOUT):
        return self._open("GET", path, headers, timeout=timeout)

    def post(self, path: str, headers: dict, body: bytes,
             timeout: float = DEFAULT_TIMEOUT):
        return self._open("POST", path, headers, body=body, timeout=timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """TLS to ``host`` (SNI + certificate) over a socket to ``ip``."""

    def __init__(self, host: str, ip: str, port: int, context, timeout: float):
        super().__init__(host, port, timeout=timeout, context=context)
        self._connect_ip = ip

    def connect(self):
        sock = socket.create_connection((self._connect_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class PinnedTransport:
    """A public bot's BYO transport: validate, resolve and pin on EVERY request.

    A hostname that resolved publicly when the server was configured and to
    127.0.0.1 (or 169.254.169.254) at ask time is refused here -- the check runs
    per request, so DNS rebinding never reaches an internal address. Redirects
    are refused and the connection goes to the validated IP with the request's
    Host/SNI still the operator's hostname.
    """

    def __init__(self, base_url: str, *, resolver=None, timeout: float = DEFAULT_TIMEOUT,
                 context=None):
        parts = _split_endpoint(base_url)
        self.base_url = base_url.rstrip("/")
        self._host = str(parts.hostname)
        self._port = parts.port or 443
        self._path = parts.path or ""
        self._resolver = resolver
        self.timeout = timeout
        self._ctx = context or ssl.create_default_context()

    def _connect(self) -> "_PinnedHTTPSConnection":
        # ONE resolution per request. The IPs validate just checked are the IPs
        # we dial; resolving again here would let a TTL-0 / round-robin zone
        # answer public on the validation lookup and private on the connect
        # lookup, defeating the per-request rebinding defence entirely.
        _, ips = _checked_endpoint(self.base_url, hosted=True,
                                   resolver=self._resolver)
        if not ips:                        # a literal-IP host always yields one
            raise BrainError("the endpoint host does not resolve")
        return _PinnedHTTPSConnection(self._host, ips[0], self._port,
                                      self._ctx, self.timeout)

    def request(self, method: str, path: str, headers: dict, body=None,
                timeout: float | None = None):
        conn = self._connect()
        if timeout:
            conn.timeout = timeout
        try:
            conn.request(method, self._path + path, body=body, headers=headers)
            response = conn.getresponse()
            return response.status, response.read()
        finally:
            conn.close()

    def get(self, path: str, headers: dict, timeout: float = DEFAULT_TIMEOUT):
        return self.request("GET", path, headers, timeout=timeout)

    def post(self, path: str, headers: dict, body: bytes,
             timeout: float = DEFAULT_TIMEOUT):
        return self.request("POST", path, headers, body=body, timeout=timeout)


def _auth_headers(api_key: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    return headers


def probe_endpoint(base_url: str, *, model: str = "", api_key: str = "",
                   hosted: bool = True, resolver=None, transport=None,
                   timeout: float = PROBE_TIMEOUT) -> dict:
    """Prove an endpoint answers before anything is saved.

    GET ``/models`` first (cheap, and it does not spend a token), then a
    one-token ``/chat/completions`` for the servers that do not implement it.
    Returns ``{"ok", "status", "via", "latency_ms", "detail"}``; the detail is
    admin-safe, never the key or a message body.
    """
    started = time.monotonic()
    base = str(base_url or "").strip().rstrip("/")
    try:
        validate_endpoint(base, hosted=hosted, resolver=resolver)
        transport = transport or (PinnedTransport(base, resolver=resolver)
                                  if hosted else PlainTransport(base))
        headers = _auth_headers(api_key)
        via, status, detail = "", 0, ""
        try:
            status, _ = transport.get("/models", headers, timeout)
            if 200 <= status < 300:
                via, detail = "models", "ok"
        except urllib.error.HTTPError as exc:
            status = exc.code
            detail = f"models HTTP {exc.code}"
        except Exception as exc:            # noqa: BLE001 - fall through to completion
            detail = f"models unreachable ({type(exc).__name__})"
        if not via:
            body = json.dumps({"model": model or "default",
                               "messages": [{"role": "user", "content": "ping"}],
                               "max_tokens": 1}).encode()
            try:
                status, raw = transport.post("/chat/completions", headers, body, timeout)
            except urllib.error.HTTPError as exc:
                status, raw = exc.code, b""
            if 200 <= status < 300:
                try:
                    data = json.loads(raw.decode("utf-8"))
                except ValueError:
                    data = {}
                if data.get("choices"):
                    via, detail = "completion", "ok"
                else:
                    detail = "the completion had no choices"
            else:
                detail = f"completion HTTP {status}"
        elapsed = int((time.monotonic() - started) * 1000)
        return {"ok": bool(via), "status": status, "via": via,
                "latency_ms": elapsed, "detail": detail}
    except BrainError as exc:
        return {"ok": False, "status": 0, "via": "", "latency_ms": 0,
                "detail": str(exc)}
    except Exception as exc:                # noqa: BLE001 - a probe never raises
        return {"ok": False, "status": 0, "via": "", "latency_ms": 0,
                "detail": f"probe failed ({type(exc).__name__})"}


# -- hosted inference ----------------------------------------------------------------

class HostedLookupError(Exception):
    """A guild's hosted record could not be read. Never cached as hosted."""


class HostedQuotaError(AgentError):
    """The guild's daily allowance is spent (gateway 429). Shown verbatim."""


def hosted_enabled() -> bool:
    """True only when the operator opted in. Unset means OFF, as today."""
    return os.getenv(HOSTED_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def _is_loopback_host(host: str) -> bool:
    h = str(host or "").strip("[]").lower()
    if h in ("localhost", "::1"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def hosted_base_url() -> str:
    """The gateway base, refusing a plain-http name a deployed bot could reach.

    ``https://`` always; ``http://`` only to a loopback host, which is a local
    test or a self-hosted gateway on the same machine -- never an internal fleet
    name masquerading as the public gateway.
    """
    raw = (os.getenv(HOSTED_BASE_ENV) or DEFAULT_HOSTED_BASE).strip()
    parts = urlsplit(raw)
    scheme = (parts.scheme or "").lower()
    if not parts.hostname:
        raise BrainError("the hosted endpoint has no host")
    if scheme != "https" and not (scheme == "http" and _is_loopback_host(parts.hostname)):
        raise BrainError("the hosted endpoint must be https://")
    if parts.query or parts.fragment:
        raise BrainError("the hosted endpoint may not carry a query or fragment")
    return raw.rstrip("/")


def _ca_bundle(prefix: str = "AWDISCORD_") -> str | None:
    """The CA file used to trust the fleet's internal certificate.

    Same rule as the rest of the package: an explicit bundle, else the fleet CA
    when it is installed, else the system store. Never an unverified context.
    """
    configured = (os.getenv(prefix + "CA_BUNDLE") or "").strip()
    if configured:
        return configured
    if os.path.isfile("/etc/aither-ca.pem"):
        return "/etc/aither-ca.pem"
    return None


class GenesisInferenceClient:
    """Read a guild's hosted record from Genesis (the authority for entitlement).

    Genesis answers ``{guild_id, mode, plan, tier, status}`` and nothing secret:
    the guild's gateway key is kept in the per-guild secret file and never
    crosses this call. Any transport, status or shape problem is a
    ``HostedLookupError`` so the resolver can fail closed without caching it.
    """

    def __init__(self, base_url: str, *, bot_key: str = "",
                 ca_bundle: str | None = None, timeout: float = PROBE_TIMEOUT):
        raw = str(base_url or "").strip().rstrip("/")
        if not raw:
            raise HostedLookupError("no Genesis URL is configured")
        if not urlsplit(raw).hostname:
            raise HostedLookupError("the Genesis URL has no host")
        # ``AWDISCORD_OWNER_AGENT_URL`` is an OpenAI-compatible base ending in
        # /v1; the binding route lives at the application root.
        if raw.endswith("/v1"):
            raw = raw[:-3]
        self.base_url = raw
        self.bot_key = str(bot_key or "")
        self.timeout = float(timeout)
        self._ctx = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else None

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_") -> "GenesisInferenceClient | None":
        base = (os.getenv(prefix + "GENESIS_URL")
                or os.getenv(prefix + "OWNER_AGENT_URL") or "").strip()
        if not base:
            return None
        return cls(base, bot_key=os.getenv(BOT_KEY_ENV, ""),
                   ca_bundle=_ca_bundle(prefix))

    def lookup(self, guild_id) -> dict:
        guild = str(guild_id or "").strip()
        if not guild:
            raise HostedLookupError("a server is required")
        url = (self.base_url + GENESIS_INFERENCE_PATH + "?"
               + urlencode({"guild_id": guild}))
        headers = {"Accept": "application/json"}
        if self.bot_key:
            headers[BOT_KEY_HEADER] = self.bot_key
        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self._ctx) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raise HostedLookupError(f"Genesis answered HTTP {exc.code}") from exc
        except Exception as exc:            # noqa: BLE001 - any failure is "none"
            raise HostedLookupError(
                f"Genesis is unreachable ({type(exc).__name__})") from exc
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise HostedLookupError("Genesis returned no JSON record") from exc
        if not isinstance(data, dict):
            raise HostedLookupError("Genesis returned no JSON record")
        if str(data.get("guild_id") or "").strip() != guild:
            raise HostedLookupError("Genesis answered for another server")
        return {"guild_id": guild,
                "mode": str(data.get("mode") or "none").strip().lower(),
                "plan": str(data.get("plan") or "").strip(),
                "tier": str(data.get("tier") or "").strip(),
                "status": str(data.get("status") or "").strip().lower()}


class HostedAgent:
    """The guild's own hosted brain: the strict gateway route plus its own key.

    It exists so a gateway refusal reaches the resolver as a decision rather
    than a sentence shown to a member: 401/402/403 invalidate the record at
    once (the next resolve is ``none``), and 429 becomes the friendly "allowance
    used up" reply with no retry.
    """

    def __init__(self, inner, *, guild: str, on_auth_failure=None):
        self._inner = inner
        self.guild = str(guild)
        self._on_auth_failure = on_auth_failure
        self.base_url = str(getattr(inner, "base_url", ""))
        self.name = str(getattr(inner, "name", ""))
        self.last_route: dict = {}
        self.sees_images = False

    def ask(self, prompt, *, history=None, **kwargs):
        # The dispatcher filters its kwargs against THIS signature, and this one
        # takes **kwargs -- so a turn-origin kwarg (guild_id/channel_id/surface)
        # would reach an inner agent that has an explicit signature as a
        # TypeError. Filter again against the inner agent, as the dispatcher does.
        from awdiscord.dispatch import _accepts
        kwargs = {k: v for k, v in kwargs.items() if _accepts(self._inner.ask, k)}
        try:
            answer = self._inner.ask(prompt, history=history, **kwargs)
        except AgentError as exc:
            status = getattr(exc, "status", None)
            if status in (401, 402, 403):
                if self._on_auth_failure is not None:
                    self._on_auth_failure(self.guild)
                raise AgentError(
                    "this server's hosted model is not available; an admin "
                    "must reconnect it") from exc
            if status == 429:
                raise HostedQuotaError(HOSTED_QUOTA_TEXT) from exc
            raise
        self.last_route = dict(getattr(self._inner, "last_route", {}) or {})
        return answer

    def health(self) -> str:
        return "ok (hosted)"

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _default_hosted_factory(*, base_url: str, model: str, api_key: str,
                            guild: str, ca_bundle: str | None, strict: bool,
                            on_auth_failure=None) -> HostedAgent:
    """Build the hosted agent. The guild is server-derived, never client input."""
    from awdiscord.agent import OpenAICompatAgent
    if strict and (urlsplit(base_url).scheme or "").lower() == "https":
        transport = PinnedTransport(base_url)
    else:
        transport = PlainTransport(base_url)
    inner = OpenAICompatAgent(
        base_url, model=model or DEFAULT_HOSTED_MODEL, api_key=api_key or None,
        timeout=DEFAULT_TIMEOUT, max_tokens=700, ca_bundle=ca_bundle,
        # A person typed the turn: the same interactive priority the bot's own
        # env agent sends. Without it every hosted Discord turn queued behind
        # tens of minutes of forge:/saga background work (measured 2026-11-07:
        # 43-276 s queue waits, every Discord turn timing out).
        priority=(os.getenv("AWDISCORD_AGENT_PRIORITY") or "user"),
        vision_models=[], transport=transport,
        extra_headers={GUILD_HEADER: guild,
                       "User-Agent": HOSTED_USER_AGENT})
    return HostedAgent(inner, guild=guild, on_auth_failure=on_auth_failure)


# -- the brain ----------------------------------------------------------------------

@dataclass(frozen=True)
class Brain:
    """One server's answer to "who thinks here?".

    ``mode`` is the contract: ``owner``/``byo`` carry a ready ``agent``;
    ``none`` never does, and ``reason`` says why (shown to admins, logged).
    ``endpoint`` is display only -- never a key, never a query string.
    """

    mode: str
    agent: object | None = None
    endpoint: str = ""
    credential_ref: str = ""
    model: str = ""
    reason: str = ""

    def host(self) -> str:
        """The endpoint's host, for /status and /model status (no path, no key)."""
        raw = str(self.endpoint or "")
        if not raw or raw.startswith("env:"):
            return raw.split(":", 1)[-1] if raw else ""
        try:
            return str(urlsplit(raw).hostname or "")
        except ValueError:
            return ""


def endpoint_host(url: str) -> str:
    """The host of ``url`` (display only)."""
    try:
        return str(urlsplit(str(url or "")).hostname or "")
    except ValueError:
        return ""


class DenyResolver:
    """The resolver a deployment gets when its own could not be built."""

    def resolve(self, guild_id, user_id=""):
        return Brain(NONE, reason="resolver_unavailable")


_DenyResolver = DenyResolver


class BrainResolver:
    """``resolve(guild, user) -> Brain``, plus the local writes /model needs.

    ``served`` is the allowlist that makes a guild ``owner``. ``store`` is the
    per-guild GuildStore (mode + non-secret BYO facts). ``secrets`` is
    ``awdiscord.brainsecrets.BrainSecrets``; a key never enters ``store``.
    ``hosted`` selects the SSRF policy: the public bot is strict, a standalone
    bot may use loopback/LAN. It defaults to True whenever a ``served`` gate or
    an ``owner_agent`` is present, so missing config can never make a deployed
    bot permissive or serve every guild; a self-hosted bot opts into the
    standalone policy explicitly (``AWDISCORD_STANDALONE``). ``legacy_agent``
    is the env-configured agent an existing standalone bot already answers with;
    with no brain row it keeps answering exactly as before (mode ``byo``).
    """

    def __init__(self, agent, *, owner_agent=None, owners=(), owner_tool_loop: bool = False,
                 served=None, store=None, secrets=None, hosted: bool | None = None,
                 legacy_agent=None, agent_factory=None, resolver=None,
                 probe=None, deny_all: bool = False, hosted_client=None,
                 hosted_factory=None, hosted_base: str | None = None,
                 hosted_ttl: float = HOSTED_TTL):
        self.agent = agent
        self.owner_agent = owner_agent
        self.owners = frozenset(str(o) for o in owners if str(o))
        self.owner_tool_loop = bool(owner_tool_loop)
        self.served = served
        self.store = store
        self.secrets = secrets
        self.legacy_agent = legacy_agent
        self.deny_all = bool(deny_all)
        self.resolver = resolver           # DNS resolver, injectable in tests
        self._probe = probe or probe_endpoint
        self._factory = agent_factory or _default_agent_factory
        if hosted is None:
            # A bot built WITH a served gate, or with an owner/fleet agent, is
            # the public deployment: strict SSRF policy and no "serve everyone"
            # fallback. Only an explicit ``served=None`` (the library embedding)
            # or an explicit ``hosted=False`` opts into the standalone policy.
            # Absence of config is NOT evidence of a standalone deployment --
            # inferring it from an empty allowlist turned "serve nobody" into
            # "serve every guild with the env agent" (and made BYO permissive).
            hosted = served is not None or owner_agent is not None
        self.hosted = bool(hosted)
        self._lock = threading.Lock()
        self._cache: dict[str, tuple[object, int]] = {}
        self._hosted_client = hosted_client
        if self._hosted_client is None:
            try:
                self._hosted_client = GenesisInferenceClient.from_env()
            except Exception:              # noqa: BLE001 - absent config is none
                self._hosted_client = None
        self._hosted_factory = hosted_factory or _default_hosted_factory
        self._hosted_base = hosted_base
        self._hosted_ttl = float(hosted_ttl)
        self._hosted_lock = threading.Lock()
        self._hosted_cache: dict[str, tuple[Brain, float]] = {}
        self._hosted_denied: dict[str, float] = {}

    # -- the predicate ---------------------------------------------------------------
    def resolve(self, guild_id, user_id="") -> Brain:
        try:
            return self._resolve(str(guild_id or ""), str(user_id or ""))
        except Exception as exc:           # noqa: BLE001 - never raises, never owner
            _log.warning("awdiscord brain: resolver error (%s)", type(exc).__name__)
            return Brain(NONE, reason=f"resolver_error:{type(exc).__name__}")

    def _resolve(self, guild: str, user: str) -> Brain:
        if self.deny_all:
            return Brain(NONE, reason="resolver_unavailable")
        mode = self.mode(guild) if (guild and self.store is not None) else ""
        if mode == "byo":
            return self._byo_brain(guild) or Brain(NONE, reason="byo_unconfigured")
        if mode == "hosted":
            # A paid guild never falls back to the owner's model, and OFF is the
            # default: with AWDISCORD_HOSTED unset this is exactly today's none.
            return self._hosted_brain(guild)
        if not guild:
            if self.served is None or user in self.owners:
                return self._owner_brain(user, guild)
            return Brain(NONE, reason="dm_unserved")
        if self.legacy_agent is not None and not self.hosted:
            # An explicitly standalone bot (AWDISCORD_STANDALONE): no brain row,
            # env agent, keep answering. A served/gateway bot never gets here.
            return Brain(BYO, self.legacy_agent,
                         endpoint=str(getattr(self.legacy_agent, "base_url", "")),
                         credential_ref="env:AWDISCORD_AGENT_URL",
                         model=str(getattr(self.legacy_agent, "name", "")),
                         reason="legacy_env")
        if self.served is None:
            return self._owner_brain(user, guild)   # library embedding: no gate
        if self.served.serves_guild(guild):
            return self._owner_brain(user, guild)
        return Brain(NONE, reason="unserved")

    def _is_home(self, guild: str) -> bool:
        """Is ``guild`` the owner's own server (``served.home``)?"""
        home = str(getattr(self.served, "home", "") or "")
        return bool(guild) and bool(home) and guild == home

    def _owner_brain(self, user: str, guild: str = "") -> Brain:
        """The owner-mode brain for (guild, user).

        The operator's fleet agent (``owner_agent``) is the path in the owner's
        HOME server and in a DM. A PARTNER server instead resolves to that
        guild's own brain -- the bot's configured model here, or a BYO/hosted
        agent further up ``_resolve`` -- and the fleet is attached as the
        ``ask_fleet`` tool (``dispatch._agent_kwargs``). Sending an operator's
        mention in a customer server straight to the heavy fleet agent made a
        partner turn wait behind the owner's whole queue (measured 2026-11-07:
        240 s, then a timeout).
        """
        partner = self.served is not None and bool(guild) and not self._is_home(guild)
        if (not partner and self.owner_agent is not None and user in self.owners
                and not self.owner_tool_loop):
            agent = self.owner_agent
            reason = "operator"
        else:
            agent = self.agent
            reason = ("partner_operator" if partner and user in self.owners
                      else "owner_served")
        return Brain(OWNER, agent,
                     endpoint=str(getattr(agent, "base_url", "")),
                     credential_ref="env:owner",
                     model=str(getattr(agent, "name", "")),
                     reason=reason)

    def _byo_brain(self, guild: str) -> Brain | None:
        profile = self.store.get(guild, BRAIN_PLUGIN, BYO_KEY, None)
        if not isinstance(profile, dict) or not profile.get("base_url"):
            return None
        generation = self._generation(guild)
        with self._lock:
            cached = self._cache.get(guild)
            if cached is not None and cached[1] == generation:
                return cached[0]          # type: ignore[return-value]
        key = self.secrets.get(guild) if self.secrets is not None else ""
        if not key:
            return None                   # a profile without its key is not a brain
        base_url = str(profile["base_url"])
        try:
            validate_endpoint(base_url, hosted=self.hosted, resolver=self.resolver)
        except BrainError as exc:
            _log.warning("awdiscord brain: guild %s BYO endpoint refused (%s)",
                         guild, exc)
            return None
        vision = profile.get("vision_models")
        if not isinstance(vision, list):
            vision = None
        transport = (PinnedTransport(base_url, resolver=self.resolver)
                     if self.hosted else PlainTransport(base_url))
        try:
            agent = self._factory(base_url=base_url,
                                  model=str(profile.get("model") or "default"),
                                  api_key=key, vision_models=vision,
                                  timeout=DEFAULT_TIMEOUT, max_tokens=700,
                                  transport=transport)
        except Exception as exc:          # noqa: BLE001 - a bad profile is none
            _log.warning("awdiscord brain: guild %s BYO agent could not be built (%s)",
                         guild, type(exc).__name__)
            return None
        brain = Brain(BYO, agent, endpoint=base_url,
                      credential_ref=f"file:{guild}",
                      model=str(profile.get("model") or ""),
                      reason="byo")
        with self._lock:
            self._cache[guild] = (brain, generation)
        return brain

    # -- hosted (server buys inference from the platform) -----------------------------
    def _hosted_brain(self, guild: str) -> Brain:
        """Genesis decides, then the guild's own key talks to the gateway.

        Every failure is ``none``. A positive record is reused for ``TTL``
        seconds; an error is NEVER cached as hosted, and a gateway refusal
        (401/402/403) drops the record so the next resolve fails closed.
        """
        if not hosted_enabled():
            return Brain(NONE, reason="hosted_not_available")
        if self.secrets is None:
            return Brain(NONE, reason="hosted_unconfigured")
        now = time.monotonic()
        with self._hosted_lock:
            if self._hosted_denied.get(guild, 0.0) > now:
                return Brain(NONE, reason="hosted_denied")
            cached = self._hosted_cache.get(guild)
            if cached is not None:
                if cached[1] > now:
                    return cached[0]
                self._hosted_cache.pop(guild, None)
        client = self._hosted_client
        if client is None:
            return Brain(NONE, reason="hosted_no_genesis")
        try:
            record = client.lookup(guild)
        except Exception as exc:           # noqa: BLE001 - never serves on error
            _log.warning("awdiscord brain: guild %s hosted lookup failed (%s)",
                         guild, type(exc).__name__)
            return Brain(NONE, reason="hosted_lookup_error")
        if str(record.get("mode") or "none").strip().lower() != HOSTED:
            return Brain(NONE, reason="hosted_unbound")
        if str(record.get("status") or "").strip().lower() != "active":
            return Brain(NONE, reason="hosted_inactive")
        key = self.secrets.get(guild) if self.secrets is not None else ""
        if not key:
            return Brain(NONE, reason="hosted_no_key")
        base = self._hosted_base
        if not base:
            try:
                base = hosted_base_url()
            except BrainError as exc:
                _log.warning("awdiscord brain: hosted endpoint refused (%s)", exc)
                return Brain(NONE, reason="hosted_endpoint_refused")
        model = (os.getenv(HOSTED_MODEL_ENV) or DEFAULT_HOSTED_MODEL).strip()
        try:
            agent = self._hosted_factory(
                base_url=base, model=model, api_key=key, guild=guild,
                ca_bundle=_ca_bundle(), strict=self.hosted,
                on_auth_failure=self._invalidate_hosted)
        except Exception as exc:           # noqa: BLE001 - a bad key is none
            _log.warning("awdiscord brain: guild %s hosted agent could not be "
                         "built (%s)", guild, type(exc).__name__)
            return Brain(NONE, reason="hosted_agent_error")
        brain = Brain(HOSTED, agent, endpoint=base,
                      credential_ref=f"file:{guild}", model=model, reason="hosted")
        with self._hosted_lock:
            # A concurrent turn may have been refused while this lookup was in
            # flight; never let that slow success re-cache a rejected key.
            if self._hosted_denied.get(guild, 0.0) > time.monotonic():
                return Brain(NONE, reason="hosted_denied")
            self._hosted_cache[guild] = (brain, time.monotonic() + self._hosted_ttl)
        return brain

    def _invalidate_hosted(self, guild: str) -> None:
        """A gateway refused this guild's key: forget the record and stop asking."""
        with self._hosted_lock:
            self._hosted_cache.pop(guild, None)
            self._hosted_denied[guild] = time.monotonic() + HOSTED_DENY_TTL

    # -- local rows (/model) ---------------------------------------------------------
    def mode(self, guild: str) -> str:
        if self.store is None or not guild:
            return ""
        return str(self.store.get(guild, BRAIN_PLUGIN, MODE_KEY, "") or "")

    def profile(self, guild: str) -> dict:
        if self.store is None or not guild:
            return {}
        value = self.store.get(guild, BRAIN_PLUGIN, BYO_KEY, {})
        return value if isinstance(value, dict) else {}

    def _generation(self, guild: str) -> int:
        if self.store is None:
            return 0
        try:
            return int(self.store.get(guild, BRAIN_PLUGIN, GEN_KEY, 0) or 0)
        except (TypeError, ValueError):
            return 0

    def _bump(self, guild: str) -> None:
        if self.store is not None:
            self.store.set(guild, BRAIN_PLUGIN, GEN_KEY, self._generation(guild) + 1)
        with self._lock:
            self._cache.pop(guild, None)

    def save_byo(self, guild: str, *, base_url: str, model: str, api_key: str,
                 vision_models: list[str] | None) -> dict:
        """Validate, probe, then persist. Only a passing probe writes the row."""
        if self.store is None or self.secrets is None:
            raise BrainError("this bot has no brain store configured")
        if not guild:
            raise BrainError("a server is required")
        validate_endpoint(base_url, hosted=self.hosted, resolver=self.resolver)
        result = self._probe(base_url, model=model, api_key=api_key,
                             hosted=self.hosted, resolver=self.resolver)
        if not result.get("ok"):
            raise BrainError(f"the endpoint did not answer a health check "
                             f"({result.get('detail') or 'no answer'})")
        self.secrets.put(guild, api_key)
        profile = {"base_url": base_url.rstrip("/"), "model": model,
                   "vision_models": vision_models,
                   "fingerprint": _fingerprint(api_key),
                   "probe": {"at": int(time.time()), "status": result.get("status"),
                             "latency_ms": result.get("latency_ms")}}
        self.store.set(guild, BRAIN_PLUGIN, BYO_KEY, profile)
        self.store.set(guild, BRAIN_PLUGIN, MODE_KEY, "byo")
        self._bump(guild)
        return profile

    def test(self, guild: str) -> dict:
        profile = self.profile(guild)
        if not profile.get("base_url"):
            return {"ok": False, "detail": "no BYO endpoint is configured"}
        key = self.secrets.get(guild) if self.secrets is not None else ""
        result = self._probe(str(profile["base_url"]),
                             model=str(profile.get("model") or ""),
                             api_key=key or "", hosted=self.hosted,
                             resolver=self.resolver)
        if result.get("ok") and self.store is not None:
            profile["probe"] = {"at": int(time.time()), "status": result.get("status"),
                                "latency_ms": result.get("latency_ms")}
            self.store.set(guild, BRAIN_PLUGIN, BYO_KEY, profile)
        return result

    def turn_off(self, guild: str) -> None:
        """Drop this server's own model and fall back to the bot's default.

        Deleting the mode row (rather than writing an ``off`` sentinel) makes
        ``resolve`` answer exactly as it would for a server that never
        configured BYO -- the owner's model on a served server. ``/model off``
        therefore does what its reply says, and no server can be left in a
        state with no command to leave it.
        """
        if self.store is not None and guild:
            self.store.delete(guild, BRAIN_PLUGIN, MODE_KEY)
            self.store.delete(guild, BRAIN_PLUGIN, BYO_KEY)
        if self.secrets is not None and guild:
            self.secrets.delete(guild)
        self._bump(guild)

    def status(self, guild: str) -> dict:
        brain = self.resolve(guild, "")
        profile = self.profile(guild)
        return {"mode": self.mode(guild) or ("none" if brain.mode == NONE else brain.mode),
                "resolved": brain.mode, "reason": brain.reason,
                "endpoint_host": endpoint_host(profile.get("base_url", "")),
                "model": profile.get("model") or brain.model,
                "vision": bool(profile.get("vision_models")),
                "probe": (profile.get("probe") or {}),
                "fingerprint": profile.get("fingerprint", "")}


# -- helpers -------------------------------------------------------------------------

def _fingerprint(api_key: str) -> str:
    """A stable, non-reversible tag so /model status can identify a key."""
    import hashlib
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:12]


def _default_agent_factory(**kwargs):
    from awdiscord.agent import OpenAICompatAgent
    return OpenAICompatAgent(**kwargs)


def resolve_brain(guild_id, user_id="") -> Brain:
    """The module-level handle for callers that do not hold the resolver.

    It is installed by ``Bot.from_env`` (and by tests) so plugins and status
    code have one function to call. With nothing installed it is ``none`` --
    fail closed, never the owner's agent.
    """
    if _CURRENT is None:
        return Brain(NONE, reason="resolver_unavailable")
    return _CURRENT.resolve(guild_id, user_id)


_CURRENT: BrainResolver | _DenyResolver | None = None


def install(resolver) -> None:
    """Make ``resolve_brain`` answer from ``resolver`` (None uninstalls)."""
    global _CURRENT
    _CURRENT = resolver
