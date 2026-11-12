"""Calls that run themselves: join, notes that survive a restart, talk filed as work.

WHY THESE CASES
---------------
Until 2026-11-12 the bot joined a call only on ``/join``, took notes only when told,
answered nothing while it took them, kept the notes in memory (a restart mid-call
lost the call) and posted action items as prose nobody tracked. A server that opts in
now gets a bot that joins when a person does, says in the chat that it is taking
notes and how to pause, still answers "Aither, ...", journals every line to disk,
and at the end files each action item and lists it with its link and owner. An item
that spends, deletes or publishes is marked for a decision and never done. A server
that did not opt in sees none of this.
"""

from __future__ import annotations

import asyncio
import io
import json
import time
import types

import pytest
from awdiscord import callnotes
from awdiscord.store import GuildStore
from awdiscord.voice import (
    DiscordVoice,
    VoiceConfig,
    _Player,
    _Session,
    parse_command,
)
from awvoice.loop import CallableSpeaker, CallableTranscriber, VoiceLoop


@pytest.fixture(autouse=True)
def _no_leaked_filers():
    callnotes.clear_registrations()
    yield
    callnotes.clear_registrations()


# ---------------------------------------------------------------------------
# The per-server opt-in
# ---------------------------------------------------------------------------


def test_calls_run_themselves_only_where_a_server_opted_in(monkeypatch):
    store = GuildStore()
    assert callnotes.voice_calls_on("10", store) is False          # the default
    callnotes.set_voice_calls(store, "10", True)
    assert callnotes.voice_calls_on("10", store) is True
    assert callnotes.voice_calls_on("11", store) is False          # another server
    callnotes.set_voice_calls(store, "10", False)
    assert callnotes.voice_calls_on("10", store) is False
    monkeypatch.setenv("AWDISCORD_VOICE_AUTO_GUILDS", "12, nope ,13")
    assert callnotes.auto_guilds() == ("12", "13")
    assert callnotes.voice_calls_on("12", store, callnotes.auto_guilds()) is True
    assert VoiceConfig.from_env().auto_guilds == ("12", "13")


def test_an_unreadable_setting_is_off_never_a_guess():
    class Broken:
        def get(self, *a, **k):
            raise RuntimeError("db locked")

    assert callnotes.voice_calls_on("10", Broken()) is False
    assert callnotes.voice_calls_on("", GuildStore()) is False
    assert callnotes.voice_calls_on("10", None) is False


# ---------------------------------------------------------------------------
# Join / stay / leave
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("kwargs,move", [
    # not opted in: nothing, whoever is in voice
    (dict(auto=False, bot_channel="", humans={"a": 3}), ("stay", "")),
    # a person joins a channel and the bot is nowhere: join them
    (dict(auto=True, bot_channel="", humans={"a": 1, "b": 0}), ("join", "a")),
    # the bot's channel still has people: stay, even when another is fuller
    (dict(auto=True, bot_channel="a", humans={"a": 1, "b": 4}), ("stay", "")),
    # its channel emptied and people are elsewhere: move to them
    (dict(auto=True, bot_channel="a", humans={"a": 0, "b": 2}), ("join", "b")),
    # a tie goes to the channel the event came from
    (dict(auto=True, bot_channel="", humans={"a": 1, "b": 1}, event_channel="a"),
     ("join", "a")),
    # the last person left: leave
    (dict(auto=True, bot_channel="a", humans={"a": 0, "b": 0}), ("leave", "")),
    # ...unless the server has a standing room: go back to it
    (dict(auto=True, bot_channel="a", humans={"a": 0, "b": 0}, standing=("b",)),
     ("join", "b")),
    (dict(auto=True, bot_channel="b", humans={"a": 0, "b": 0}, standing=("b",)),
     ("stay", "")),
    # nobody anywhere and the bot is not in voice: nothing
    (dict(auto=True, bot_channel="", humans={"a": 0}), ("stay", "")),
])
def test_next_move(kwargs, move):
    assert callnotes.next_move(**kwargs) == move


# ---------------------------------------------------------------------------
# The journal
# ---------------------------------------------------------------------------


