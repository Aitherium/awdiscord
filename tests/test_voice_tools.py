"""A voice turn is the same turn a text message is: same tools, same caller identity.

WHY THESE CASES
---------------
A spoken question used to reach ``agent.ask`` with persona/history only, so it could not
search the web or read a file the same user could ask for in a text message, and an
owner-only plugin tool was simply never offered. The speaker's Discord id (from the voice
event, never the model) now selects exactly the plugin tools a text turn gets. The
refusal arm is the point: a non-owner must still never see an operator-only tool.
"""

from __future__ import annotations

import time

from awdiscord.dispatch import Dispatcher
from awdiscord.plugins import Plugin, PluginHost, Tool
from awdiscord.store import GuildStore
from awdiscord.voice import BotThinker, DiscordVoice, VoiceConfig

OP, MEMBER, GUILD, CHANNEL = "1", "2", "10", "20"


class _Agent:
    runs_bot_tools = True
    name = "bot"

    def __init__(self) -> None:
        self.calls = []

    def ask(self, prompt, persona=None, user=None, history=None, tools=None,
            context=None, **kw):
        self.calls.append({"prompt": prompt, "persona": persona, "user": user,
                           "history": history, "tools": list(tools or []),
                           "context": context})
        return "ok"


class _Bot:
    def __init__(self, dispatcher) -> None:
        self.dispatcher = dispatcher
        self.agent = dispatcher.agent
        self.lines = []

    def _log_voice(self, **fields):
        self.lines.append(fields)


def _dispatcher(tmp_path):
    seen = []

    def web(caller=None):
        seen.append(caller)
        return "search result"

    def admin(caller=None):
        seen.append(caller)
        return "host status"

    plugin = Plugin("p", "0", "d", tools=[
        Tool("web", "search the web", web, wants_caller=True),
        Tool("admin", "operator only", admin, wants_caller=True,
             operators_only=True)])
    host = PluginHost(GuildStore(tmp_path / "g.db"), operators={OP}, plugins=[plugin])
    host.set_enabled(GUILD, "p", True)
    agent = _Agent()
    return Dispatcher(agent, host=host), agent, seen


def _thinker(dispatcher, agent, speaker):
    thinker = BotThinker(agent, dispatcher=dispatcher, guild_id=GUILD,
                         channel_id=CHANNEL)
    thinker.set_speaker(speaker, "speaker")
    return thinker


def test_a_voice_turn_offers_the_same_tools_a_text_turn_does(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    dispatcher.chat(MEMBER, GUILD, "hello", channel=CHANNEL)
    text_tools = {t.name for t in agent.calls[-1]["tools"]}
    _thinker(dispatcher, agent, MEMBER).think("hello")
    voice_tools = {t.name for t in agent.calls[-1]["tools"]}
    assert voice_tools == text_tools == {"web"}


def test_a_voice_turn_carries_the_speaker_as_the_caller(tmp_path):
    dispatcher, agent, seen = _dispatcher(tmp_path)
    _thinker(dispatcher, agent, MEMBER).think("search the web")
    web = next(t for t in agent.calls[-1]["tools"] if t.name == "web")
    assert web.run({}) == "search result"
    assert seen[-1]["user"] == MEMBER
    assert seen[-1]["operator"] is False


def test_a_non_owner_voice_turn_never_gets_owner_only_tools(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    _thinker(dispatcher, agent, MEMBER).think("what is down?")
    assert {t.name for t in agent.calls[-1]["tools"]} == {"web"}


def test_an_owner_voice_turn_gets_the_owner_only_tools(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    _thinker(dispatcher, agent, OP).think("what is down?")
    assert {t.name for t in agent.calls[-1]["tools"]} == {"web", "admin"}


def test_the_voice_turn_still_carries_persona_and_history(tmp_path):
    dispatcher, agent, _ = _dispatcher(tmp_path)
    thinker = BotThinker(agent, "vera", dispatcher=dispatcher, guild_id=GUILD,
                         channel_id=CHANNEL)
    thinker.set_speaker(MEMBER, "speaker")
    past = [{"role": "user", "content": "earlier"}]
    thinker.think("question", history=past)
    call = agent.calls[-1]
    assert call["persona"] == "vera"
    assert call["history"] == past
    assert call["user"] == MEMBER


def test_a_voice_turn_is_rate_limited_like_a_text_turn(tmp_path):
    """A voice turn now carries tools, so it must cost a token like every text turn.

    The same limiter instance and the same (speaker, guild) key: an open channel cannot
    become an unmetered path to a tool-capable agent while /ask refuses the same user.
    """
    from awdiscord.ratelimit import RateLimiter

    dispatcher, agent, _ = _dispatcher(tmp_path)
    dispatcher.limiter = RateLimiter(per_user=(1, 60.0), per_guild=(50, 60.0),
                                     global_=(50, 60.0), clock=lambda: 0.0)
    thinker = _thinker(dispatcher, agent, MEMBER)

    assert thinker.think("first") == "ok"
    calls = len(agent.calls)
    reply = thinker.think("second")
    assert "Slow down" in reply
    assert len(agent.calls) == calls, "a throttled voice turn reached the model"
    # ...and the refusal comes from the SAME bucket a text message would use.
    text = dispatcher.chat(MEMBER, GUILD, "third", channel=CHANNEL)
    assert "Slow down" in text["content"]


def test_a_spoken_answer_is_shareable_the_way_a_text_answer_is(tmp_path):
    """``/share`` reads the dispatcher's last-answer cache, which a voice turn fills too."""
    dispatcher, agent, _ = _dispatcher(tmp_path)
    _thinker(dispatcher, agent, MEMBER).think("what is down?")

    share = {"type": 2, "user": {"id": MEMBER}, "guild_id": GUILD,
             "data": {"name": "share"}}
    content = dispatcher.handle(share).response["data"]["content"]
    assert "Nothing to share" not in content
    assert "what is down?" in content


def test_a_voice_turn_without_a_dispatcher_keeps_the_plain_ask():
    seen = {}

    class Plain:
        def ask(self, prompt, persona=None, history=None):
            seen.update(prompt=prompt, persona=persona, history=history)
            return "ok"

    thinker = BotThinker(Plain(), "vera")
    assert thinker.think("q", history=[{"role": "user", "content": "a"}]) == "ok"
    assert seen["persona"] == "vera"


class _Thinker:
    def __init__(self) -> None:
        self.bound = []

    def set_speaker(self, user_id, user_name=""):
        self.bound.append((user_id, user_name))


class _Loop:
    history = []
    user = ""

    def __init__(self) -> None:
        self.thinker = _Thinker()

    def handle_utterance(self, pcm):
        return None


def test_the_worker_binds_the_speaker_id_from_the_voice_event(tmp_path):
    dispatcher, _, _ = _dispatcher(tmp_path)
    voice = DiscordVoice(_Bot(dispatcher), VoiceConfig(enabled=True))
    voice.log = lambda **fields: None
    voice.play_turn = lambda guild_id, turn: None
    loop = _Loop()
    from awdiscord.voice import _Session

    session = _Session(voice, GUILD, CHANNEL, loop)
    session.submit(MEMBER, "tester", b"\x00\x00" * 320)
    for _ in range(100):
        if loop.thinker.bound:
            break
        time.sleep(0.02)
    session.stop()
    assert loop.thinker.bound == [(MEMBER, "tester")]
