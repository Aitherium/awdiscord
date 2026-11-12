"""Wake word, notes sessions, background lookups and the stuck-model notice.

WHY THESE CASES
---------------
With two people in a voice channel every utterance used to be a model turn: people
talking to EACH OTHER drew answers, and the rate limiter answered "Slow down" to the
rest (2026-11-11). Only "Aither, ..." starts a turn now (STT spells the name "ether",
"either", "aether"), alone with one person nothing changes. "Aither, take notes"
records every line, tells the text chat, still answers a line that names her
(2026-11-12; see test_voice_calls.py), and "Aither, stop" posts a summary plus the
full transcript. "Aither, look up X" runs off the voice loop and posts to the
chat. A model stuck in a full MicroScheduler lane is SAID, not left as silence.
"""

from __future__ import annotations

import threading
import time
import types

import pytest
from awdiscord.voice import (
    BotThinker,
    DiscordVoice,
    VoiceConfig,
    _chunker,
    _Player,
    _Session,
    parse_command,
    strip_wake_word,
)
from awvoice.loop import CallableSpeaker, CallableTranscriber, VoiceLoop

# ---------------------------------------------------------------------------
# Pure parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("heard,rest", [
    ("Aither, what time is it?", "what time is it?"),
    ("aither what time is it", "what time is it"),
    ("Ether. What time is it?", "What time is it?"),
    ("Either, look up the weather.", "look up the weather."),
    ("Aether, take notes.", "take notes."),
    ("Hey Aither, how are you?", "how are you?"),
    ("Okay, ether, stop.", "stop."),
    ("Aither's going to help.", "going to help."),
    ("Aither?", ""),
])
def test_the_wake_word_and_its_stt_spellings_are_stripped(heard, rest):
    assert strip_wake_word(heard) == rest


@pytest.mark.parametrize("heard", [
    "What time is it?", "Etherium is up today", "I asked either of them",
    "the aither service", "",
])
def test_an_utterance_without_the_wake_word_is_not_a_turn(heard):
    assert strip_wake_word(heard) is None


def test_the_wake_word_list_is_configurable(monkeypatch):
    monkeypatch.setenv("AWDISCORD_VOICE_WAKE_WORDS", "computer, hey bot")
    cfg = VoiceConfig.from_env()
    assert strip_wake_word("Computer, lights", cfg.wake_words) == "lights"
    assert strip_wake_word("hey bot lights", cfg.wake_words) == "lights"
    assert strip_wake_word("Aither, lights", cfg.wake_words) is None


def test_wake_mode_env(monkeypatch):
    monkeypatch.delenv("AWDISCORD_VOICE_WAKE", raising=False)
    assert VoiceConfig.from_env().wake == "auto"
    monkeypatch.setenv("AWDISCORD_VOICE_WAKE", "off")
    assert VoiceConfig.from_env().wake == "off"
    monkeypatch.setenv("AWDISCORD_VOICE_WAKE", "always")
    assert VoiceConfig.from_env().wake == "on"


@pytest.mark.parametrize("said,kind,arg", [
    ("take notes", "notes_start", ""),
    ("Take notes.", "notes_start", ""),
    ("please take notes for this meeting", "notes_start", ""),
    ("record", "notes_start", ""),
    ("start notes", "notes_start", ""),
    ("start recording", "notes_start", ""),
    ("stop", "notes_stop", ""),
    ("stop notes.", "notes_stop", ""),
    ("end notes", "notes_stop", ""),
    ("stop recording", "notes_stop", ""),
    ("look up the Paris weather", "web", "the Paris weather"),
    ("Could you search for RTX 5090 prices please?", "web", "RTX 5090 prices"),
    ("check the knowledge base for onboarding steps", "kb", "onboarding steps"),
    ("search the knowledge base for the Q3 plan", "kb", "the Q3 plan"),
    ("what is a record label", "", ""),
    ("how do I stop the build", "", ""),
    ("what should I write in my notes", "", ""),
])
def test_spoken_commands(said, kind, arg):
    assert parse_command(said) == (kind, arg)


