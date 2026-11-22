"""A face for the voice call: spoken emotes and the avatar tap.

Discord does not let a bot send video, so an agent that joins a voice channel cannot
show a body on its own. What it can do is hand its own voice, as it is spoken, to
whatever draws the body (a desktop avatar window that someone shares into the call,
a stream overlay), and take a spoken request for a gesture ("Aither, do a little
dance").  This module is that hand-off and nothing else; it has no Discord import and
no opinion about what listens on the other end.

* :class:`AvatarFeed` -- posts each clip the bot puts on the wire (16 kHz mono WAV) and
  each gesture to ONE configured URL, from a background thread.  A dead or slow
  receiver drops frames and is reported by :meth:`AvatarFeed.status`; it never delays
  the audio and never raises into the call.
* :func:`parse_emote` -- "dance", "wave", "take a bow", ... -> a gesture name, or ``""``.
  Narrow on purpose: a question that merely mentions dancing stays a question.

Two ways to deliver: PUSH (the URL is a receiver the bot can reach) and PULL (the URL is
the word ``pull``): events are held in a short ring and a reader on the bot's own inbox
long-polls ``GET /voice/avatar/events``.  Pull is for a bot in a container that cannot
reach the machine drawing the body; the reader runs where it can (``podman exec``).

Settings: ``AWDISCORD_AVATAR_URL`` (receiver base URL, ``http(s)://host:port``, or ``pull``) and
``AWDISCORD_AVATAR_TOKEN`` (bearer).  Both can also be set while the bot runs
(inbox ``POST /voice/avatar``) so pointing a call at a receiver needs no restart.
"""
from __future__ import annotations

import base64
import collections
import io
import json
import os
import queue
import re
import ssl
import threading
import time
import urllib.error
import urllib.request
import wave
from typing import Callable, Dict, Optional

#: Gesture names a receiver understands. The receiver maps them to its own clips.
GESTURES = ("DANCE", "GREETING", "HAPPY", "FINGER_GUN", "IDLE")

#: Longest clip posted (seconds of 16 kHz audio); a longer one is not forwarded.
MAX_CLIP_S = 40.0

_POLITE = r"(?:(?:please|can you|could you|would you|will you|go ahead and)\s+)*"
_ART = r"(?:a|an|the|your|some)?\s*"
_EMOTES = (
    ("DANCE", re.compile(
        r"^" + _POLITE + r"(?:(?:do|show|give)\s+(?:us\s+|me\s+)?" + _ART
        + r"(?:(?:little|quick|brief|small|short|victory|happy|silly)\s+)*dance"
        r"|dance|bust a move|cut a rug|show (?:us |me )?(?:your|some) moves)"
        r"(?:\s+(?:for us|for me|now|then|a bit|for everyone))?$", re.I)),
    ("GREETING", re.compile(
        r"^" + _POLITE + r"(?:wave(?:\s+(?:hello|hi|at us|to (?:everyone|us|the room|the group)))?"
        r"|say hello with your (?:hand|body)|take a bow)$", re.I)),
    ("HAPPY", re.compile(
        r"^" + _POLITE + r"(?:celebrate|cheer|do (?:a |your )?(?:happy|celebration) "
        r"(?:dance|animation|emote)|(?:show|act) (?:happy|excited))$", re.I)),
    ("FINGER_GUN", re.compile(
        r"^" + _POLITE + r"(?:do (?:a |the |your )?finger ?guns?|finger ?guns?)$", re.I)),
)