def test_a_journal_keeps_every_line_and_survives_a_torn_write(tmp_path):
    journal = callnotes.Journal(tmp_path, "10", "20")
    assert not journal.exists()
    journal.start(1000.0, "ann", "1", auto=True)
    journal.line(1001.0, "ann", "We ship Friday.", "1")
    journal.mark("pause", "bob")
    journal.mark("resume", "bob")
    journal.line(1002.0, "bob", "Ann deploys.")
    with open(journal.path, "a", encoding="utf-8") as fh:
        fh.write('{"type": "line", "at": 1003.0, "name": "bob", "te')   # the crash
    assert callnotes.unfinished(tmp_path) == [journal.path]
    header, lines, paused = callnotes.read_journal(journal.path)
    assert header["by"] == "ann" and header["auto"] is True
    assert header["people"] == {"ann": "1"}
    assert lines == [(1001.0, "ann", "We ship Friday."), (1002.0, "bob", "Ann deploys.")]
    assert paused is False
    done = journal.close()
    assert done is not None and done.parent.name == "done" and done.is_file()
    assert callnotes.unfinished(tmp_path) == []
    assert journal.close() is None                                  # nothing left to close


def test_a_pause_left_open_is_read_back_as_paused(tmp_path):
    journal = callnotes.Journal(tmp_path, "10", "20")
    journal.start(1000.0, "ann")
    journal.mark("pause", "ann")
    assert callnotes.read_journal(journal.path)[2] is True


# ---------------------------------------------------------------------------
# Spoken commands
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("said,kind", [
    ("off the record", "notes_pause"),
    ("go off the record.", "notes_pause"),
    ("pause the notes", "notes_pause"),
    ("back on the record", "notes_resume"),
    ("we're back on the record", "notes_resume"),
    ("resume notes", "notes_resume"),
    ("create a task to send Bob the deck", "action"),
    ("please open an issue for the login bug", "action"),
    ("remind Ann to renew the certificate", "action"),
    ("ask Atlas to draft the pricing page", "action"),
    ("action item: Bob calls the vendor", "action"),
    ("put that on the board", "action"),
    ("what tasks are open", ""),
    ("how do I file an issue", ""),
    ("did we track the budget", ""),
    ("what did we decide", ""),
])
def test_spoken_call_commands(said, kind):
    assert parse_command(said)[0] == kind


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------

_PLAN = {
    "title": "Launch sync",
    "decisions": ["Ship on Friday."],
    "actions": [
        {"title": "Deploy the release", "detail": "Friday morning", "owner": "ann",
         "kind": "engineering", "needs_decision": False},
        {"title": "Draft the launch notes", "detail": "", "owner": "atlas",
         "kind": "agent"},
        {"title": "Buy two more GPU servers", "detail": "about $40k", "owner": "bob",
         "kind": "task", "needs_decision": False},
        {"title": "Tidy the old staging box", "detail": "", "owner": "bob",
         "kind": "weird", "needs_decision": True, "why": "it removes the volumes"},
        {"title": "x", "owner": "nobody"},
    ],
    "questions": ["Who tells the customers?", "None"],
}


def test_the_plan_is_read_and_risky_items_are_marked_for_a_decision():
    plan = callnotes.parse_call_plan("Here you go:\n" + json.dumps(_PLAN) + "\nDone.")
    assert plan["parsed"] and plan["title"] == "Launch sync"
    assert plan["decisions"] == ["Ship on Friday."]
    assert plan["questions"] == ["Who tells the customers?"]
    by_title = {a["title"]: a for a in plan["actions"]}
    assert len(by_title) == 4                                      # the 1-char title is dropped
    assert by_title["Deploy the release"]["kind"] == "engineering"
    assert by_title["Deploy the release"]["needs_decision"] is False
    # The model said "no decision needed"; the words say money. The words win.
    assert by_title["Buy two more GPU servers"]["needs_decision"] is True
    assert by_title["Buy two more GPU servers"]["risk"] == "spend"
    # The model's own flag is honoured, and an unknown kind is a plain task.
    assert by_title["Tidy the old staging box"]["needs_decision"] is True
    assert by_title["Tidy the old staging box"]["kind"] == "task"


def test_a_reply_that_is_not_json_files_nothing():
    plan = callnotes.parse_call_plan("Decisions: ship Friday. Action items: Ann - deploy.")
    assert plan["parsed"] is False and plan["actions"] == []


@pytest.mark.parametrize("words,risk", [
    ("Buy the domain", "spend"), ("renew the $99 plan", "spend"),
    ("Delete the old user table", "delete"), ("wipe the staging disk", "delete"),
    ("Publish the blog post", "publish"), ("email the customers about the outage", "publish"),
    ("post it on LinkedIn", "publish"), ("make the repo public", "publish"),
    ("Draft the blog post", ""), ("Review the budget", ""), ("fix the login bug", ""),
])
def test_what_counts_as_spend_delete_or_publish(words, risk):
    assert callnotes.risk_of(words) == risk


