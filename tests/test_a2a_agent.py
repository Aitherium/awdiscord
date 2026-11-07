"""A2AAgent speaks A2A v0.3 ``message/send``, proven against a real local server.

A local ``http.server`` on 127.0.0.1 answers JSON-RPC, so every assertion here
runs the actual urllib path -- the request body, the header, the reply parsing --
with no mocked transport.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from awdiscord.agent import A2AAgent, AgentError, EchoAgent, OpenAICompatAgent
from awdiscord.dispatch import Dispatcher, _accepts


class _State:
    def __init__(self):
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.reply = None                       # dict, or callable(body) -> dict
        self.card = {"name": "Local A2A Agent"}
        self.card_status = 200


@pytest.fixture()
def a2a():
    state = _State()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _write(self, status, payload):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            state.requests.append(json.loads(self.rfile.read(length).decode()))
            state.headers.append({k: v for k, v in self.headers.items()})
            reply = state.reply
            self._write(200, reply(state.requests[-1]) if callable(reply) else reply)

        def do_GET(self):
            self._write(state.card_status, state.card)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/a2a", state
    finally:
        server.shutdown()
        server.server_close()


def _task(*, text="", state="completed", history=None, task_id="t1", message=None):
    """A JSON-RPC reply carrying a v0.3 Task, the shape adk's server returns."""
    result = {"kind": "task", "id": task_id, "contextId": "c",
              "status": {"state": state}, "history": list(history or [])}
    if message is not None:
        result["message"] = message
    return {"jsonrpc": "2.0", "id": "1", "result": result}


def _turn(role, text):
    return {"kind": "message", "role": role, "parts": [{"kind": "text", "text": text}]}


def test_v0_3_task_history_is_answered_when_there_is_no_message(a2a):
    url, state = a2a
    state.reply = _task(history=[_turn("user", "hi"), _turn("agent", "pong")])

    assert A2AAgent(url).ask("hi") == "pong"

    body = state.requests[0]
    assert body["method"] == "message/send"
    msg = body["params"]["message"]
    assert msg["kind"] == "message"
    assert msg["role"] == "user"
    assert msg["messageId"]
    assert msg["parts"] == [{"kind": "text", "text": "hi"}]


def test_result_message_wins_over_history(a2a):
    url, state = a2a
    state.reply = _task(
        message={"kind": "message", "role": "agent",
                 "parts": [{"kind": "text", "text": "from message"}]},
        history=[_turn("agent", "from history")])
    assert A2AAgent(url).ask("hi") == "from message"


def test_context_id_is_stable_per_channel_and_user(a2a):
    url, state = a2a
    state.reply = _task(history=[_turn("agent", "ok")])
    agent = A2AAgent(url)

    agent.ask("a", user="7", channel="c1")
    agent.ask("b", user="7", channel="c1")
    agent.ask("c", user="8", channel="c1")
    agent.ask("d", user="7", channel="c2")

    ids = [r["params"]["message"]["contextId"] for r in state.requests]
    assert ids[0] == ids[1]
    assert ids[2] != ids[0]
    assert ids[3] != ids[0]
    assert ids[0] == "discord:c1:7"


def test_json_rpc_error_raises_agent_error(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "error": {"code": -32601, "message": "Method not found: message/send"}}
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "Method not found" in str(exc.value)
    assert "protocol error" in str(exc.value)
    assert url not in str(exc.value)


def test_a_failed_task_is_an_agent_error(a2a):
    url, state = a2a
    state.reply = _task(state="failed")
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "task failed" in str(exc.value)


def test_stale_task_id_is_dropped_and_retried_once(a2a):
    url, state = a2a
    calls = {"n": 0}

    def reply(body):
        calls["n"] += 1
        if calls["n"] == 1:
            return _task(history=[_turn("agent", "one")])
        if "taskId" in body["params"]["message"]:
            return {"jsonrpc": "2.0", "id": "1",
                    "error": {"code": -32602, "message": "Task not found: t1"}}
        return _task(task_id="t2", history=[_turn("agent", "two")])

    state.reply = reply
    agent = A2AAgent(url)
    assert agent.ask("first") == "one"
    assert agent.ask("second") == "two"
    assert len(state.requests) == 3
    assert state.requests[1]["params"]["message"]["taskId"] == "t1"
    assert "taskId" not in state.requests[2]["params"]["message"]


def test_a_working_task_without_text_is_an_agent_error(a2a):
    url, state = a2a
    state.reply = _task(state="working")
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "has not answered yet" in str(exc.value)


def _message(text, role="agent"):
    return {"kind": "message", "role": role, "messageId": "m1",
            "parts": [{"kind": "text", "text": text}]}


def test_a_bare_message_result_is_the_answer(a2a):
    # A2A v0.3 SendMessageResponse is Task | Message: the result itself may be
    # the agent's Message, with no wrapper at all.
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1", "result": _message("bare hello")}

    assert A2AAgent(url).ask("hi") == "bare hello"


