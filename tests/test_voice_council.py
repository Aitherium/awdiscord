"""Multi-name turns and a quick council across the persona bots, with no server.

WHY THESE CASES
---------------
Eight persona bots share one voice channel, each its own process. "Atlas, Aither,
yadda" reached only Atlas (he heard "Aither, yadda" as his question) and Aither
stayed silent because another name led. The parse, the order, the caps and the stop
words are pure functions; the turn-taking is :class:`SlotWaiter`, driven here with a
fake clock and a fake floor so the fallback path is proven without waiting.
"""

from __future__ import annotations

import time
import types

import pytest
from awdiscord import voice_council as vc
from awdiscord.voice import (
    DEFAULT_WAKE_WORDS,
    DiscordVoice,
    VoiceConfig,
    _Player,
    _Session,
)
from awvoice.loop import CallableSpeaker, CallableTranscriber, VoiceLoop

# ---------------------------------------------------------------------------
# Name parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("heard,names,body", [
    ("Atlas, Aither and Lyra, quick take on X",
     ("atlas", "aither", "lyra"), "quick take on X"),
    ("Atlas and Aither, what do you think",
     ("atlas", "aither"), "what do you think"),
    ("Hey Atlas, Aither, yadda", ("atlas", "aither"), "yadda"),
    ("Okay, ether, what time is it?", ("aither",), "what time is it?"),
    ("Demi, quick take", ("demiurge",), "quick take"),
    ("Atlas, Demi, Lyra & Iris, weigh in", ("atlas", "demiurge", "lyra", "iris"),
     "weigh in"),
    ("Aither's going to help.", ("aither",), "going to help."),
    ("Atlas?", ("atlas",), ""),
    ("What time is it?", (), "What time is it?"),
    ("Atlas orbited the Earth", ("atlas",), "orbited the Earth"),
])
def test_the_leading_names_come_off_the_question(heard, names, body):
    got = vc.parse_addresses(heard)
    assert got.names == names
    assert got.body == body


def test_a_name_repeated_is_answered_once():
    got = vc.parse_addresses("Atlas, Aither, Atlas, hello")
    assert got.names == ("atlas", "aither")


def test_stt_spellings_are_the_same_bot():
    for word in DEFAULT_WAKE_WORDS:
        assert vc.canonical(word) == "aither", word
    assert vc.canonical("Demi") == "demiurge"
    assert vc.canonical("demi-urge") == "demiurge"
    assert vc.canonical("Lira") == "lyra"
    assert vc.canonical("bank") == ""


def test_every_alias_maps_to_a_roster_name():
    assert set(vc.ALIASES) == set(vc.ROSTER)
    assert vc.LEAD in vc.ROSTER
    for words in vc.ALIASES.values():
        assert words and all(isinstance(w, str) and w for w in words)


# ---------------------------------------------------------------------------
# Cue detection, caps and stop words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("said", [
    "discuss the Q3 plan", "let's debate this", "argue it out",
    "quick council on hiring", "Lyra should weigh in", "weigh-in time",
])
def test_a_discussion_cue_is_detected(said):
    assert vc.is_council_cue(said) is True


@pytest.mark.parametrize("said", [
    "quick take on the Q3 plan", "what is the plan?", "", "summarize the notes",
])
def test_an_ordinary_question_is_not_a_council(said):
    assert vc.is_council_cue(said) is False


def test_a_council_is_capped_and_the_lead_is_reserved_for_the_verdict():
    names = ("aither", "atlas", "demiurge", "lyra", "iris", "saga", "vera")
    plan = vc.plan_council(names, max_agents=5)
    assert plan.lead == "aither"
    assert plan.speakers == ("atlas", "demiurge", "lyra", "iris")
    assert plan.capped is True
    assert vc.LEAD not in plan.speakers
    assert len(plan.speakers) + 1 == 5                     # the whole round
    small = vc.plan_council(("atlas", "demi", "lyra"), max_agents=5)
    assert small.speakers == ("atlas", "demiurge", "lyra") and small.capped is False
    assert small.budget_s == vc.BUDGET_S == 45.0


