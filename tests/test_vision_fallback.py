"""A turn with an image always lands, even when no vision lane answers.

Measured 2026-11-15: every bot answered an @-mention carrying an image with "Working
on it - the model queue is busy" and then never posted. Both vision lanes timed out at
the scheduler; each try waited the full agent timeout and the busy-retry loop re-ran
the pair. Pinned here:

1. a vision lane gets a SHORT probe (``vision_timeout``), then the TEXT model answers
   and the reply says the image was not read;
2. a lane that failed is skipped by later turns for a cooldown, so several personas do
   not each queue the same dead call;
3. a describe-this-image call (``text_fallback=False``) raises instead of letting a
   text model invent a description;
4. a turn that crashes still replaces the "working on it" ack with an honest line.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from awdiscord import agent as agent_mod  # noqa: E402
from awdiscord.agent import AgentError, AgentTimeoutError, OpenAICompatAgent  # noqa: E402
from awdiscord.gateway import Bot  # noqa: E402

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38
IMG = ["data:image/png;base64,AAAA"]


@pytest.fixture(autouse=True)
def _fresh_breaker():
    agent_mod._VISION_DOWN.clear()
    yield
    agent_mod._VISION_DOWN.clear()


def _agent(monkeypatch, vision_ok=False):
    a = OpenAICompatAgent("http://x/v1", model="text-4b", timeout=300,
                          vision_models=["gemma4-12b", "bonsai2-27b"])
    calls = []

    def fake(prompt, persona, user, context, tools, history, images, model, timeout=None):
        calls.append({"model": model, "images": images, "timeout": timeout,
                      "prompt": prompt})
        if images and not vision_ok:
            raise AgentTimeoutError("the agent is taking longer than expected")
        return f"answer from {model}"
    monkeypatch.setattr(a, "_ask", fake)
    return a, calls


def test_vision_probe_is_short_then_text_model_answers_and_says_so(monkeypatch):
    a, calls = _agent(monkeypatch)
    out = a.ask("what is in this?", images=IMG)
    # 2026-11-16: the turn model is the last vision lane (it is often the same
    # multimodal Bonsai 2 lane), then the text-only fallback.
    assert [c["model"] for c in calls] == ["gemma4-12b", "bonsai2-27b", "text-4b",
                                           "text-4b"]
    assert all(c["timeout"] == a.vision_timeout and c["images"] for c in calls[:3])
    assert a.vision_timeout < 300 and calls[3]["timeout"] is None
    assert calls[3]["images"] is None
    assert "cannot see them" in calls[3]["prompt"]
    assert out.startswith(agent_mod.VISION_UNREAD_NOTE)
    assert out.endswith("answer from text-4b")


def test_a_failed_lane_is_skipped_by_the_next_turn(monkeypatch):
    a, calls = _agent(monkeypatch)
    a.ask("first", images=IMG)
    calls.clear()
    out = a.ask("second persona, same image", images=IMG)
    # no second probe of the dead lanes: only the turn model, once with the image
    assert [(c["model"], bool(c["images"])) for c in calls] == [("text-4b", True),
                                                                ("text-4b", False)]
    assert out.startswith(agent_mod.VISION_UNREAD_NOTE)


def test_a_lane_is_tried_again_after_its_cooldown(monkeypatch):
    a, calls = _agent(monkeypatch)
    a.ask("first", images=IMG)
    for m in list(agent_mod._VISION_DOWN):
        agent_mod._VISION_DOWN[m] = 0.0
    calls.clear()
    a.ask("later", images=IMG)
    assert calls[0]["model"] == "gemma4-12b"


def test_describe_call_raises_instead_of_guessing(monkeypatch):
    a, calls = _agent(monkeypatch)
    with pytest.raises(AgentError):
        a.ask("describe", images=IMG, text_fallback=False)
    assert all(c["images"] for c in calls)      # no text model ever "described" it


def test_a_working_vision_lane_still_reads_the_image(monkeypatch):
    a, calls = _agent(monkeypatch, vision_ok=True)
    assert a.ask("look", images=IMG) == "answer from gemma4-12b"
    assert len(calls) == 1


def test_vision_timeout_comes_from_env_and_never_exceeds_the_agent_timeout(monkeypatch):
    monkeypatch.setenv("AWDISCORD_VISION_TIMEOUT", "40")
    assert OpenAICompatAgent("http://x/v1", timeout=300).vision_timeout == 40.0
    assert OpenAICompatAgent("http://x/v1", timeout=10).vision_timeout == 10.0


class _Rest:
    def __init__(self):
        self.sent, self.deleted, self._n = [], [], 0

    def request(self, method, path, *a, **k):
        if method == "DELETE":
            self.deleted.append(path)
        return {}

    def create_message(self, channel, payload):
        self._n += 1
        self.sent.append((channel, payload))
        return {"id": f"m{self._n}"}


class _Agent:
    def ask(self, prompt, **k):
        return "unused"


def test_a_crashed_turn_still_answers_after_the_ack(monkeypatch):
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0.01")
    bot = Bot(TOKEN, _Agent())
    bot.rest = _Rest()

    def boom(*a, **k):
        import time
        time.sleep(0.2)
        raise RuntimeError("bug")
    monkeypatch.setattr(bot.dispatcher, "chat", boom)
    bot.handle_message({"id": "q1", "channel_id": "c", "guild_id": None, "user": "7",
                        "content": "hi"})
    texts = [p.get("content") for _, p in bot.rest.sent]
    assert texts[0] == Bot.ACK_TEXT
    assert "couldn't finish this answer" in texts[-1] and "RuntimeError" in texts[-1]
    assert bot.rest.deleted == ["/channels/c/messages/m1"]
