"""S3: a server buys inference from the platform, and Genesis stays the authority.

The gateway is a real local HTTP server and Genesis is a fake client, injected
the same way a deployment injects it. The positive path proves the guild's OWN
key and guild header reach the strict hosted route -- never the bot owner's
model. Every negative path (unbound, inactive, lookup error, missing key, flag
off) resolves to ``none``, and a gateway 401 invalidates the cached record.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from awdiscord.agent import AgentError
from awdiscord.brain import (
    HOSTED,
    HOSTED_QUOTA_TEXT,
    NONE,
    BrainResolver,
    HostedQuotaError,
)
from awdiscord.brainsecrets import BrainSecrets
from awdiscord.dispatch import Dispatcher
from awdiscord.served import ServedGuilds
from awdiscord.store import GuildStore

GUILD = "111111111111111111"
MEMBER = "7"
HOSTED_KEY = "sk-guild-hosted-test"
BASE = "/v1/hosted"


class SpyAgent:
    """The bot owner's model: it must never answer for a hosted guild."""

    name = "owner-spy"
    sees_images = False

    def __init__(self):
        self.calls = []

    def ask(self, prompt, **kwargs):
        self.calls.append(prompt)
        return "owner answer"

    def health(self):
        return "ok"


class FakeGenesis:
    """The Genesis binding route: a record, or a lookup failure."""

    def __init__(self, record=None, error=None):
        self.record = record
        self.error = error
        self.calls = []

    def lookup(self, guild):
        self.calls.append(guild)
        if self.error is not None:
            raise self.error
        return dict(self.record or {})


class _Gateway:
    """A strict hosted-route server; records exactly what reached it."""

    def __init__(self, status=200, content="hosted answer"):
        outer = self
        self.status = status
        self.content = content
        self.requests: list[tuple[str, str, dict, bytes]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append(
                    ("POST", self.path, dict(self.headers), raw))
                if outer.status != 200:
                    body = b'{"error": "refused"}'
                else:
                    body = json.dumps(
                        {"choices": [{"message": {"content": outer.content}}]}
                    ).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}{BASE}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def gateway():
    server = _Gateway()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def hosted_on(monkeypatch):
    monkeypatch.setenv("AWDISCORD_HOSTED", "1")
    monkeypatch.delenv("AWDISCORD_HOSTED_BASE", raising=False)
    monkeypatch.delenv("AWDISCORD_HOSTED_MODEL", raising=False)
    monkeypatch.delenv("AWDISCORD_AGENT_PRIORITY", raising=False)


ACTIVE = {"guild_id": GUILD, "mode": "hosted", "plan": "tools_pro",
          "tier": "community", "status": "active"}


def _resolver(tmp_path, genesis, *, base=None, key=HOSTED_KEY, served=None,
              factory=None):
    store = GuildStore(tmp_path / "guilds.db")
    secrets = BrainSecrets(tmp_path)
    store.set(GUILD, "brain", "mode", "hosted")
    if key:
        secrets.put(GUILD, key)
    return BrainResolver(
        SpyAgent(), served=served if served is not None else ServedGuilds(),
        store=store, secrets=secrets, hosted_client=genesis, hosted_base=base,
        hosted_factory=factory)


def _header(headers, name):
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def test_no_binding_resolves_to_none(tmp_path):
    genesis = FakeGenesis({"guild_id": GUILD, "mode": "none",
                           "plan": "", "tier": "", "status": "inactive"})
    brain = _resolver(tmp_path, genesis).resolve(GUILD, MEMBER)
    assert brain.mode == NONE
    assert brain.reason == "hosted_unbound"
    assert brain.agent is None


def test_inactive_record_resolves_to_none(tmp_path):
    record = dict(ACTIVE, status="past_due")
    brain = _resolver(tmp_path, FakeGenesis(record)).resolve(GUILD, MEMBER)
    assert brain.mode == NONE
    assert brain.reason == "hosted_inactive"
    assert brain.agent is None


