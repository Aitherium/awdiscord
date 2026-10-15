import io
import json
import socket
import types
import urllib.error
import urllib.request

import pytest
from awdiscord import cli
from awdiscord.agent import AgentError, CallableAgent, EchoAgent, OpenAICompatAgent, load_personas
from awdiscord.approvals import ApprovalDesk, ApprovalError
from awdiscord.dispatch import DEFERRED, EPHEMERAL, MESSAGE, PONG, UPDATE, Dispatcher, commands
from awdiscord.guard import TokenRefusedError, check_bot_token, require_bot_identity
from awdiscord.install import FORBIDDEN_PERMISSIONS, MINIMAL_PERMISSIONS, install_url
from awdiscord.ratelimit import RateLimiter
from awdiscord.rest import Rest
from awdiscord.webhook import WebhookError, post_webhook

FAKE = "A" * 24 + "." + "B" * 6 + "." + "C" * 27   # bot-token shaped, not a token


def _ask(question="hi", user="42", guild="g1", **extra):
    opts = [{"name": "question", "value": question}]
    opts += [{"name": k, "value": v} for k, v in extra.items()]
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}}, "data": {"name": "ask", "options": opts}}


def test_self_test_passes():
    assert cli.main(["--self-test"]) == 0


# -- install link -----------------------------------------------------------------
def test_install_url_minimal_and_scoped():
    url = install_url(123456789012345678)
    assert url.startswith("https://discord.com/oauth2/authorize?")
    assert "client_id=123456789012345678" in url
    assert "scope=bot+applications.commands" in url
    assert f"permissions={MINIMAL_PERMISSIONS}" in url


@pytest.mark.parametrize("name", sorted(FORBIDDEN_PERMISSIONS))
def test_install_url_refuses_dangerous_bits(name):
    with pytest.raises(ValueError):
        install_url("1", permissions=MINIMAL_PERMISSIONS | FORBIDDEN_PERMISSIONS[name])


def test_install_url_rejects_non_numeric_id():
    with pytest.raises(ValueError):
        install_url("abc")


# -- bot tokens only ---------------------------------------------------------------
@pytest.mark.parametrize("bad", ["", None, "mfa." + "x" * 80, "Bearer " + FAKE, "short.token"])
def test_non_bot_tokens_refused(bad):
    with pytest.raises(TokenRefusedError):
        check_bot_token(bad)


def test_bot_prefix_stripped_and_value_never_echoed():
    assert check_bot_token("Bot " + FAKE) == FAKE
    with pytest.raises(TokenRefusedError) as exc:
        check_bot_token("mfa." + "secretvalue" * 8)
    assert "secretvalue" not in str(exc.value)


def test_user_account_refused_after_whoami():
    with pytest.raises(TokenRefusedError):
        require_bot_identity({"id": "1", "username": "someone"})
    assert require_bot_identity({"id": "1", "bot": True})["id"] == "1"


def test_rest_always_sends_bot_authorization():
    seen = {}

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout):
        seen["auth"] = req.get_header("Authorization")
        return Resp(json.dumps({"id": "9", "bot": True}).encode())

    me = Rest(FAKE, opener=opener).me()
    assert me["bot"] is True and seen["auth"] == "Bot " + FAKE


def test_rest_refuses_user_identity():
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    rest = Rest(FAKE, opener=lambda req, timeout: Resp(b'{"id": "9", "bot": false}'))
    with pytest.raises(TokenRefusedError):
        rest.me()


# -- rate limits -------------------------------------------------------------------
def test_rate_limit_scopes():
    now = [0.0]
    lim = RateLimiter(per_user=(2, 10.0), per_guild=(3, 10.0), global_=(100, 10.0),
                      clock=lambda: now[0])
    assert lim.check("a", "g").allowed and lim.check("a", "g").allowed
    v = lim.check("a", "g")
    assert not v.allowed and v.scope == "user" and v.retry_after > 0
    assert lim.check("b", "g").allowed
    v = lim.check("c", "g")
    assert not v.allowed and v.scope == "guild"
    now[0] += 10
    assert lim.check("a", "g").allowed


def test_refused_request_takes_no_tokens():
    lim = RateLimiter(per_user=(1, 100.0), per_guild=(1, 100.0), clock=lambda: 0.0)
    assert lim.check("a", "g").allowed
    assert not lim.check("b", "g").allowed      # guild empty
    assert lim.check("b", "other").allowed      # b's own bucket was not charged


