"""Discord voice: drag the bot into a channel and talk to it.

The transport half of :mod:`awvoice.loop`.  Discord delivers each speaker's Opus
as 48 kHz stereo PCM through ``discord.ext.voice_recv``; this module downmixes
that to the 16 kHz mono the STT door expects, end-points each speaker separately,
and hands the finished utterance to ONE shared ``VoiceLoop`` -- so every person
in the channel is talking to the same Aither, with the same memory of the last
few exchanges.

The answer is played back as PCM on the same voice connection.  TTS returns
MP3/WAV; ffmpeg (already in the bot image for attachment thumbnails) decodes it
to the 48 kHz stereo stream Discord accepts.

Streaming (``AWDISCORD_VOICE_STREAM``, on by default): the model's answer is read
from MicroScheduler's SSE and cut into sentences; each sentence is synthesized and
queued for playback as it lands, so sentence one plays while sentence two is still
being written and synthesized.  A turn whose model asks for a tool falls back to the
ordinary tool loop.  Barge-in (``AWDISCORD_VOICE_BARGE_IN``, on by default): anyone
speaking for ``AWDISCORD_VOICE_BARGE_IN_MS`` (250) over the answer stops playback,
drops the queued sentences and cancels the stream; their utterance is the next turn.
Discord delivers one stream per user, so the bot never hears itself.

Joining happens three ways, all of them opt-in:

* ``/join`` -- the invoker is pulled from their current voice channel.
* A drag -- dragging the bot between voice channels fires
  ``on_voice_state_update``; the bot re-joins where it was put.
* ``AWDISCORD_VOICE_JOIN`` -- a comma-separated channel-id list the bot sits in
  after it connects (a standing conversation room).

Leaving is ``/leave`` or moving the bot out of voice.  ``AWDISCORD_VOICE=1``
turns the whole transport on; without it the gateway behaves exactly as before
and neither PyNaCl nor the voice-receive extension is needed.
"""

from __future__ import annotations

import asyncio
import inspect
import io
import json
import os
import queue
import struct
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional

from awvoice.loop import (
    BargeInDetector,
    HttpSpeaker,
    HttpTranscriber,
    LoopConfig,
    SentenceChunker,
    VoiceLoop,
    VoiceTurn,
    split_sentences,
)

try:                       # Python 3.12 and earlier; removed in 3.13
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        import audioop
except ImportError:        # pragma: no cover - the container is 3.12 today
    audioop = None


def _env(*names: str, default: str = "") -> str:
    for name in names:
        value = os.getenv(name)
        if value:
            return value.strip()
    return default


def _flag(name: str) -> bool:
    return _env(name).lower() in ("1", "true", "yes", "on")


def _flag_on(name: str) -> bool:
    """A flag that is ON unless set to 0/false/no/off."""
    return _env(name).lower() not in ("0", "false", "no", "off")


@dataclass
class VoiceConfig:
    """Everything the transport reads from the environment, in one place."""
    enabled: bool = False
    stt_url: str = ""
    tts_url: str = ""
    voice: str = ""
    format: str = "mp3"
    tts_stream: bool = False
    ca_bundle: str = ""
    internal_key: str = ""
    persona: str = ""
    priority: str = "user"
    join_channels: tuple = ()
    speech_rms: float = 480.0
    silence_ms: int = 400
    min_speech_ms: int = 300
    max_utterance_ms: int = 30000
    history_turns: int = 8
    announce: bool = True
    stream: bool = True
    barge_in: bool = True
    barge_in_ms: int = 250
    sentence_chars: int = 200

    @classmethod
    def from_env(cls, prefix: str = "AWDISCORD_") -> "VoiceConfig":
        join = tuple(c.strip() for c in _env(prefix + "VOICE_JOIN").split(",")
                     if c.strip())
        ca = _env(prefix + "CA_BUNDLE", "AWDISCORD_CA_BUNDLE")
        return cls(
            enabled=_flag(prefix + "VOICE"),
            stt_url=_env(prefix + "VOICE_STT_URL", default=""),
            tts_url=_env(prefix + "VOICE_TTS_URL", default=""),
            voice=_env(prefix + "VOICE_TTS_VOICE", default=""),
            format=_env(prefix + "VOICE_TTS_FORMAT", default="mp3"),
            tts_stream=_flag(prefix + "VOICE_TTS_STREAM"),
            ca_bundle=ca,
            internal_key=_env("AITHER_INTERNAL_SECRET", default=""),
            persona=_env(prefix + "VOICE_PERSONA", prefix + "DEFAULT_PERSONA"),
            priority=_env(prefix + "VOICE_PRIORITY", prefix + "AGENT_PRIORITY",
                          default="user"),
            join_channels=join,
            speech_rms=float(_env(prefix + "VOICE_SPEECH_RMS", default="480")),
            silence_ms=int(_env(prefix + "VOICE_SILENCE_MS", default="400")),
            min_speech_ms=int(_env(prefix + "VOICE_MIN_SPEECH_MS", default="300")),
            max_utterance_ms=int(_env(prefix + "VOICE_MAX_UTTERANCE_MS",
                                      default="30000")),
            history_turns=int(_env(prefix + "VOICE_HISTORY_TURNS", default="8")),
            announce=not _flag(prefix + "VOICE_QUIET"),
            stream=_flag_on(prefix + "VOICE_STREAM"),
            barge_in=_flag_on(prefix + "VOICE_BARGE_IN"),
            barge_in_ms=int(_env(prefix + "VOICE_BARGE_IN_MS", default="250")),
            sentence_chars=int(_env(prefix + "VOICE_SENTENCE_CHARS", default="200")),
        )

    def loop_config(self) -> LoopConfig:
        return LoopConfig(speech_rms=self.speech_rms, silence_ms=self.silence_ms,
                          min_speech_ms=self.min_speech_ms,
                          max_utterance_ms=self.max_utterance_ms)


