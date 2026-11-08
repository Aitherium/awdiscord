"""A slow mention gets a visible "working on it" reply, and the answer still lands.

Measured 2026-11-08: a member's request waited 153 s on a busy GPU lane; the bot
gave up at its 120 s agent timeout and posted "try again in a minute" while the
model went on to finish the answer, which was thrown away.
"""
from __future__ import annotations

import time

from awdiscord.agent import OpenAICompatAgent, env_timeout
from awdiscord.gateway import Bot

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38


class Rest:
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


class SlowAgent:
    def __init__(self, delay):
        self.delay = delay

    def ask(self, prompt, **k):
        time.sleep(self.delay)
        return "the real answer"


def _bot(agent):
    b = Bot(TOKEN, agent)
    b.rest = Rest()
    return b


MSG = {"id": "q1", "channel_id": "c", "guild_id": None, "user": "7", "content": "hi"}


def test_slow_turn_is_acked_then_answered_and_ack_removed(monkeypatch):
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0.05")
    bot = _bot(SlowAgent(0.4))
    bot.handle_message(dict(MSG))
    texts = [p.get("content") for _, p in bot.rest.sent]
    assert texts[0] == Bot.ACK_TEXT
    assert "the real answer" in texts[-1]
    assert bot.rest.deleted == ["/channels/c/messages/m1"]


def test_fast_turn_gets_no_ack(monkeypatch):
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "5")
    bot = _bot(SlowAgent(0))
    bot.handle_message(dict(MSG))
    texts = [p.get("content") for _, p in bot.rest.sent]
    assert Bot.ACK_TEXT not in texts and bot.rest.deleted == []
    time.sleep(0.05)
    assert len(bot.rest.sent) == 1          # a cancelled ack never fires late


def test_ack_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("AWDISCORD_ACK_AFTER_S", "0")
    bot = _bot(SlowAgent(0.2))
    bot.handle_message(dict(MSG))
    assert [p.get("content") for _, p in bot.rest.sent] == ["the real answer"]


def test_agent_timeout_comes_from_env(monkeypatch):
    monkeypatch.setenv("AWDISCORD_AGENT_URL", "http://x/v1")
    monkeypatch.setenv("AWDISCORD_AGENT_TIMEOUT", "300")
    assert OpenAICompatAgent.from_env()._timeout == 300.0
    monkeypatch.setenv("AWDISCORD_AGENT_TIMEOUT", "nonsense")
    assert OpenAICompatAgent.from_env()._timeout == 120.0
    monkeypatch.delenv("AWDISCORD_AGENT_TIMEOUT")
    assert OpenAICompatAgent.from_env()._timeout == 120.0
    assert env_timeout("AWDISCORD_NOPE", 9.0) == 9.0