# -- approvals ---------------------------------------------------------------------
def test_approval_round_trip_and_callback(tmp_path):
    hits = []

    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers["Content-Length"])
            hits.append((json.loads(self.rfile.read(n)), self.headers["X-Awdiscord-Signature"]))
            self.send_response(204)
            self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    desk = ApprovalDesk(tmp_path, "k", ["42"])
    rec = desk.raise_card("Publish post", "body", callback_url=f"http://127.0.0.1:{srv.server_port}/cb",
                          meta={"campaign": "c1"})
    card = desk.card(rec)
    ids = [b["custom_id"] for b in card["components"][0]["components"]]
    assert all(len(i) <= 100 for i in ids)
    assert card["allowed_mentions"] == {"parse": []}
    d = desk.resolve(ids[0], "42")
    srv.shutdown()
    assert d.verdict == "approved" and d.callback == "delivered"
    assert hits[0][0]["approval_id"] == rec["id"] and hits[0][0]["meta"] == {"campaign": "c1"}
    decided = desk.decided_card(desk.get(rec["id"]))
    assert all(b["disabled"] for b in decided["components"][0]["components"])


def test_approval_forgery_and_operator(tmp_path):
    desk = ApprovalDesk(tmp_path, "k", ["42"])
    rec = desk.raise_card("t", "b")
    other = ApprovalDesk(tmp_path / "x", "different-secret", ["42"])
    with pytest.raises(ApprovalError):
        desk.resolve(other.custom_id(rec["id"], "approve"), "42")
    with pytest.raises(ApprovalError):
        desk.resolve(desk.custom_id(rec["id"], "approve"), "43")
    with pytest.raises(ApprovalError):
        desk.resolve(desk.custom_id("nope", "approve"), "42")
    assert desk.get(rec["id"])["status"] == "pending"


def test_approval_requires_secret_and_operator(tmp_path):
    with pytest.raises(ValueError):
        ApprovalDesk(tmp_path, "", ["1"])
    with pytest.raises(ValueError):
        ApprovalDesk(tmp_path, "k", [])


def test_approval_decided_once(tmp_path):
    desk = ApprovalDesk(tmp_path, "k", ["1", "2"])
    rec = desk.raise_card("t", "b")
    first = desk.resolve(desk.custom_id(rec["id"], "deny"), "1")
    second = desk.resolve(desk.custom_id(rec["id"], "approve"), "2")
    assert first.verdict == second.verdict == "denied" and second.already


# -- dispatcher --------------------------------------------------------------------
def test_ping():
    assert Dispatcher(EchoAgent()).handle({"type": 1}).response == {"type": PONG}


def test_ask_private_is_ephemeral_and_mentions_off():
    res = Dispatcher(EchoAgent()).handle(_ask(private=True))
    assert res.response == {"type": DEFERRED, "data": {"flags": EPHEMERAL}}
    out = res.deferred()
    assert out["allowed_mentions"] == {"parse": []} and out["content"] == "echo: hi"


def test_ask_clips_long_answers():
    res = Dispatcher(CallableAgent(lambda p: "x" * 5000)).handle(_ask())
    assert len(res.deferred()["content"]) <= 2000


def test_ask_agent_error_is_shown_not_raised():
    def boom(prompt):
        raise AgentError("the agent is unreachable right now")
    res = Dispatcher(CallableAgent(boom)).handle(_ask())
    assert "could not answer" in res.deferred()["content"]


def test_ask_rate_limited():
    d = Dispatcher(EchoAgent(), RateLimiter(per_user=(1, 600.0)))
    assert d.handle(_ask()).response["type"] == DEFERRED
    limited = d.handle(_ask())
    assert limited.response["type"] == MESSAGE and limited.response["data"]["flags"] == EPHEMERAL


def test_share_needs_an_answer_first():
    d = Dispatcher(EchoAgent())
    share = {**_ask(), "data": {"name": "share"}}
    assert "Nothing to share" in d.handle(share).response["data"]["content"]
    d.handle(_ask("what is up")).deferred()
    out = d.handle(share).response
    assert "flags" not in out["data"] and "echo: what is up" in out["data"]["content"]


def test_status_is_private():
    out = Dispatcher(EchoAgent()).handle({"type": 2, "user": {"id": "1"},
                                          "data": {"name": "status"}}).response
    assert out["data"]["flags"] == EPHEMERAL and "echo - ok" in out["data"]["content"]