@pytest.mark.parametrize("said", [
    "enough", "Enough!", "that's enough", "Ok, enough already", "stop", "stop it",
    "stop now", "wrap it up", "wrap this up", "cut it out", "halt", "quit it",
    "settle down", "that is enough",
])
def test_the_ways_a_human_ends_a_council(said):
    assert vc.is_stop(said) is True


@pytest.mark.parametrize("said", [
    "stop the build", "how do I stop the service", "what is enough", "don't stop",
    "the stop sign", "", "not enough information", "stop notes",
])
def test_an_ordinary_sentence_is_not_a_stop(said):
    assert vc.is_stop(said) is False


def test_answers_are_capped_to_one_line():
    assert vc.one_line("Ship it. Then tell Ann.") == "Ship it."
    assert vc.one_line("  We   ship  Friday.  ") == "We ship Friday."
    long = vc.one_line("a" * 400)
    assert len(long) <= 240 and long.endswith("...")


# ---------------------------------------------------------------------------
# Slot order, with a fake clock and a fake floor
# ---------------------------------------------------------------------------


class _Fake:
    def __init__(self, busy=False):
        self.now = 0.0
        self.slept = []
        self.busy = busy
        self.spoke = 0

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += max(0.01, seconds)


def _waiter(fake, abort=None):
    return vc.SlotWaiter(lambda: fake.busy, lambda: fake.spoke,
                         clock=fake.clock, sleep=fake.sleep, poll_s=0.1,
                         abort=abort)


def test_the_first_named_bot_speaks_immediately():
    fake = _Fake(busy=True)
    assert _waiter(fake).wait(0) == "now"


def test_bot_k_waits_for_the_predecessor_and_a_quiet_floor():
    fake = _Fake(busy=True)
    fake.spoke = 1
    fake.now = 2.0
    # Predecessor has spoken but the floor is busy: keep waiting, then take it.
    original = fake.sleep

    def sleep(seconds):
        original(seconds)
        fake.busy = False                  # the predecessor finishes inside the wait

    waiter = vc.SlotWaiter(lambda: fake.busy, lambda: fake.spoke,
                           clock=fake.clock, sleep=sleep, poll_s=0.1)
    assert waiter.wait(1, slot_s=12.0) == "now"
    assert fake.now < 12.0                 # it did not sit out the whole slot


def test_the_deterministic_fallback_skips_a_bot_when_the_floor_stays_busy():
    fake = _Fake(busy=True)
    assert _waiter(fake).wait(2, slot_s=5.0) == "skipped"
    assert fake.now == pytest.approx(10.0, abs=0.2)
    # Nobody spoke but the floor went quiet by the deadline: take it.
    quiet = _Fake(busy=True)

    def sleep(seconds):
        quiet.slept.append(seconds)
        quiet.now += seconds
        if quiet.now >= 5.0:
            quiet.busy = False

    assert vc.SlotWaiter(lambda: quiet.busy, lambda: quiet.spoke,
                         clock=quiet.clock, sleep=sleep).wait(1, slot_s=5.0) == "now"


def test_a_stop_aborts_the_wait():
    fake = _Fake()
    stopped = {"yes": False}
    waiter = _waiter(fake, abort=lambda: stopped["yes"])
    stopped["yes"] = True
    assert waiter.wait(3, slot_s=5.0) == "aborted"


def test_a_council_budget_caps_the_wait():
    fake = _Fake(busy=True)
    assert _waiter(fake).wait(4, slot_s=12.0, max_wait_s=3.0) == "skipped"
    assert fake.now == pytest.approx(3.0, abs=0.2)


# ---------------------------------------------------------------------------
# The text channel is how separate processes read each other's lines
# ---------------------------------------------------------------------------