def test_a_bare_message_nested_under_task_is_the_answer(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": {"task": _message("nested hello")}}

    assert A2AAgent(url).ask("hi") == "nested hello"


def test_a_bare_message_carries_the_task_id_for_the_next_turn(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": {**_message("one"), "taskId": "t9"}}
    agent = A2AAgent(url)
    assert agent.ask("first") == "one"
    assert agent.ask("second") == "one"
    assert state.requests[1]["params"]["message"]["taskId"] == "t9"


def test_an_echoed_user_message_result_is_not_an_answer(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": _message("hi", role="user")}
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "empty answer" in str(exc.value)


def test_input_required_surfaces_the_peers_question(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": {"kind": "task", "id": "t1", "contextId": "c",
                              "status": {"state": "input-required",
                                         "message": "need a repo name"}}}
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "need a repo name" in str(exc.value)
    assert "empty answer" not in str(exc.value)


def test_input_required_surfaces_a_message_shaped_question(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": {"kind": "task", "id": "t1",
                              "status": {"state": "input-required",
                                         "message": _message("which branch?")}}}
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "which branch?" in str(exc.value)


def test_input_required_without_a_message_names_the_clarification(a2a):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": {"kind": "task", "id": "t1",
                              "status": {"state": "input-required"}}}
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert "clarification" in str(exc.value)


@pytest.mark.parametrize("state_name", ["rejected", "canceled", "auth-required"])
def test_a_terminal_state_without_text_names_the_state(a2a, state_name):
    url, state = a2a
    state.reply = {"jsonrpc": "2.0", "id": "1",
                   "result": {"kind": "task", "id": "t1",
                              "status": {"state": state_name}}}
    with pytest.raises(AgentError) as exc:
        A2AAgent(url).ask("hi")
    assert state_name in str(exc.value)
    assert "empty answer" not in str(exc.value)


def test_health_reports_the_card_name(a2a):
    url, state = a2a
    assert A2AAgent(url).health() == "ok (Local A2A Agent)"
    state.card_status = 404
    assert A2AAgent(url).health() == "HTTP 404"


def test_from_env_is_none_unless_the_url_is_set(monkeypatch):
    monkeypatch.delenv("AWDISCORD_A2A_URL", raising=False)
    monkeypatch.delenv("AWDISCORD_A2A_TOKEN", raising=False)
    assert A2AAgent.from_env() is None
    monkeypatch.setenv("AWDISCORD_A2A_URL", "   ")
    assert A2AAgent.from_env() is None

    monkeypatch.setenv("AWDISCORD_A2A_URL", "https://peer.example/a2a/")
    monkeypatch.setenv("AWDISCORD_A2A_TOKEN", "tok")
    agent = A2AAgent.from_env()
    assert isinstance(agent, A2AAgent)
    assert agent.url == "https://peer.example/a2a"
    assert agent._headers()["Authorization"] == "Bearer tok"

    monkeypatch.setenv("AWDISCORD_A2A_URL", "ftp://peer.example/a2a")
    with pytest.raises(AgentError):
        A2AAgent.from_env()


def test_bot_from_env_prefers_a2a_only_when_configured(monkeypatch, tmp_path, a2a):
    url, state = a2a
    from awdiscord.gateway import Bot

    # Bot-token shaped (never a real token); any other shape is refused at startup.
    monkeypatch.setenv("AWDISCORD_TOKEN", "A" * 24 + "." + "B" * 6 + "." + "C" * 27)
    monkeypatch.setenv("AWDISCORD_NO_PLUGINS", "1")
    monkeypatch.setenv("AWDISCORD_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AWDISCORD_AGENT_URL", raising=False)
    monkeypatch.setenv("AWDISCORD_A2A_URL", url)
    # Today's path would have raised here: no AWDISCORD_AGENT_URL was set.
    assert isinstance(Bot.from_env().agent, A2AAgent)

    monkeypatch.delenv("AWDISCORD_A2A_URL")
    monkeypatch.setenv("AWDISCORD_AGENT_URL", "http://127.0.0.1:9/v1")
    assert isinstance(Bot.from_env().agent, OpenAICompatAgent)


def test_a_channel_reaches_an_agent_that_accepts_it():
    seen: dict = {}

    class Stub:
        name = "stub"

        def ask(self, prompt, **kw):
            seen.update(kw)
            return "ok"

        def health(self):
            return "ok"

    assert Dispatcher(Stub()).chat("7", "g1", "hi", channel="c9")["content"] == "ok"
    assert seen["channel"] == "c9"

    # EchoAgent has an explicit signature without `channel`, so the guard drops it
    # and the existing bot behaves exactly as before.
    assert _accepts(EchoAgent().ask, "channel") is False
    assert Dispatcher(EchoAgent()).chat("7", "g1", "hi",
                                        channel="c9")["content"] == "echo: hi"
