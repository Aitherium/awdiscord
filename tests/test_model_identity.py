"""Asked "what model are you?", the bot names what serves it instead of refusing.

Reproduces 2026-11-11: the answer was "I don't have a specific model name or version
to share". The ground rules forbid inventing specifics and nothing in the system
prompt named the model, so the honest-sounding refusal was the only option left.
"""
from __future__ import annotations

from awdiscord.agent import OpenAICompatAgent


def _system(agent, **kw):
    return agent._messages("what model are you?", None, "", None, None, **kw)[0]["content"]


def test_system_prompt_names_the_turn_model_and_the_image_reader():
    # MUTATION: drop model_line from _messages.
    agent = OpenAICompatAgent("https://x.invalid/v1", model="bonsai2-27b-5090",
                              vision_models=["gemma4-12b", "bonsai2-27b"])
    system = _system(agent, model="bonsai2-27b-5090")
    assert "answered by the model `bonsai2-27b-5090`" in system
    assert "Images are read by `gemma4-12b`" in system
    assert "never claim you have no model name" in system


def test_served_by_from_the_last_route_is_named_when_it_differs():
    # MUTATION: ignore last_route.
    agent = OpenAICompatAgent("https://x.invalid/v1", model="bonsai2-27b-5090")
    agent.last_route = {"requested": "bonsai2-27b-5090", "served_by": "bonsai2-27b",
                        "cross_model": False}
    assert "last served it as `bonsai2-27b`" in _system(agent, model="bonsai2-27b-5090")


def test_no_configured_model_adds_no_claim():
    # MUTATION: state a model when none was configured ("default").
    agent = OpenAICompatAgent("https://x.invalid/v1")
    assert "answered by the model" not in _system(agent)


def test_the_tool_turn_names_the_tool_model_actually_sent(monkeypatch):
    # MUTATION: name self.model when the turn went to tool_model.
    from awdiscord.plugins import Tool
    agent = OpenAICompatAgent("https://x.invalid/v1", model="chat-m", tool_model="tool-m")
    sent = {}

    def fake_complete(payload):
        sent["model"] = payload["model"]
        sent["system"] = payload["messages"][0]["content"]
        return {"content": "ok"}

    monkeypatch.setattr(agent, "_complete", fake_complete)
    agent.ask("hi", tools=[Tool("t", "a tool", lambda: "x")])
    assert sent["model"] == "tool-m"
    assert "answered by the model `tool-m`" in sent["system"]
