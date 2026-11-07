"""Streaming voice replies: SSE sentences, the tool-call fallback, barge-in.

WHY THESE CASES
---------------
A spoken reply used to wait for the whole model answer (6.4 s) and then the whole TTS
clip. The stream path reads MicroScheduler's SSE, so the first sentence plays while the
rest is written. It must stay the SAME turn: a model that wants a tool falls back to the
non-streaming tool loop with the same arguments, owner gating is unchanged, and the
dispatcher's last-answer cache gets the full reply. Barge-in: a listener talking over
the answer stops playback and drops what was still queued.
"""

from __future__ import annotations

import io
import json
import struct
import threading
import time

import pytest
from awdiscord import agent as agent_mod
from awdiscord.agent import OpenAICompatAgent, StreamFallbackError
from awdiscord.voice import BotThinker, DiscordVoice, VoiceConfig, _Player, _Session

# ---------------------------------------------------------------------------
# SSE from MicroScheduler
# ---------------------------------------------------------------------------

class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _sse(events) -> bytes:
    lines = [f"data: {json.dumps(e)}\n\n" for e in events] + ["data: [DONE]\n\n"]
    return "".join(lines).encode()


def _delta(text=None, tool_calls=None, finish=None):
    delta = {}
    if text is not None:
        delta["content"] = text
    if tool_calls is not None:
        delta["tool_calls"] = tool_calls
    return {"model": "m", "choices": [{"delta": delta, "finish_reason": finish}]}


def _serve(monkeypatch, body: bytes, seen: list):
    def urlopen(req, timeout=None, context=None):
        seen.append(json.loads(req.data.decode()))
        return _Resp(body)
    monkeypatch.setattr(agent_mod.urllib.request, "urlopen", urlopen)


class _T:
    name = "web"

    def schema(self):
        return {"type": "function", "function": {"name": "web", "parameters": {}}}

    def run(self, args):
        return "found"


def test_the_sse_stream_yields_text_and_asks_for_stream_true(monkeypatch):
    seen = []
    _serve(monkeypatch, _sse([_delta("Hello the"), _delta("re. How are"),
                              _delta(" you?")]), seen)
    agent = OpenAICompatAgent("https://ms/v1", model="m", priority="user")
    pieces = list(agent.ask_stream("hi", user="7", tools=[_T()]))
    assert "".join(pieces) == "Hello there. How are you?"
    assert seen[0]["stream"] is True
    assert seen[0]["tools"]                      # the model may still pick a tool
    assert seen[0]["metadata"]["priority"] == "user"


def test_a_tool_call_delta_is_a_fallback_before_anything_is_spoken(monkeypatch):
    seen = []
    call = [{"index": 0, "id": "c1", "function": {"name": "web", "arguments": ""}}]
    _serve(monkeypatch, _sse([_delta(tool_calls=call)]), seen)
    agent = OpenAICompatAgent("https://ms/v1", model="m")
    with pytest.raises(StreamFallbackError):
        list(agent.ask_stream("hi", tools=[_T()]))


def test_tool_markup_in_content_is_never_spoken(monkeypatch):
    seen = []
    _serve(monkeypatch, _sse([_delta('<tool_call>{"name": "web"}'),
                              _delta("</tool_call>")]), seen)
    agent = OpenAICompatAgent("https://ms/v1", model="m")
    with pytest.raises(StreamFallbackError):
        list(agent.ask_stream("hi", tools=[_T()]))


# ---------------------------------------------------------------------------
# BotThinker.think_stream: same turn, delivered incrementally
# ---------------------------------------------------------------------------

class _StreamAgent:
    runs_bot_tools = True
    name = "bot"

    def __init__(self, pieces=None, fallback=False):
        self.pieces = pieces or []
        self.fallback = fallback
        self.asked = []
        self.streamed = []

    def ask(self, prompt, persona=None, user=None, history=None, tools=None,
            context=None, **kw):
        self.asked.append({"prompt": prompt, "user": user,
                           "tools": [t.name for t in tools or []]})
        return "Used a tool. Here is the answer."

    def ask_stream(self, prompt, persona=None, user=None, history=None, tools=None,
                   context=None, cancel=None, **kw):
        self.streamed.append({"prompt": prompt, "user": user,
                              "tools": [t.name for t in tools or []]})
        if self.fallback:
            raise StreamFallbackError("the model asked for a tool")
        yield from self.pieces


def _dispatcher(tmp_path, agent):
    from awdiscord.dispatch import Dispatcher
    from awdiscord.plugins import Plugin, PluginHost, Tool
    from awdiscord.store import GuildStore

    plugin = Plugin("p", "0", "d", tools=[Tool("web", "search", lambda: "r")])
    host = PluginHost(GuildStore(tmp_path / "g.db"), operators={"1"}, plugins=[plugin])
    host.set_enabled("10", "p", True)
    return Dispatcher(agent, host=host)


def _thinker(tmp_path, agent):
    d = _dispatcher(tmp_path, agent)
    t = BotThinker(agent, dispatcher=d, guild_id="10", channel_id="20")
    t.set_speaker("2", "member")
    return t, d


def test_think_stream_yields_sentences_in_order(tmp_path):
    agent = _StreamAgent(["Hello the", "re. How are", " you? Fine."])
    thinker, d = _thinker(tmp_path, agent)
    assert list(thinker.think_stream("q")) == ["Hello there.", "How are you?", "Fine."]
    assert agent.asked == []                         # no second, non-streaming call
    assert agent.streamed[0]["user"] == "2" and agent.streamed[0]["tools"] == ["web"]