def test_prior_lines_are_read_from_the_channel_and_this_round_only():
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    messages = [                       # Discord returns newest first
        {"content": "**Council** Lyra: Wait a week.", "timestamp": now},
        {"content": "chatter from a human", "timestamp": now},
        {"content": "**Council** Atlas: Ship on Friday.", "timestamp": now},
        {"content": "**Council** Atlas: Old round line.", "timestamp": "2000-01-01T00:00:00"},
    ]
    lines = vc.prior_lines(messages, names=("atlas", "lyra", "iris"),
                           since_iso=now)
    assert lines == [("atlas", "Ship on Friday."), ("lyra", "Wait a week.")]


def test_the_prompts_carry_the_previous_lines():
    prior = [("atlas", "Ship on Friday.")]
    member = vc.member_prompt("When do we ship?", prior, "lyra")
    assert "When do we ship?" in member and "Atlas: Ship on Friday." in member
    verdict = vc.verdict_prompt("When do we ship?", prior, stopped=True)
    assert "Atlas: Ship on Friday." in verdict and "wrap it up early" in verdict
    assert vc.council_line("demiurge", "Ship it. Today.") == \
        "**Council** Demi: Ship it. Today."


# ---------------------------------------------------------------------------
# The session, with fakes for STT/TTS/Discord
# ---------------------------------------------------------------------------


class _Rest:
    def __init__(self):
        self.messages = []
        self.read = []

    def create_message(self, channel_id, payload):
        self.messages.append((channel_id, payload["content"]))
        return {"id": "m"}

    def request(self, method, path, body=None, auth=True):
        assert method == "GET" and path.startswith("/channels/20/messages")
        return list(self.read)


class _Bot:
    agent = None
    dispatcher = None

    def __init__(self):
        self.rest = _Rest()


class _Thinker:
    def __init__(self, answer="Ship it."):
        self.answer = answer
        self.asked = []
        self.speaker = ("", "")

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


def _session(persona, thinker=None, slot_s=0.05, **config):
    words = []

    def transcribe(pcm):
        return words.pop(0) if words else ""

    spoken = []

    def speak(text):
        spoken.append(text)
        return (text.encode(), "wav")

    thinker = thinker or _Thinker()
    loop = VoiceLoop(CallableTranscriber(transcribe), thinker, CallableSpeaker(speak))
    bot = _Bot()
    voice = DiscordVoice(bot, VoiceConfig(enabled=True, persona=persona, slot_s=slot_s,
                                          **config))
    logs = []
    voice.log = lambda **f: logs.append(f)
    voice._decode = lambda audio, fmt: b"pcm:" + audio
    session = _Session(voice, "10", "20", loop)
    session.vc = types.SimpleNamespace(channel=types.SimpleNamespace(members=[
        types.SimpleNamespace(id="1", name="ann", bot=False),
        types.SimpleNamespace(id="100", name=persona, bot=True)]))
    session.player.close()
    session.player = _Player(lambda pcm, after: after(None), lambda: None)
    session._test_words = words
    return session, thinker, bot.rest, spoken, logs, words


def _route(session, text, user="1", name="ann"):
    session._test_words.append(text)
    session._route(user, name, b"pcm", time.monotonic())


def _join(session):
    for t in list(session.side_threads):
        t.join(5)


def test_each_named_bot_answers_once_with_all_the_names_stripped():
    session, thinker, _, spoken, logs, _ = _session("atlas")
    _route(session, "Atlas, Aither and Lyra, quick take on X")
    _join(session)
    session.stop()
    questions = [a[2] for a in thinker.asked if a[0] == "think"]
    assert len(questions) == 1                       # exactly one answer
    assert "quick take on X" in questions[0]
    assert "Aither and Lyra" not in questions[0]     # no other name leads the question
    assert spoken and spoken[0] == "Ship it."
    assert any(f.get("event") == "voice_multi_name" and f.get("index") == 0
               for f in logs)


