"""A plugin may take a message addressed to the bot before the chat brain does."""
from __future__ import annotations

import pytest
from awdiscord import intake
from awdiscord.gateway import Bot

TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4." + "G" * 6 + "." + "x" * 38


class Rest:
    def __init__(self):
        self.sent = []

    def create_message(self, channel_id, payload):
        self.sent.append((channel_id, payload))
        return {"id": f"m{len(self.sent)}", "channel_id": channel_id}

    def request(self, method, path, body=None, auth=True):
        return {}


class Agent:
    name = "spy"

    def __init__(self):
        self.asked = []

    def ask(self, prompt, **kw):
        self.asked.append(prompt)
        return "brain answer"


@pytest.fixture(autouse=True)
def _clean():
    intake.clear()
    yield
    intake.clear()


def _bot():
    agent = Agent()
    bot = Bot(TOKEN, agent)
    bot.rest = Rest()
    return bot, agent


def _dm(user="77", text="the upload fails", **extra):
    return {"id": "900", "channel_id": "dm1", "guild_id": None, "user": user,
            "content": text, "author_name": "Sam", **extra}


def test_with_nothing_registered_the_message_reaches_the_brain():
    bot, agent = _bot()
    bot.handle_message(_dm())
    assert intake.registered() == 0 and agent.asked


def test_a_registered_intake_takes_the_dm_and_the_brain_never_runs():
    bot, agent = _bot()
    seen = []

    def take(event):
        seen.append(event)
        event.rest.create_message(event.channel_id, {"content": "Got it."})
        return True

    intake.register(take)
    intake.register(take)                       # registering twice is once
    bot.handle_message(_dm(reference_id="555", attachments=[{"filename": "a.csv"}]))
    assert intake.registered() == 1 and len(seen) == 1 and not agent.asked
    event = seen[0]
    assert (event.user, event.channel_id, event.message_id) == ("77", "dm1", "900")
    assert event.is_dm and event.content == "the upload fails" and event.author_name == "Sam"
    assert event.reference_id == "555" and event.attachments == [{"filename": "a.csv"}]
    assert event.agent is agent
    assert bot.rest.sent == [("dm1", {"content": "Got it."})]


def test_an_intake_that_declines_or_raises_never_eats_the_message():
    bot, agent = _bot()
    calls = []

    def broken(event):
        calls.append("broken")
        raise RuntimeError("boom")

    def not_mine(event):
        calls.append("not_mine")
        return False

    intake.register(broken)
    intake.register(not_mine)
    bot.handle_message(_dm())
    assert calls == ["broken", "not_mine"] and agent.asked


def test_offer_reports_the_error_and_the_first_true_wins():
    errors = []
    order = []
    intake.register(lambda e: order.append("a") or False)
    intake.register(lambda e: (_ for _ in ()).throw(ValueError("x")))
    intake.register(lambda e: order.append("c") or True)
    intake.register(lambda e: order.append("d") or True)
    event = intake.IntakeEvent(user="1", channel_id="2", message_id="3", content="hi",
                               guild_id="9")
    assert intake.offer(event, on_error=errors.append) is True
    assert order == ["a", "c"] and [type(e).__name__ for e in errors] == ["ValueError"]
    assert not event.is_dm