def test_the_list_shows_five_and_counts_the_rest():
    rows = [{"title": "Item %d" % i, "owner": "ann", "link": "https://x/%d" % i,
             "status": "filed"} for i in range(1, 8)]
    rows[1] = {"title": "Buy servers", "owner": "", "link": "https://x/card",
               "status": "decision", "note": "spend"}
    main, more = callnotes.action_list(rows)
    lines = main.splitlines()
    assert lines[1] == "1. Item 1 - ann - <https://x/1>"
    assert "needs a decision, not done" in lines[2] and "no owner yet" in lines[2]
    assert lines[-1] == "+2 more below" and len(lines) == 7
    assert more.splitlines()[0].startswith("6. Item 6") and "7. Item 7" in more
    assert callnotes.action_list(rows[:5])[1] == ""
    assert callnotes.action_list([]) == ("", "")


def test_items_nobody_filed_say_so():
    plan = callnotes.parse_call_plan(json.dumps(_PLAN))
    rows = callnotes.file_actions("10", "20", plan)
    assert rows and all(r["status"] == "failed" for r in rows)
    assert "no tracker is connected" in rows[0]["note"]

    def declines(guild, channel, plan, context):
        return None                                   # not this filer's server

    def breaks(guild, channel, plan, context):
        raise RuntimeError("board is down")

    callnotes.register_filer(declines)
    callnotes.register_filer(breaks)
    rows = callnotes.file_actions("10", "20", plan)
    assert all(r["status"] == "failed" for r in rows) and "board is down" in rows[0]["note"]

    def files(guild, channel, plan, context):
        return [{"title": a["title"], "owner": a["owner"], "link": "https://b/1",
                 "status": "filed"} for a in plan["actions"]]

    callnotes.register_filer(files)
    assert all(r["status"] == "filed" for r in callnotes.file_actions("10", "20", plan))
    assert callnotes.file_actions("10", "20", {"actions": []}) == []


def test_a_github_issue_is_filed_with_the_token_and_returns_its_link():
    seen = {}

    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=0):
        seen["url"] = req.full_url
        seen["auth"] = req.get_header("Authorization")
        seen["body"] = json.loads(req.data.decode())
        return Resp(json.dumps({"html_url": "https://github.com/o/r/issues/7"}).encode())

    url = callnotes.github_issue("o/r", "Fix login", "said in a call", "tok", opener,
                                 labels=["from-call"])
    assert url == "https://github.com/o/r/issues/7"
    assert seen["url"] == "https://api.github.com/repos/o/r/issues"
    assert seen["auth"] == "Bearer tok" and seen["body"]["labels"] == ["from-call"]
    with pytest.raises(RuntimeError):
        callnotes.github_issue("o/r", "t", "b", "")
    with pytest.raises(ValueError):
        callnotes.github_issue("not a repo", "t", "b", "tok")


# ---------------------------------------------------------------------------
# The session, with a real VoiceLoop and fakes for STT/TTS/Discord
# ---------------------------------------------------------------------------


class _Rest:
    def __init__(self, fail_upload=False, threads=False):
        self.messages = []
        self.files = []
        self.threads = []
        self.fail_upload = fail_upload
        self._threads = threads

    def create_message(self, channel_id, payload):
        self.messages.append((channel_id, payload["content"]))
        return {"id": "m%d" % len(self.messages)}

    def upload(self, channel_id, filename, data, content_type="", content=""):
        if self.fail_upload:
            raise RuntimeError("discord is down")
        self.files.append((channel_id, filename, data.decode("utf-8"), content))
        return {"id": "f"}

    def request(self, method, path, body=None):
        if not self._threads:
            raise RuntimeError("threads are not allowed here")
        self.threads.append(path)
        return {"id": "thread1"}


class _Bot:
    agent = None
    dispatcher = None

    def __init__(self, rest=None):
        self.rest = rest or _Rest()


class _Thinker:
    def __init__(self, answer="Sure."):
        self.answer = answer
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
        yield "It is sunny."


def _member(name, bot=False):
    return types.SimpleNamespace(name=name, bot=bot)


