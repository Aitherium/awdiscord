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

Talking to it (``AWDISCORD_VOICE_WAKE``: ``auto`` by default): with two or more
people in the channel only an utterance that starts with the wake word ("Aither, ...";
STT spellings such as "ether"/"either"/"aether" and a "hey" lead-in are tolerated,
list in ``AWDISCORD_VOICE_WAKE_WORDS``) starts a turn; alone with one person every
utterance does. ``on`` always needs it, ``off`` never does. Spoken commands:

* "Aither, take notes" / "record" -- a notes session: every speaker's words are
  transcribed (name + time) and the text chat is told. A line that starts with the
  wake word is still answered. "Aither, off the record" pauses, "Aither, back on the
  record" resumes, and "Aither, stop" posts a summary (decisions, action items, open
  questions) and the full transcript as a file to the voice channel's text chat. Each
  line is written to a journal under the state directory as it is heard, so a
  restart mid-call resumes the session or, when everyone has left, finishes it.
* "Aither, create a task to ..." / "remind Ann to ..." / "ask Atlas to ..." -- the
  instruction is filed through the server's trackers and the result is posted to the
  text chat with its link and owner. Anything that would spend money, delete data
  or publish outside the organisation is marked for a decision, never done.
* "Aither, look up X" / "search for X" / "check the knowledge base for X" -- a
  background turn through the same agent and tools; the answer is posted to the
  text chat while the call goes on.

A model that has not answered after ``AWDISCORD_VOICE_STUCK_S`` (8) seconds is
announced ("I'm stuck waiting on the model") instead of left as silence.

Calls that run themselves (opt-in per server: the store key ``core/voice_calls`` or
the id in ``AWDISCORD_VOICE_AUTO_GUILDS``; off everywhere else): the bot joins a voice
channel when a person does, tells the text chat it is taking notes and how to pause,
keeps notes for the whole call, and when the last person leaves posts the summary and
transcript, files the action items (:mod:`awdiscord.callnotes`) and leaves.

Leaving is ``/leave`` or moving the bot out of voice.  ``AWDISCORD_VOICE=1``
turns the whole transport on; without it the gateway behaves exactly as before
and neither PyNaCl nor the voice-receive extension is needed.
"""

from __future__ import annotations

import asyncio
import copy
import inspect
import io
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

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

from awdiscord import callnotes

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


#: What STT hears for "Aither": the name plus its common mis-spellings.
DEFAULT_WAKE_WORDS = ("aither", "aether", "ether", "either", "eather", "ayther",
                      "aitherr", "eyther")
#: Words people put before a name ("hey Aither", "ok Aither").
_WAKE_LEADS = ("hey", "hi", "ok", "okay", "yo", "oh", "so")


def _wake_mode(value: str) -> str:
    v = (value or "").strip().lower()
    if v in ("1", "true", "yes", "on", "always", "required"):
        return "on"
    if v in ("0", "false", "no", "off", "never", "none"):
        return "off"
    return "auto"


def _wake_words(value: str) -> tuple:
    words = tuple(w.strip().lower() for w in (value or "").split(",") if w.strip())
    return words or DEFAULT_WAKE_WORDS


def strip_wake_word(text: str, words=DEFAULT_WAKE_WORDS) -> Optional[str]:
    """``text`` without its leading wake word, or None when it does not start with one.

    Tolerates a lead-in ("hey", "ok"), punctuation STT puts around the name
    ("Aither, ..." / "Ether. ..."), a possessive, and multi-word entries in
    ``words`` ("hey aither"). A bare "Aither?" returns "".
    """
    names = sorted({w.strip().lower() for w in words if w and w.strip()},
                   key=len, reverse=True)
    if not names or not text:
        return None
    alts = "|".join(r"\s+".join(re.escape(p) for p in n.split()) for n in names)
    leads = "|".join(_WAKE_LEADS)
    pattern = (r"^[\W_]*(?:(?:" + leads + r")[\s,.!?;:-]+)?(?:" + alts
               + r")(?:'s)?(?![\w'])[\s,.!?;:-]*")
    m = re.match(pattern, text, flags=re.IGNORECASE)
    if m is None:
        return None
    return text[m.end():].strip()


_POLITE = re.compile(r"^(?:(?:please|can you|could you|would you|will you|go ahead and|"
                     r"i need you to|i want you to|now)\s+)+", re.IGNORECASE)
_NOTES_START = re.compile(
    r"^(?:start\s+)?(?:take|taking|start taking|keep)\s+(?:some\s+|the\s+)?"
    r"(?:meeting\s+)?notes?\b.*$"
    r"|^(?:start\s+)?notes(?:\s+on)?$"
    r"|^(?:start\s+)?record(?:ing)?(?:\s+(?:this|the|our)(?:\s+(?:call|meeting|chat))?"
    r"|\s+notes)?$", re.IGNORECASE)
_NOTES_STOP = re.compile(
    r"^(?:stop|end|finish|done|quit)(?:\s+(?:the\s+)?(?:notes?|note\s*taking|"
    r"taking\s+notes|recording|meeting\s+notes|the\s+recording))?(?:\s+now)?$"
    r"|^notes\s+off$|^(?:that'?s|that\s+is)\s+(?:all|it)$", re.IGNORECASE)
_NOTES_PAUSE = re.compile(
    r"^(?:(?:go|going|let'?s\s+go|this\s+is|we'?re|we\s+are)\s+)?off\s+(?:the\s+)?record$"
    r"|^pause(?:\s+(?:the\s+)?(?:notes?|note\s*taking|recording))?$", re.IGNORECASE)
_NOTES_RESUME = re.compile(
    r"^(?:(?:go|going|let'?s\s+go|we'?re|we\s+are)\s+)?back\s+on\s+(?:the\s+)?record$"
    r"|^on\s+the\s+record$"
    r"|^resume(?:\s+(?:the\s+)?(?:notes?|note\s*taking|recording))?$", re.IGNORECASE)
_LOOKUP = re.compile(
    r"^(?P<verb>look\s+up|lookup|search\s+(?:the\s+)?(?:web|internet|online)\s+for|"
    r"search\s+for|google|find\s+out(?:\s+about)?|"
    r"(?:check|search|look\s+in|look\s+at)\s+(?:the\s+|our\s+)?"
    r"(?:knowledge\s*base|kb|company\s+brain|docs)\s+(?:for|about|on)|"
    r"(?:check|search)\s+(?:the\s+|our\s+)?(?:knowledge\s*base|kb|company\s+brain))"
    r"\s+(?P<query>.+)$", re.IGNORECASE)


def parse_command(text: str) -> Tuple[str, str]:
    """A spoken command (the wake word already stripped) -> (kind, argument).

    kind is ``notes_start``, ``notes_stop``, ``notes_pause``, ``notes_resume``,
    ``web``, ``kb``, ``action`` (file or hand off work; the argument is the whole
    instruction) or ``""`` (an ordinary question for the agent). Matching is
    deliberately narrow: a question that merely mentions notes or searching stays a
    question.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"^[\W_]+|[\s.!?,;:]+$", "", t)
    t = _POLITE.sub("", t).strip()
    t = re.sub(r"\s+(?:please|for me|thanks|thank you)$", "", t, flags=re.I).strip()
    if not t:
        return "", ""
    if _NOTES_PAUSE.match(t):
        return "notes_pause", ""
    if _NOTES_RESUME.match(t):
        return "notes_resume", ""
    if _NOTES_STOP.match(t):
        return "notes_stop", ""
    if _NOTES_START.match(t):
        return "notes_start", ""
    m = _LOOKUP.match(t)
    if m:
        verb = m.group("verb").lower()
        kind = "kb" if re.search(r"knowledge|\bkb\b|company brain|docs", verb) else "web"
        query = re.sub(r"\s+(?:please|for me)$", "", m.group("query"), flags=re.I).strip()
        if query:
            return kind, query
    if callnotes.is_action_request(t):
        return "action", t
    return "", ""


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
    stale_ms: int = 10000
    sentence_chars: int = 200
    first_clause_chars: int = 24
    #: "auto" = the wake word is needed when 2+ humans are in the channel; "on" =
    #: always; "off" = answer every utterance (the old 1:1 behaviour).
    wake: str = "auto"
    wake_words: tuple = DEFAULT_WAKE_WORDS
    #: After a bare "Aither?", the same speaker's next utterance needs no wake word.
    attention_s: float = 8.0
    #: No first sentence after this long -> say so instead of sitting silent.
    stuck_s: float = 8.0
    stuck_text: str = "I'm stuck waiting on the model. I'll answer as soon as it frees up."
    #: Servers whose calls run themselves, beside the ones the store opts in.
    auto_guilds: tuple = ()
    #: Where notes journals live (``<state_dir>/callnotes``); "" = notes in memory only.
    state_dir: str = ""

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
            stale_ms=int(_env(prefix + "VOICE_STALE_MS", default="10000")),
            sentence_chars=int(_env(prefix + "VOICE_SENTENCE_CHARS", default="200")),
            first_clause_chars=int(_env(prefix + "VOICE_FIRST_CLAUSE_CHARS",
                                        default="24")),
            wake=_wake_mode(_env(prefix + "VOICE_WAKE", default="auto")),
            wake_words=_wake_words(_env(prefix + "VOICE_WAKE_WORDS", default="")),
            attention_s=float(_env(prefix + "VOICE_ATTENTION_S", default="8")),
            stuck_s=float(_env(prefix + "VOICE_STUCK_S", default="8")),
            stuck_text=_env(prefix + "VOICE_STUCK_TEXT",
                            default=cls.stuck_text),
            auto_guilds=callnotes.auto_guilds(prefix),
            state_dir=_env(prefix + "STATE_DIR", default=""),
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
                 sentence_chars: int = 200, first_clause_chars: int = 0) -> None:
        self.agent = agent
        self.persona = persona or None
        self.dispatcher = dispatcher
        self.guild_id = str(guild_id or "")
        self.channel_id = str(channel_id or "")
        self.user_id = ""
        self.user_name = ""
        self.stream = stream
        self.sentence_chars = sentence_chars
        self.first_clause_chars = first_clause_chars
        #: Why the last ``think_stream`` was not streamed ("" = it was, or not tried).
        self.fallback_reason = ""
        #: False for a turn whose answer is READ (a lookup posted to the text chat,
        #: a notes summary): no spoken-style hint.
        self.spoken_style = True
        #: How much of the turn's text reaches the model (a notes summary needs more).
        self.clip_chars = 1500
        #: Where the last ``think_stream`` spent its time: plan_ms (gates, tools,
        #: context) and first_token_ms (the model's first streamed text).
        self.timing: Dict[str, int] = {}

    def side_copy(self, user_id: str, user_name: str = "", *,
                  spoken: bool = False, clip_chars: int = 1500) -> "BotThinker":
        """A copy for a turn that runs OFF the voice worker (a lookup, a summary).

        The worker rebinds ``set_speaker`` on every utterance, so a background turn
        must not share this object: the copy keeps the speaker it was started for.
        """
        other = copy.copy(self)
        # A per-instance wrapper (a stage timer patched onto ``think``) is bound to
        # THIS object; copied, it would run the turn as the worker's current speaker.
        for name in ("think", "think_stream"):
            other.__dict__.pop(name, None)
        other.timing = {}
        other.fallback_reason = ""
        other.spoken_style = spoken
        other.clip_chars = clip_chars
        other.set_speaker(user_id, user_name)
        return other

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
        began = time.monotonic()
        self.timing = {}
        plan = self._plan(text, history)
        self.timing["plan_ms"] = int((time.monotonic() - began) * 1000)
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
                chunker = _chunker(self.sentence_chars, self.first_clause_chars)
                asked = time.monotonic()
                try:
                    for piece in stream(prompt, cancel=cancel, **kwargs):
                        if not started:
                            self.timing["first_token_ms"] = int(
                                (time.monotonic() - asked) * 1000)
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
            asked = time.monotonic()
            answer = agent.ask(prompt, **kwargs)      # today's path, unchanged
            self.timing["ask_ms"] = int((time.monotonic() - asked) * 1000)
            for sentence in split_sentences(answer, self.sentence_chars):
                spoken.append(sentence)
                yield sentence
        finally:
            if user and spoken:
                self.dispatcher._remember_answer(
                    self.guild_id or None, user, text, " ".join(spoken))

    def _spoken(self, text: str) -> str:
        """The turn as the model sees it: the words, plus how a spoken answer is shaped.

        A voice reply is heard, not read, and every extra token is time the speaker
        waits. ``AWDISCORD_VOICE_STYLE`` replaces the hint; ``off`` sends the words alone.
        A side turn (``spoken_style`` False) is read in the text chat: no hint.
        """
        if not getattr(self, "spoken_style", True):
            return text
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
            from awdiscord.dispatch import _accepts

            kwargs = {"persona": self.persona, "history": list(history or ())}
            # The turn still knows which server it was spoken in: without it the
            # session id and the memory surface would fall back to a DM's.
            if self.guild_id and _accepts(self.agent.ask, "guild_id"):
                kwargs["guild_id"] = self.guild_id
            if self.channel_id and _accepts(self.agent.ask, "channel_id"):
                kwargs["channel_id"] = self.channel_id
            return self.agent, self._spoken(text), kwargs, ""
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
        return agent, self._spoken(clip(text, int(self.clip_chars or 1500))), kwargs, user


