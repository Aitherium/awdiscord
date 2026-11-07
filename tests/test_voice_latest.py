"""Only the newest utterance is answered; stale ones are dropped (no reply backlog)."""

import queue
import time
import types

from awdiscord.voice import _Session


def _session(stale_ms=10000):
    s = _Session.__new__(_Session)
    s.queue = queue.Queue()
    s.guild_id = "g"
    lines = []
    s.voice = types.SimpleNamespace(config=types.SimpleNamespace(stale_ms=stale_ms),
                                    log=lambda **f: lines.append(f))
    return s, lines


def test_a_backlog_answers_only_the_newest_utterance():
    s, lines = _session()
    now = time.monotonic()
    first = ("u", "n", b"one", now)
    s.queue.put(("u", "n", b"two", now))
    s.queue.put(("u", "n", b"three", now))
    assert s._latest(first)[2] == b"three"
    assert lines[0]["event"] == "voice_utterance_dropped" and lines[0]["skipped"] == 2


def test_an_utterance_older_than_stale_ms_is_dropped():
    s, lines = _session(stale_ms=1000)
    assert s._latest(("u", "n", b"old", time.monotonic() - 5)) is None
    assert lines[0]["stale"] is True


def test_a_fresh_single_utterance_passes_untouched():
    s, lines = _session()
    item = ("u", "n", b"hi", time.monotonic())
    assert s._latest(item) is item and lines == []