def _session(heard, thinker=None, humans=2, rest=None, **config):
    words = list(heard)
    spoken = []

    def transcribe(pcm):
        return words.pop(0) if words else ""

    def speak(text):
        spoken.append(text)
        return (text.encode(), "wav")

    thinker = thinker or _Thinker()
    loop = VoiceLoop(CallableTranscriber(transcribe), thinker, CallableSpeaker(speak))
    bot = _Bot(rest)
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


_PCM = b"\x00\x00" * 16000


def _say(session, n=1, user="2", name="bob"):
    for _ in range(n):
        session.submit(user, name, _PCM)


def _drain(session):
    assert _wait(lambda: session.queue.empty())
    time.sleep(0.15)
    for t in list(session.side_threads):
        t.join(3)


def _texts(rest):
    return [m[1] for m in rest.messages]


def test_a_question_is_answered_while_notes_are_being_taken():
    session, thinker, rest, _, logs = _session([
        "Aither, take notes",
        "We ship on Friday.",
        "Aither, what's the weather?",
        "Aither, stop",
    ], thinker=_Thinker(json.dumps(_PLAN)))
    _say(session, 4)
    _drain(session)
    session.stop()
    # Answered, with what the call has said so far in front of the model...
    asked = [a[2] for a in thinker.asked if a[0] == "stream"]
    assert len(asked) == 1 and asked[0].startswith("what's the weather?")
    assert "bob: We ship on Friday." in asked[0]
    assert asked[0].count("what's the weather?") == 1     # the question is not repeated
    # ...and both the question and her answer are in the record.
    data = rest.files[0][2]
    assert "bob: Aither, what's the weather?" in data
    assert "Aither: It is sunny." in data
    assert "bob: We ship on Friday." in data


def test_a_line_not_addressed_to_her_is_only_recorded_even_one_to_one():
    session, thinker, _, _, _ = _session(
        ["Aither, take notes", "I think we should ship Friday."], humans=1)
    _say(session)                # one at a time: alone, only the newest is kept
    _drain(session)
    _say(session)
    _drain(session)
    assert not [a for a in thinker.asked if a[0] == "stream"]
    assert session.notes.lines[-1][2] == "I think we should ship Friday."
    session.notes = None
    session.stop()


def test_off_the_record_writes_nothing_until_back_on_the_record(tmp_path):
    session, _, rest, spoken, _ = _session([
        "Aither, take notes",
        "This part is fine.",
        "Aither, off the record",
        "The secret number is 42.",
        "Aither, back on the record",
        "And we are back.",
    ], state_dir=str(tmp_path))
    _say(session, 6)
    _drain(session)
    said = [t for _, _, t in session.notes.lines]
    assert said == ["This part is fine.", "And we are back."]
    journal = (tmp_path / "callnotes" / "10-20.jsonl").read_text(encoding="utf-8")
    assert "secret number" not in journal and "This part is fine." in journal
    texts = _texts(rest)
    assert any("Off the record" in t for t in texts)
    assert any("Back on the record" in t for t in texts)
    assert "Off the record." in spoken and "Back on the record." in spoken
    session.notes = None
    session.stop()


def test_a_call_that_runs_itself_tells_the_room_keeps_notes_and_files_the_work(tmp_path):
    filed, kept = [], []

    def filer(guild, channel, plan, context):
        filed.append((guild, channel, context["source"], dict(context["people"])))
        return [{"title": a["title"], "owner": a["owner"],
                 "link": "https://board/%d" % i,
                 "status": "decision" if a["needs_decision"] else "filed"}
                for i, a in enumerate(plan["actions"], 1)]

    def keeper(guild, channel, title, document):
        kept.append((guild, channel, title, document))
        return True

    callnotes.register_filer(filer)
    callnotes.register_keeper(keeper)
    session, thinker, rest, spoken, logs = _session(
        ["We ship on Friday.", "Ann will deploy it."],
        thinker=_Thinker(json.dumps(_PLAN)), state_dir=str(tmp_path))
    session.sync_call(2)                         # the first people are in the room
    session.sync_call(2)                         # a second event changes nothing
    assert _texts(rest) == [callnotes.JOIN_NOTICE]
    assert rest.messages[0][0] == "20"
    assert "off the record" in callnotes.JOIN_NOTICE and "taking notes" in callnotes.JOIN_NOTICE
    assert "Taking notes." not in spoken         # told in writing, not by interrupting
    _say(session, 1, user="1", name="ann")
    _say(session, 1, user="2", name="bob")
    _drain(session)
    assert (tmp_path / "callnotes" / "10-20.jsonl").is_file()
    session.sync_call(0)                         # the last person left
    _drain(session)
    channel, filename, data, content = rest.files[0]
    assert channel == "20" and filename.startswith("call-notes-")
    assert "**Decisions**" in content and "Ship on Friday." in content
    assert "ann: We ship on Friday." in data and "bob: Ann will deploy it." in data
    listing = [t for t in _texts(rest) if t.startswith("**Action items from this call**")]
    assert len(listing) == 1
    assert "1. Deploy the release - ann - <https://board/1>" in listing[0]
    assert "3. Buy two more GPU servers - bob - <https://board/3> - **needs a decision, " \
           "not done**" in listing[0]
    assert filed == [("10", "20", "call", {"ann": "1", "bob": "2"})]
    assert kept and kept[0][2] == "Launch sync" and "kind: voice-call-notes" in kept[0][3]
    assert "https://board/1" in kept[0][3]
    # The journal is closed, not left to be posted a second time.
    assert callnotes.unfinished(tmp_path) == []
    assert list((tmp_path / "callnotes" / "done").glob("*.jsonl"))
    assert session.notes is None
    session.stop()