def test_lookup_error_resolves_to_none_and_is_not_cached(tmp_path):
    genesis = FakeGenesis(error=RuntimeError("genesis down"))
    resolver = _resolver(tmp_path, genesis)
    assert resolver.resolve(GUILD, MEMBER).mode == NONE
    assert resolver.resolve(GUILD, MEMBER).mode == NONE
    # An error is never cached as hosted: it is re-asked, not served.
    assert genesis.calls == [GUILD, GUILD]


def test_missing_guild_key_resolves_to_none(tmp_path):
    brain = _resolver(tmp_path, FakeGenesis(ACTIVE), key="").resolve(GUILD, MEMBER)
    assert brain.mode == NONE
    assert brain.reason == "hosted_no_key"


def test_flag_off_resolves_to_none_exactly_as_before(tmp_path, monkeypatch):
    monkeypatch.delenv("AWDISCORD_HOSTED", raising=False)
    genesis = FakeGenesis(ACTIVE)
    brain = _resolver(tmp_path, genesis).resolve(GUILD, MEMBER)
    assert brain.mode == NONE
    assert brain.reason == "hosted_not_available"
    assert genesis.calls == []


def test_active_record_answers_at_the_hosted_route_with_the_guild_key(
        tmp_path, gateway):
    genesis = FakeGenesis(ACTIVE)
    resolver = _resolver(tmp_path, genesis, base=gateway.base_url)
    brain = resolver.resolve(GUILD, MEMBER)
    assert brain.mode == HOSTED
    assert brain.agent is not None
    assert brain.agent.ask("hello") == "hosted answer"

    method, path, headers, raw = gateway.requests[-1]
    assert method == "POST"
    assert path == f"{BASE}/chat/completions"
    assert _header(headers, "Authorization") == f"Bearer {HOSTED_KEY}"
    assert _header(headers, "X-Aither-Guild-Id") == GUILD
    body = json.loads(raw.decode())
    assert body["model"] == "aither-bonsai"
    assert body["messages"][-1]["content"] == "hello"
    # A person typed this turn: the scheduler must not queue it behind tens of
    # minutes of background work. The hosted client alone lacked this, so every
    # Discord turn queued (measured 2026-11-07: 43-276 s, all timing out).
    assert body["metadata"]["priority"] == "user"
    assert body["metadata"]["source"] == "chat:awdiscord"


def test_the_hosted_priority_follows_the_operator_setting(tmp_path, gateway,
                                                          monkeypatch):
    monkeypatch.setenv("AWDISCORD_AGENT_PRIORITY", "background")
    brain = _resolver(tmp_path, FakeGenesis(ACTIVE),
                      base=gateway.base_url).resolve(GUILD, MEMBER)
    assert brain.agent is not None
    brain.agent.ask("hello")
    body = json.loads(gateway.requests[-1][3].decode())
    assert body["metadata"]["priority"] == "background"


def test_positive_record_is_cached_for_the_ttl(tmp_path, gateway):
    genesis = FakeGenesis(ACTIVE)
    resolver = _resolver(tmp_path, genesis, base=gateway.base_url)
    first = resolver.resolve(GUILD, MEMBER)
    second = resolver.resolve(GUILD, MEMBER)
    assert first.mode == second.mode == HOSTED
    assert first.agent is second.agent
    assert genesis.calls == [GUILD]


def test_gateway_401_invalidates_the_record_and_the_next_resolve_is_none(
        tmp_path):
    gateway = _Gateway(status=401)
    try:
        genesis = FakeGenesis(ACTIVE)
        resolver = _resolver(tmp_path, genesis, base=gateway.base_url)
        brain = resolver.resolve(GUILD, MEMBER)
        assert brain.mode == HOSTED
        with pytest.raises(AgentError):
            brain.agent.ask("hello")
        after = resolver.resolve(GUILD, MEMBER)
        assert after.mode == NONE
        assert after.agent is None
        # A refused key is not re-asked on the very next turn.
        assert genesis.calls == [GUILD]
    finally:
        gateway.close()