def _chunker(max_chars: int, first_clause_chars: int) -> SentenceChunker:
    """A SentenceChunker, on an awvoice too old to know ``first_clause_chars`` too.

    2026-11-11: the live image paired this module with awvoice 0.5.x, whose chunker
    takes only ``max_chars`` -- every streamed voice turn died with TypeError before
    the model was asked, which the room heard as "Aither does not answer".
    """
    try:
        return SentenceChunker(max_chars, first_clause_chars=first_clause_chars)
    except TypeError:
        return SentenceChunker(max_chars)


# ---------------------------------------------------------------------------
# PCM helpers
# ---------------------------------------------------------------------------

_INBOUND_STATS = {"raw": 0, "no_user": 0, "packets": 0, "dave_decrypted": 0,
                  "dave_failed": 0, "decoded": 0}

# Raw UDP on the voice socket, below voice_recv: proves Discord can reach us at all.
_VOICE_UDP = {"udp_rx": 0, "udp_rx_bytes": 0, "keepalive_sent": 0, "keepalive_failed": 0}

# Seconds between UDP keepalives. The voice socket carries nothing while the bot is
# silent; the podman MASQUERADE conntrack entry (nf_conntrack_udp_timeout 30 s) and the
# WSL NAT then expire, and every packet Discord sends afterwards is dropped before the
# socket: 2026-11-08, dave active, owner speaking, packets 0, no conntrack entry for
# the flow. voice_recv ships a keepalive but sleeps ``time.sleep(5000)`` -- one packet
# every 83 minutes. Must stay well under 30.
_KEEPALIVE_S = 5.0


def _count_udp(data: bytes) -> None:
    _VOICE_UDP["udp_rx"] += 1
    _VOICE_UDP["udp_rx_bytes"] += len(data or b"")


def _keepalive_packet(ssrc: int) -> bytes:
    """An IP-discovery request (type 1, length 70, ssrc, zero address/port).

    Discord answers every one with a 74-byte discovery response, which voice_recv
    drops at DEBUG (``_is_ip_discovery_packet``). An 8-byte counter keepalive also
    works on the wire, but its echo fails RTP decode and voice_recv logs a full
    traceback for every one."""
    return struct.pack(">HHI", 1, 70, ssrc & 0xFFFFFFFF) + bytes(66)