def test_stopping_by_voice_keeps_notes_off_until_the_room_empties():
    session, _, rest, _, _ = _session(["Aither, stop"])
    session.sync_call(2)
    _say(session)
    _drain(session)
    assert session.notes is None
    session.sync_call(3)                         # someone else joins: still off
    assert session.notes is None
    session.sync_call(0)
    session.sync_call(1)                         # a new call: on again
    assert session.notes is not None and session.notes.auto
    session.notes = None
    session.stop()


def test_more_than_five_items_show_five_and_the_rest_follow(tmp_path):
    plan = {"title": "Big call", "decisions": [], "questions": [],
            "actions": [{"title": "Item number %d" % i, "owner": "ann", "kind": "task"}
                        for i in range(1, 8)]}
    callnotes.register_filer(lambda g, c, p, ctx: [
        {"title": a["title"], "owner": a["owner"], "link": "", "status": "filed"}
        for a in p["actions"]])
    for threads in (False, True):
        rest = _Rest(threads=threads)
        session, _, _, _, _ = _session(["One."], thinker=_Thinker(json.dumps(plan)),
                                       rest=rest)
        session.sync_call(2)
        _say(session)
        _drain(session)
        session.sync_call(0)
        _drain(session)
        main = [t for t in _texts(rest) if t.startswith("**Action items")][0]
        assert "5. Item number 5" in main and "6. Item number 6" not in main
        assert main.endswith("+2 more below")
        rest_post = [m for m in rest.messages if m[1].startswith("6. Item number 6")][0]
        assert "7. Item number 7" in rest_post[1]
        # In a thread when the channel allows one, else in the channel itself.
        assert rest_post[0] == ("thread1" if threads else "20")
        session.stop()


def test_notes_that_cannot_be_posted_stay_on_disk_and_nothing_is_filed(tmp_path):
    filed = []
    callnotes.register_filer(lambda g, c, p, ctx: filed.append(1) or [])
    rest = _Rest(fail_upload=True)
    session, _, _, _, logs = _session(["We ship Friday."], rest=rest,
                                      thinker=_Thinker(json.dumps(_PLAN)),
                                      state_dir=str(tmp_path))
    session.sync_call(2)
    _say(session)
    _drain(session)
    session.sync_call(0)
    _drain(session)
    session.stop()
    assert filed == []
    assert len(callnotes.unfinished(tmp_path)) == 1
    assert any(f.get("event") == "voice_notes_posted" and f.get("posted") is False
               for f in logs)


def test_a_restart_mid_call_resumes_the_same_notes(tmp_path):
    first, _, rest1, _, _ = _session(["We ship on Friday."], state_dir=str(tmp_path))
    first.sync_call(2)
    _say(first, 1, user="1", name="ann")
    _drain(first)
    # The process dies here: no stop(), no summary. Only the journal is left.
    first._stop.set()
    first.player.close()
    assert rest1.files == []

    second, _, rest2, _, logs = _session(["Ann will deploy it."],
                                         thinker=_Thinker(json.dumps(_PLAN)),
                                         state_dir=str(tmp_path))
    second.sync_call(2)                          # people are still in the room
    assert _texts(rest2) == [callnotes.RESUME_NOTICE]
    assert [t for _, _, t in second.notes.lines] == ["We ship on Friday."]
    assert second.notes.people == {"ann": "1"}
    _say(second, 1, user="2", name="bob")
    _drain(second)
    second.sync_call(0)
    _drain(second)
    data = rest2.files[0][2]
    assert "ann: We ship on Friday." in data and "bob: Ann will deploy it." in data
    assert callnotes.unfinished(tmp_path) == []
    second.stop()