#: How a spoken answer is shaped. Heard, not read: short, plain, no markup.
_SPOKEN_STYLE = ("(You are speaking aloud in a voice channel. Answer in one to three short "
                 "sentences of plain speech: no markdown, lists, links or code.)")


class BotThinker:
    """The bot's own agent as a ``Thinker``: persona, tools and all.

    A voice turn must be the SAME turn a text message from that Discord user in that
    guild would be: the same agent (``Dispatcher.agent_for``), the same plugin context
    and tools, the same rate limit, and owner-only tools gated by the SPEAKER's Discord
    id -- never by anything the model said. ``set_speaker`` is called by the voice worker
    before each utterance (one worker per channel, so it is not a race), and the
    plugin/tool builder is the dispatcher's own ``_agent_kwargs`` rather than a second
    copy. The answer also lands in the dispatcher's last-answer cache, so ``/share``
    works after a spoken question exactly as it does after ``/ask``.

    Two things stay deliberately local to the voice path: the loop keeps its OWN turn
    history (a spoken follow-up sees the last voice exchanges, not the text channel's),
    and a file a tool writes into the turn's ``Outbox`` is not posted because a voice
    channel has no attachment surface.
    """

    def __init__(self, agent, persona: str = "", dispatcher=None, guild_id: str = "",
                 channel_id: str = "", stream: bool = True,
                 sentence_chars: int = 200) -> None:
        self.agent = agent
        self.persona = persona or None
        self.dispatcher = dispatcher
        self.guild_id = str(guild_id or "")
        self.channel_id = str(channel_id or "")
        self.user_id = ""
        self.user_name = ""
        self.stream = stream
        self.sentence_chars = sentence_chars
        #: Why the last ``think_stream`` was not streamed ("" = it was, or not tried).
        self.fallback_reason = ""

    def set_speaker(self, user_id: str, user_name: str = "") -> None:
        """Who is talking right now (Discord's event, never the model's claim)."""
        self.user_id = str(user_id or "")
        self.user_name = str(user_name or "")

    def think(self, text: str, *, history=()) -> str:
        plan = self._plan(text, history)
        if isinstance(plan, str):
            return plan
        agent, prompt, kwargs, user = plan
        answer = agent.ask(prompt, **kwargs)
        if user:
            # ``/share`` reads this cache: a spoken question must be shareable too.
            self.dispatcher._remember_answer(self.guild_id or None, user, text, answer)
        return answer

    def think_stream(self, text: str, *, history=(), cancel=None):
        """``think`` as sentences, read from the model's stream as they are written.

        The SAME turn as ``think`` (same gates, agent, tools, owner gating): only the
        delivery differs. An agent with ``ask_stream`` is streamed; when it raises
        before any text (the model asked for a tool, the stream failed) the turn is
        ``agent.ask`` with the same arguments, cut into sentences afterwards. The
        dispatcher's answer cache gets the full reply, even after a barge-in.
        """
        plan = self._plan(text, history)
        if isinstance(plan, str):
            yield from split_sentences(plan, self.sentence_chars)
            return
        agent, prompt, kwargs, user = plan
        spoken: List[str] = []
        self.fallback_reason = ""
        stream = getattr(agent, "ask_stream", None) if self.stream else None
        try:
            started = False
            if callable(stream):
                chunker = SentenceChunker(self.sentence_chars)
                try:
                    for piece in stream(prompt, cancel=cancel, **kwargs):
                        started = True
                        for sentence in chunker.feed(piece):
                            spoken.append(sentence)
                            yield sentence
                except Exception as exc:  # noqa: BLE001 - StreamFallbackError, broken stream
                    # Before any text: today's path below. After: keep what was said.
                    # The session logs the reason with the turn.
                    self.fallback_reason = f"{type(exc).__name__}: {exc}"[:160]
                if started:
                    for sentence in chunker.flush():
                        spoken.append(sentence)
                        yield sentence
                    return
            answer = agent.ask(prompt, **kwargs)      # today's path, unchanged
            for sentence in split_sentences(answer, self.sentence_chars):
                spoken.append(sentence)
                yield sentence
        finally:
            if user and spoken:
                self.dispatcher._remember_answer(
                    self.guild_id or None, user, text, " ".join(spoken))

    @staticmethod
    def _spoken(text: str) -> str:
        """The turn as the model sees it: the words, plus how a spoken answer is shaped.

        A voice reply is heard, not read, and every extra token is time the speaker
        waits. ``AWDISCORD_VOICE_STYLE`` replaces the hint; ``off`` sends the words alone.
        """
        style = _env("AWDISCORD_VOICE_STYLE", default=_SPOKEN_STYLE).strip()
        if not style or style.lower() in ("0", "off", "false", "no"):
            return text
        return style + "\n\n" + text

    def _plan(self, text: str, history=()):
        """A gated turn's spoken reply (str), else (agent, prompt, ask kwargs, user)."""
        # The text gate applies to voice too: a spoken turn is a model call, so it
        # resolves through the SAME brain predicate. A server that is not served
        # (or has opted out) gets a spoken pointer, never an answer.
        brain = None
        if self.dispatcher is not None:
            brain = self.dispatcher._brain_for(
                {"guild_id": self.guild_id or None,
                 "member": {"user": {"id": self.user_id}}})
            if getattr(brain, "mode", "owner") == "none":
                return ("I only answer with the server owner's model. To bring your own, "
                        "install awdiscord and run awdiscord setup.")
        if self.dispatcher is None or not self.user_id:
            return (self.agent, self._spoken(text),
                    {"persona": self.persona, "history": list(history or ())}, "")
        from awdiscord.dispatch import _accepts, clip
        from awdiscord.outbox import Outbox

        user = self.user_id
        # The SAME throttle a text turn passes through (``/ask``, a mention, a DM). It
        # bounds tool use as well as spend, and an open voice channel is easier to hammer
        # than a text channel, not harder.
        verdict = self.dispatcher.limiter.check(user, self.guild_id or None)
        if not verdict.allowed:
            return f"Slow down - try again in {verdict.retry_after:g}s."
        agent = brain.agent if brain is not None else None
        if agent is None:
            return ("I only answer with the server owner's model. To bring your own, "
                    "install awdiscord and run awdiscord setup.")
        host = self.dispatcher.host
        context = host.persona_for(self.guild_id) if host is not None else ""
        kwargs = self.dispatcher._agent_kwargs(
            self.guild_id, context, user, agent, text,
            channel=self.channel_id, outbox=Outbox(), brain=brain)
        if self.persona and _accepts(agent.ask, "persona"):
            kwargs["persona"] = self.persona
        past = list(history or ())
        if past and _accepts(agent.ask, "history"):
            kwargs["history"] = past
        kwargs["user"] = user
        return agent, self._spoken(clip(text, 1500)), kwargs, user


