"""A model's tool-call markup is never posted as the bot's answer.

Reproduces 2026-10-31 #eusyra-tasks: a cloud fallback answered a mention with
DeepSeek DSML as TEXT and the bot posted it verbatim.
"""
from __future__ import annotations

import json

import pytest
from awdiscord.agent import (
    AgentError,
    GenesisAgent,
    OpenAICompatAgent,
    clean_answer,
    has_tool_markup,
    parse_tool_markup,
    strip_tool_markup,
)

BAR = "｜｜"
# The logged payload shape, verbatim structure (double full-width bars, a space
# before the tag name).
DSML = (f"<{BAR}DSML{BAR} calls><{BAR}DSML{BAR} invoke name=\"web_search\">"
        f"<{BAR}DSML{BAR} parameter name=\"query\" string=\"true\">X handle @Aitherium owner"
        f"</{BAR}DSML{BAR} parameter></{BAR}DSML{BAR} invoke></{BAR}DSML{BAR} calls>")
DSML_CUT = (f"Let me check.\n<{BAR}DSML{BAR} calls><{BAR}DSML{BAR} invoke name=\"web_search\">"
            f"<{BAR}DSML{BAR} parameter name=\"query\" string=\"true\">X handle")


def test_logged_dsml_parses_into_a_real_call():
    calls = parse_tool_markup(DSML, {"web_search"})
    assert len(calls) == 1
    assert calls[0]["function"]["name"] == "web_search"
    assert json.loads(calls[0]["function"]["arguments"]) == {
        "query": "X handle @Aitherium owner"}


def test_unoffered_tool_is_not_a_call():
    assert parse_tool_markup(DSML, {"something_else"}) == []


def test_strip_closed_and_truncated():
    assert has_tool_markup(DSML)
    assert strip_tool_markup(DSML) == ""
    assert strip_tool_markup(DSML_CUT) == "Let me check."
    assert not has_tool_markup("plain prose about a <b>tag</b>")


def test_clean_answer_refuses_markup_only():
    with pytest.raises(AgentError):
        clean_answer(DSML)
    assert clean_answer(DSML_CUT) == "Let me check."


class _Tool:
    name = "web_search"

    def __init__(self):
        self.ran = []

    def schema(self):
        return {"type": "function", "function": {"name": self.name, "parameters": {}}}

    def run(self, args):
        self.ran.append(args)
        return "result: @aitherium on X"


def _agent(replies):
    a = OpenAICompatAgent("https://x.invalid/v1")
    sent = []

    def fake(payload):
        sent.append(payload)
        return replies.pop(0)
    a._complete = fake
    return a, sent


def test_markup_with_tools_offered_runs_the_tool():
    tool = _Tool()
    a, sent = _agent([{"content": DSML, "tool_calls": None},
                      {"content": "It's @aitherium on X."}])
    out = a.ask("what's publicly available?", tools=[tool])
    assert out == "It's @aitherium on X."
    assert tool.ran == [{"query": "X handle @Aitherium owner"}]
    assert sent[1]["messages"][-1]["role"] == "tool"


def test_markup_without_tools_is_never_posted():
    a, _ = _agent([{"content": DSML}])
    with pytest.raises(AgentError):
        a.ask("hello")


def test_genesis_markup_is_never_posted(monkeypatch):
    import io
    import urllib.request

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: _R(json.dumps({"response": DSML}).encode()))
    with pytest.raises(AgentError):
        GenesisAgent("https://g.invalid").ask("let me know what's publicly available")
