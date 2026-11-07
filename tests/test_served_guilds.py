"""Only an explicit allowlist of servers may spend the owner's inference.

The deployed bot answers with the owner's model. A server that installs it without
being served must get one setup card and NEVER reach a model call. These tests put a
spy where the agent goes: the positive path proves the spy is reachable, the
negative path proves an unserved server cannot reach it at all.
"""
from __future__ import annotations

import logging

from awdiscord.dispatch import EPHEMERAL, MESSAGE, Dispatcher
from awdiscord.gateway import Bot
from awdiscord.served import (
    HOME_ENV,
    HOME_ENV_ALT,
    SERVED_ENV,
    SETUP_TEXT,
    ServedGuilds,
)
from awdiscord.triage import Candidate

HOME = "111111111111111111"
OTHER = "222222222222222222"
THIRD = "333333333333333333"


class SpyAgent:
    """A model stub. Every call is recorded, so "not called" is assertable."""

    name = "spy"

    def __init__(self, answer="answered"):
        self.calls = []
        self._answer = answer

    def ask(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return self._answer

    def health(self):
        return "ok"


def _ask(question="hi", user="42", guild=HOME):
    return {"type": 2, "id": "1", "token": "t", "guild_id": guild,
            "member": {"user": {"id": user}},
            "data": {"name": "ask", "options": [
                {"name": "question", "value": question}]}}


def _no_env(monkeypatch):
    for name in (HOME_ENV, HOME_ENV_ALT, SERVED_ENV):
        monkeypatch.delenv(name, raising=False)


# -- the allowlist ---------------------------------------------------------------
def test_a_served_guild_reaches_the_model():
    """Positive path: the gate lets a served server through to the agent."""
    spy = SpyAgent()
    d = Dispatcher(spy, served=ServedGuilds(home=HOME))
    assert d.chat("42", HOME, "hello")["content"] == "answered"
    result = d.handle(_ask(guild=HOME))
    assert result.deferred is not None
    assert result.deferred()["content"] == "answered"
    assert len(spy.calls) == 2


def test_an_unserved_guild_never_calls_the_model():
    """Security pattern #5: the negative path asserts the model stub is NOT called."""
    spy = SpyAgent()
    d = Dispatcher(spy, served=ServedGuilds(home=HOME))
    reply = d.chat("42", OTHER, "hello")
    assert SETUP_TEXT in reply["content"]
    assert spy.calls == []


def test_an_unserved_slash_command_is_ephemeral_and_never_defers_work():
    spy = SpyAgent()
    d = Dispatcher(spy, served=ServedGuilds(home=HOME))
    result = d.handle(_ask(guild=OTHER))
    assert result.deferred is None
    assert spy.calls == []
    assert result.response["type"] == MESSAGE
    assert result.response["data"]["flags"] == EPHEMERAL
    assert "aitherium.com/discord" in result.response["data"]["content"]


def test_autocomplete_never_lists_the_owners_pages_to_an_unserved_server():
    spy = SpyAgent()
    d = Dispatcher(spy, served=ServedGuilds(home=HOME))
    result = d.handle({"type": 4, "guild_id": OTHER,
                       "member": {"user": {"id": "1"}},
                       "data": {"name": "artifact"}})
    assert result.response["data"]["choices"] == []
    assert spy.calls == []


def test_a_dm_is_served_only_to_an_owner():
    spy = SpyAgent()
    d = Dispatcher(spy, owners={"7"}, served=ServedGuilds(home=HOME))
    assert d.chat("7", None, "hi")["content"] == "answered"
    assert d.chat("8", None, "hi")["content"] == SETUP_TEXT
    assert len(spy.calls) == 1


def test_a_dm_with_no_owner_configured_is_refused():
    spy = SpyAgent()
    d = Dispatcher(spy, served=ServedGuilds(home=HOME))
    assert d.chat("7", None, "hi")["content"] == SETUP_TEXT
    assert spy.calls == []


# -- fail closed -----------------------------------------------------------------
def test_empty_env_serves_only_the_home_guild(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setenv(HOME_ENV, HOME)
    served = ServedGuilds.from_env()
    assert served.serves_guild(HOME)
    assert not served.serves_guild(OTHER)
    assert served.guilds == frozenset({HOME})


def test_no_home_and_no_allowlist_serves_nothing(monkeypatch):
    _no_env(monkeypatch)
    served = ServedGuilds.from_env()
    assert served.guilds == frozenset()
    assert not served.serves_guild(HOME)


def test_garbage_env_fails_closed_to_the_home_guild(monkeypatch, caplog):
    _no_env(monkeypatch)
    monkeypatch.setenv(HOME_ENV, HOME)
    monkeypatch.setenv(SERVED_ENV, f"{OTHER}, not-a-guild")
    with caplog.at_level(logging.WARNING, logger="awdiscord.served"):
        served = ServedGuilds.from_env()
    # The WHOLE value is dropped: a half-parsed allowlist is not a decision.
    assert served.guilds == frozenset({HOME})
    assert not served.serves_guild(OTHER)
    assert "unparseable" in served.problem
    assert any("unparseable" in rec.message for rec in caplog.records)


def test_comma_and_space_separated_allowlist(monkeypatch):
    _no_env(monkeypatch)
    monkeypatch.setenv(HOME_ENV, HOME)
    monkeypatch.setenv(SERVED_ENV, f"{OTHER}, {THIRD}\n{HOME}")
    served = ServedGuilds.from_env()
    assert served.guilds == frozenset({HOME, OTHER, THIRD})


# -- the setup card is rate limited ------------------------------------------------
def test_the_setup_reply_is_rate_limited_per_server():
    now = [0.0]
    served = ServedGuilds(home=HOME, window=600.0, clock=lambda: now[0])
    assert served.allow_prompt(served.key(OTHER, "")) is True
    assert served.allow_prompt(served.key(OTHER, "")) is False
    assert served.allow_prompt(served.key(THIRD, "")) is True   # a different server
    now[0] = 601.0
    assert served.allow_prompt(served.key(OTHER, "")) is True


def test_a_repeat_mention_is_silent_inside_the_window():
    spy = SpyAgent()
    now = [0.0]
    served = ServedGuilds(home=HOME, window=600.0, clock=lambda: now[0])
    d = Dispatcher(spy, served=served)
    assert SETUP_TEXT in d.chat("42", OTHER, "hi")["content"]
    assert d.chat("42", OTHER, "again") is None       # already on screen
    now[0] = 601.0
    assert SETUP_TEXT in d.chat("42", OTHER, "later")["content"]
    assert spy.calls == []


# -- guild join --------------------------------------------------------------------
class FakeRest:
    def __init__(self):
        self.posted = []

    def create_message(self, channel_id, payload):
        self.posted.append((channel_id, payload))
        return {"id": "m1"}


class FakeHost:
    def __init__(self):
        self.joins = 0
        self.messages = 0

    def dispatch_guild_join(self, guild):
        self.joins += 1
        return {"p": "ok"}

    def dispatch_message(self, msg):
        self.messages += 1
        return True


def _bare_bot(served, host=None):
    bot = Bot.__new__(Bot)
    bot.rest = FakeRest()
    bot.served = served
    bot.host = host
    return bot


def test_join_on_an_unserved_server_posts_the_card_once_and_stays():
    served = ServedGuilds(home=HOME, window=600.0, clock=lambda: 0.0)
    bot = _bare_bot(served, host=FakeHost())
    guild = {"id": OTHER, "name": "Stranger", "system_channel_id": "c2"}
    assert bot.handle_guild_join(guild) == {"setup": "posted"}
    assert bot.rest.posted[0][0] == "c2"
    assert SETUP_TEXT in bot.rest.posted[0][1]["content"]
    assert bot.host.joins == 0            # no plugin hooks for an unserved server
    assert bot.handle_guild_join(guild) == {"setup": "skipped"}   # rate limited
    assert len(bot.rest.posted) == 1


def test_join_on_a_served_server_reaches_the_plugins():
    served = ServedGuilds(home=HOME)
    bot = _bare_bot(served, host=FakeHost())
    assert bot.handle_guild_join({"id": HOME, "system_channel_id": "c1"}) == {"p": "ok"}
    assert bot.host.joins == 1
    assert bot.rest.posted == []


def test_message_hooks_do_not_run_in_an_unserved_server():
    served = ServedGuilds(home=HOME)
    bot = _bare_bot(served, host=FakeHost())
    assert bot.handle_guild_message({"guild_id": OTHER, "user": "1"}) is False
    assert bot.host.messages == 0
    assert bot.handle_guild_message({"guild_id": HOME, "user": "1"}) is True
    assert bot.host.messages == 1


def test_triage_never_sees_an_unserved_servers_messages():
    class ExplodingTriage:
        def submit(self, candidate):
            raise AssertionError("triage reached the model for an unserved server")

    bot = _bare_bot(ServedGuilds(home=HOME))
    bot.triage = ExplodingTriage()
    candidate = Candidate(channel="c2", text="a bug?", guild=OTHER, user="1")
    assert bot.handle_candidate(candidate) == []


def test_voice_never_asks_the_model_on_an_unserved_server():
    from awdiscord.voice import BotThinker

    spy = SpyAgent()
    d = Dispatcher(spy, served=ServedGuilds(home=HOME))
    thinker = BotThinker(spy, dispatcher=d, guild_id=OTHER)
    thinker.set_speaker("42")
    assert "awdiscord setup" in thinker.think("hello")
    assert spy.calls == []
