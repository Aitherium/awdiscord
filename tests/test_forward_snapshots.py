"""A Discord "Forwarded" message has empty ``content``; its text is in ``message_snapshots``.

Measured 2026-11-15: forwarding Prometheus's GPU alert to Atlas got the canned "Hi - ask me
anything here" (Atlas logged ``mention_answered chars=39``), because the bot never read the
snapshot, so the alert text never reached the turn.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from awdiscord.gateway import Bot, _snapshot_text

ALERT = ("DOWN: GPU memory is being used outside the arbiter: python pid 45012 holds "
         "1.15 GB; the fleet's models can be pushed off the card and slow to a crawl.")
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38


class Rest:
    def __init__(self):
        self.sent = []

    def request(self, *a, **k):
        return {}

    def create_message(self, channel, payload):
        self.sent.append((channel, payload))
        return {"id": "posted-1"}


class EchoAgent:
    """Answers with the prompt it was given, so a test can see what the turn carried."""

    def __init__(self):
        self.prompts = []

    def ask(self, prompt, **k):
        self.prompts.append(prompt)
        return "answered"


def _snapshot(content="", embeds=()):
    return SimpleNamespace(content=content, embeds=list(embeds), attachments=[])


def _forward(snapshots, content="", user="42"):
    author = SimpleNamespace(id=int(user), bot=False, roles=[], display_name="owner",
                             name="owner")
    return SimpleNamespace(id=7, guild=None, channel=SimpleNamespace(id=555), author=author,
                           content=content, mentions=[], attachments=[], reference=None,
                           embeds=[], message_snapshots=list(snapshots))


def _bot(agent=None, owners=("42",)):
    bot = Bot(TOKEN, agent or EchoAgent(), owners=list(owners))
    bot.rest = Rest()
    bot.state["user_id"] = "999"
    return bot


def _send(bot, message):
    asyncio.run(bot.dispatch_message(message, SimpleNamespace(id=999)))


def test_snapshot_text_reads_content_and_embeds():
    embed = SimpleNamespace(title="Atlas", description="held: python 1.15", footer=None,
                            fields=[])
    got = _snapshot_text(_forward([_snapshot(ALERT, [embed])]))
    assert "GPU memory" in got and "held: python 1.15" in got


def test_forward_reaches_the_turn_as_text_and_is_flagged():
    bot = _bot()
    seen = []
    bot.handle_message = seen.append
    _send(bot, _forward([_snapshot(ALERT)]))
    assert len(seen) == 1
    assert "GPU memory" in seen[0]["content"] and seen[0]["forwarded"] is True


def test_typed_text_wins_over_a_snapshot():
    bot = _bot()
    seen = []
    bot.handle_message = seen.append
    _send(bot, _forward([_snapshot(ALERT)], content="look at this"))
    assert seen[0]["content"] == "look at this" and seen[0]["forwarded"] is False


def test_forward_no_longer_gets_the_canned_greeting():
    agent = EchoAgent()
    bot = _bot(agent)
    _send(bot, _forward([_snapshot(ALERT)]))
    assert bot.rest.sent, "the bot answered nothing"
    assert "Hi - ask me anything" not in bot.rest.sent[0][1]["content"]
    assert any("GPU memory" in p for p in agent.prompts)
