"""S2: a server runs on its own model, with the key kept out of the store.

The positive path is a real local OpenAI-compatible endpoint: /brain setup opens a
modal, the submission probes it (GET /models), saves, and the next question is
answered BY THAT endpoint. The negative paths are the SSRF matrix for the hosted
bot (private/loopback/metadata/plain-http/redirect/rebinding) and the admin check.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import awdiscord.brain as brain_mod
import pytest
from awdiscord.brain import (
    BRAIN_MODAL_ID,
    NONE,
    BrainError,
    BrainResolver,
    PinnedTransport,
    probe_endpoint,
    validate_endpoint,
)
from awdiscord.brainsecrets import BrainSecrets
from awdiscord.dispatch import Dispatcher
from awdiscord.served import ServedGuilds
from awdiscord.store import GuildStore

GUILD = "111111111111111111"
OTHER = "222222222222222222"
ADMIN, MEMBER = "42", "7"
MANAGE_GUILD = 1 << 5
TOKEN = "x" * 24 + ".abcd." + "y" * 24


class SpyAgent:
    name = "spy"
    sees_images = False

    def __init__(self):
        self.calls = []

    def ask(self, prompt, **kwargs):
        self.calls.append(prompt)
        return "owner answer"

    def health(self):
        return "ok"


class _Endpoint:
    """A tiny OpenAI-compatible server; records what reached it."""

    def __init__(self):
        outer = self
        self.requests: list[tuple[str, str, dict]] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                outer.requests.append(("GET", self.path, dict(self.headers)))
                self._send({"object": "list", "data": [{"id": "own-model"}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length)
                outer.requests.append(("POST", self.path, dict(self.headers)))
                outer.posts = getattr(outer, "posts", []) + [raw]
                self._send({"choices": [{"message": {"content": "from own model"}}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/v1"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def endpoint():
    server = _Endpoint()
    yield server
    server.close()


def _resolver(tmp_path, *, hosted=False, served=None):
    store = GuildStore(tmp_path / "guilds.db")
    secrets = BrainSecrets(tmp_path)
    return BrainResolver(SpyAgent(), served=served, hosted=hosted, store=store,
                         secrets=secrets, legacy_agent=None)


def _command(action, *, guild=GUILD, user=ADMIN, permissions=MANAGE_GUILD):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}, "permissions": str(permissions)},
            "data": {"name": "model",
                     "options": [{"name": "action", "value": action}]}}


def _modal(base_url, *, key="sk-local-test", guild=GUILD, user=ADMIN,
           permissions=MANAGE_GUILD, vision=""):
    return {"type": 5, "id": "2", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}, "permissions": str(permissions)},
            "data": {"custom_id": BRAIN_MODAL_ID, "components": [{"type": 1, "components": [
                {"type": 4, "custom_id": "endpoint", "value": base_url},
                {"type": 4, "custom_id": "model", "value": "own-model"},
                {"type": 4, "custom_id": "api_key", "value": key},
                {"type": 4, "custom_id": "vision", "value": vision},
            ]}]}}


# -- self-serve, end to end ----------------------------------------------------------
def test_setup_modal_saves_and_the_server_answers_with_its_own_model(tmp_path, endpoint):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)

    opened = d.handle(_command("setup"))
    assert opened.response["type"] == 9
    assert opened.response["data"]["custom_id"] == BRAIN_MODAL_ID
    assert [r["components"][0]["custom_id"] for r in
            opened.response["data"]["components"]] == ["endpoint", "model", "api_key",
                                                       "vision"]

    submitted = d.handle(_modal(endpoint.base_url))
    assert submitted.deferred is not None
    saved = submitted.deferred()
    assert "now answers with `own-model`" in saved["content"]
    assert brains.mode(GUILD) == "byo"

    out = d.chat(MEMBER, GUILD, "hello")
    assert out["content"] == "from own model"
    assert any(method == "POST" for method, _p, _h in endpoint.requests)


def test_the_key_is_a_0600_file_and_never_in_guilds_db(tmp_path, endpoint):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)
    assert d.handle(_modal(endpoint.base_url, key="sk-very-secret")).deferred()

    path = brains.secrets.path(GUILD)
    assert path.exists()
    assert "sk-very-secret" in path.read_text(encoding="utf-8")
    if os.name != "nt":                        # NTFS ignores POSIX mode bits
        assert (os.stat(path).st_mode & 0o777) == 0o600
        assert (os.stat(path.parent).st_mode & 0o777) == 0o700

    db_bytes = (tmp_path / "guilds.db").read_bytes()
    assert b"sk-very-secret" not in db_bytes
    profile = brains.profile(GUILD)
    assert "sk-very-secret" not in json.dumps(profile)
    assert profile["fingerprint"] and len(profile["fingerprint"]) == 12


def test_a_non_admin_cannot_configure_the_brain(tmp_path):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)
    refused = d.handle(_command("setup", user=MEMBER, permissions=0))
    assert refused.response["type"] == 4
    assert "admin" in refused.response["data"]["content"].lower()
    assert brains.mode(GUILD) == ""

    modal = d.handle(_modal("https://api.example.com/v1", user=MEMBER, permissions=0))
    assert "admin" in modal.response["data"]["content"].lower()
    assert brains.mode(GUILD) == ""


def test_status_test_and_off(tmp_path, endpoint):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)
    d.handle(_modal(endpoint.base_url)).deferred()

    status = d.handle(_command("status"))
    text = status.response["data"]["content"]
    assert "mode: byo" in text and "own-model" in text
    assert "127.0.0.1" in text and "sk-local-test" not in text

    checked = d.handle(_command("test"))
    assert checked.deferred is not None
    assert "answered" in checked.deferred()["content"]

    off = d.handle(_command("off"))
    assert off.response["type"] == 4
    # "off" DELETES the row: the server falls back to the default brain rather
    # than being parked in an unrepairable "off" state.
    assert brains.mode(GUILD) == ""
    assert not brains.secrets.path(GUILD).exists()
    assert brains.resolve(GUILD, MEMBER).mode != "byo"
    assert d.chat(MEMBER, GUILD, "hello")["content"] == "owner answer"
    # /model still answers after opting out, so it can be set up again.
    assert d.handle(_command("status")).response["type"] == 4


def test_off_returns_a_guild_that_never_had_byo_to_its_previous_mode(tmp_path):
    """The reply promises the default model; the resolver must actually do it."""
    brains = _resolver(tmp_path)                    # no BYO row ever written
    before = brains.resolve(GUILD, MEMBER)
    brains.turn_off(GUILD)
    after = brains.resolve(GUILD, MEMBER)
    assert before.mode == after.mode
    assert after.mode != NONE


def test_a_failed_probe_saves_nothing(tmp_path):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)
    result = d.handle(_modal("http://127.0.0.1:1/v1")).deferred()
    assert result["content"].startswith("Not saved:")
    assert brains.mode(GUILD) == ""
    assert not brains.secrets.path(GUILD).exists()


def test_a_modal_without_the_guild_is_refused(tmp_path, endpoint):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)
    it = _modal(endpoint.base_url)
    it.pop("guild_id")
    assert "inside your server" in d.handle(it).response["data"]["content"]


def test_a_foreign_modal_id_is_not_ours(tmp_path):
    d = Dispatcher(SpyAgent(), brains=_resolver(tmp_path))
    it = _modal("https://api.example.com/v1")
    it["data"]["custom_id"] = "awp:someplugin:x"
    assert "not handled" in d.handle(it).response["data"]["content"]


def test_vision_comes_from_configuration_not_a_constant(tmp_path):
    from awdiscord.agent import OpenAICompatAgent
    assert OpenAICompatAgent("http://x/v1", model="m", vision_models=[]).sees_images is False
    assert OpenAICompatAgent("http://x/v1", model="m").sees_images is True


# -- the admin check is the signed interaction's, not a payload field ---------------
def test_a_payload_field_cannot_grant_admin(tmp_path):
    brains = _resolver(tmp_path)
    d = Dispatcher(SpyAgent(), brains=brains)
    it = _command("setup", user=MEMBER, permissions=0)
    it["member"]["is_admin"] = True                      # ignored
    it["data"]["permissions"] = str(MANAGE_GUILD)         # ignored
    assert "admin" in d.handle(it).response["data"]["content"].lower()
    assert brains.mode(GUILD) == ""


# -- SSRF: the hosted bot is strict, a standalone bot owns its LAN ------------------
HOSTED_REFUSED = [
    "https://127.0.0.1/v1",
    "https://10.1.2.3/v1",
    "https://192.168.1.10/v1",
    "https://169.254.169.254/latest/meta-data",
    "http://api.example.com/v1",
    "https://api.example.com:8443/v1",
    "https://metadata.google.internal/v1",
    "https://user:pass@api.example.com/v1",
]


@pytest.mark.parametrize("url", HOSTED_REFUSED)
def test_hosted_refuses_non_public_endpoints(url):
    with pytest.raises(BrainError):
        validate_endpoint(url, hosted=True, resolver=lambda host, port: ["93.184.216.34"])


STANDALONE_ALLOWED = [u for u in HOSTED_REFUSED if "user:pass@" not in u]


@pytest.mark.parametrize("url", STANDALONE_ALLOWED)
def test_standalone_allows_loopback_and_lan(url):
    assert validate_endpoint(url, hosted=False)


def test_standalone_also_refuses_credentials_in_the_url():
    with pytest.raises(BrainError):
        validate_endpoint("https://user:pass@api.example.com/v1", hosted=False)


def test_hosted_refuses_a_public_name_that_resolves_private():
    with pytest.raises(BrainError):
        validate_endpoint("https://innocent.example/v1", hosted=True, resolver=(
            lambda host, port: ["10.0.0.5"]))


def test_hosted_refuses_when_any_resolved_address_is_private():
    with pytest.raises(BrainError):
        validate_endpoint("https://mixed.example/v1", hosted=True, resolver=(
            lambda host, port: ["93.184.216.34", "127.0.0.1"]))


def test_hosted_accepts_a_public_https_endpoint():
    url = "https://api.example.com/v1/"
    assert validate_endpoint(url, hosted=True, resolver=(
        lambda host, port: ["93.184.216.34"])) == "https://api.example.com/v1"


def test_a_redirect_is_never_followed():
    class Redirecting:
        def get(self, path, headers, timeout=None):
            return 302, b""

        def post(self, path, headers, body=None, timeout=None):
            return 302, b""

    result = probe_endpoint("https://api.example.com/v1", hosted=True,
                            transport=Redirecting())
    assert result["ok"] is False


def test_redirect_handler_refuses_to_follow():
    handler = brain_mod._NoRedirect()
    assert handler.redirect_request(None, None, 302, "Found", {}, "http://127.0.0.1/") is None


def test_dns_rebinding_is_caught_per_request(monkeypatch):
    """The host resolved publicly for request 1, loopback for request 2: refused.

    One lookup per request (see the within-request test below), so the flip is
    scheduled on the second lookup.
    """
    calls: list[int] = []

    def flip(host, port):
        calls.append(1)
        if len(calls) <= 1:
            return ["93.184.216.34"]
        return ["127.0.0.1"]

    class FakeConn:
        def __init__(self, *a, **kw):
            self.timeout = 1.0

        def request(self, *a, **kw):
            return None

        def getresponse(self):
            class R:
                status = 200

                def read(self):
                    return b"{}"
            return R()

        def close(self):
            pass

    monkeypatch.setattr(brain_mod, "_PinnedHTTPSConnection", FakeConn)
    transport = PinnedTransport("https://rebind.example/v1", resolver=flip)
    assert transport.request("GET", "/models", {})[0] == 200
    with pytest.raises(BrainError):
        transport.request("GET", "/models", {})


def test_rebinding_within_one_request_dials_only_the_validated_ip(monkeypatch):
    """A TTL-0 zone cannot answer public on validate and private on connect.

    The validation lookup and the connect lookup must be the SAME lookup: the
    old code resolved twice per request, so the second answer (here
    169.254.169.254) was dialed even though the first had passed.
    """
    seen: list[str] = []

    def flip(host, port):
        seen.append(host)
        # First answer public, every later answer the cloud metadata address.
        return ["93.184.216.34"] if len(seen) == 1 else ["169.254.169.254"]

    dialed: dict[str, str] = {}

    class FakeConn:
        def __init__(self, host, ip, port, context, timeout):
            dialed["ip"] = ip
            self.timeout = timeout

        def request(self, *a, **kw):
            return None

        def getresponse(self):
            class R:
                status = 200

                def read(self):
                    return b"{}"
            return R()

        def close(self):
            pass

    monkeypatch.setattr(brain_mod, "_PinnedHTTPSConnection", FakeConn)
    transport = PinnedTransport("https://rebind.example/v1", resolver=flip)
    assert transport.request("GET", "/models", {})[0] == 200
    assert dialed["ip"] == "93.184.216.34"
    assert len(seen) == 1               # exactly one DNS lookup per request


def test_a_byo_row_with_a_private_endpoint_is_none_on_the_hosted_bot(tmp_path):
    brains = _resolver(tmp_path, hosted=True, served=ServedGuilds())
    brains.store.set(GUILD, "brain", "mode", "byo")
    brains.store.set(GUILD, "brain", "byo", {"base_url": "https://10.0.0.5/v1",
                                             "model": "m"})
    brains.secrets.put(GUILD, "sk")
    brain = brains.resolve(GUILD, MEMBER)
    assert brain.mode == NONE and brain.agent is None


# -- gateway routing ----------------------------------------------------------------
def test_gateway_routes_a_core_modal_to_the_dispatcher(monkeypatch):
    from awdiscord.gateway import Bot

    bot = Bot(TOKEN, SpyAgent(), served=ServedGuilds())
    routed = []
    monkeypatch.setattr(bot.dispatcher, "handle",
                        lambda it: routed.append(("dispatcher", it["data"]["custom_id"]))
                        or Dispatcher(SpyAgent()).handle(
                            {"type": 2, "data": {"name": "status"},
                             "member": {"user": {"id": ADMIN}}}))
    result = {}
    monkeypatch.setattr(bot.rest, "interaction_callback",
                        lambda *a: result.setdefault("sent", a))
    bot.handle_raw({"type": 5, "id": "1", "token": "t", "guild_id": GUILD,
                    "member": {"user": {"id": ADMIN}, "permissions": str(MANAGE_GUILD)},
                    "data": {"custom_id": BRAIN_MODAL_ID, "components": []}})
    assert routed == [("dispatcher", BRAIN_MODAL_ID)]
    assert result["sent"]


def test_gateway_routes_a_plugin_modal_to_the_host(monkeypatch):
    from awdiscord.gateway import Bot
    from awdiscord.plugins import PluginHost
    from awdiscord.store import GuildStore

    seen = []
    host = PluginHost(GuildStore(":memory:"))
    monkeypatch.setattr(host, "handle_modal", lambda it: seen.append(it["data"]["custom_id"])
                        or None)
    bot = Bot(TOKEN, SpyAgent(), host=host, served=ServedGuilds(home=GUILD))
    # A host that returns None must fall through to the dispatcher, not crash.
    monkeypatch.setattr(bot.rest, "interaction_callback", lambda *a: None)
    bot.handle_raw({"type": 5, "id": "1", "token": "t", "guild_id": GUILD,
                    "member": {"user": {"id": ADMIN}},
                    "data": {"custom_id": "awp:thing:x", "components": []}})
    assert seen == ["awp:thing:x"]
