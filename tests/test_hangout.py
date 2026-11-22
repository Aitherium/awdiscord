"""The avatar tap and spoken gestures (awdiscord.hangout) and how the voice transport uses them."""
import base64
import io
import json
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from awdiscord.hangout import AvatarFeed, parse_emote, pcm_to_wav
from awdiscord.voice import parse_command


@pytest.mark.parametrize("said,gesture", [
    ("dance", "DANCE"),
    ("do a little dance", "DANCE"),
    ("Can you do a little dance for us?", "DANCE"),
    ("please do a victory dance", "DANCE"),
    ("show us your moves", "DANCE"),
    ("wave", "GREETING"),
    ("wave hello", "GREETING"),
    ("take a bow", "GREETING"),
    ("celebrate", "HAPPY"),
    ("do finger guns", "FINGER_GUN"),
])
def test_spoken_gestures(said, gesture):
    assert parse_emote(said) == gesture
    assert parse_command(said) == ("emote", gesture)


@pytest.mark.parametrize("said", [
    "what is the best dance music for a party",
    "I saw them dance at the wedding last night and it went badly",
    "tell me about the history of the waltz",
    "can you celebrate the launch in the notes",
    "",
])
def test_a_question_that_mentions_dancing_stays_a_question(said):
    assert parse_emote(said) == ""
    assert parse_command(said)[0] != "emote"


def test_wav_container_is_readable():
    pcm = b"\x01\x00" * 1600
    with wave.open(io.BytesIO(pcm_to_wav(pcm))) as w:
        assert (w.getnchannels(), w.getframerate(), w.getnframes()) == (1, 16000, 1600)


class _Receiver:
    def __init__(self, status=200):
        self.got = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                outer.got.append((self.path, self.headers.get("Authorization", ""),
                                  json.loads(self.rfile.read(n))))
                self.send_response(status)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a):
                pass

        self.srv = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self):
        self.srv.shutdown()


def _wait(pred, s=8.0):
    end = time.time() + s
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


def test_feed_posts_clip_and_gesture_with_the_bearer():
    rx = _Receiver()
    try:
        feed = AvatarFeed(rx.url, "tok")
        assert feed.speak(b"\x00\x01" * 800, "hi")
        assert feed.emote("dance")
        assert _wait(lambda: len(rx.got) == 2)
        (p1, a1, b1), (p2, _, b2) = rx.got
        assert (p1, a1) == ("/speak", "Bearer tok")
        assert base64.b64decode(b1["audioBase64"])[:4] == b"RIFF"
        assert (p2, b2) == ("/emote", {"animation": "DANCE"})
        assert feed.status()["sent"] == 2
    finally:
        rx.close()


def test_unconfigured_feed_does_nothing_and_unknown_gesture_is_refused():
    off = AvatarFeed("")
    assert off.speak(b"\x00\x01" * 800) is False
    assert off.emote("DANCE") is False
    rx = _Receiver()
    try:
        assert AvatarFeed(rx.url).emote("BACKFLIP") is False
    finally:
        rx.close()


def test_a_dead_receiver_never_raises_and_is_counted():
    feed = AvatarFeed("http://127.0.0.1:9", timeout=0.5)     # nothing listens on :9
    assert feed.speak(b"\x00\x01" * 800) is True              # queued, not delivered
    assert _wait(lambda: feed.status()["failed"] == 1)
    assert "URLError" in feed.status()["last_error"] or feed.status()["last_error"]


def test_full_queue_drops_the_oldest_not_the_call():
    feed = AvatarFeed("http://127.0.0.1:9", timeout=0.5, maxsize=2)
    gate = threading.Event()
    feed._opener = lambda r, t: (gate.wait(5), (_ for _ in ()).throw(OSError("x")))[1]
    for _ in range(6):
        assert feed.emote("DANCE")
    assert feed.status()["dropped"] >= 3
    gate.set()


def test_set_target_validates_and_can_turn_off():
    feed = AvatarFeed("")
    feed.set_target("http://host.docker.internal:8365/", "t")
    assert feed.url == "http://host.docker.internal:8365" and feed.configured
    with pytest.raises(ValueError):
        feed.set_target("file:///etc/passwd")
    feed.set_target("")
    assert not feed.configured


def test_overlong_clip_is_not_forwarded():
    rx = _Receiver()
    try:
        feed = AvatarFeed(rx.url)
        assert feed.speak(b"\x00\x00" * 16000 * 45) is False
    finally:
        rx.close()


def test_pull_mode_holds_events_and_a_reader_gets_them_in_order():
    feed = AvatarFeed("pull")
    assert feed.pull and feed.configured and feed.status()["mode"] == "pull"
    assert feed.emote("dance")
    assert feed.speak(b"\x00\x01" * 800, "hi")
    got = feed.events_since(0, timeout=1.0)
    assert [e["kind"] for e in got["events"]] == ["emote", "speak"]
    assert got["next"] == 2
    # a reader that is caught up waits, then comes back empty with the same cursor
    t0 = time.monotonic()
    empty = feed.events_since(got["next"], timeout=0.3)
    assert empty == {"events": [], "next": 2} and time.monotonic() - t0 >= 0.25


def test_pull_reader_wakes_when_an_event_arrives_and_skips_stale_ones():
    feed = AvatarFeed("pull")
    threading.Timer(0.15, lambda: feed.emote("greeting")).start()
    got = feed.events_since(0, timeout=3.0)
    assert [e["body"]["animation"] for e in got["events"]] == ["GREETING"]
    time.sleep(0.12)
    assert feed.events_since(0, timeout=0.1, max_age_s=0.05)["events"] == []   # too old


def test_pull_mode_never_posts_anywhere():
    feed = AvatarFeed("pull")
    feed._opener = lambda r, t: (_ for _ in ()).throw(AssertionError("posted"))
    assert feed.speak(b"\x00\x01" * 800) and feed.emote("DANCE")
    time.sleep(0.2)
    assert feed.status()["failed"] == 0
