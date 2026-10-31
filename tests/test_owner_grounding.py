"""An operator's @mention reaches Genesis WITH the server's plugin grounding.

2026-10-31: the owner asked "@Aither i linked my aitherium account. what does that
do?" in the Collective. Operators' mentions go to ``GenesisAgent`` (fleet tools),
and ``GenesisAgent.ask`` dropped the plugins' ``context`` and ``tools`` -- so the
brain plugin's company-brain search never ran and the collective persona (what
/link gives) never arrived. Genesis guessed and answered about VRoid Hub.

A plugin tool marked ``grounds = True`` is now run by the dispatcher with the
member's question BEFORE an agent that cannot call the bot's tools answers, and
the result rides in ``context`` with the plugins' persona. Nothing else runs: a
plugin's slash-command work (``/draft``) is never reachable from a mention.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from awdiscord.dispatch import Dispatcher
from awdiscord.plugins import Command, Plugin, PluginHost, Tool
from awdiscord.ratelimit import RateLimiter
from awdiscord.store import GuildStore


class Recorder:
    """An agent like GenesisAgent: it brings its own tools, never runs the bot's."""
    runs_bot_tools = False
    name = "genesis:aither"

    def __init__(self):
        self.calls = []

    def ask(self, prompt, *, persona=None, user="", context="", tools=None,
            history=None):
        self.calls.append({"prompt": prompt, "context": context, "tools": tools})
        return "ok"


def _brainlike(searched, drafted):
    def search(query, limit=5):
        searched.append(query)
        return "Linking sets your tier.\n\nSources: docs/link.md"

    def draft(ctx):
        drafted.append(ctx)
        return ctx.reply("No draft: should never run from a mention", private=True)

    t = Tool("company_brain_search", "search", search,
             parameters={"type": "object", "properties": {"query": {"type": "string"}}})
    t.grounds = True
    other = Tool("company_brain_remember", "never stores", lambda text="": "nope")
    return Plugin("brain", "1", "x", default_enabled=True,
                  persona="Answer company questions with Sources.",
                  tools=[t, other], commands=[Command("draft", "d", draft)])


def _dispatcher(agent, plugin):
    host = PluginHost(GuildStore(":memory:"), plugins=[plugin])
    return Dispatcher(agent, RateLimiter(per_user=(100, 60.0)), host=host,
                      owner_agent=agent, owners=["42"])


def test_owner_mention_reaches_genesis_with_persona_and_company_brain_grounding():
    searched, drafted = [], []
    agent = Recorder()
    d = _dispatcher(agent, _brainlike(searched, drafted))
    q = "i linked my aitherium account. what does that do?"
    out = d.chat("42", "g1", f"<@9> {q}", bot_id="9", channel="c1")
    assert out["content"] == "ok"
    assert searched == [q]                          # the brain was asked THE question
    assert drafted == []                            # a mention never drafts
    ctx = agent.calls[0]["context"]
    assert "Answer company questions with Sources." in ctx
    assert "Sources: docs/link.md" in ctx
    assert "nope" not in ctx                        # only grounds=True tools pre-run


def test_ask_command_grounds_the_same_way_for_an_operator():
    searched, drafted = [], []
    agent = Recorder()
    d = _dispatcher(agent, _brainlike(searched, drafted))
    it = {"type": 2, "guild_id": "g1", "member": {"user": {"id": "42"}},
          "data": {"name": "ask", "options": [{"name": "question", "value": "what is /link?"}]}}
    res = d.handle(it)
    assert res.response["type"] == 5                # ACK first; work runs deferred
    assert searched == []
    res.deferred()
    assert searched == ["what is /link?"]
    assert "Sources: docs/link.md" in agent.calls[0]["context"]


def test_an_agent_that_runs_tools_itself_is_not_pre_grounded():
    searched, drafted = [], []

    class ToolRunner(Recorder):
        runs_bot_tools = True
    agent = ToolRunner()
    d = _dispatcher(agent, _brainlike(searched, drafted))
    d.chat("42", "g1", "what does linking do?", channel="c1")
    assert searched == []                           # it will call the tool itself
    assert {t.name for t in agent.calls[0]["tools"]} >= {"company_brain_search"}


def test_a_grounding_tool_failure_is_named_not_fatal():
    def boom(query, limit=5):
        raise RuntimeError("graph down")
    t = Tool("company_brain_search", "s", boom)
    t.grounds = True
    agent = Recorder()
    d = _dispatcher(agent, Plugin("brain", "1", "x", default_enabled=True, tools=[t]))
    assert d.chat("42", "g1", "hello?", channel="c1")["content"] == "ok"
    assert "company_brain_search failed: RuntimeError" in agent.calls[0]["context"]


def test_genesis_agent_sends_the_plugin_context_with_the_question():
    from awdiscord.agent import GenesisAgent
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            out = json.dumps({"response": "fine"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        a = GenesisAgent(f"http://127.0.0.1:{srv.server_port}")
        assert GenesisAgent.runs_bot_tools is False
        a.ask("what does linking do?", user="42", context="Sources: docs/link.md")
        body = seen[0]
        assert "Sources: docs/link.md" in body["message"]
        assert body["message"].rstrip().endswith("what does linking do?")
        assert body["context"]["server_context"] == "Sources: docs/link.md"
        a.ask("plain", user="42")
        assert seen[1]["message"] == "plain"             # no context: unchanged
    finally:
        srv.shutdown()