def test_a_tool_turn_falls_back_to_the_non_streaming_ask(tmp_path):
    agent = _StreamAgent(fallback=True)
    thinker, d = _thinker(tmp_path, agent)
    out = list(thinker.think_stream("q"))
    assert out == ["Used a tool.", "Here is the answer."]
    assert len(agent.asked) == 1
    assert agent.asked[0]["tools"] == ["web"] and agent.asked[0]["user"] == "2"


def test_the_share_cache_gets_the_full_streamed_reply(tmp_path):
    agent = _StreamAgent(["One. ", "Two."])
    thinker, d = _thinker(tmp_path, agent)
    seen = []
    d._remember_answer = lambda user, q, a: seen.append((user, q, a))
    list(thinker.think_stream("q"))
    assert seen == [("2", "q", "One. Two.")]


def test_voice_stream_off_uses_the_plain_ask(tmp_path):
    agent = _StreamAgent(["never"])
    thinker, d = _thinker(tmp_path, agent)
    thinker.stream = False
    assert list(thinker.think_stream("q")) == ["Used a tool.", "Here is the answer."]
    assert agent.streamed == []


# ---------------------------------------------------------------------------
# Env knobs
# ---------------------------------------------------------------------------

def test_stream_and_barge_in_default_on_and_silence_is_400ms(monkeypatch):
    for k in ("AWDISCORD_VOICE_STREAM", "AWDISCORD_VOICE_BARGE_IN",
              "AWDISCORD_VOICE_SILENCE_MS"):
        monkeypatch.delenv(k, raising=False)
    cfg = VoiceConfig.from_env()
    assert cfg.stream is True and cfg.barge_in is True and cfg.silence_ms == 400
    monkeypatch.setenv("AWDISCORD_VOICE_STREAM", "0")
    monkeypatch.setenv("AWDISCORD_VOICE_BARGE_IN", "off")
    monkeypatch.setenv("AWDISCORD_VOICE_SILENCE_MS", "650")
    cfg = VoiceConfig.from_env()
    assert cfg.stream is False and cfg.barge_in is False and cfg.silence_ms == 650


# ---------------------------------------------------------------------------
# The session: per-sentence playback queue and barge-in
# ---------------------------------------------------------------------------

class _FakeWire:
    """vc.play / vc.stop: a clip plays until stop() or until finish() is called."""

    def __init__(self):
        self.played = []
        self.stops = 0
        self._after = None
        self.lock = threading.Lock()

    def start(self, pcm, after):
        with self.lock:
            self.played.append(pcm)
            self._after = after

    def stop(self):
        with self.lock:
            self.stops += 1
            after, self._after = self._after, None
        if after:
            after(None)

    def finish(self):
        with self.lock:
            after, self._after = self._after, None
        if after:
            after(None)


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def _loud(ms):
    return struct.pack("<h", 4000) * (16 * ms)


class _Bot:
    agent = None
    dispatcher = None


def test_barge_in_stops_playback_and_drops_the_queued_sentences():
    voice = DiscordVoice(_Bot(), VoiceConfig(enabled=True))
    logs = []
    voice.log = lambda **f: logs.append(f)

    class _Loop:
        history = []
        user = ""
        thinker = None

        def handle_utterance(self, pcm):
            return None

    session = _Session(voice, "10", "20", _Loop())
    wire = _FakeWire()
    session.player.close()
    session.player = _Player(wire.start, wire.stop)
    gen = session.player.generation()
    for clip in (b"s1", b"s2", b"s3"):
        session.player.enqueue(clip, gen)
    assert _wait(lambda: wire.played == [b"s1"])      # s1 on the wire, s2+s3 queued
    assert session.player.active
    cancel = threading.Event()
    session._cancel = cancel                         # the in-flight stream
    fired = [session.heard("2", _loud(20)) for _ in range(15)]
    assert any(fired)
    assert cancel.is_set()                           # the model stream is cancelled
    assert wire.stops >= 1                           # vc.stop()
    assert _wait(lambda: not session.player.active)
    time.sleep(0.15)
    assert wire.played == [b"s1"]                    # s2 and s3 never played
    assert any(f.get("event") == "voice_barge_in" for f in logs)
    session.stop()


def test_the_worker_plays_sentence_one_while_two_is_synthesizing(monkeypatch):
    from awvoice.loop import CallableSpeaker, CallableTranscriber, VoiceLoop

    release = threading.Event()
    spoken = []

    def speak(text):
        spoken.append(text)
        if text == "Two.":
            assert release.wait(3)                   # held until s1 is on the wire
        return (text.encode(), "wav")

    class _Thinker:
        def think_stream(self, text, *, history=(), cancel=None):
            yield from ["One.", "Two."]

    loop = VoiceLoop(CallableTranscriber(lambda pcm: "q"), _Thinker(),
                     CallableSpeaker(speak))
    voice = DiscordVoice(_Bot(), VoiceConfig(enabled=True))
    logs = []
    voice.log = lambda **f: logs.append(f)
    voice._decode = lambda audio, fmt: b"pcm:" + audio
    session = _Session(voice, "10", "20", loop)
    wire = _FakeWire()
    session.player.close()

    def start(pcm, after):
        wire.start(pcm, after)
        release.set()
        threading.Timer(0.05, wire.finish).start()

    session.player = _Player(start, wire.stop)
    session.submit("2", "member", b"\x00\x00" * 320)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    assert _wait(lambda: wire.played == [b"pcm:One.", b"pcm:Two."])
    turn = [f for f in logs if f.get("event") == "voice_turn"][0]
    assert turn["said"] == "One. Two."
    assert turn["barged_in"] is False
    assert "first_audio_ms" in turn and "stt_ms" in turn and "think_ms" in turn
    assert loop.history[-1] == {"role": "assistant", "content": "One. Two."}
    session.stop()