def test_a_call_that_ended_during_the_restart_is_still_posted(tmp_path):
    journal = callnotes.Journal(tmp_path, "10", "20")
    journal.start(time.time() - 600, "Aither", "", auto=True)
    journal.line(time.time() - 500, "ann", "We ship on Friday.", "1")
    filed = []
    callnotes.register_filer(lambda g, c, p, ctx: filed.append((g, c)) or [])
    bot = _Bot()
    voice = DiscordVoice(bot, VoiceConfig(enabled=True, state_dir=str(tmp_path)))
    voice.log = lambda **f: None
    thinker = _Thinker(json.dumps(_PLAN))
    voice.build_loop = lambda **kw: types.SimpleNamespace(thinker=thinker)
    assert voice.recover_all() == 1
    channel, _, data, content = bot.rest.files[0]
    assert channel == "20" and "ann: We ship on Friday." in data
    assert "the call ended while Aither was restarting" in data
    assert filed == [("10", "20")]
    assert callnotes.unfinished(tmp_path) == []
    assert voice.recover_all() == 0


def test_an_old_journal_is_posted_first_and_never_glued_to_a_new_call(tmp_path):
    journal = callnotes.Journal(tmp_path, "10", "20")
    journal.start(time.time() - 90000, "Aither", "", auto=True)
    journal.line(time.time() - 89000, "ann", "Yesterday's call.", "1")
    session, _, rest, _, _ = _session([], thinker=_Thinker(json.dumps(_PLAN)),
                                      state_dir=str(tmp_path))
    session.voice.build_loop = lambda **kw: types.SimpleNamespace(thinker=_Thinker("{}"))
    session.sync_call(1)
    assert "Yesterday's call." in rest.files[0][2]
    assert session.notes.lines == []
    assert _texts(rest)[-1] == callnotes.JOIN_NOTICE
    session.notes = None
    session.stop()


def test_a_spoken_instruction_is_filed_and_listed_mid_call():
    seen = []

    def filer(guild, channel, plan, context):
        seen.append((context["source"], context["actor"], [a["title"] for a in plan["actions"]]))
        return [{"title": a["title"], "owner": a["owner"], "link": "https://board/9",
                 "status": "filed"} for a in plan["actions"]]

    callnotes.register_filer(filer)
    one = {"title": "x", "decisions": [], "questions": [], "actions": [
        {"title": "Send Bob the deck", "detail": "today", "owner": "ann", "kind": "task"}]}
    session, thinker, rest, spoken, _ = _session(
        ["Aither, create a task for Ann to send Bob the deck"],
        thinker=_Thinker(json.dumps(one)))
    _say(session, 1, user="7", name="dave")
    _drain(session)
    session.stop()
    assert seen == [("spoken", "7", ["Send Bob the deck"])]
    assert "On it. I'll post it in the chat." in spoken
    post = [t for t in _texts(rest) if t.startswith("**Filed for dave**")][0]
    assert "1. Send Bob the deck - ann - <https://board/9>" in post
    assert not [a for a in thinker.asked if a[0] == "stream"]      # not a chat turn


def test_what_was_filed_mid_call_is_in_the_record_so_it_is_not_filed_twice():
    callnotes.register_filer(lambda g, c, p, ctx: [
        {"title": a["title"], "owner": "", "link": "", "status": "filed"}
        for a in p["actions"]])
    one = {"title": "x", "actions": [{"title": "Send Bob the deck", "owner": "ann"}]}
    session, _, _, _, _ = _session(["Aither, create a task to send Bob the deck"],
                                   thinker=_Thinker(json.dumps(one)))
    session.sync_call(2)
    _say(session)
    _drain(session)
    assert session.notes.lines[-1][1:] == ("Aither", "Filed: Send Bob the deck")
    assert "already filed" in callnotes.CALL_PROMPT
    session.notes = None
    session.stop()