def test_foreign_button_ignored(tmp_path):
    d = Dispatcher(EchoAgent(), desk=ApprovalDesk(tmp_path, "k", ["1"]))
    res = d.handle({"type": 3, "user": {"id": "1"}, "data": {"custom_id": "other:thing"}})
    assert res.response["data"]["flags"] == EPHEMERAL


def test_button_press_updates_card(tmp_path):
    desk = ApprovalDesk(tmp_path, "k", ["1"])
    rec = desk.raise_card("t", "b")
    d = Dispatcher(EchoAgent(), desk=desk)
    res = d.handle({"type": 3, "member": {"user": {"id": "1"}},
                    "data": {"custom_id": desk.custom_id(rec["id"], "approve")}})
    assert res.response["type"] == UPDATE and res.log["approval"] == "approved"
    refused = d.handle({"type": 3, "member": {"user": {"id": "2"}},
                        "data": {"custom_id": desk.custom_id(rec["id"], "deny")}})
    assert refused.response["type"] == MESSAGE and desk.get(rec["id"])["status"] == "approved"


def test_commands_shape():
    cmds = commands(["aither", "lyra"])
    assert [c["name"] for c in cmds] == ["ask", "share", "status"]
    persona = [o for o in cmds[0]["options"] if o["name"] == "persona"][0]
    assert [c["value"] for c in persona["choices"]] == ["aither", "lyra"]


# -- agents ------------------------------------------------------------------------
def test_personas_loaded_from_both_layouts(tmp_path):
    (tmp_path / "Lyra").mkdir()
    (tmp_path / "Lyra" / "SOUL.md").write_text("I am Lyra.")
    (tmp_path / "vera.md").write_text("I am Vera.")
    (tmp_path / "empty.md").write_text("  ")
    assert load_personas(tmp_path) == {"lyra": "I am Lyra.", "vera": "I am Vera."}
    assert load_personas(tmp_path / "missing") == {}


def test_openai_compat_sends_persona_system_prompt(monkeypatch):
    sent = {}

    class Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout, context=None):
        sent.update(json.loads(req.data))
        return Resp(json.dumps({"choices": [{"message": {"content": " hello "}}]}).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    agent = OpenAICompatAgent("http://x/v1", model="m", personas={"lyra": "I am Lyra."})
    assert agent.ask("hi", persona="Lyra") == "hello"
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][0]["content"].startswith("I am Lyra.\n\nToday is ")


def test_openai_compat_unreachable_is_agent_error():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    agent = OpenAICompatAgent(f"http://127.0.0.1:{port}/v1", timeout=2)
    with pytest.raises(AgentError):
        agent.ask("hi")


# -- webhook -----------------------------------------------------------------------
@pytest.mark.parametrize("url", ["http://discord.com/api/webhooks/1/x",
                                 "https://evil.example/api/webhooks/1/x",
                                 "https://discord.com/api/channels/1"])
def test_webhook_refuses_non_discord(url):
    with pytest.raises(WebhookError):
        post_webhook(url, "hi")


# -- gateway inbox -----------------------------------------------------------------
def test_inbox_requires_bearer_and_raises_cards(tmp_path):
    from awdiscord.gateway import Bot, _raw_interaction

    desk = ApprovalDesk(tmp_path, "k", ["42"])
    bot = Bot(FAKE, EchoAgent(), app_id="1", desk=desk, approval_channel="c9",
              inbox_port=0, inbox_token="tok")
    posted = []
    bot.rest = types.SimpleNamespace(
        create_message=lambda ch, payload: posted.append((ch, payload)) or {"id": "m1"})
    bot._start_inbox()
    url = f"http://127.0.0.1:{bot.inbox_port}/approvals"
    req = urllib.request.Request(url, data=b'{"title":"t"}', method="POST")
    with pytest.raises(urllib.error.HTTPError) as exc:
        urllib.request.urlopen(req, timeout=5)
    assert exc.value.code == 401
    req = urllib.request.Request(url, data=b'{"title":"t","body":"b"}', method="POST",
                                 headers={"Authorization": "Bearer tok"})
    out = json.loads(urllib.request.urlopen(req, timeout=5).read())
    assert out["messages"] == ["m1"] and posted[0][0] == "c9"
    assert desk.get(out["id"])["status"] == "pending"
    bot.inbox_server.shutdown()

    fake = types.SimpleNamespace(user=types.SimpleNamespace(id=42), id=7, token="tk",
                                 type=types.SimpleNamespace(value=2), data={"name": "ask"},
                                 channel_id=5, guild_id=None)
    raw = _raw_interaction(fake)
    assert raw["user"] == {"id": "42"} and "member" not in raw