def _udp_keepalive(vc, counter: int = 0) -> bool:
    """Send one keepalive down the voice UDP flow; never raises.

    Also (once per connection state) hooks a raw-datagram counter onto the voice
    socket reader, so ``udp_rx`` climbing is proof the return path is open --
    Discord answers each keepalive, so it climbs even when nobody speaks."""
    conn = getattr(vc, "_connection", None)
    sock = getattr(conn, "socket", None)
    ip = getattr(conn, "endpoint_ip", None)
    port = getattr(conn, "voice_port", None)
    if conn is None or not sock or not isinstance(ip, str) or not ip or not port:
        return False
    if not getattr(conn, "_aw_udp_counted", False):
        try:
            conn.add_socket_listener(_count_udp)
            conn._aw_udp_counted = True       # noqa: SLF001 - our own marker
        except Exception as exc:  # noqa: BLE001 - counting is diagnostics only
            _VOICE_UDP["listener_error"] = type(exc).__name__
    try:
        sock.sendto(_keepalive_packet(int(getattr(conn, "ssrc", 0) or 0)), (ip, int(port)))
    except Exception:  # noqa: BLE001 - a closed socket mid-reconnect is normal
        _VOICE_UDP["keepalive_failed"] += 1
        return False
    _VOICE_UDP["keepalive_sent"] += 1
    return True


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
        #: The notes session, when someone said "Aither, take notes" (else None).
        self.notes: Optional[_Notes] = None
        self._notes_lock = threading.Lock()
        #: Someone ended the notes by voice in a call that runs itself: they stay off
        #: until the room empties, whoever joins next.
        self._auto_stopped = False
        #: Notes that were just ended but whose last utterance may still be in STT.
        self._closing: Optional[_Notes] = None
        #: The worker is transcribing or answering an utterance right now.
        self._busy = False
        #: speaker id -> monotonic deadline: a bare "Aither?" opens one follow-up.
        self._attention: Dict[str, float] = {}
        #: Side threads (lookups, notes summaries) still running -- tests join them.
        self.side_threads: List[threading.Thread] = []
        self.vc = None
        #: Who the turn being answered belongs to (barge-in rights in a crowd).
        self.asker = ""
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

    def _latest(self, item, keep_all: bool = False):
        """The utterance worth answering now: the newest one, if it is still fresh.

        Utterances queue while a turn is being answered. Answering each in order
        made every reply later than the last (2026-11-07: first audio 24 s -> 52 s
        over five turns, while background audio kept the mic busy). Only the newest
        is answered; the skipped ones and any older than ``stale_ms`` are dropped
        and counted.

        ``keep_all`` (a wake-word room, a notes session): every utterance is heard in
        order. Most of them cost one STT call and no model call, and dropping them
        would lose a note or the one line that said "Aither". A notes session never
        drops a stale one either.
        """
        if keep_all:
            if self.notes is not None:
                return item
            stale_ms = int(getattr(self.voice.config, "stale_ms", 0) or 0)
            age_ms = int((time.monotonic() - item[3]) * 1000)
            if stale_ms > 0 and age_ms > stale_ms * 3:
                self.voice.log(event="voice_utterance_dropped", guild=self.guild_id,
                               skipped=0, stale=True, age_ms=age_ms)
                return None
            return item
        skipped = 0
        while True:
            try:
                item = self.queue.get_nowait()
            except queue.Empty:
                break
            skipped += 1
        stale_ms = int(getattr(self.voice.config, "stale_ms", 0) or 0)
        age_ms = int((time.monotonic() - item[3]) * 1000)
        stale = stale_ms > 0 and age_ms > stale_ms
        if skipped or stale:
            self.voice.log(event="voice_utterance_dropped", guild=self.guild_id,
                           skipped=skipped, stale=stale, age_ms=age_ms)
        return None if stale else item

    # -- barge-in ------------------------------------------------------------
    def heard(self, user_id: str, pcm16k: bytes) -> bool:
        """Inbound speech (16 kHz mono) from ``user_id``; True when it barged in."""
        config = self.voice.config
        user_id = str(user_id)
        # In a wake-word room people talk to EACH OTHER over Aither; only the person
        # it is answering can talk over it ("Aither, stop" from anyone else still
        # lands as the next utterance).
        asker = getattr(self, "asker", "")
        crosstalk = bool(asker) and asker != user_id and self.wake_required()
        if not config.barge_in or not self.player.active or crosstalk:
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

    def _stream_turn(self, pcm: bytes, heard_at: float, text: Optional[str] = None):
        """Sentence -> TTS -> the playback queue, while the model is still writing.

        ``text`` is an already-transcribed (wake-word-stripped) turn; without it the
        loop transcribes ``pcm`` itself. While no sentence has arrived the stuck guard
        is armed: after ``stuck_s`` the room hears that the model is the wait.
        """
        player = self.player
        gen = player.generation()
        cancel = threading.Event()
        first = threading.Event()
        emitted = threading.Event()
        stamp: Dict[str, int] = {}
        self._cancel = cancel
        self._barged = False

        def started() -> None:
            if not first.is_set():
                stamp["first_audio_ms"] = int((time.monotonic() - heard_at) * 1000)
                first.set()

        def emit(sentence: str, audio: bytes, audio_format: str):
            emitted.set()
            if cancel.is_set() or player.generation() != gen:
                return False
            if not audio:
                return True
            pcm48 = self.voice._decode(audio, audio_format)
            if cancel.is_set():
                return False
            return player.enqueue(pcm48, gen, started)

        guard = self._stuck_guard(emitted, cancel, heard_at, stamp)
        try:
            if text is None:
                turn = self.loop.handle_utterance_stream(
                    pcm, emit=emit, cancel=cancel,
                    max_chars=self.voice.config.sentence_chars)
            else:
                turn = self.loop.answer_stream(
                    text, emit=emit, cancel=cancel,
                    max_chars=self.voice.config.sentence_chars)
        finally:
            guard.cancel()
            self._cancel = None
        if turn is None:
            return None
        if "stuck_notice_ms" in stamp:
            turn.timings["stuck_notice_ms"] = stamp["stuck_notice_ms"]
        if turn.timings.get("sentences") and not cancel.is_set():
            first.wait(5.0)                 # a one-sentence reply may not be on air yet
        if "first_audio_ms" in stamp:
            turn.timings["first_audio_ms"] = stamp["first_audio_ms"]
        turn.barged_in = bool(turn.barged_in or self._barged)
        return turn

    def _routes(self) -> bool:
        """The loop can be transcribed first and answered second (a real VoiceLoop).

        That split is what lets the session read the words BEFORE deciding: wake
        word, notes, a background lookup, or an ordinary turn."""
        loop = self.loop
        return bool(getattr(loop, "transcriber", None) is not None
                    and callable(getattr(loop, "answer", None)))

    def humans(self) -> int:
        """People (not bots) in the voice channel right now."""
        channel = getattr(getattr(self, "vc", None), "channel", None)
        members = getattr(channel, "members", None) or []
        return sum(1 for m in members if not getattr(m, "bot", False))

    def wake_required(self) -> bool:
        mode = getattr(self.voice.config, "wake", "auto")
        if mode == "on":
            return True
        if mode == "off":
            return False
        return self.humans() >= 2

    def _run(self) -> None:
        while not self._stop.is_set():
            self._busy = False
            try:
                user_id, user_name, pcm, heard_at = self.queue.get(timeout=0.5)
            except queue.Empty:
                continue
            self._busy = True
            routes = self._routes()
            keep_all = routes and (self.notes is not None or self.wake_required())
            item = self._latest((user_id, user_name, pcm, heard_at), keep_all=keep_all)
            if item is None:
                continue
            user_id, user_name, pcm, heard_at = item
            self.loop.user = user_name
            # The speaker's Discord id decides the tools (owner-only stays owner-only),
            # exactly as it does for a text message from the same user (2026-11-01).
            bind = getattr(getattr(self.loop, "thinker", None), "set_speaker", None)
            if callable(bind):
                bind(user_id, user_name)
            if routes:
                try:
                    self._route(user_id, user_name, pcm, heard_at)
                except Exception as exc:   # a failed turn keeps the channel open
                    self.voice.log(event="voice_turn_error", user=user_name,
                                   error=type(exc).__name__, detail=str(exc)[:200])
                    self.say("Sorry, the model didn't answer. Try me again in a minute.")
                continue
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
            self._log_turn(user_name, turn, streams)
            if not streams:
                self.voice.play_turn(self.guild_id, turn)
        self._busy = False

    def _log_turn(self, user_name: str, turn: VoiceTurn, streams: bool,
                  extra: Optional[dict] = None) -> None:
        extra = dict(extra or {})
        if streams:
            thinker = getattr(self.loop, "thinker", None)
            fallback = getattr(thinker, "fallback_reason", "")
            timing = getattr(thinker, "timing", None)
            if isinstance(timing, dict):
                for key, value in timing.items():
                    extra.setdefault(key, value)
            self.voice.log(event="voice_turn", user=user_name,
                           heard=turn.transcript[:160], said=turn.reply[:160],
                           streamed=not fallback, fallback=fallback or None,
                           barged_in=turn.barged_in, **turn.timings, **extra)
            return
        stages = getattr(self, "timings", None)       # a per-stage timer, when wired
        if isinstance(stages, dict):
            for key, value in stages.items():
                extra.setdefault(key, value)
        self.voice.log(event="voice_turn", user=user_name,
                       heard=turn.transcript[:160], said=turn.reply[:160],
                       audio_bytes=len(turn.audio), **extra)

    # -- routing: wake word, notes, background lookups, an ordinary turn ------
    def _route(self, user_id: str, user_name: str, pcm: bytes, heard_at: float) -> None:
        loop = self.loop
        queue_ms = int((time.monotonic() - heard_at) * 1000)
        t0 = time.monotonic()
        rate = int(getattr(getattr(loop, "config", None), "sample_rate", 16000) or 16000)
        text = (loop.transcriber.transcribe(pcm, sample_rate=rate) or "").strip()
        stt_ms = int((time.monotonic() - t0) * 1000)
        phantom = getattr(loop, "_phantom", None)
        if not text or (callable(phantom) and phantom(text, pcm)):
            return
        config = self.voice.config
        body = strip_wake_word(text, getattr(config, "wake_words", DEFAULT_WAKE_WORDS))
        woke = body is not None
        if body is None:
            deadline = self._attention.pop(str(user_id), 0.0)
            if deadline and time.monotonic() <= deadline:
                body, woke = text, True
            elif self.wake_required():
                if (self.notes or self._closing) is not None:
                    self._note(user_name, text, heard_at, user_id)
                else:
                    self.voice.log(event="voice_no_wake", guild=self.guild_id,
                                   user=user_name, heard=text[:80], stt_ms=stt_ms)
                return
            else:
                body = text
        kind, arg = parse_command(body)
        if kind == "notes_start":
            self.start_notes(user_id, user_name)
            return
        if kind == "notes_stop":
            if self.notes is not None:
                self._auto_stopped = True
                self.stop_notes(user_name)
            elif self.player.active:
                self.barge_in(str(user_id))
            return
        if kind in ("notes_pause", "notes_resume"):
            self.pause_notes(user_name, kind == "notes_pause")
            return
        taking = (self.notes or self._closing) is not None
        if taking:
            # Notes and answers coexist: every line is recorded, and a line that
            # named her is ALSO answered (until 2026-11-12 a notes session
            # transcribed "Aither, what did we decide?" instead of answering it).
            self._note(user_name, text, heard_at, user_id)
        if kind in ("web", "kb"):
            self.start_background(kind, arg, user_id, user_name)
            return
        if kind == "action":
            self.start_action(arg, user_id, user_name)
            return
        if taking and not woke:
            return
        if not body:
            # "Aither?" alone: answer, and let the next thing they say need no name.
            if woke:
                self._attention[str(user_id)] = time.monotonic() + float(
                    getattr(config, "attention_s", 8.0) or 8.0)
                self.say("Yes?")
            return
        timings = {"queue_ms": queue_ms, "stt_ms": stt_ms}
        self.asker = str(user_id)
        if taking:
            body = self._with_notes(body)
        if self._streams():
            turn = self._stream_turn(pcm, heard_at, text=body)
            if turn is None:
                return
            turn.timings.update(timings)
            self._log_turn(user_name, turn, True)
            self._note_reply(turn)
            return
        guard_cancel = threading.Event()
        guard = self._stuck_guard(guard_cancel, threading.Event(), heard_at, timings)
        try:
            turn = loop.answer(body)
        finally:
            guard.cancel()
        self._log_turn(user_name, turn, False, extra=timings)
        self._note_reply(turn)
        self.voice.play_turn(self.guild_id, turn)

    def _with_notes(self, question: str, limit: int = 2500) -> str:
        """The question plus what the call has said so far.

        Her chat history holds only the turns addressed to her, so without this
        "Aither, what did we decide?" was answered "I have no record of that" while
        the decision sat in her own notes (measured 2026-11-12). The newest lines
        win when the call is long; the question itself is not repeated.
        """
        notes = self.notes
        notes = notes or self._closing
        if notes is None:
            return question
        with self._notes_lock:
            lines = [n + ": " + t for _, n, t in (notes.lines or [])[:-1]]
        kept: List[str] = []
        used = 0
        for line in reversed(lines):
            used += len(line) + 1
            if used > limit:
                break
            kept.append(line)
        if not kept:
            return question
        return (question + "\n\n(What has been said in this call so far, from your "
                "own notes; answer from it when it holds the answer:\n"
                + "\n".join(reversed(kept)) + ")")

    def _note_reply(self, turn) -> None:
        """Her own answer belongs in the notes: the record shows what the room heard."""
        reply = str(getattr(turn, "reply", "") or "").strip()
        if reply and self.notes is not None:
            self._note("Aither", reply, time.monotonic())

    # -- speaking outside a turn ---------------------------------------------
    def say(self, text: str) -> bool:
        """Speak one short line (an acknowledgement, a status) on the player queue."""
        speak = getattr(getattr(self.loop, "speaker", None), "speak", None)
        if not callable(speak) or not text or self._stop.is_set():
            return False
        try:
            audio, fmt = speak(text)
            if not audio:
                return False
            pcm48 = self.voice._decode(audio, fmt or "")
            return self.player.enqueue(pcm48, self.player.generation())
        except Exception as exc:  # noqa: BLE001 - a status line must never kill the worker
            self.voice.log(event="voice_say_error", guild=self.guild_id,
                           error=type(exc).__name__, detail=str(exc)[:160])
            return False

    def _stuck_guard(self, answered: threading.Event, cancel: threading.Event,
                     heard_at: float, stamp: dict) -> threading.Timer:
        """Arm a timer that says "I'm stuck waiting on the model" if nothing came back.

        2026-11-11: a leaked MicroScheduler lane ("lane llamacpp_local is full (3 of
        3)" with the backend idle) held voice requests until they expired at 120 s;
        the room heard silence and assumed Aither was not listening.
        """
        config = self.voice.config
        wait_s = float(getattr(config, "stuck_s", 0) or 0)
        thinker = getattr(self.loop, "thinker", None)

        def fire() -> None:
            if answered.is_set() or cancel.is_set() or self._stop.is_set():
                return
            timing = getattr(thinker, "timing", None)
            if isinstance(timing, dict) and "first_token_ms" in timing:
                return                      # the model answered; TTS is the wait
            stamp["stuck_notice_ms"] = int((time.monotonic() - heard_at) * 1000)
            self.voice.log(event="voice_stuck_notice", guild=self.guild_id,
                           waited_ms=stamp["stuck_notice_ms"])
            self.say(getattr(config, "stuck_text", "") or VoiceConfig.stuck_text)

        timer = threading.Timer(wait_s if wait_s > 0 else 1e9, fire)
        timer.daemon = True
        if wait_s > 0:
            timer.start()
        return timer

    def _side(self, name: str, target, *args) -> threading.Thread:
        worker = threading.Thread(target=target, args=args, daemon=True,
                                  name="awdiscord-voice-" + name)
        self.side_threads = [t for t in self.side_threads if t.is_alive()]
        self.side_threads.append(worker)
        worker.start()
        return worker

    def _side_thinker(self, user_id: str, user_name: str, clip_chars: int = 1500):
        thinker = getattr(self.loop, "thinker", None)
        side = getattr(thinker, "side_copy", None)
        if callable(side):
            return side(user_id, user_name, spoken=False, clip_chars=clip_chars)
        return thinker

    # -- background lookups --------------------------------------------------
    def start_background(self, kind: str, query: str, user_id: str,
                         user_name: str) -> None:
        """Acknowledge in one sentence; look it up off the voice loop; post the answer."""
        where = "the knowledge base" if kind == "kb" else "that up"
        self.say(("Checking " + where if kind == "kb" else "Looking " + where)
                 + ". I'll post it in the chat.")
        self.voice.log(event="voice_background_start", guild=self.guild_id,
                       user=user_name, kind=kind, query=query[:120])
        self._side("lookup", self._run_background, kind, query, str(user_id), user_name)

    def _run_background(self, kind: str, query: str, user_id: str,
                        user_name: str) -> None:
        if kind == "kb":
            prompt = ("Search the company knowledge base with the company_brain_search "
                      "tool for: " + query + "\n\nAnswer from what it returns in a short "
                      "paragraph or a few bullets and name the sources. If it finds "
                      "nothing, say so plainly; never invent an answer.")
        else:
            prompt = ("Search the web with the web_search tool for: " + query + "\n\n"
                      "Answer in a short paragraph or a few bullets, with the links "
                      "you used. If the search finds nothing useful, say so plainly.")
        started = time.monotonic()
        thinker = self._side_thinker(user_id, user_name)
        ok = True
        try:
            answer = (thinker.think(prompt) or "").strip() or "I found nothing to say."
        except Exception as exc:  # noqa: BLE001 - the chat still hears what failed
            ok = False
            answer = "I couldn't finish that lookup: " + type(exc).__name__ + ": " \
                + str(exc)[:200]
        label = "Knowledge base" if kind == "kb" else "Web"
        content = "**" + label + " lookup for " + user_name + ":** " + query + "\n\n" + answer
        posted = self.voice.post_text(self.channel_id, content)
        self.voice.log(event="voice_background_done", guild=self.guild_id,
                       user=user_name, kind=kind, ok=ok, posted=posted,
                       ms=int((time.monotonic() - started) * 1000))
        if posted and not self._stop.is_set():
            self.say("I posted it in the chat.")

    # -- spoken instructions: file it, hand it off ---------------------------
    def start_action(self, text: str, user_id: str, user_name: str) -> None:
        """Acknowledge in one sentence; file the instruction off the voice loop."""
        self.say("On it. I'll post it in the chat.")
        self.voice.log(event="voice_action_start", guild=self.guild_id,
                       user=user_name, heard=text[:120])
        self._side("action", self._run_action, text, str(user_id), user_name)

    def _run_action(self, text: str, user_id: str, user_name: str) -> None:
        notes = self.notes
        recent = ""
        if notes is not None:
            with self._notes_lock:
                recent = "\n".join(n + ": " + t for _, n, t in (notes.lines or [])[-12:])
        focus = ("This is ONE instruction " + user_name + " just gave you by voice: \""
                 + text + "\". Return it under actions (normally exactly one item) and "
                 "leave decisions and questions empty. The transcript is context only.\n")
        thinker = self._side_thinker(user_id, user_name, clip_chars=6000)
        reply = ""
        try:
            if thinker is not None:
                reply = thinker.think(callnotes.CALL_PROMPT.format(
                    focus=focus, transcript=recent or (user_name + ": " + text))) or ""
        except Exception as exc:  # noqa: BLE001 - the instruction is still filed as said
            self.voice.log(event="voice_action_plan_error", guild=self.guild_id,
                           error=type(exc).__name__, detail=str(exc)[:160])
        plan = callnotes.parse_call_plan(reply)
        if not plan["actions"]:
            # The model returned nothing usable: file the words as they were said.
            risk = callnotes.risk_of(text)
            plan["actions"] = [{"title": text[:90], "detail": text[:250], "owner": "",
                                "kind": "task", "needs_decision": bool(risk),
                                "risk": risk, "why": ""}]
        people = dict(getattr(notes, "people", None) or {})
        people[user_name.lower()] = str(user_id)
        rows = callnotes.file_actions(self.guild_id, self.channel_id, plan, {
            "source": "spoken", "people": people, "actor": str(user_id),
            "actor_name": user_name, "said": text})
        posted = self.voice.post_action_list(self.channel_id, rows,
                                             head="**Filed for " + user_name + "**")
        self.voice.log(event="voice_action_done", guild=self.guild_id, user=user_name,
                       items=len(rows), posted=posted,
                       decisions=sum(1 for r in rows if r.get("status") == "decision"),
                       failed=sum(1 for r in rows if r.get("status") == "failed"))
        done = [str(r.get("title") or "") for r in rows if r.get("status") != "failed"]
        if done:
            # In the record, so the end-of-call write-up does not file it again.
            self._note("Aither", "Filed: " + "; ".join(done), time.monotonic())
        if posted and not self._stop.is_set():
            self.say("Posted in the chat.")

    # -- notes ---------------------------------------------------------------
    def _journal(self) -> Optional["callnotes.Journal"]:
        root = str(getattr(self.voice.config, "state_dir", "") or "")
        if not root:
            return None
        return callnotes.Journal(root, self.guild_id, self.channel_id)

    def start_notes(self, user_id: str, user_name: str, auto: bool = False) -> None:
        """Open a notes session and tell the chat. Nothing is recorded untold.

        ``auto`` = the server opted in to calls that run themselves, so the session
        opens when the first person is in the room. A journal a previous process
        left for this channel is picked up when it is recent (a restart mid-call);
        an old one is finished and posted first.
        """
        journal = self._journal()
        resumed = False
        header: dict = {}
        lines: list = []
        paused = False
        if self.notes is None and journal is not None and journal.exists():
            header, lines, paused = callnotes.read_journal(journal.path)
            fresh = time.time() - float(header.get("last_at") or 0.0) <= RESUME_WINDOW_S
            if header and fresh:
                resumed = True
            else:
                self.voice.recover_journal(journal.path)
                header, lines, paused = {}, [], False
        with self._notes_lock:
            if self.notes is not None:
                already = True
            else:
                already = False
                if resumed:
                    self.notes = _Notes(
                        started_by=str(header.get("by") or user_name),
                        started_by_id=str(header.get("by_id") or user_id),
                        started_at=float(header.get("at") or 0.0), lines=list(lines),
                        auto=bool(header.get("auto")) or auto, paused=paused,
                        journal=journal, people=dict(header.get("people") or {}))
                else:
                    self.notes = _Notes(started_by=user_name, started_by_id=str(user_id),
                                        auto=auto, journal=journal)
                    if journal is not None:
                        journal.start(self.notes.started_at, user_name, str(user_id),
                                      auto=auto)
        if already:
            if not auto:
                self.say("I'm already taking notes.")
            return
        self.voice.log(event="voice_notes_start", guild=self.guild_id,
                       channel=self.channel_id, user=user_name, auto=auto,
                       resumed=resumed, lines=len(lines) if resumed else 0,
                       journal=journal is not None)
        if resumed:
            told = self.voice.post_text(self.channel_id, callnotes.RESUME_NOTICE)
        elif auto:
            told = self.voice.post_text(self.channel_id, callnotes.JOIN_NOTICE)
        else:
            told = self.voice.post_text(
                self.channel_id,
                "**Aither is taking notes** in this call (started by " + user_name + "). "
                "Everything said is transcribed until someone says \"Aither, stop\"; "
                "\"Aither, off the record\" pauses. The notes are posted here when they "
                "end.")
        if not told:
            # The room could not be told in writing, so it is told out loud: a notes
            # session nobody was told about must not exist.
            self.say("I'm taking notes on this call. Say Aither, off the record to pause.")
        elif not auto and not resumed:
            self.say("Taking notes.")

    def _note(self, user_name: str, text: str, heard_at: float,
              user_id: str = "") -> None:
        notes = self.notes or self._closing
        if notes is None or notes.paused:
            return
        wall = time.time() - max(0.0, time.monotonic() - heard_at)
        with self._notes_lock:
            notes.lines.append((wall, user_name, text))
            if user_id and user_name:
                notes.people[user_name.lower()] = str(user_id)
        if notes.journal is not None and not notes.journal.line(
                wall, user_name, text, str(user_id or "")):
            self.voice.log(event="voice_journal_error", guild=self.guild_id,
                           channel=self.channel_id)

    def pause_notes(self, user_name: str, pause: bool) -> None:
        """"Off the record" stops recording until "back on the record"; both are said."""
        notes = self.notes
        if notes is None:
            self.say("I'm not taking notes right now.")
            return
        with self._notes_lock:
            changed = notes.paused != pause
            notes.paused = pause
        if changed and notes.journal is not None:
            notes.journal.mark("pause" if pause else "resume", user_name)
        self.voice.log(event="voice_notes_pause" if pause else "voice_notes_resume",
                       guild=self.guild_id, channel=self.channel_id, user=user_name)
        if pause:
            self.voice.post_text(self.channel_id, "**Off the record** (" + user_name
                                 + "). Nothing is being written down until someone "
                                 "says \"Aither, back on the record\".")
            self.say("Off the record.")
        else:
            self.voice.post_text(self.channel_id, "**Back on the record** (" + user_name
                                 + "). Notes are being taken again.")
            self.say("Back on the record.")

    def sync_call(self, humans: int) -> None:
        """A call that runs itself: notes open with the first person, close with the last.

        Called on every voice-state change in an opted-in server, off the event loop.
        """
        if humans > 0:
            if self.notes is None and not self._auto_stopped:
                self.start_notes("", "Aither", auto=True)
            return
        self._auto_stopped = False
        notes = self.notes
        if notes is not None and notes.auto:
            self.stop_notes("(everyone left)", spoken=False)

    def stop_notes(self, user_name: str = "", spoken: bool = True) -> Optional[threading.Thread]:
        with self._notes_lock:
            notes, self.notes = self.notes, None
        if notes is None:
            return None
        notes.stopped_by = user_name
        self._closing = notes
        self.voice.log(event="voice_notes_stop", guild=self.guild_id,
                       channel=self.channel_id, user=user_name, lines=len(notes.lines))
        if spoken:
            self.say("Stopped taking notes. I'll post them in the chat.")
        return self._side("notes", self._finish_notes, notes)

    def _finish_notes(self, notes: "_Notes") -> None:
        # The last thing said may still be in speech-to-text when the room empties:
        # give the worker a moment to land it before the notes are written up.
        deadline = time.monotonic() + LAST_WORDS_S
        while time.monotonic() < deadline and (
                self._busy or (not self._stop.is_set() and not self.queue.empty())):
            time.sleep(0.05)
        if self._closing is notes:
            self._closing = None
        # A call that ran itself has no starter: the summary runs as the first
        # person who spoke, so it gets that person's tools and limits.
        uid = notes.started_by_id or next(iter((notes.people or {}).values()), "")
        thinker = self._side_thinker(uid, notes.started_by, clip_chars=24000)
        self.voice.finish_call(self.guild_id, self.channel_id, notes, thinker)

    def detach_notes(self) -> Optional["_Notes"]:
        """Hand the open notes to the session that replaces this one (a reconnect)."""
        with self._notes_lock:
            notes, self.notes = self.notes, None
        return notes

    def stop(self) -> None:
        # Leaving mid-notes still posts them: the room asked for notes.
        lock = getattr(self, "_notes_lock", None)
        if lock is not None:
            with lock:
                notes, self.notes = self.notes, None
            if notes is not None:
                # Nothing is spoken here (this runs on the event loop via leave()); the
                # post happens on a side thread.
                notes.stopped_by = notes.stopped_by or "(Aither left the call)"
                self._closing = notes
                self._side("notes", self._finish_notes, notes)
        self._stop.set()
        self.player.close()