# ---------------------------------------------------------------------------
# PCM helpers
# ---------------------------------------------------------------------------

_INBOUND_STATS = {"raw": 0, "no_user": 0, "packets": 0, "dave_decrypted": 0,
                  "dave_failed": 0, "decoded": 0}


def _inbound_pcm(vc, user, data, decoders: dict) -> bytes:
    """Inbound Opus -> 48 kHz stereo PCM, DAVE-decrypting first when the call is E2EE.

    Returns b"" (and counts why) instead of raising: a sink that raises stops
    voice_recv's router for every speaker."""
    opus = getattr(data, "opus", None)
    if not opus:
        return getattr(data, "pcm", b"") or b""
    _INBOUND_STATS["packets"] += 1
    conn = getattr(vc, "_connection", None)
    dave = getattr(conn, "dave_session", None)
    if dave is not None and getattr(conn, "dave_protocol_version", 0):
        try:
            import davey  # noqa: PLC0415 - only present when DAVE is negotiated
            opus = dave.decrypt(int(user.id), davey.MediaType.audio, bytes(opus))
            _INBOUND_STATS["dave_decrypted"] += 1
        except Exception as exc:  # noqa: BLE001 - passthrough frames / not-yet-ready epochs
            # A session that is not ready (or allows passthrough for this user) is
            # receiving UNENCRYPTED Opus; decode it as-is instead of dropping it.
            _INBOUND_STATS["dave_error"] = f"{type(exc).__name__}: {exc}"[:160]
            _INBOUND_STATS["dave_uid"] = str(getattr(user, "id", ""))
            # Discord ends every talk spurt with plaintext Opus silence frames; dropping
            # them meant the segmenter never heard speech END, so no utterance ever closed.
            passthrough = "Unencrypted" in str(exc)
            try:
                passthrough = passthrough or (not getattr(dave, "ready", False)) or bool(
                    dave.can_passthrough(int(user.id)))
            except Exception:  # noqa: BLE001
                passthrough = not getattr(dave, "ready", False)
            if not passthrough:
                _INBOUND_STATS["dave_failed"] += 1
                _INBOUND_STATS["dave_error"] = f"{type(exc).__name__}: {exc}"[:160]
                _INBOUND_STATS["dave_status"] = str(getattr(dave, "status", ""))[:60]
                return b""
            _INBOUND_STATS["dave_passthrough"] = _INBOUND_STATS.get("dave_passthrough", 0) + 1
    try:
        from discord.opus import Decoder  # noqa: PLC0415
        dec = decoders.get(user.id)
        if dec is None:
            dec = decoders[user.id] = Decoder()
        pcm = dec.decode(bytes(opus), fec=False)
        _INBOUND_STATS["decoded"] += 1
        return pcm
    except Exception:  # noqa: BLE001 - one bad frame must not kill the stream
        return b""


def _resolve_speaker(vc, data):
    """The member behind an RTP stream voice_recv has not mapped yet.

    voice_recv learns ssrc -> user from SPEAKING; when that never arrives the router
    hands the sink user=None for every packet, and DAVE cannot decrypt without the
    user id. Use the ssrc map if it has filled since, else the channel's only human."""
    if vc is None:
        return None
    ssrc = getattr(getattr(data, "packet", None), "ssrc", None)
    try:
        uid = vc._get_id_from_ssrc(ssrc) if ssrc is not None else None  # noqa: SLF001
        if uid is not None:
            return vc.guild.get_member(uid)
        humans = [m for m in getattr(vc.channel, "members", []) if not m.bot]
        if len(humans) == 1:
            if ssrc is not None:
                vc._add_ssrc(humans[0].id, ssrc)  # noqa: SLF001 - teach the router too
            return humans[0]
    except Exception:  # noqa: BLE001 - a lookup must never stop the router
        return None
    return None


def downmix_resample(pcm: bytes, in_rate: int = 48000, out_rate: int = 16000,
                     channels: int = 2) -> bytes:
    """48 kHz stereo int16 -> ``out_rate`` mono int16, for the STT door."""
    if not pcm:
        return b""
    if audioop is not None:
        if channels == 2:
            pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
        if in_rate != out_rate:
            pcm, _ = audioop.ratecv(pcm, 2, 1, in_rate, out_rate, None)
        return pcm
    # Pure-python fallback (3.13 removed audioop): average the channels, then
    # pick every step-th frame.  Good enough for speech; the container is 3.12.
    step = max(1, round(in_rate / out_rate))
    frames = len(pcm) // (2 * channels)
    out = bytearray()
    for i in range(0, frames, step):
        base = i * 2 * channels
        sample = 0
        for c in range(channels):
            sample += struct.unpack_from("<h", pcm, base + 2 * c)[0]
        out += struct.pack("<h", int(sample / channels))
    return bytes(out)