def test_a_spoken_instruction_survives_a_model_that_returns_nothing():
    got = []

    def filer(guild, channel, plan, context):
        got.extend(plan["actions"])
        return [{"title": a["title"], "owner": "", "link": "",
                 "status": "decision" if a["needs_decision"] else "filed"}
                for a in plan["actions"]]

    callnotes.register_filer(filer)
    session, _, rest, _, _ = _session(["Aither, remind Bob to buy the domain"],
                                      thinker=_Thinker("I could not do that."))
    _say(session)
    _drain(session)
    session.stop()
    assert got[0]["title"] == "remind Bob to buy the domain"
    assert got[0]["needs_decision"] is True and got[0]["risk"] == "spend"
    assert "needs a decision, not done" in _texts(rest)[-1]


def test_a_reconnect_hands_the_open_notes_to_the_new_session():
    session, _, _, _, _ = _session([])
    session.sync_call(2)
    notes = session.detach_notes()
    assert notes is not None and session.notes is None
    session.stop()                               # nothing left to post


# ---------------------------------------------------------------------------
# The transport: who joins what
# ---------------------------------------------------------------------------


def _guild(gid, channels, bot_in=""):
    voice_channels = []
    for cid, humans in channels.items():
        members = [_member("p%d" % i) for i in range(humans)]
        if cid == bot_in:
            members.append(_member("aither", True))
        voice_channels.append(types.SimpleNamespace(id=cid, members=members))
    vc = None
    if bot_in:
        vc = types.SimpleNamespace(channel=types.SimpleNamespace(id=bot_in))
    return types.SimpleNamespace(id=gid, voice_channels=voice_channels, voice_client=vc,
                                 afk_channel=None)


def _transport(auto=("10",), standing=()):
    voice = DiscordVoice(_Bot(), VoiceConfig(enabled=True, auto_guilds=tuple(auto),
                                             join_channels=tuple(standing)))
    voice.log = lambda **f: None
    voice.client = types.SimpleNamespace(user=types.SimpleNamespace(id=999))
    calls = []

    async def join_channel(guild_id, channel_id):
        calls.append(("join", guild_id, channel_id))
        voice._sessions[guild_id] = types.SimpleNamespace(
            channel_id=channel_id, notes=None,
            sync_call=lambda n: calls.append(("sync", channel_id, n)))
        return True

    async def leave(guild_id):
        calls.append(("leave", guild_id))
        voice._sessions.pop(guild_id, None)

    voice.join_channel = join_channel
    voice.leave = leave
    voice._offload = lambda fn, *a: fn(*a)
    return voice, calls


def _state(channel_id=None):
    return types.SimpleNamespace(
        channel=None if channel_id is None else types.SimpleNamespace(id=channel_id))


def test_a_person_joining_voice_brings_the_bot_in_an_opted_in_server():
    voice, calls = _transport()
    guild = _guild(10, {"a": 1, "b": 0})
    human = types.SimpleNamespace(id=5, bot=False, guild=guild)
    asyncio.run(voice.on_voice_state_update(human, _state(), _state("a")))
    assert calls == [("join", "10", "a"), ("sync", "a", 1)]


def test_the_last_person_leaving_ends_the_call():
    voice, calls = _transport()
    voice._sessions["10"] = types.SimpleNamespace(channel_id="a", notes=None,
                                                  sync_call=lambda n: None)
    guild = _guild(10, {"a": 0, "b": 0}, bot_in="a")
    human = types.SimpleNamespace(id=5, bot=False, guild=guild)
    asyncio.run(voice.on_voice_state_update(human, _state("a"), _state()))
    assert calls == [("leave", "10")]


def test_with_a_standing_room_the_bot_stays_and_only_the_notes_close():
    voice, calls = _transport(standing=("a",))
    voice._sessions["10"] = types.SimpleNamespace(
        channel_id="a", notes=None, sync_call=lambda n: calls.append(("sync", "a", n)))
    guild = _guild(10, {"a": 0}, bot_in="a")
    human = types.SimpleNamespace(id=5, bot=False, guild=guild)
    asyncio.run(voice.on_voice_state_update(human, _state("a"), _state()))
    assert calls == [("sync", "a", 0)]


def test_a_server_that_did_not_opt_in_is_left_alone():
    voice, calls = _transport(auto=("10",))
    guild = _guild(77, {"a": 3})                          # a partner or community server
    human = types.SimpleNamespace(id=5, bot=False, guild=guild)
    asyncio.run(voice.on_voice_state_update(human, _state(), _state("a")))
    assert calls == []
    assert voice.auto("77") is False and voice.auto("10") is True


