"""Every tool call leaves evidence, and the bot never invents a tool failure.

Reproduces 2026-11-11 15:01: "The specific details about turboquant and aitherkvcache
could not be verified due to failed search tools." The search service was healthy and
the turn's logs were gone after a restart, so nobody could tell what happened.
"""
from __future__ import annotations

import json

from awdiscord.agent import (
    GROUND_RULES,
    OpenAICompatAgent,
    strip_unfounded_failure_claims,
    tool_result_failed,
)
from awdiscord.plugins import Tool


def _agent(monkeypatch, replies):
    agent = OpenAICompatAgent("https://x.invalid/v1", model="m")
    queue = list(replies)
    monkeypatch.setattr(agent, "_complete", lambda payload: queue.pop(0))
    return agent


def _call(name, args):
    return {"tool_calls": [{"id": "c1", "function": {"name": name,
                                                      "arguments": json.dumps(args)}}]}


def test_each_tool_call_is_logged_as_a_json_line_and_recorded(monkeypatch, capsys):
    # MUTATION: drop the _log_line / last_tools record in the tool loop.
    agent = _agent(monkeypatch, [_call("web_search", {"query": "aitherkvcache"}),
                                 {"content": "AitherKVCache compresses the KV cache."}])
    tool = Tool("web_search", "search", lambda query: "- AitherKVCache v2.0.0\n  https://x")
    agent.ask("what is aitherkvcache", tools=[tool])
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    calls = [x for x in lines if x.get("event") == "tool_call"]
    assert len(calls) == 1 and calls[0]["tool"] == "web_search" and calls[0]["ok"] is True
    assert calls[0]["turn"] == agent.last_turn_id and "aitherkvcache" in calls[0]["args"]
    assert "ms" in calls[0]
    assert agent.last_tools == [{k: calls[0][k] for k in ("tool", "args", "ok", "error",
                                                           "ms")}]


def test_a_failure_claim_with_no_failed_tool_is_removed(monkeypatch):
    # MUTATION: skip strip_unfounded_failure_claims in _ask.
    agent = _agent(monkeypatch, [
        {"content": "KV cache quantization saves memory. The specific details about "
                    "turboquant could not be verified due to failed search tools."}])
    out = agent.ask("turboquant?")
    assert "failed search" not in out and "KV cache quantization saves memory." in out


def test_a_real_tool_failure_keeps_the_claim(monkeypatch):
    agent = _agent(monkeypatch, [_call("web_search", {"query": "x"}),
                                 {"content": "The web search failed, so I cannot say."}])

    def boom(query):
        raise TimeoutError("search timed out")
    out = agent.ask("x", tools=[Tool("web_search", "search", boom)])
    assert "The web search failed" in out
    assert agent.last_tools[0]["ok"] is False and "TimeoutError" in agent.last_tools[0]["error"]


def test_failure_detection_and_rules():
    assert tool_result_failed("tool error: TimeoutError: x")
    assert not tool_result_failed("- AitherKVCache v2.0.0")
    assert not tool_result_failed("No results for 'x'. Try fewer words.")
    text, n = strip_unfounded_failure_claims("Only this. Search tools failed.", [])
    assert n == 1 and text == "Only this."
    assert "ONLY when a tool call in this turn returned an error" in GROUND_RULES
    assert "Never state anyone's hardware" in GROUND_RULES
