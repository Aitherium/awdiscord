"""A long answer in a public channel posts a short summary; the full text goes by DM.

Reproduces OccupySaaS #general, 2026-11-08: the bot posted two messages of memory
research into a public channel and a member asked for long content to go to DM.
"""
from __future__ import annotations

from awdiscord import gateway
from awdiscord.dispatch import Dispatcher, first_sentences, is_long, wants_full
from awdiscord.rest import RestError

TOKEN = "a" * 24 + "." + "G" * 6 + "." + "x" * 38  # fake, bot-token shaped

LONG = ("Memory is stored in three tiers. The hot tier lives in process. "
        "The warm tier is a vector index. " + "Detail sentence about recall. " * 80)
SHORT = "Memory is stored in three tiers."


class _Agent:
    """Answers the question with ``answer``; a summary request gets ``summary``."""
    name = "bonsai"
    personas: dict = {}

    def __init__(self, answer, summary=None):
        self.answer, self.summary, self.prompts = answer, summary, []

    def ask(self, prompt, **kw):
        self.prompts.append(prompt)
        if prompt.startswith("Summarise the answer below"):
            if isinstance(self.summary, Exception):
                raise self.summary
            return self.summary if self.summary is not None else self.answer
        return self.answer


class _Rest:
    def __init__(self, dm_closed=False):
        self.dm_closed = dm_closed
        self.calls: list = []

    def request(self, method, path, body=None, auth=True):
        self.calls.append((method, path, body))
        if path.endswith("/threads"):
            return {"id": "t1"}
        return {}

    def dm_channel(self, user_id):
        if self.dm_closed:
            raise RestError(403, "Cannot send messages to this user")
        return {"id": "dm-" + user_id}

    def create_message(self, channel, payload):
        self.calls.append(("POST", channel, payload))
        return {"id": "posted", "channel_id": channel}

    def posts(self, channel):
        return [c[2]["content"] for c in self.calls if c[0] == "POST" and c[1] == channel]


def _bot(agent, rest):
    bot = gateway.Bot(TOKEN, agent, app_id="app")
    bot.rest = rest
    return bot


def _mention(text, guild="g1"):
    return {"user": "u1", "guild_id": guild, "channel_id": "c1", "id": "m1",
            "content": text, "bot_id": "1"}


# -- dispatcher policy ------------------------------------------------------------

def test_short_public_stays_inline():
    out = Dispatcher(_Agent(SHORT)).chat("u1", "g1", "where is memory?")
    assert out["content"] == SHORT and "long_form" not in out


def test_long_public_is_summarised_with_the_full_text_attached():
    agent = _Agent(LONG, summary="Memory has a hot, warm and cold tier.")
    out = Dispatcher(agent).chat("u1", "g1", "where is memory?")
    assert out["content"] == "Memory has a hot, warm and cold tier."
    assert out["long_form"]["how"] == "agent" and out["long_form"]["user"] == "u1"
    assert "".join(out["long_form"]["parts"]).count("Detail sentence") == 80
    assert len(agent.prompts) == 2                 # the answer, then the same agent summarises


def test_summary_falls_back_to_first_sentences_when_the_agent_fails():
    out = Dispatcher(_Agent(LONG, summary=RuntimeError("down"))).chat("u1", "g1", "q")
    assert out["long_form"]["how"] == "first_sentences"
    assert out["content"] == ("Memory is stored in three tiers. The hot tier lives in "
                              "process. The warm tier is a vector index.")


def test_dm_conversation_stays_full():
    out = Dispatcher(_Agent(LONG)).chat("u1", None, "where is memory?")
    assert "long_form" not in out and out["content"].startswith("Memory is stored")
    assert len(out["content"]) > 700


def test_explicit_full_stays_full():
    for ask in ("give me the full answer", "details please", "in more DETAIL"):
        out = Dispatcher(_Agent(LONG)).chat("u1", "g1", ask)
        assert "long_form" not in out, ask
        assert len(out["content"]) > 700


def test_limit_is_configurable_and_zero_turns_it_off(monkeypatch):
    medium = "A sentence. " * 30                    # 360 chars
    assert "long_form" not in Dispatcher(_Agent(medium)).chat("u1", "g1", "q")
    assert "long_form" in Dispatcher(_Agent(medium), public_reply_chars=200).chat("u1", "g1", "q")
    monkeypatch.setenv("AWDISCORD_PUBLIC_REPLY_CHARS", "200")
    assert "long_form" in Dispatcher(_Agent(medium)).chat("u1", "g1", "q")
    assert "long_form" not in Dispatcher(_Agent(LONG), public_reply_chars=0).chat("u1", "g1", "q")


def test_helpers():
    assert wants_full("FULL please") and not wants_full("fully agree")
    assert is_long("x" * 701, 700) and not is_long("x" * 700, 700)
    assert first_sentences("x" * 900, 700).endswith("…")
    assert first_sentences("```\ncode\n```\nPlain words. More.", 700) == "Plain words. More."


# -- gateway delivery -------------------------------------------------------------

def test_long_public_posts_summary_in_channel_and_dms_the_full_answer():
    rest = _Rest()
    _bot(_Agent(LONG, summary="Memory has three tiers."), rest).handle_message(
        _mention("<@1> where is memory?"))
    channel = rest.posts("c1")
    assert len(channel) == 1 and channel[0].startswith("Memory has three tiers.")
    assert "sent to you by DM" in channel[0]
    dms = rest.posts("dm-u1")
    assert "<#c1>" in dms[0]
    assert "".join(dms[1:]).count("Detail sentence") == 80
    assert not [c for c in rest.calls if str(c[1]).endswith("/threads")]


def test_dm_closed_falls_back_to_a_thread_on_the_summary():
    rest = _Rest(dm_closed=True)
    _bot(_Agent(LONG, summary="Memory has three tiers."), rest).handle_message(
        _mention("<@1> where is memory?"))
    channel = rest.posts("c1")
    assert len(channel) == 1 and "thread" in channel[0]
    assert ("POST", "/channels/c1/messages/posted/threads",
            {"name": "Full answer", "auto_archive_duration": 1440}) in rest.calls
    assert "".join(rest.posts("t1")).count("Detail sentence") == 80


def test_thread_refused_posts_the_full_answer_inline():
    rest = _Rest(dm_closed=True)

    def no_threads(method, path, body=None, auth=True):
        rest.calls.append((method, path, body))
        if path.endswith("/threads"):
            raise RestError(400, "cannot thread here")
        return {}
    rest.request = no_threads
    _bot(_Agent(LONG, summary="Memory has three tiers."), rest).handle_message(
        _mention("<@1> where is memory?"))
    assert "".join(rest.posts("c1")[1:]).count("Detail sentence") == 80


def test_dm_conversation_is_delivered_in_full_with_no_dm_hop():
    rest = _Rest()
    _bot(_Agent(LONG), rest).handle_message(_mention("where is memory?", guild=None))
    assert "".join(rest.posts("c1")).count("Detail sentence") == 80
    assert rest.posts("dm-u1") == []