def test_another_bot_joining_voice_moves_nothing():
    voice, calls = _transport()
    guild = _guild(10, {"a": 0})
    other = types.SimpleNamespace(id=6, bot=True, guild=guild)
    asyncio.run(voice.on_voice_state_update(other, _state(), _state("a")))
    assert calls == []


def test_the_opt_in_is_read_from_the_server_store():
    store = GuildStore()
    bot = _Bot()
    bot.host = types.SimpleNamespace(store=store)
    voice = DiscordVoice(bot, VoiceConfig(enabled=True))
    assert voice.auto("10") is False
    callnotes.set_voice_calls(store, "10", True)
    assert voice.auto("10") is True and voice.auto("11") is False


def test_a_call_in_progress_at_startup_is_picked_up():
    voice, calls = _transport()
    voice.client.guilds = [_guild(10, {"a": 2}), _guild(77, {"z": 5})]
    asyncio.run(voice.on_ready())
    assert calls == [("join", "10", "a"), ("sync", "a", 2)]


# ---------------------------------------------------------------------------
# /calls: anyone can see what is listened to; only the bot's owner can switch it
# ---------------------------------------------------------------------------


class _CmdRest(_Rest):
    def __init__(self):
        super().__init__()
        self.answers = []

    def interaction_callback(self, iid, token, payload):
        self.answers.append(payload["data"])


def _calls(mode=None, user="5", owners=("1",)):
    bot = _Bot(_CmdRest())
    bot.owners = list(owners)
    bot.host = types.SimpleNamespace(store=GuildStore())
    voice = DiscordVoice(bot, VoiceConfig(enabled=True))
    voice.log = lambda **f: None
    voice.client = object()
    options = [{"name": "mode", "value": mode}] if mode else []
    handled = voice.intercept({"id": "i", "token": "t", "guild_id": "10",
                               "member": {"user": {"id": user}},
                               "data": {"name": "calls", "options": options}})
    assert handled
    return voice, bot.rest.answers[-1]


def test_calls_status_says_what_is_on_and_is_private():
    voice, answer = _calls()
    assert answer["flags"] == 64
    assert "Joins on her own: **off**" in answer["content"]
    assert "off the record" in answer["content"]
    assert "calls" in [c["name"] for c in voice.command_defs()]


def test_only_the_bots_owner_can_turn_calls_on():
    voice, answer = _calls("on", user="5")               # a server admin, not the owner
    assert "Only the bot's owner" in answer["content"]
    assert voice.auto("10") is False
    voice, answer = _calls("on", user="1")
    assert answer["content"].startswith("Saved.") and "**on**" in answer["content"]
    assert voice.auto("10") is True and voice.auto("11") is False
    assert "off" in voice.calls_command("10", "1", "off") and voice.auto("10") is False


def test_a_call_where_nothing_was_said_ends_quietly():
    session, _, rest, _, _ = _session([])
    session.sync_call(1)
    session.sync_call(0)
    _drain(session)
    session.stop()
    assert _texts(rest) == [callnotes.JOIN_NOTICE] and rest.files == []


def test_the_last_thing_said_lands_even_when_the_room_empties_mid_transcription():
    import threading

    gate = threading.Event()
    session, _, rest, _, _ = _session([], thinker=_Thinker(json.dumps(_PLAN)))

    def slow(pcm, sample_rate=16000):
        gate.wait(3)
        return "One last thing: ship Friday."

    session.loop.transcriber.transcribe = slow
    session.sync_call(2)
    _say(session)
    assert _wait(lambda: session._busy)
    session.sync_call(0)                         # everyone left while STT was running
    gate.set()
    _drain(session)
    session.stop()
    assert "bob: One last thing: ship Friday." in rest.files[0][2]


def test_startup_says_which_servers_run_their_own_calls_and_who_files(monkeypatch):
    import awdiscord.voice as voice_mod

    monkeypatch.setattr(voice_mod, "RECOVER_DELAY_S", 0.0)
    callnotes.register_filer(lambda *a: None)
    voice, _ = _transport()
    logs = []
    voice.log = lambda **f: logs.append(f)
    voice.client.guilds = [_guild(10, {}), _guild(77, {})]
    voice._recover_after_ready()
    ready = [f for f in logs if f.get("event") == "voice_calls_ready"][0]
    assert ready["auto_guilds"] == ["10"] and ready["filers"] == 1 and ready["keepers"] == 0