def parse_emote(text: str) -> str:
    """A spoken request (wake word already stripped) -> a gesture name, or ``""``."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"^[\W_]+|[\s.!?,;:]+$", "", t)
    if not t or len(t) > 80:
        return ""
    for name, pattern in _EMOTES:
        if pattern.match(t):
            return name
    return ""


def pcm_to_wav(pcm: bytes, rate: int = 16000) -> bytes:
    """16-bit mono PCM -> a WAV container (what a browser's decodeAudioData reads)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(pcm)
    return buf.getvalue()


class AvatarFeed:
    """Forward clips and gestures to one receiver without ever blocking the call.

    ``speak(pcm16k_mono)`` and ``emote(name)`` return at once. A worker thread posts
    them in order; when the queue is full the OLDEST frame is dropped (a mouth that
    lags is worse than one that skips). Every failure is counted, the last error is
    kept, and nothing is raised.
    """

    def __init__(self, url: str = "", token: str = "", *, ca_bundle: str = "",
                 timeout: float = 4.0, maxsize: int = 8,
                 log: Optional[Callable[..., None]] = None,
                 opener: Optional[Callable] = None) -> None:
        self._lock = threading.Lock()
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.timeout = timeout
        self._log = log
        self._opener = opener
        self._ctx = None
        if ca_bundle and os.path.exists(ca_bundle):
            self._ctx = ssl.create_default_context(cafile=ca_bundle)
        self._q: "queue.Queue[tuple]" = queue.Queue(maxsize=maxsize)
        self.sent = 0
        self.failed = 0
        self.dropped = 0
        self.last_error = ""
        self.last_ms = 0
        self.last_at = 0.0
        self._thread: Optional[threading.Thread] = None
        #: pull mode: a short ring of (seq, kind, body, queued_at); readers long-poll it.
        self._ring: "collections.deque" = collections.deque(maxlen=48)
        self._seq = 0
        self._cond = threading.Condition()

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_", **kw) -> "AvatarFeed":
        ca = os.getenv(prefix + "CA_BUNDLE", "")
        return cls(os.getenv(prefix + "AVATAR_URL", ""),
                   os.getenv(prefix + "AVATAR_TOKEN", ""), ca_bundle=ca, **kw)

    # -- configuration ------------------------------------------------------
    @property
    def configured(self) -> bool:
        return bool(self.url)

    @property
    def pull(self) -> bool:
        return self.url == "pull"

    def set_target(self, url: str, token: str = "") -> None:
        """Point the feed somewhere else (or nowhere: an empty url turns it off)."""
        url = (url or "").strip().rstrip("/")
        if url and url != "pull" and not re.match(r"^https?://[^\s/]+", url):
            raise ValueError("avatar url must be http(s)://host[:port] or pull")
        with self._lock:
            self.url = url
            self.token = token or ""
            self.last_error = ""

    # -- the two things the call hands over ----------------------------------
    def speak(self, pcm16k: bytes, text: str = "") -> bool:
        """One clip of the bot's own voice, as it goes on the wire."""
        if not self.configured or not pcm16k:
            return False
        if len(pcm16k) / 32000.0 > MAX_CLIP_S:
            return False
        return self._put(("speak", {"audioBase64": base64.b64encode(
            pcm_to_wav(pcm16k)).decode("ascii"), "format": "wav",
            "text": (text or "")[:400]}, time.monotonic()))

    def emote(self, gesture: str) -> bool:
        """One gesture request ("DANCE", ...)."""
        name = (gesture or "").strip().upper()
        if not self.configured or name not in GESTURES:
            return False
        return self._put(("emote", {"animation": name}, time.monotonic()))

    # -- plumbing -------------------------------------------------------------
    def _put(self, item: tuple) -> bool:
        if self.pull:
            kind, body, queued = item
            with self._cond:
                self._seq += 1
                self._ring.append((self._seq, kind, body, queued))
                self._cond.notify_all()
            self.sent += 1
            return True
        self._ensure_thread()
        while True:
            try:
                self._q.put_nowait(item)
                return True
            except queue.Full:
                try:
                    self._q.get_nowait()
                    self.dropped += 1
                except queue.Empty:
                    continue            # the worker took it first: room now, retry

    def _ensure_thread(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._run, name="awdiscord-avatar",
                                            daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                kind, body, queued = self._q.get(timeout=30.0)
            except queue.Empty:
                return                      # idle: the next frame restarts the thread
            self._post(kind, body, queued)

    def _post(self, kind: str, body: dict, queued: float) -> None:
        with self._lock:
            url, token = self.url, self.token
        if not url:
            return
        started = time.monotonic()
        req = urllib.request.Request(
            url + "/" + kind, data=json.dumps(body).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json",
                     **({"Authorization": "Bearer " + token} if token else {})})
        try:
            opener = self._opener or self._direct_open
            with opener(req, self.timeout) as resp:
                resp.read(2048)
            self.sent += 1
            self.last_error = ""
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.failed += 1
            self.last_error = (type(exc).__name__ + ": " + str(exc))[:160]
            if self._log is not None:
                try:
                    self._log(event="avatar_feed_error", kind=kind,
                              error=self.last_error)
                except Exception as log_exc:  # noqa: BLE001 - a logger must not stop the feed
                    self.last_error += " (log failed: " + type(log_exc).__name__ + ")"
        self.last_ms = int((time.monotonic() - started) * 1000)
        self.last_at = time.time()

    def events_since(self, since: int, timeout: float = 20.0,
                     max_age_s: float = 5.0) -> Dict[str, object]:
        """Pull mode: events newer than ``since``, waiting up to ``timeout`` for one.

        An event older than ``max_age_s`` is skipped (a mouth that lags the voice is
        worse than one that skips); ``next`` is the sequence number to ask for next.
        """
        deadline = time.monotonic() + max(0.0, min(float(timeout), 25.0))
        with self._cond:
            while self._seq <= since:
                left = deadline - time.monotonic()
                if left <= 0:
                    return {"events": [], "next": self._seq}
                self._cond.wait(left)
            now = time.monotonic()
            out = [{"seq": q, "kind": k, "body": b}
                   for q, k, b, t in self._ring if q > since and now - t <= max_age_s]
            return {"events": out, "next": self._seq}

    def _direct_open(self, req, timeout):
        """No system proxy: the receiver is a machine the operator named, never the web."""
        https = (urllib.request.HTTPSHandler(context=self._ctx) if self._ctx
                 else urllib.request.HTTPSHandler())
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), https).open(
            req, timeout=timeout)

    def status(self) -> Dict[str, object]:
        return {"configured": self.configured, "url": self.url,
                "mode": "pull" if self.pull else ("push" if self.configured else "off"),
                "sent": self.sent,
                "failed": self.failed, "dropped": self.dropped,
                "last_error": self.last_error, "last_post_ms": self.last_ms,
                "last_at": self.last_at}