def decode_to_pcm48k(audio: bytes, audio_format: str = "") -> bytes:
    """Any audio ffmpeg can read -> 48 kHz stereo int16, Discord's play format."""
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
           "-f", "s16le", "-acodec", "pcm_s16le", "-ac", "2", "-ar", "48000",
           "pipe:1"]
    try:
        proc = subprocess.run(cmd, input=audio, capture_output=True, timeout=120)
    except FileNotFoundError as exc:       # pragma: no cover - image has ffmpeg
        raise RuntimeError("ffmpeg is required to play synthesized audio") from exc
    if proc.returncode != 0 or not proc.stdout:
        detail = proc.stderr.decode("utf-8", "replace")[:300]
        raise RuntimeError("could not decode " + (audio_format or "audio")
                           + ": " + detail)
    return proc.stdout


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

class _SinkProtocol:
    """What the sink needs from a session (so tests need no discord import)."""
    def on_audio(self, user_id: str, user_name: str, pcm16k: bytes) -> None:
        raise NotImplementedError


class _Player:
    """Plays decoded clips one after another on a voice connection.

    ``start(pcm, after)`` puts one clip on the wire and calls ``after`` when it ends
    (``vc.play(..., after=)``); ``stop()`` cuts the clip playing (``vc.stop()``).
    ``cancel`` is barge-in: it bumps the generation, drops every queued clip and stops
    the one playing, so a clip enqueued for the old generation never plays.
    """

    def __init__(self, start, stop, on_error=None) -> None:
        self._start = start
        self._stop = stop
        self._on_error = on_error
        self._q: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._gen = 0
        self._pending = 0
        self._closed = threading.Event()
        threading.Thread(target=self._run, name="awdiscord-voice-play",
                         daemon=True).start()

    @property
    def active(self) -> bool:
        """A clip is playing or queued (Aither is talking)."""
        return self._pending > 0

    def generation(self) -> int:
        return self._gen

    def enqueue(self, pcm: bytes, gen: int, on_start=None) -> bool:
        with self._lock:
            if gen != self._gen or self._closed.is_set():
                return False
            self._pending += 1
        self._q.put((gen, pcm, on_start))
        return True

    def cancel(self) -> int:
        """Drop the queue and stop the clip playing; the number of clips dropped."""
        dropped = 0
        with self._lock:
            self._gen += 1
            while True:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    break
                dropped += 1
                self._pending = max(0, self._pending - 1)
        self._safe_stop()
        return dropped

    def close(self) -> None:
        self._closed.set()
        self.cancel()

    def _safe_stop(self) -> None:
        try:
            self._stop()
        except Exception as exc:  # noqa: BLE001 - a dead connection must not raise
            self._report("stop", exc)

    def _report(self, where: str, exc: Exception) -> None:
        if self._on_error is not None:
            try:
                self._on_error(where, exc)
            except Exception as cb_exc:  # noqa: BLE001 - a broken reporter must not stop audio
                print("awdiscord: voice play reporter failed: " + repr(cb_exc),
                      file=sys.stderr)

    def _run(self) -> None:
        while not self._closed.is_set():
            try:
                gen, pcm, on_start = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                if gen != self._gen:
                    continue
                done = threading.Event()
                try:
                    self._start(pcm, lambda *_: done.set())
                except Exception as exc:  # noqa: BLE001 - skip the clip, keep the queue
                    self._report("play", exc)
                    continue
                if on_start is not None:
                    on_start()
                while not done.wait(0.05):
                    if gen != self._gen or self._closed.is_set():
                        self._safe_stop()      # cancelled between dequeue and start
                        break
            finally:
                with self._lock:
                    self._pending = max(0, self._pending - 1)