def test_gateway_429_gets_the_friendly_reply_and_never_the_owner_model(
        tmp_path):
    gateway = _Gateway(status=429)
    try:
        genesis = FakeGenesis(ACTIVE)
        resolver = _resolver(tmp_path, genesis, base=gateway.base_url)
        owner = SpyAgent()
        dispatcher = Dispatcher(owner, brains=resolver)
        out = dispatcher.chat(MEMBER, GUILD, "hello")
        assert out["content"] == HOSTED_QUOTA_TEXT
        assert "could not answer" not in out["content"]
        assert owner.calls == []
        assert resolver.agent.calls == []
        # One attempt, no retry storm against a spent allowance.
        assert len(gateway.requests) == 1
    finally:
        gateway.close()


def test_a_refusal_racing_the_lookup_is_not_recached(tmp_path):
    holder = {}

    def factory(**kwargs):
        # Another turn's call was refused while this lookup was in flight.
        holder["resolver"]._invalidate_hosted(GUILD)
        return SpyAgent()

    resolver = _resolver(tmp_path, FakeGenesis(ACTIVE),
                         base="https://gateway.example/v1/hosted",
                         factory=factory)
    holder["resolver"] = resolver
    brain = resolver.resolve(GUILD, MEMBER)
    assert brain.mode == NONE
    assert brain.reason == "hosted_denied"


def test_hosted_agent_raises_the_quota_error_type(tmp_path):
    gateway = _Gateway(status=429)
    try:
        resolver = _resolver(tmp_path, FakeGenesis(ACTIVE), base=gateway.base_url)
        agent = resolver.resolve(GUILD, MEMBER).agent
        with pytest.raises(HostedQuotaError) as caught:
            agent.ask("hello")
        assert str(caught.value) == HOSTED_QUOTA_TEXT
    finally:
        gateway.close()


# -- the account-linked read (GET /served) -------------------------------------------
class _ServedRoute:
    """Genesis' served route: a body the test controls, served over real HTTP."""

    def __init__(self, body=None, status=200):
        outer = self
        self.body = body if body is not None else {"guild_id": GUILD, "served": True,
                                                   "reason": "served"}
        self.status = status
        self.paths: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                outer.paths.append(self.path)
                payload = (outer.body if isinstance(outer.body, bytes)
                           else json.dumps(outer.body).encode())
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _served_client(route):
    from awdiscord.brain import GenesisInferenceClient

    return GenesisInferenceClient(route.base_url, bot_key="bot-key")


def test_the_served_read_returns_the_verdict_for_this_guild():
    route = _ServedRoute()
    try:
        client = _served_client(route)
        assert client.served(GUILD) is True
        assert route.paths[-1].startswith(
            "/v1/workspace/discord-guild-binding/served?guild_id=" + GUILD)
    finally:
        route.close()


def test_the_served_read_is_false_when_genesis_says_false():
    route = _ServedRoute({"guild_id": GUILD, "served": False,
                          "reason": "account_unlinked"})
    try:
        assert _served_client(route).served(GUILD) is False
    finally:
        route.close()


@pytest.mark.parametrize("body", [
    {"guild_id": GUILD, "served": "true", "reason": "served"},     # not a bool
    {"guild_id": GUILD, "reason": "served"},                       # no verdict
    {"guild_id": "999999999999999999", "served": True},            # another guild
])
def test_a_served_answer_that_is_not_about_this_guild_fails_closed(body):
    from awdiscord.brain import HostedLookupError

    route = _ServedRoute(body)
    try:
        with pytest.raises(HostedLookupError):
            _served_client(route).served(GUILD)
    finally:
        route.close()


def test_an_http_error_on_the_served_read_fails_closed():
    from awdiscord.brain import HostedLookupError

    route = _ServedRoute({"error": "bot_key_required"}, status=401)
    try:
        with pytest.raises(HostedLookupError):
            _served_client(route).served(GUILD)
    finally:
        route.close()