def test_a_twin_not_named_stays_silent():
    session, thinker, _, spoken, logs, _ = _session("lyra")
    _route(session, "Atlas, Aither, quick take on X")
    _join(session)
    session.stop()
    assert thinker.asked == [] and spoken == []
    assert any(f.get("event") == "voice_for_other" for f in logs)


def test_a_busy_floor_skips_the_later_bot():
    session, thinker, _, spoken, _, _ = _session("lyra", slot_s=0.02)
    session.floor_started(types.SimpleNamespace(id="200", bot=True))
    _route(session, "Atlas, Lyra, quick take on X")
    _join(session)
    session.stop()
    assert [a for a in thinker.asked if a[0] == "think"] == []
    assert spoken == []


def test_a_council_line_is_read_from_and_posted_to_the_chat():
    session, thinker, rest, spoken, logs, _ = _session("demiurge", slot_s=0.05)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    rest.read = [{"content": "**Council** Atlas: Ship on Friday.", "timestamp": now}]
    _route(session, "Atlas, Demi, discuss when we ship")
    _join(session)
    session.stop()
    asked = [a[2] for a in thinker.asked if a[0] == "think"]
    assert len(asked) == 1
    assert "Atlas: Ship on Friday." in asked[0]       # saw the previous line
    assert spoken == ["Ship it."]
    posted = [content for _, content in rest.messages]
    assert "**Council** Demi: Ship it." in posted
    assert any(f.get("event") == "voice_council_line" and f.get("speaker") == "demiurge"
               for f in logs)


def test_the_lead_gives_one_verdict_after_the_round():
    session, thinker, rest, spoken, logs, _ = _session("aither", slot_s=0.05)
    now = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    rest.read = [{"content": "**Council** Atlas: Ship on Friday.", "timestamp": now}]
    _route(session, "Atlas, Demi, discuss when we ship")
    _join(session)
    session.stop()
    asked = [a[2] for a in thinker.asked if a[0] == "think"]
    assert len(asked) == 1 and "verdict" in asked[0].lower()
    assert spoken == ["Ship it."]
    assert any(f.get("event") == "voice_council_verdict" for f in logs)


def test_enough_ends_the_round_and_the_lead_sums_up_at_once():
    session, thinker, _, spoken, _, _ = _session("aither", slot_s=5.0)
    session._council = vc.CouncilRound("when we ship", ("atlas", "demiurge"),
                                       budget_s=45.0)
    _route(session, "enough")
    _join(session)
    session.stop()
    assert session._council is None or session._council.verdict_claimed.is_set()
    asked = [a[2] for a in thinker.asked if a[0] == "think"]
    assert len(asked) == 1 and "wrap it up early" in asked[0]
    assert spoken == ["Ship it."]


def test_a_stopped_speaker_never_says_its_line():
    session, thinker, rest, spoken, _, _ = _session("demiurge", slot_s=1.0)
    state = vc.CouncilRound("when we ship", ("atlas", "demiurge"), budget_s=45.0)
    session._council = state
    state.stop.set()
    session._council_turn(state, "1", "ann")           # the stop landed first
    session.stop()
    assert thinker.asked == [] and spoken == []
    assert state.stop.is_set()


def test_a_skipped_lead_ends_the_round_instead_of_keeping_it_active():
    # A busy floor made the lead's slot "skipped"; the round is over. It must not
    # stay active: that refused every later council as busy AND let a stray
    # "enough" an hour later make the lead read out a verdict nobody asked for.
    session, thinker, _, _, logs, _ = _session(
        "aither", slot_s=0.02, council_budget_s=0.05)
    session.floor_started(types.SimpleNamespace(id="200", bot=True))
    _route(session, "Atlas, Demi, discuss when we ship")
    _join(session)
    assert session._council is None and session._council_active() is False
    assert any(f.get("event") == "voice_slot" and f.get("verdict") == "skipped"
               for f in logs)
    _route(session, "enough")                          # no round is running now
    _join(session)
    session.stop()
    # It is an ordinary utterance now: no verdict, nothing about a council.
    assert not any(f.get("event") == "voice_council_verdict" for f in logs)
    assert not any("closing a quick council" in a[2]
                   for a in thinker.asked if a[0] == "think")