class _Session:
    """One connected voice channel: the sink, the worker, the shared loop."""

    def __init__(self, voice: "DiscordVoice", guild_id: str, channel_id: str,
                 loop: VoiceLoop) -> None:
        self.voice = voice
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.loop = loop
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self._stop = threading.Event()
        self._cancel: Optional[threading.Event] = None   # the turn being spoken
        self._barged = False
        self._detectors: Dict[str, BargeInDetector] = {}
        self.player = _Player(
            lambda pcm, after: voice._clip_start(guild_id, pcm, after),
            lambda: voice._clip_stop(guild_id),
            on_error=lambda where, exc: voice.log(
                event="voice_play_error", guild=guild_id, where=where,
                error=type(exc).__name__, detail=str(exc)[:200]))
        self._worker = threading.Thread(target=self._run, name="awdiscord-voice",
                                        daemon=True)
        self._worker.start()

    def submit(self, user_id: str, user_name: str, pcm: bytes) -> None:
        self.queue.put((user_id, user_name, pcm, time.monotonic()))

    # -- barge-in ------------------------------------------------------------
    def heard(self, user_id: str, pcm16k: bytes) -> bool:
        """Inbound speech (16 kHz mono) from ``user_id``; True when it barged in."""
        config = self.voice.config
        user_id = str(user_id)
        if not config.barge_in or not self.player.active:
            det = self._detectors.get(user_id)
            if det is not None:
                det.reset()
            return False
        det = self._detectors.get(user_id)
        if det is None:
            det = self._detectors[user_id] = BargeInDetector(
                config.speech_rms, config.barge_in_ms)
        if det.feed(pcm16k):
            self.barge_in(user_id)
            return True
        return False

    def barge_in(self, user_id: str = "") -> None:
        """Stop talking: cancel the stream, drop the queued sentences, stop playback."""
        cancel = self._cancel
        if cancel is not None:
            cancel.set()
        self._barged = True
        dropped = self.player.cancel()
        self.voice.log(event="voice_barge_in", guild=self.guild_id, user=user_id,
                       dropped=dropped)

    # -- the worker ----------------------------------------------------------
    def _streams(self) -> bool:
        return bool(self.voice.config.stream
                    and callable(getattr(self.loop, "handle_utterance_stream", None)))

    def _stream_turn(self, pcm: bytes, heard_at: float):
        """Sentence -> TTS -> the playback queue, while the model is still writing."""
        player = self.player
        gen = player.generation()
        cancel = threading.Event()
        first = threading.Event()
        stamp: Dict[str, int] = {}
        self._cancel = cancel
        self._barged = False

        def started() -> None:
            if not first.is_set():
                stamp["first_audio_ms"] = int((time.monotonic() - heard_at) * 1000)
                first.set()

        def emit(sentence: str, audio: bytes, audio_format: str):
            if cancel.is_set() or player.generation() != gen:
                return False
            if not audio:
                return True
            pcm48 = self.voice._decode(audio, audio_format)
            if cancel.is_set():
                return False
            return player.enqueue(pcm48, gen, started)

        try:
            turn = self.loop.handle_utterance_stream(
                pcm, emit=emit, cancel=cancel,
                max_chars=self.voice.config.sentence_chars)
        finally:
            self._cancel = None
        if turn is None:
            return None
        if turn.timings.get("sentences") and not cancel.is_set():
            first.wait(5.0)                 # a one-sentence reply may not be on air yet
        if "first_audio_ms" in stamp:
            turn.timings["first_audio_ms"] = stamp["first_audio_ms"]
        turn.barged_in = bool(turn.barged_in or self._barged)
        return turn

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                user_id, user_name, pcm, heard_at = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self.loop.user = user_name
            # The speaker's Discord id decides the tools (owner-only stays owner-only),
            # exactly as it does for a text message from the same user (2026-11-01).
            bind = getattr(getattr(self.loop, "thinker", None), "set_speaker", None)
            if callable(bind):
                bind(user_id, user_name)
            streams = self._streams()
            try:
                if streams:
                    turn = self._stream_turn(pcm, heard_at)
                else:
                    turn = self.loop.handle_utterance(pcm)
            except Exception as exc:       # a failed turn keeps the channel open
                self.voice.log(event="voice_turn_error", user=user_name,
                               error=type(exc).__name__, detail=str(exc)[:200])
                continue
            if turn is None:
                continue
            if streams:
                fallback = getattr(getattr(self.loop, "thinker", None),
                                   "fallback_reason", "")
                self.voice.log(event="voice_turn", user=user_name,
                               heard=turn.transcript[:160], said=turn.reply[:160],
                               streamed=not fallback, fallback=fallback or None,
                               barged_in=turn.barged_in, **turn.timings)
                continue
            self.voice.log(event="voice_turn", user=user_name,
                           heard=turn.transcript[:160], said=turn.reply[:160],
                           audio_bytes=len(turn.audio))
            self.voice.play_turn(self.guild_id, turn)

    def stop(self) -> None:
        self._stop.set()
        self.player.close()