#: A journal whose newest line is older than this is a finished call, not one to resume.
RESUME_WINDOW_S = 1800.0
#: How long the write-up waits for an utterance still being transcribed.
LAST_WORDS_S = 10.0
#: How long startup waits for live sessions to claim their journals before the rest
#: are treated as calls that ended during the restart.
RECOVER_DELAY_S = 20.0


@dataclass
class _Notes:
    """One notes session. Lines are journaled to disk as they are heard."""
    started_by: str = ""
    started_by_id: str = ""
    stopped_by: str = ""
    started_at: float = 0.0
    lines: Optional[list] = None
    #: Opened by a call that runs itself (closes when the room empties).
    auto: bool = False
    #: "Off the record": nothing is written until someone resumes.
    paused: bool = False
    journal: Optional["callnotes.Journal"] = None
    #: Spoken name (lower case) -> Discord id, for owners of action items.
    people: Optional[dict] = None

    def __post_init__(self) -> None:
        self.started_at = self.started_at or time.time()
        if self.lines is None:
            self.lines = []
        if self.people is None:
            self.people = {}

    def transcript(self) -> str:
        out = ["Call notes, started " + time.strftime(
            "%Y-%m-%d %H:%M UTC", time.gmtime(self.started_at)) + " by " + self.started_by]
        for wall, name, text in self.lines or []:
            out.append("[" + time.strftime("%H:%M:%S", time.gmtime(wall)) + "] "
                       + name + ": " + text)
        if self.stopped_by:
            out.append("(stopped by " + self.stopped_by + ")")
        return "\n".join(out) + "\n"


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
                             sentence_chars=self.config.sentence_chars,
                             first_clause_chars=self.config.first_clause_chars)
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
            {"name": "calls", "type": 1, "contexts": [0], "integration_types": [0],
             "description": "What Aither does in this server's voice calls",
             "options": [{"type": 3, "name": "mode", "required": False,
                          "description": "status (default), or on / off (bot owner only)",
                          "choices": [{"name": "status", "value": "status"},
                                      {"name": "on", "value": "on"},
                                      {"name": "off", "value": "off"}]}]},
        ]

    def calls_command(self, guild_id: str, user_id: str, mode: str = "status") -> str:
        """``/calls``: say what the bot does in this server's calls, or switch it.

        Anyone may read it -- what is listened to is never hidden. Only the bot's
        owner may switch it: a server's own admins cannot turn recording on.
        """
        mode = (mode or "status").strip().lower()
        store = self._settings_store()
        said = ""
        if mode in ("on", "off"):
            owners = {str(o) for o in (getattr(self.bot, "owners", None) or [])}
            if str(user_id) not in owners:
                return ("Only the bot's owner can change this. `/calls` shows what is "
                        "on.")
            if store is None:
                return "This bot has no per-server store, so the setting cannot be saved."
            callnotes.set_voice_calls(store, guild_id, mode == "on")
            self.log(event="voice_calls_setting", guild=guild_id, on=mode == "on",
                     by=str(user_id))
            said = "Saved. "
        auto = self.auto(guild_id)
        session = self._sessions.get(str(guild_id))
        notes = getattr(session, "notes", None)
        lines = [said + "**Voice calls in this server**"]
        if auto:
            lines.append("- Joins on her own when a person joins a voice channel, and "
                         "leaves when the last person does: **on**")
            lines.append("- Notes: taken for every call, announced in the channel's "
                         "chat, posted there with the action items when it ends")
        else:
            lines.append("- Joins on her own: **off** (`/join` brings her in)")
            lines.append("- Notes: only after someone says \"Aither, take notes\"")
        lines.append("- Answers: a sentence that starts with \"Aither\"")
        if session is None:
            lines.append("- Right now: not in a voice channel")
        elif notes is None:
            lines.append("- Right now: in <#" + str(session.channel_id)
                         + ">, not taking notes")
        else:
            lines.append("- Right now: in <#" + str(session.channel_id) + ">, "
                         + ("**off the record** (paused)" if notes.paused
                            else "taking notes (" + str(len(notes.lines)) + " lines)"))
        lines.append("Say \"Aither, off the record\" to pause, \"Aither, back on the "
                     "record\" to resume, \"Aither, stop\" to end the notes.")
        return "\n".join(lines)

    def intercept(self, interaction: dict) -> bool:
        """Handle /join or /leave before the text dispatcher sees them (blocking)."""
        data = interaction.get("data") or {}
        name = str(data.get("name") or "")
        if name not in ("join", "leave", "calls"):
            return False
        guild_id = str(interaction.get("guild_id") or "")
        user = ((interaction.get("member") or {}).get("user") or {}).get("id") or ""
        if not guild_id:
            self._answer(interaction, "Voice commands only work inside a server.")
            return True
        if name == "calls":
            mode = "status"
            for opt in data.get("options") or []:
                if opt.get("name") == "mode" and opt.get("value"):
                    mode = str(opt["value"])
            self._answer(interaction, self.calls_command(guild_id, str(user), mode),
                         private=True)
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

    def _answer(self, interaction: dict, content: str, private: bool = False) -> None:
        try:
            data = {"content": content[:1900], "allowed_mentions": {"parse": []}}
            if private:
                data["flags"] = 64                  # only the asker sees it
            self.bot.rest.interaction_callback(
                interaction["id"], interaction["token"], {"type": 4, "data": data})
        except Exception as exc:
            self.log(event="voice_answer_error", error=type(exc).__name__,
                     detail=str(exc)[:200])

    def _schedule(self, coro) -> None:
        if self.client is None:
            return
        asyncio.run_coroutine_threadsafe(coro, self.client.loop)

    def post_text(self, channel_id: str, content: str) -> bool:
        """Post to a voice channel's own text chat (blocking; call off the event loop).

        A Discord voice channel IS a text channel too, so the voice channel id takes
        messages directly. Longer than one message -> the rest goes as a file.
        """
        rest = getattr(self.bot, "rest", None)
        if rest is None or not channel_id:
            self.log(event="voice_post_skipped", channel=channel_id, reason="no rest")
            return False
        try:
            if len(content) <= 1900:
                rest.create_message(str(channel_id), {"content": content})
                return True
            return self.post_file(channel_id, "answer.txt", content.encode("utf-8"),
                                  content[:1880].rstrip() + " ... (full text attached)")
        except Exception as exc:  # noqa: BLE001 - a post failure is logged, never raised
            self.log(event="voice_post_error", channel=channel_id,
                     error=type(exc).__name__, detail=str(exc)[:200])
            return False

    def post_file(self, channel_id: str, filename: str, data: bytes,
                  content: str = "") -> bool:
        rest = getattr(self.bot, "rest", None)
        if rest is None or not channel_id:
            self.log(event="voice_post_skipped", channel=channel_id, reason="no rest")
            return False
        try:
            rest.upload(str(channel_id), filename, data, "text/plain; charset=utf-8",
                        content=content[:2000])
            return True
        except Exception as exc:  # noqa: BLE001 - logged, never raised
            self.log(event="voice_post_error", channel=channel_id,
                     error=type(exc).__name__, detail=str(exc)[:200])
            return False

    def post_action_list(self, channel_id: str, rows: list,
                         head: str = "**Action items from this call**") -> bool:
        """The numbered list (five at most), the rest in a thread under it.

        A voice channel's chat may refuse a thread; the rest then follows as a second
        message, so no item is ever dropped from view.
        """
        main, more = callnotes.action_list(rows, head=head)
        rest = getattr(self.bot, "rest", None)
        if not main or rest is None or not channel_id:
            return False
        quiet = {"parse": []}
        try:
            sent = rest.create_message(str(channel_id), {"content": main,
                                                         "allowed_mentions": quiet})
            if more:
                target = str(channel_id)
                try:
                    made = rest.request(
                        "POST", "/channels/" + str(channel_id) + "/messages/"
                        + str((sent or {}).get("id") or "") + "/threads",
                        {"name": "More action items", "auto_archive_duration": 1440})
                    target = str((made or {}).get("id") or channel_id)
                except Exception as exc:  # noqa: BLE001 - the channel takes it instead
                    self.log(event="voice_thread_refused", channel=channel_id,
                             error=type(exc).__name__)
                rest.create_message(target, {"content": more, "allowed_mentions": quiet})
            return True
        except Exception as exc:  # noqa: BLE001 - logged, never raised
            self.log(event="voice_post_error", channel=channel_id,
                     error=type(exc).__name__, detail=str(exc)[:200])
            return False

    def finish_call(self, guild_id: str, channel_id: str, notes: "_Notes",
                    thinker) -> bool:
        """End one notes session: summary + transcript, the action items, the memory.

        Blocking (a side thread). Returns False when the notes could not be posted;
        the journal then stays on disk and the next start tries again, so a Discord
        outage at the end of a call does not lose it. Nothing is filed before the
        post lands, so a retry never files an item twice.
        """
        journal = notes.journal
        if not notes.lines:
            if not notes.auto:
                # Someone asked for these notes, so they hear there are none. A call
                # that ran itself and heard nothing (a person passing through an
                # empty room) ends quietly instead of posting a second line.
                self.post_text(channel_id, "**Notes ended** -- nothing was said while "
                                           "I was taking notes.")
            if journal is not None:
                journal.close()
            return True
        transcript = notes.transcript()
        reply, failed = "", ""
        try:
            if thinker is not None:
                reply = (thinker.think(callnotes.CALL_PROMPT.format(
                    focus="", transcript=transcript[-20000:])) or "").strip()
        except Exception as exc:  # noqa: BLE001 - the transcript still gets posted
            failed = ("(The summary failed: " + type(exc).__name__ + ": "
                      + str(exc)[:160] + ". The full transcript is attached.)")
        plan = callnotes.parse_call_plan(reply)
        if plan["parsed"]:
            summary = callnotes.render_summary(plan)
        else:
            summary = failed or reply or ("(The model returned no summary; the "
                                          "transcript is attached.)")
        head = ("**Call notes** (" + str(len(notes.lines)) + " lines, started by "
                + notes.started_by + ")\n\n")
        body = head + summary
        stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(notes.started_at))
        data = ("SUMMARY\n\n" + summary + "\n\nTRANSCRIPT\n\n" + transcript).encode("utf-8")
        if len(body) > 1900:
            body = body[:1880].rstrip() + " ... (full summary in the file)"
        posted = self.post_file(channel_id, "call-notes-" + stamp + ".txt", data, body)
        if not posted:
            self.log(event="voice_notes_posted", guild=guild_id, channel=channel_id,
                     lines=len(notes.lines), posted=False, kept_journal=journal is not None)
            return False
        people = dict(notes.people or {})
        rows = callnotes.file_actions(guild_id, channel_id, plan, {
            "source": "call", "people": people, "actor": notes.started_by_id,
            "actor_name": notes.started_by, "transcript": transcript})
        listed = self.post_action_list(channel_id, rows) if rows else False
        kept = callnotes.keep_summary(
            guild_id, channel_id, plan["title"], callnotes.brain_document(
                plan, guild_id, channel_id, notes.started_at,
                [n for _, n, _ in notes.lines], transcript, rows))
        if journal is not None:
            journal.close()
        self.log(event="voice_notes_posted", guild=guild_id, channel=channel_id,
                 lines=len(notes.lines), posted=True, parsed=plan["parsed"],
                 actions=len(rows), listed=listed, kept=kept,
                 decisions=sum(1 for r in rows if r.get("status") == "decision"),
                 failed=sum(1 for r in rows if r.get("status") == "failed"))
        return True

    # -- journals a previous process left open -------------------------------
    def recover_journal(self, path) -> bool:
        """Finish one journal nobody is writing: post it, file it, close it."""
        header, lines, _ = callnotes.read_journal(path)
        stem = str(getattr(path, "stem", "") or "")
        guild_id = str(header.get("guild") or stem.partition("-")[0])
        channel_id = str(header.get("channel") or stem.partition("-")[2])
        journal = callnotes.Journal(self.config.state_dir, guild_id, channel_id)
        notes = _Notes(started_by=str(header.get("by") or "Aither"),
                       started_by_id=str(header.get("by_id") or ""),
                       started_at=float(header.get("at") or 0.0), lines=list(lines),
                       auto=bool(header.get("auto")), journal=journal,
                       people=dict(header.get("people") or {}),
                       stopped_by="(the call ended while Aither was restarting)")
        thinker = None
        try:
            thinker = self.build_loop(guild_id=guild_id, channel_id=channel_id).thinker
            side = getattr(thinker, "side_copy", None)
            if callable(side):
                thinker = side(notes.started_by_id, notes.started_by, spoken=False,
                               clip_chars=24000)
        except Exception as exc:  # noqa: BLE001 - the transcript still gets posted
            self.log(event="voice_recover_error", guild=guild_id,
                     error=type(exc).__name__, detail=str(exc)[:160])
        done = self.finish_call(guild_id, channel_id, notes, thinker)
        self.log(event="voice_notes_recovered", guild=guild_id, channel=channel_id,
                 lines=len(lines), posted=done)
        return done

    def recover_all(self) -> int:
        """Finish every journal no live session owns. Returns how many it finished."""
        root = str(getattr(self.config, "state_dir", "") or "")
        if not root:
            return 0
        live = set()
        for session in list(self._sessions.values()):
            journal = getattr(getattr(session, "notes", None), "journal", None)
            if journal is not None:
                live.add(str(journal.path))
        count = 0
        for path in callnotes.unfinished(root):
            if str(path) in live:
                continue
            try:
                count += 1 if self.recover_journal(path) else 0
            except Exception as exc:  # noqa: BLE001 - one bad file never blocks the rest
                self.log(event="voice_recover_error", path=str(path),
                         error=type(exc).__name__, detail=str(exc)[:160])
        return count

    # -- calls that run themselves -------------------------------------------
    def _settings_store(self):
        getter = getattr(self.bot, "_settings_store", None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # noqa: BLE001 - no store = the setting is off
                return None
        return getattr(getattr(self.bot, "host", None), "store", None)

    def auto(self, guild_id: str) -> bool:
        """This server opted in to calls that run themselves."""
        return callnotes.voice_calls_on(guild_id, self._settings_store(),
                                        getattr(self.config, "auto_guilds", ()))

    @staticmethod
    def _humans_by_channel(guild) -> Dict[str, int]:
        afk = str(getattr(getattr(guild, "afk_channel", None), "id", "") or "")
        out: Dict[str, int] = {}
        for channel in getattr(guild, "voice_channels", None) or []:
            cid = str(getattr(channel, "id", "") or "")
            if not cid or cid == afk:
                continue
            out[cid] = sum(1 for m in (getattr(channel, "members", None) or [])
                           if not getattr(m, "bot", False))
        return out

    def _offload(self, fn, *args) -> threading.Thread:
        worker = threading.Thread(target=fn, args=args, daemon=True,
                                  name="awdiscord-voice-call")
        worker.start()
        return worker

    async def _auto_step(self, guild, event_channel: str = "") -> Optional[tuple]:
        """Join, move or leave for an opted-in server, then open or close its notes."""
        guild_id = str(guild.id)
        if not self.auto(guild_id):
            return None
        humans = self._humans_by_channel(guild)
        vc = getattr(guild, "voice_client", None)
        here = str(getattr(getattr(vc, "channel", None), "id", "") or "")
        move = callnotes.next_move(auto=True, bot_channel=here, humans=humans,
                                   standing=self.config.join_channels,
                                   event_channel=event_channel)
        if move[0] == "leave":
            self.log(event="voice_auto_leave", guild=guild_id, channel=here)
            await self.leave(guild_id)
            return move
        if move[0] == "join":
            self.log(event="voice_auto_join", guild=guild_id, channel=move[1],
                     humans=humans.get(move[1], 0))
            if not await self.join_channel(guild_id, move[1]):
                return move
        session = self._sessions.get(guild_id)
        if session is not None:
            self._offload(session.sync_call, humans.get(str(session.channel_id), 0))
        return move

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
        current = self._sessions.get(guild_id)
        listening = getattr(vc, "is_listening", None)
        if (current is not None and getattr(current, "vc", None) is vc
                and str(current.channel_id) == str(channel_id)
                and callable(listening) and listening()):
            # The same join twice (2026-11-07: two joins 53 ms apart at boot). Tearing
            # the reader down and starting a fresh one left voice_recv delivering
            # NOTHING -- 0 packets for an hour while connected -- so a duplicate join
            # keeps the pipeline that is already live.
            self.log(event="voice_join_dedup", guild=guild_id, channel=channel_id)
            return
        old = self._sessions.pop(guild_id, None)
        carried = None
        if old is not None:
            if str(old.channel_id) == str(channel_id):
                # A reconnect to the same room is the same call: its notes go on.
                carried = old.detach_notes()
            old.stop()
        loop = self.build_loop(guild_id=str(guild_id), channel_id=str(channel_id))
        session = _Session(self, guild_id, channel_id, loop)
        session.vc = vc
        if carried is not None:
            session.notes = carried
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
        threading.Thread(target=self._warm, args=(guild_id, session),
                         name="awdiscord-voice-warm", daemon=True).start()
        self.log(event="voice_listen_attached", guild=guild_id,
                 dave=bool(dave), dave_ready=bool(getattr(dave, "ready", False)),
                 dave_version=int(getattr(conn, "dave_protocol_version", 0) or 0),
                 voice=self.config.voice or "(service default)")
        self._watch_inbound(guild_id, session)

    def _warm(self, guild_id: str, session: "_Session") -> None:
        """Speak one short clip off-air so the first real reply is not a cold start.

        The first synthesis after idle took 1.9 s against ~0.3 s warm (2026-11-07);
        paying it at join time keeps it out of the first answer.
        """
        speaker = getattr(getattr(session, "loop", None), "speaker", None)
        speak = getattr(speaker, "speak", None)
        if not callable(speak):
            return
        started = time.monotonic()
        try:
            speak("Hi.")
        except Exception as exc:  # noqa: BLE001 - a failed warm-up is only a log line
            self.log(event="voice_warm_failed", guild=guild_id,
                     error=type(exc).__name__, detail=str(exc)[:160])
            return
        self.log(event="voice_warm", guild=guild_id,
                 tts_ms=int((time.monotonic() - started) * 1000))

    def _watch_inbound(self, guild_id: str, session: "_Session",
                       attached: bool = True) -> None:
        """Report the inbound pipeline counters, so a silent channel names its stage.
        packets -> dave_decrypted/dave_failed -> decoded -> utterance -> turn."""

        def _tick() -> None:
            nonlocal attached
            ticks = 0
            vc = getattr(session, "vc", None)       # noqa: SLF001
            if vc is not None:
                _udp_keepalive(vc, ticks)
            while not session._stop.is_set():       # noqa: SLF001 - same package
                session._stop.wait(_KEEPALIVE_S)    # noqa: SLF001
                if session._stop.is_set():
                    break
                ticks += 1
                vc = getattr(session, "vc", None)   # noqa: SLF001
                connected = bool(vc is not None and vc.is_connected())
                if connected:
                    # Keep the NAT/conntrack mapping alive or inbound audio is dropped.
                    _udp_keepalive(vc, ticks)
                if ticks % 2:
                    continue                        # stats every 10 s, keepalive every 5 s
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
                         connected=connected, **_INBOUND_STATS, **_VOICE_UDP,
                         **dave_state)

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
        """Join the standing channels (AWDISCORD_VOICE_JOIN), pick up calls in progress
        in the servers that opted in, and finish the notes a restart interrupted."""
        for guild in list(getattr(self.client, "guilds", []) or []):
            for channel in guild.voice_channels:
                if str(channel.id) in self.config.join_channels:
                    await self.join_channel(str(guild.id), str(channel.id))
        for guild in list(getattr(self.client, "guilds", []) or []):
            try:
                await self._auto_step(guild)
            except Exception as exc:  # noqa: BLE001 - one server never blocks the rest
                self.log(event="voice_auto_error", guild=str(getattr(guild, "id", "")),
                         error=type(exc).__name__, detail=str(exc)[:200])
        if getattr(self.config, "state_dir", ""):
            self._offload(self._recover_after_ready)

    def _recover_after_ready(self) -> None:
        # The sessions opened above resume their own journals first (side threads);
        # what is left belongs to calls that ended while the bot was down.
        time.sleep(RECOVER_DELAY_S)
        filers, keepers = callnotes.registered()
        opted = [str(g.id) for g in list(getattr(self.client, "guilds", []) or [])
                 if self.auto(str(g.id))]
        # One line that says whether talk can become work here: which servers run
        # their own calls, and whether anything is registered to file the items.
        self.log(event="voice_calls_ready", auto_guilds=opted, filers=filers,
                 keepers=keepers, journal=bool(self.config.state_dir))
        self.recover_all()

    async def on_voice_state_update(self, member, before, after) -> None:
        """A drag is a command: follow the bot wherever a human puts it. In a server
        that opted in, a person joining or leaving voice moves the bot on its own."""
        me = getattr(self.client, "user", None)
        guild = getattr(member, "guild", None)
        if me is None or guild is None:
            return
        if getattr(member, "id", None) != me.id:
            if getattr(member, "bot", False):
                return
            channel = getattr(after, "channel", None) or getattr(before, "channel", None)
            await self._auto_step(guild, str(getattr(channel, "id", "") or ""))
            return
        guild_id = str(guild.id)
        if after.channel is None:
            await self.leave(guild_id)
            return
        if before.channel is not None and before.channel.id == after.channel.id:
            return
        await self.join_channel(guild_id, str(after.channel.id))
        await self._auto_step(guild)

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
            notes = session.notes
            out[guild_id] = {"channel_id": session.channel_id,
                             "turns": len(session.loop.history) // 2,
                             "queued": session.queue.qsize(),
                             "auto": self.auto(guild_id),
                             "notes": notes is not None,
                             "notes_lines": len(notes.lines) if notes is not None else 0,
                             "notes_paused": bool(notes is not None and notes.paused)}
        return {"enabled": True, "since": self._started_at, "guilds": out,
                "inbound": dict(_INBOUND_STATS)}