def test_the_chunker_survives_an_old_awvoice(monkeypatch):
    import awdiscord.voice as voice_mod

    class OldChunker:                        # awvoice 0.5.x: max_chars only
        def __init__(self, max_chars=200):
            self.max_chars = max_chars

    monkeypatch.setattr(voice_mod, "SentenceChunker", OldChunker)
    assert _chunker(120, 24).max_chars == 120


# ---------------------------------------------------------------------------
# The session, with a real VoiceLoop and fakes for STT/TTS/Discord
# ---------------------------------------------------------------------------


class _Rest:
    def __init__(self):
        self.messages = []
        self.files = []

    def create_message(self, channel_id, payload):
        self.messages.append((channel_id, payload["content"]))
        return {"id": "m"}

    def upload(self, channel_id, filename, data, content_type="", content=""):
        self.files.append((channel_id, filename, data.decode("utf-8"), content))
        return {"id": "f"}


class _Bot:
    agent = None
    dispatcher = None

    def __init__(self):
        self.rest = _Rest()


class _Thinker:
    """BotThinker's surface: think, think_stream, set_speaker, side_copy."""

    def __init__(self, answer="Sure.", hold=None):
        self.answer = answer
        self.hold = hold
        self.asked = []
        self.speaker = ("", "")
        self.timing = {}
        self.fallback_reason = ""

    def set_speaker(self, user_id, user_name=""):
        self.speaker = (user_id, user_name)

    def side_copy(self, user_id, user_name="", *, spoken=False, clip_chars=1500):
        other = _Thinker(self.answer)
        other.asked = self.asked
        other.speaker = (user_id, user_name)
        return other

    def think(self, text, *, history=()):
        self.asked.append(("think", self.speaker[0], text))
        return self.answer

    def think_stream(self, text, *, history=(), cancel=None):
        self.asked.append(("stream", self.speaker[0], text))
        if self.hold is not None:
            self.hold.wait(5)
        yield self.answer


def _member(name, bot=False):
    return types.SimpleNamespace(name=name, bot=bot)


def _session(heard, thinker=None, humans=2, **config):
    """A session whose STT returns the next string in ``heard`` for each utterance."""
    words = list(heard)
    spoken = []

    def transcribe(pcm):
        return words.pop(0) if words else ""

    def speak(text):
        spoken.append(text)
        return (text.encode(), "wav")

    thinker = thinker or _Thinker()
    loop = VoiceLoop(CallableTranscriber(transcribe), thinker, CallableSpeaker(speak))
    bot = _Bot()
    voice = DiscordVoice(bot, VoiceConfig(enabled=True, **config))
    logs = []
    voice.log = lambda **f: logs.append(f)
    voice._decode = lambda audio, fmt: b"pcm:" + audio
    session = _Session(voice, "10", "20", loop)
    session.vc = types.SimpleNamespace(channel=types.SimpleNamespace(
        members=[_member("p%d" % i) for i in range(humans)] + [_member("aither", True)]))
    session.player.close()
    session.player = _Player(lambda pcm, after: after(None), lambda: None)
    return session, thinker, bot.rest, spoken, logs