class DiscordVoice:
    """The bot's voice side.  ``attach`` once; the event hooks are registered there."""

    def __init__(self, bot, config: VoiceConfig) -> None:
        self.bot = bot
        self.config = config
        self.client = None
        self._sessions: Dict[str, _Session] = {}
        self._connecting: Dict[str, asyncio.Future] = {}
        self._seg_buffers: Dict[str, object] = {}   # handled inside the sink
        self._started_at = time.time()

    # -- construction --------------------------------------------------------
    @classmethod
    def from_env(cls, bot, prefix: str = "AWDISCORD_") -> Optional["DiscordVoice"]:
        config = VoiceConfig.from_env(prefix)
        if not config.enabled:
            return None
        if not config.stt_url or not config.tts_url:
            raise RuntimeError(
                "AWDISCORD_VOICE=1 needs AWDISCORD_VOICE_STT_URL and "
                "AWDISCORD_VOICE_TTS_URL (the AitherVoice doors)")
        return cls(bot, config)

    def build_loop(self, persona: str = "", guild_id: str = "",
                   channel_id: str = "") -> VoiceLoop:
        headers = {}
        if self.config.internal_key:
            headers["X-Internal-Key"] = self.config.internal_key
        transcriber = HttpTranscriber(self.config.stt_url, headers=headers,
                                      ca_bundle=self.config.ca_bundle or None)
        speaker = HttpSpeaker(self.config.tts_url, voice=self.config.voice,
                              format=self.config.format, headers=headers,
                              ca_bundle=self.config.ca_bundle or None,
                              stream=self.config.tts_stream)
        # Same turn a text message gets: the dispatcher builds the tools/context and
        # the SPEAKER's id decides owner-only (the worker binds it per utterance).
        thinker = BotThinker(self.bot.agent, persona or self.config.persona,
                             dispatcher=getattr(self.bot, "dispatcher", None),
                             guild_id=guild_id, channel_id=channel_id,
                             stream=self.config.stream,
                             sentence_chars=self.config.sentence_chars)
        return VoiceLoop(transcriber, thinker, speaker,
                         config=self.config.loop_config(),
                         history_turns=self.config.history_turns,
                         on_error=lambda where, exc: self.log(
                             event="voice_error", where=where,
                             error=type(exc).__name__, detail=str(exc)[:200]))

    def attach(self, client) -> None:
        """Remember the connected client; commands can now be scheduled on its loop."""
        self.client = client

    def log(self, **fields) -> None:
        try:
            self.bot._log_voice(**fields)     # the gateway's JSON logger
        except Exception:
            print(json.dumps({"voice": True, **fields}, sort_keys=True), flush=True)

    # -- commands ------------------------------------------------------------
    def command_defs(self) -> List[dict]:
        return [
            {"name": "join", "description": "Bring Aither into your voice channel",
             "type": 1, "contexts": [0], "integration_types": [0],
             "options": [{"type": 7, "name": "channel", "required": False,
                          "description": "A voice channel (default: yours)"}]},
            {"name": "leave", "description": "Make Aither leave voice",
             "type": 1, "contexts": [0], "integration_types": [0]},
        ]

    def intercept(self, interaction: dict) -> bool:
        """Handle /join or /leave before the text dispatcher sees them (blocking)."""
        data = interaction.get("data") or {}
        name = str(data.get("name") or "")
        if name not in ("join", "leave"):
            return False
        guild_id = str(interaction.get("guild_id") or "")
        user = ((interaction.get("member") or {}).get("user") or {}).get("id") or ""
        if not guild_id:
            self._answer(interaction, "Voice commands only work inside a server.")
            return True
        if self.client is None:
            self._answer(interaction, "The bot is still starting; try again in a moment.")
            return True
        if name == "leave":
            self._schedule(self.leave(guild_id))
            self._answer(interaction, "Leaving voice.")
            return True
        channel_id = ""
        for opt in data.get("options") or []:
            if opt.get("name") == "channel" and opt.get("value"):
                channel_id = str(opt["value"])
        self._schedule(self.join_from_interaction(guild_id, str(user), channel_id))
        self._answer(interaction, "Joining voice — talk whenever you are ready.")
        return True

    async def join_from_interaction(self, guild_id: str, user_id: str,
                                    channel_id: str = "") -> None:
        if not channel_id:
            guild = self.client.get_guild(int(guild_id))
            member = guild.get_member(int(user_id)) if guild and user_id else None
            channel = getattr(getattr(member, "voice", None), "channel", None)
            if channel is None:
                await self._announce(guild_id, "Join a voice channel first, then `/join`.")
                return
            channel_id = str(channel.id)
        await self.join_channel(guild_id, channel_id)

    def _answer(self, interaction: dict, content: str) -> None:
        try:
            self.bot.rest.interaction_callback(
                interaction["id"], interaction["token"],
                {"type": 4, "data": {"content": content[:1900],
                                     "allowed_mentions": {"parse": []}}})
        except Exception as exc:
            self.log(event="voice_answer_error", error=type(exc).__name__,
                     detail=str(exc)[:200])

    def _schedule(self, coro) -> None:
        if self.client is None:
            return
        asyncio.run_coroutine_threadsafe(coro, self.client.loop)

    async def _announce(self, guild_id: str, content: str) -> None:
        # A voice channel has no text surface; say it in the last text channel the
        # bot can post in, or just log it when there is none.
        self.log(event="voice_announce", guild=guild_id, text=content[:200])

    # -- join / leave --------------------------------------------------------
    async def join_channel(self, guild_id: str, channel_id: str) -> bool:
        from discord.ext import voice_recv
        guild = self.client.get_guild(int(guild_id))
        if guild is None:
            self.log(event="voice_no_guild", guild=guild_id)
            return False
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            self.log(event="voice_no_channel", guild=guild_id, channel=channel_id)
            return False
        pending = self._connecting.get(guild_id)
        if pending is not None and not pending.done():
            return False
        current = guild.voice_client
        if current is not None and getattr(current, "channel", None) is not None \
                and current.channel.id == channel.id and guild_id in self._sessions:
            return True
        try:
            if current is not None and getattr(current, "channel", None) is not None:
                await current.move_to(channel)
                vc = current
            else:
                vc = await channel.connect(cls=voice_recv.VoiceRecvClient,
                                           self_deaf=False, self_mute=False)
            # ``connect`` RETURNS before the voice websocket is ready; attaching a
            # sink to it answers ClientException("Not connected to voice") and the
            # channel stays silent (2026-11-01 17:56).
            wait = getattr(vc, "wait_until_connected", None)
            if wait is not None:
                try:
                    # discord.py 2.7 made this a BLOCKING sync call returning a bool
                    # (awaiting it raised TypeError, so the sink attached before the
                    # socket was ready). Older builds had a coroutine. Run the sync
                    # form on a worker thread so the gateway heartbeat keeps going.
                    if inspect.iscoroutinefunction(wait):
                        ready = await wait(timeout=30.0)
                    else:
                        # blocking-ok: wait_until_connected only waits on an Event and
                        # reads a bool; it schedules no loop work, so a thread is safe.
                        ready = await asyncio.to_thread(wait, 30.0)
                    if ready is False:
                        self.log(event="voice_connect_wait", guild=guild_id,
                                 error="Timeout", detail="voice socket not ready after 30s")
                except Exception as exc:
                    self.log(event="voice_connect_wait", guild=guild_id,
                             error=type(exc).__name__, detail=str(exc)[:200])
        except Exception as exc:
            self.log(event="voice_join_error", guild=guild_id,
                     channel=channel_id, error=type(exc).__name__,
                     detail=str(exc)[:200])
            return False
        self._start_session(guild_id, channel_id, vc)
        self.log(event="voice_joined", guild=guild_id, channel=channel_id,
                 user=str(getattr(self.client.user, "id", "")))
        return True

    def _start_session(self, guild_id: str, channel_id: str, vc) -> None:
        old = self._sessions.pop(guild_id, None)
        if old is not None:
            old.stop()
        loop = self.build_loop(guild_id=str(guild_id), channel_id=str(channel_id))
        session = _Session(self, guild_id, channel_id, loop)
        session.vc = vc
        self._sessions[guild_id] = session
        # A second join for the same guild (the auto-join and a voice-state event can
        # both fire, 2026-11-06) stopped the old session above while ITS sink was still
        # attached; listen() then raised "Already receiving audio", the new session got
        # no sink and no watcher, and the channel was deaf. Detach the old sink first.
        attached = False
        try:
            is_listening = getattr(vc, "is_listening", None)
            if callable(is_listening) and is_listening():
                vc.stop_listening()
            vc.listen(self._make_sink(session))
            attached = True
        except Exception as exc:
            self.log(event="voice_listen_error", guild=guild_id,
                     error=type(exc).__name__, detail=str(exc)[:200])
        conn = getattr(vc, "_connection", None)
        dave = getattr(conn, "dave_session", None)
        if not attached:
            # The watcher retries the attach every tick, so a failed listen heals.
            self._watch_inbound(guild_id, session, attached=False)
            return
        self.log(event="voice_listen_attached", guild=guild_id,
                 dave=bool(dave), dave_ready=bool(getattr(dave, "ready", False)),
                 dave_version=int(getattr(conn, "dave_protocol_version", 0) or 0),
                 voice=self.config.voice or "(service default)")
        self._watch_inbound(guild_id, session)

    def _watch_inbound(self, guild_id: str, session: "_Session",
                       attached: bool = True) -> None:
        """Report the inbound pipeline counters, so a silent channel names its stage.
        packets -> dave_decrypted/dave_failed -> decoded -> utterance -> turn."""

        def _tick() -> None:
            nonlocal attached
            while not session._stop.is_set():       # noqa: SLF001 - same package
                session._stop.wait(10.0)            # noqa: SLF001
                if session._stop.is_set():
                    break
                vc = getattr(session, "vc", None)   # noqa: SLF001
                connected = bool(vc is not None and vc.is_connected())
                if connected and not attached:
                    # A voice reconnect builds a NEW connection; the sink's reader
                    # is gone with the old one, so re-attach or the channel is silent.
                    try:
                        if callable(getattr(vc, "is_listening", None)) and vc.is_listening():
                            vc.stop_listening()
                        vc.listen(self._make_sink(session))
                        attached = True
                        self.log(event="voice_listen_reattached", guild=guild_id)
                    except Exception as exc:
                        self.log(event="voice_listen_error", guild=guild_id,
                                 error=type(exc).__name__, detail=str(exc)[:200])
                elif not connected:
                    attached = False
                dave_state = {}
                try:
                    conn = getattr(vc, "_connection", None)
                    ds = getattr(conn, "dave_session", None)
                    if ds is not None:
                        dave_state = {"dave_ready_now": bool(ds.ready),
                                      "dave_status_now": str(ds.status)[:40],
                                      "dave_epoch": int(getattr(ds, "epoch", 0) or 0),
                                      "dave_users": len(ds.get_user_ids() or []),
                                      "dave_user_ids": [str(u) for u in ds.get_user_ids() or []],
                                      "dave_can_pt": {str(u): bool(ds.can_passthrough(int(u)))
                                                      for u in ds.get_user_ids() or []}}
                except Exception as exc:  # noqa: BLE001 - stats must never raise
                    dave_state = {"dave_state_error": type(exc).__name__}
                self.log(event="voice_inbound_stats", guild=guild_id,
                         connected=connected, **_INBOUND_STATS, **dave_state)

        threading.Thread(target=_tick, name="awdiscord-voice-stats",
                         daemon=True).start()

    def _make_sink(self, session: _Session):
        from discord.ext import voice_recv
        voice = self

        class Sink(voice_recv.AudioSink):
            def __init__(self) -> None:
                super().__init__()
                self._segments = {}
                self._decoders = {}
                self._names = {}
                self._last_audio = {}
                self._lock = threading.Lock()
                self._closed = threading.Event()
                # Discord stops SENDING the moment a speaker goes quiet, so the energy
                # end-pointer never sees the trailing silence it waits for and a short
                # question is held forever (2026-11-06: 28 frames decoded, no turn).
                # Two closers: voice_recv's speaking-stop event, and this idle timer as a
                # backstop for when that event never fires (an unmapped ssrc).
                threading.Thread(target=self._idle_closer, daemon=True,
                                 name="awdiscord-voice-idle").start()

            def _close(self, user_id) -> None:
                """Hand the speaker's in-progress utterance to the session, if any."""
                with self._lock:
                    seg = self._segments.get(user_id)
                    self._last_audio.pop(user_id, None)
                    name = self._names.get(user_id, str(user_id))
                    utterance = seg.flush() if seg is not None else None
                if utterance:
                    _INBOUND_STATS["flushed"] = _INBOUND_STATS.get("flushed", 0) + 1
                    session.submit(str(user_id), name, utterance)

            def _idle_closer(self) -> None:
                idle = max(0.2, voice.config.silence_ms / 1000.0)
                while not self._closed.wait(0.1):
                    now = time.monotonic()
                    with self._lock:
                        quiet = [u for u, t in self._last_audio.items() if now - t >= idle]
                    for user_id in quiet:
                        self._close(user_id)

            @voice_recv.AudioSink.listener()
            def on_voice_member_speaking_stop(self, member) -> None:
                if member is not None and not getattr(member, "bot", False):
                    self._close(member.id)

            def wants_opus(self) -> bool:
                # Opus, not PCM: discord.py negotiates DAVE end-to-end encryption
                # (davey installed) but voice_recv cannot decrypt it, so its own
                # decode got ciphertext and every inbound packet was dropped
                # (2026-11-01: 0 turns while the owner spoke). We DAVE-decrypt,
                # then decode Opus ourselves.
                return True

            def write(self, user, data) -> None:
                # Count BEFORE any filter: "packets 0" used to mean both "no audio"
                # and "audio from a speaker voice_recv never mapped" (2026-11-06).
                _INBOUND_STATS["raw"] += 1
                if user is None:
                    user = _resolve_speaker(getattr(self, "voice_client", None), data)
                    if user is None:
                        _INBOUND_STATS["no_user"] += 1
                        return
                if getattr(user, "bot", False):
                    return
                me = getattr(voice.client, "user", None)
                if me is not None and getattr(user, "id", None) == me.id:
                    return
                pcm = _inbound_pcm(getattr(self, "voice_client", None), user, data, self._decoders)
                if not pcm:
                    return
                from awvoice.loop import SpeechSegmenter
                mono = downmix_resample(pcm, 48000, 16000, 2)
                with self._lock:
                    seg = self._segments.get(user.id)
                    if seg is None:
                        seg = self._segments[user.id] = SpeechSegmenter(
                            voice.config.loop_config())
                    self._names[user.id] = str(user.name)
                    self._last_audio[user.id] = time.monotonic()
                    done = seg.feed(mono)
                # Speech over Aither's answer stops it (barge-in); the same audio
                # keeps feeding the segmenter, so what they said is the next turn.
                try:
                    session.heard(str(user.id), mono)
                except Exception as exc:  # noqa: BLE001 - the sink must never raise
                    _INBOUND_STATS["barge_in_error"] = type(exc).__name__
                for utterance in done:
                    session.submit(str(user.id), str(user.name), utterance)

            def cleanup(self) -> None:
                self._closed.set()
                with self._lock:
                    self._segments.clear()
                    self._decoders.clear()
                    self._last_audio.clear()

        return Sink()

    async def leave(self, guild_id: str) -> None:
        session = self._sessions.pop(guild_id, None)
        if session is not None:
            session.stop()
        guild = self.client.get_guild(int(guild_id))
        vc = getattr(guild, "voice_client", None)
        if vc is not None:
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect(force=True)
            except Exception as exc:
                self.log(event="voice_leave_error", guild=guild_id,
                         error=type(exc).__name__, detail=str(exc)[:200])
        self.log(event="voice_left", guild=guild_id)

    async def on_ready(self) -> None:
        """Join the standing channels (AWDISCORD_VOICE_JOIN), if any."""
        for guild in list(getattr(self.client, "guilds", []) or []):
            for channel in guild.voice_channels:
                if str(channel.id) in self.config.join_channels:
                    await self.join_channel(str(guild.id), str(channel.id))

    async def on_voice_state_update(self, member, before, after) -> None:
        """A drag is a command: follow the bot wherever a human puts it."""
        me = getattr(self.client, "user", None)
        if me is None or getattr(member, "id", None) != me.id:
            return
        guild = getattr(member, "guild", None)
        if guild is None:
            return
        guild_id = str(guild.id)
        if after.channel is None:
            await self.leave(guild_id)
            return
        if before.channel is not None and before.channel.id == after.channel.id:
            return
        await self.join_channel(guild_id, str(after.channel.id))

    # -- playback ------------------------------------------------------------
    def play_turn(self, guild_id: str, turn: VoiceTurn) -> None:
        """Decode and play a synthesized answer (called from the worker thread)."""
        if self.client is None or not turn.audio:
            return
        try:
            pcm = decode_to_pcm48k(turn.audio, turn.audio_format)
        except Exception as exc:
            self.log(event="voice_decode_error", error=type(exc).__name__,
                     detail=str(exc)[:200])
            return
        asyncio.run_coroutine_threadsafe(self._play_pcm(guild_id, pcm),
                                         self.client.loop)

    def _decode(self, audio: bytes, audio_format: str = "") -> bytes:
        return decode_to_pcm48k(audio, audio_format)

    def _clip_start(self, guild_id: str, pcm: bytes, after) -> None:
        """Put one clip on the wire (worker thread); ``after`` fires when it ends."""
        if self.client is None:
            raise RuntimeError("voice is not attached to a client")
        fut = asyncio.run_coroutine_threadsafe(
            self._start_clip(guild_id, pcm, after), self.client.loop)
        fut.result(timeout=10)

    async def _start_clip(self, guild_id: str, pcm: bytes, after) -> None:
        import discord
        guild = self.client.get_guild(int(guild_id))
        vc = getattr(guild, "voice_client", None)
        if vc is None:
            raise RuntimeError("no voice client for guild " + str(guild_id))
        if vc.is_playing():
            vc.stop()
        vc.play(discord.PCMAudio(io.BytesIO(pcm)), after=after)

    def _clip_stop(self, guild_id: str) -> None:
        """``vc.stop()`` from any thread (barge-in)."""
        client = self.client
        if client is None:
            return

        def _stop() -> None:
            guild = client.get_guild(int(guild_id))
            vc = getattr(guild, "voice_client", None)
            if vc is not None and vc.is_playing():
                vc.stop()

        client.loop.call_soon_threadsafe(_stop)

    async def _play_pcm(self, guild_id: str, pcm: bytes) -> None:
        import discord
        guild = self.client.get_guild(int(guild_id))
        vc = getattr(guild, "voice_client", None)
        if vc is None:
            self.log(event="voice_play_no_client", guild=guild_id)
            return
        try:
            if vc.is_playing():
                vc.stop()                 # the newer answer wins
            vc.play(discord.PCMAudio(io.BytesIO(pcm)))
        except Exception as exc:
            self.log(event="voice_play_error", guild=guild_id,
                     error=type(exc).__name__, detail=str(exc)[:200])

    # -- status --------------------------------------------------------------
    def status(self) -> dict:
        out = {}
        for guild_id, session in self._sessions.items():
            out[guild_id] = {"channel_id": session.channel_id,
                             "turns": len(session.loop.history) // 2,
                             "queued": session.queue.qsize()}
        return {"enabled": True, "since": self._started_at, "guilds": out,
                "inbound": dict(_INBOUND_STATS)}