def test_the_round_reads_every_line_since_it_started_not_just_the_last_five_s():
    # The third slot and the verdict arrive 10-40 s after the first line. A fixed
    # five-second window left them reading only the line said just before them.
    session, thinker, rest, spoken, logs, _ = _session("lyra", slot_s=0.05)
    state = vc.CouncilRound("when we ship", ("atlas", "lyra"), budget_s=45.0)
    state.started_wall = time.time() - 20              # the round began 20 s ago
    session._council = state
    rest.read = [{"content": "**Council** Atlas: Ship on Friday.",
                  "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S",
                                             time.gmtime(time.time() - 15))}]
    session._council_turn(state, "1", "ann")
    session.stop()
    asked = [a[2] for a in thinker.asked if a[0] == "think"]
    assert asked and "Atlas: Ship on Friday." in asked[0]
    assert spoken == ["Ship it."]


def test_a_lost_speaking_stop_does_not_wedge_the_floor_forever():
    # A bot whose STOP event never arrived used to keep the floor busy for the rest
    # of the call: every later turn was skipped and no council could start again.
    session, *_ = _session("aither")
    session.floor_started(types.SimpleNamespace(id="200", bot=True))
    assert session.floor_busy() is True
    with session._floor_lock:
        session._floor_since["200"] = time.monotonic() - 10_000
    assert session.floor_busy() is False
    session.stop()


def test_the_floor_sees_bots_speaking_and_forgets_at_a_new_round():
    session, *_ = _session("aither")
    atlas = types.SimpleNamespace(id="200", bot=True)
    ann = types.SimpleNamespace(id="1", bot=False)
    session.floor_started(atlas)
    assert session.floor_busy() is True and session.heard_speakers() == 1
    session.floor_stopped(atlas)
    assert session.heard_speakers() == 1                # this round's count survives
    session.begin_round()
    assert session.heard_speakers() == 0
    session.floor_started(ann)                          # a human is not a council voice
    assert session.heard_speakers() == 0
    session.stop()


def test_only_other_bots_hold_the_floor_humans_never_do():
    # LR-62280c42: the speaking listener hands EVERY member to the floor; the floor
    # itself keeps only other bots. A human starting or stopping never makes the
    # floor busy (no settle window either); another bot speaking does.
    session, *_ = _session("aither")
    ann = types.SimpleNamespace(id="1", bot=False)
    atlas = types.SimpleNamespace(id="200", bot=True)
    session.floor_started(ann)
    assert session.floor_busy() is False
    session.floor_stopped(ann)
    assert session.floor_busy() is False                # no settle from a human
    session.floor_started(atlas)
    assert session.floor_busy() is True                 # another bot holds the floor
    session.floor_stopped(atlas)
    assert session.floor_busy() is True                 # settling
    session.stop()


def test_read_recent_only_puts_a_snowflake_into_the_rest_path():
    # LR-4b49a39c: the channel id is validated before it is joined into a path.
    session, _t, rest, *_ = _session("aither")
    rest.read = [{"content": "hi"}]
    seen = []
    real = rest.request

    def request(method, path, body=None, auth=True):
        seen.append(path)
        return real(method, path, body, auth)
    rest.request = request
    assert session.voice.read_recent("20", limit=5) == [{"content": "hi"}]
    for bad in ("20/../../users/@me", "20?limit=100&x=1", "abc", "20 ", "²"):
        assert session.voice.read_recent(bad) == []
    assert seen == ["/channels/20/messages?limit=5"]
    session.stop()