def _wait(cond, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


_PCM = b"\x00\x00" * 16000     # one second: not a phantom


def _say(session, n=1, user="2", name="member"):
    for _ in range(n):
        session.submit(user, name, _PCM)


def _drain(session):
    assert _wait(lambda: session.queue.empty())
    time.sleep(0.1)
    for t in list(session.side_threads):
        t.join(3)


def test_two_humans_only_the_wake_word_starts_a_turn():
    session, thinker, _, _, logs = _session(
        ["did you see the game", "Ether, what is the score?"])
    _say(session, 2)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    session.stop()
    asked = [a for a in thinker.asked if a[0] == "stream"]
    assert asked == [("stream", "2", "what is the score?")]   # wake word stripped
    assert any(f.get("event") == "voice_no_wake" for f in logs)


def test_one_human_keeps_answering_everything():
    session, thinker, _, _, logs = _session(["what is the score?"], humans=1)
    _say(session)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    session.stop()
    assert thinker.asked[0][2] == "what is the score?"


def test_wake_off_answers_everyone_even_in_a_crowd():
    session, thinker, _, _, logs = _session(["what is the score?"], humans=3, wake="off")
    _say(session)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    session.stop()
    assert thinker.asked


def test_a_bare_wake_word_opens_one_follow_up():
    session, thinker, _, spoken, logs = _session(["Aither?", "what is the score?"])
    _say(session, 2)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    session.stop()
    assert spoken[0] == "Yes?"
    assert thinker.asked[0][2] == "what is the score?"


def test_the_turn_log_carries_where_the_time_went():
    session, _, _, _, logs = _session(["Aither, hi"])
    _say(session)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    session.stop()
    turn = [f for f in logs if f.get("event") == "voice_turn"][0]
    for key in ("queue_ms", "stt_ms", "think_ms", "tts_ms", "first_audio_ms"):
        assert key in turn, key


def test_notes_start_record_stop_and_post_summary_and_transcript():
    thinker = _Thinker(answer="Decisions: ship Friday. Action items: Ann - deploy. "
                              "Open questions: None.")
    session, thinker, rest, spoken, logs = _session([
        "Aither, take notes",
        "We ship on Friday.",
        "Ann will deploy it.",
        "Aither, what's the weather?",        # in notes mode: recorded AND answered
        "Aither, stop",
    ], thinker=thinker)
    _say(session, 2, user="1", name="ann")
    _say(session, 3, user="2", name="bob")
    _drain(session)
    session.stop()
    # The room is told before anything is recorded.
    assert rest.messages and "Aither is taking notes" in rest.messages[0][1]
    assert rest.messages[0][0] == "20"                # the voice channel's own chat
    assert "Taking notes." in spoken
    # Only the line that named her was answered...
    answered = [a[2] for a in thinker.asked if a[0] == "stream"]
    assert len(answered) == 1 and answered[0].startswith("what's the weather?")
    # ...the summary went through the same agent, with the whole transcript.
    summary_calls = [a for a in thinker.asked if a[0] == "think"]
    assert len(summary_calls) == 1
    assert "actions" in summary_calls[0][2] and "Ann will deploy it." in summary_calls[0][2]
    channel, filename, data, content = rest.files[0]
    assert channel == "20" and filename.startswith("call-notes-")
    assert "ship Friday" in content
    for line in ("ann: We ship on Friday.", "bob: Ann will deploy it.",
                 "bob: Aither, what's the weather?"):
        assert line in data
    assert "take notes" not in data.split("TRANSCRIPT")[1].split("\n", 3)[-1]
    assert session.notes is None


def test_nothing_is_recorded_without_the_spoken_start():
    session, _, rest, _, _ = _session(["We ship on Friday.", "Aither, stop"])
    _say(session, 2)
    _drain(session)
    session.stop()
    assert rest.files == [] and rest.messages == []


def test_leaving_mid_notes_still_posts_them():
    session, _, rest, _, _ = _session(["Aither, record", "One thing."])
    _say(session, 2)
    _drain(session)
    worker = session.stop_notes("(left)", spoken=False)
    worker.join(3)
    session.stop()
    assert rest.files and "One thing." in rest.files[0][2]


def test_a_lookup_runs_in_the_background_and_posts_to_the_chat():
    release = threading.Event()

    class Slow(_Thinker):
        def side_copy(self, user_id, user_name="", *, spoken=False, clip_chars=1500):
            other = Slow(self.answer)
            other.asked = self.asked
            other.speaker = (user_id, user_name)
            return other

        def think(self, text, *, history=()):
            assert release.wait(3)
            return super().think(text, history=history)

    thinker = Slow(answer="Paris: 14C and cloudy (example.com).")
    session, thinker, rest, spoken, logs = _session(
        ["Aither, look up the weather in Paris", "Aither, and how are you?"],
        thinker=thinker)
    _say(session, 2)
    # The call goes on while the lookup is still running.
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    assert rest.messages == []
    release.set()
    _drain(session)
    session.stop()
    assert spoken[0].startswith("Looking that up")
    lookup = [a for a in thinker.asked if a[0] == "think"][0]
    assert lookup[1] == "2" and "web_search" in lookup[2] and "Paris" in lookup[2]
    assert rest.messages[0][0] == "20" and "14C" in rest.messages[0][1]
    assert "I posted it in the chat." in spoken


def test_a_knowledge_base_lookup_names_company_brain_search():
    session, thinker, rest, _, _ = _session(
        ["Aither, check the knowledge base for the onboarding checklist"])
    _say(session)
    _drain(session)
    session.stop()
    lookup = [a for a in thinker.asked if a[0] == "think"][0]
    assert "company_brain_search" in lookup[2] and "onboarding checklist" in lookup[2]
    assert rest.messages and "Knowledge base lookup" in rest.messages[0][1]


def test_a_stuck_model_is_said_out_loud():
    hold = threading.Event()
    session, thinker, _, spoken, logs = _session(
        ["Aither, what is up?"], thinker=_Thinker(answer="All good.", hold=hold),
        stuck_s=0.2, stuck_text="I'm stuck waiting on the model.")
    _say(session)
    assert _wait(lambda: "I'm stuck waiting on the model." in spoken)
    hold.set()
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    session.stop()
    turn = [f for f in logs if f.get("event") == "voice_turn"][0]
    assert "stuck_notice_ms" in turn
    assert spoken[-1] == "All good."


def test_a_fast_model_never_hears_the_stuck_notice():
    session, _, _, spoken, logs = _session(["Aither, hi"], stuck_s=2.0)
    _say(session)
    assert _wait(lambda: any(f.get("event") == "voice_turn" for f in logs))
    time.sleep(0.1)
    session.stop()
    assert not any("stuck" in s for s in spoken)


def test_a_failed_turn_says_so_instead_of_silence():
    class Broken(_Thinker):
        def think_stream(self, text, *, history=(), cancel=None):
            raise RuntimeError("request expired after 120 s")
            yield  # pragma: no cover

    session, _, _, spoken, logs = _session(["Aither, hi"], thinker=Broken())
    _say(session)
    assert _wait(lambda: any(f.get("event") == "voice_turn_error" for f in logs))
    assert _wait(lambda: any("didn't answer" in s for s in spoken))
    session.stop()


def test_a_side_copy_keeps_its_speaker_when_the_worker_rebinds():
    t = BotThinker(object(), dispatcher=None)
    t.set_speaker("1", "ann")
    side = t.side_copy("1", "ann")
    t.set_speaker("2", "bob")
    assert side.user_id == "1" and side.spoken_style is False
    assert side._spoken("x") == "x"


def test_a_side_copy_drops_an_instance_wrapper_bound_to_the_original():
    t = BotThinker(object(), dispatcher=None)
    t.think = lambda text, history=(): "ran on the original"   # a stage timer, say
    side = t.side_copy("1", "ann")
    assert "think" not in side.__dict__
    assert side.think.__self__ is side


def _loud(ms):
    import struct
    return struct.pack("<h", 4000) * (16 * ms)


def test_in_a_crowd_only_the_asker_can_barge_in():
    session, _, _, _, _ = _session([], humans=3)
    session.player.close()
    session.player = types.SimpleNamespace(active=True, cancel=lambda: 0,
                                           generation=lambda: 0)
    session.asker = "1"
    assert not any(session.heard("2", _loud(20)) for _ in range(20))   # crosstalk
    assert any(session.heard("1", _loud(20)) for _ in range(20))       # the asker


def test_alone_anyone_can_barge_in():
    session, _, _, _, _ = _session([], humans=1)
    session.player.close()
    session.player = types.SimpleNamespace(active=True, cancel=lambda: 0,
                                           generation=lambda: 0)
    session.asker = "1"
    assert any(session.heard("2", _loud(20)) for _ in range(20))
