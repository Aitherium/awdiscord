"""Several persona bots in one voice channel: who was named, and taking turns.

Each persona bot is its OWN process in its own container, so nothing here can lean
on a shared server or a shared lock. Everything is decided from the utterance the
bots all heard plus what the voice connection already observes:

* :func:`parse_addresses` -- the roster names an utterance OPENS with (""Atlas,
  Aither and Lyra, quick take on X""), the order they were named in, and the
  question with every one of those names stripped. STT spellings and the names
  people actually say (""demi"" for Demiurge, ""hey"" lead-ins) are folded to one
  canonical name.
* :func:`is_council_cue` -- ""discuss / debate / argue / council / weigh in"".
* :func:`is_stop` -- ""enough / wrap it up / stop"": an exact phrase, never a
  question that merely contains the word (""stop the build"" is not a stop).
* :func:`plan_council` -- one round, at most :data:`MAX_AGENTS` agents, the lead
  reserved for a one-line verdict, inside :data:`BUDGET_S` seconds.
* :class:`SlotWaiter` -- the turn order, with no shared state. Its primary signal
  is the SPEAKING events the voice connection already receives for the other bots
  (a bot's own audio is never delivered back to it); bot *k* takes the floor once
  *k* other bots have spoken and the floor is quiet. The fallback is deterministic
  slotting: bot *k* may take the floor after ``k * slot_s`` seconds, and is SKIPPED
  when the floor is busy at that deadline. Nothing is ever answered twice.
* the small prompt/marker builders that let the bots read each other's LINES from
  the text channel the voice channel already owns.

Answered speech never re-enters: the sink still drops every bot's inbound audio,
and this module holds no listener at all -- it only reads speaking START/STOP.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

#: The persona bots that share a voice channel. Each answers to its own name.
ROSTER: Tuple[str, ...] = ("aither", "atlas", "demiurge", "lyra", "iris", "saga",
                           "vera", "hera")

#: What STT hears for each roster name, and what people actually call them.
ALIASES = {
    "aither": ("aither", "aether", "ether", "either", "eather", "ayther", "aitherr",
               "eyther"),
    "atlas": ("atlas", "atlass"),
    "demiurge": ("demiurge", "demi", "demmy", "demmyurge", "demi urge", "demi-urge"),
    "lyra": ("lyra", "lira", "lyrah"),
    "iris": ("iris",),
    "saga": ("saga",),
    "vera": ("vera", "vaira"),
    "hera": ("hera", "heira"),
}

#: The lead: it answers what is addressed to nobody, and gives a council's verdict.
LEAD = "aither"

#: A council is one round of at most this many agents (the lead included).
MAX_AGENTS = 5
#: A whole council round, verdict included, fits this.
BUDGET_S = 45.0
#: The deterministic fallback: bot k may take the floor after k * SLOT_S seconds.
SLOT_S = 12.0

_LABELS = {"aither": "Aither", "atlas": "Atlas", "demiurge": "Demi", "lyra": "Lyra",
           "iris": "Iris", "saga": "Saga", "vera": "Vera", "hera": "Hera"}

#: "hey Aither", "ok Atlas" -- words people put before a name.
_LEADS = ("hey", "hi", "ok", "okay", "yo", "oh", "so")


def _alias_map() -> dict:
    out = {}
    for canonical_name, words in ALIASES.items():
        for word in (canonical_name,) + tuple(words):
            out[word.strip().lower()] = canonical_name
    return out


_ALIAS = _alias_map()
_NAME_RX = re.compile(
    r"(?:" + "|".join(re.escape(w) for w in sorted(_ALIAS, key=len, reverse=True))
    + r")(?:['’]s)?(?![\w'])", re.IGNORECASE)
_LEAD_RX = re.compile(r"^(?:" + "|".join(_LEADS) + r")[\s,.!?;:\-–—]+",
                      re.IGNORECASE)
#: Between two names: punctuation, a conjunction, or just the space between them.
_SEP_RX = re.compile(
    r"^[\s,.!?;:\-·–—'\"]*(?:(?:and\b|&|\+|plus\b)"
    r"[\s,.!?;:\-·–—]*)?", re.IGNORECASE)


def canonical(word: str) -> str:
    """The roster name behind a heard word (""Demi"" -> ""demiurge""), or ""."""
    text = (word or "").strip().lower()
    text = re.sub(r"['’]s$", "", text).strip()
    if text in _ALIAS:
        return _ALIAS[text]
    for alias, name in _ALIAS.items():
        if " " in alias and text.startswith(alias + " "):
            return name
    return text if text in ROSTER else ""


def persona_name(persona: str) -> str:
    """The canonical name a bot answers to; no persona means the lead."""
    name = canonical(persona or "")
    return name or (persona or "").strip().lower() or LEAD


def name_label(name: str) -> str:
    return _LABELS.get((name or "").lower(), (name or "").title())


@dataclass(frozen=True)
class Addressing:
    """How an utterance opened: the names, in order, and the question left over."""
    names: Tuple[str, ...] = ()
    body: str = ""
    raw: str = ""


def parse_addresses(text: str) -> Addressing:
    """Split a leading name list off ``text``.

    ""Atlas, Aither and Lyra, quick take on X"" -> names ("atlas", "aither", "lyra"),
    body ""quick take on X"". One name is still an :class:`Addressing` (the caller
    decides whether that is its own wake word or someone else's); the body has had
    every leading name removed, which is the fix for a bot answering ""Aither, yadda""
    as its question.
    """
    raw = text or ""
    rest = raw.strip()
    lead = _LEAD_RX.match(rest)
    if lead:
        rest = rest[lead.end():]
    names: List[str] = []
    body = rest
    while True:
        m = _NAME_RX.match(rest)
        if m is None:
            body = rest
            break
        found = canonical(m.group(0))
        after = rest[m.end():]
        sep = _SEP_RX.match(after)
        if sep is None:
            body = rest
            break
        rest = after[sep.end():]
        if found and found not in names:
            names.append(found)
        if _NAME_RX.match(rest) is None:
            body = rest
            break
    return Addressing(names=tuple(names), body=body.strip(), raw=raw)


_CUE_RX = re.compile(
    r"\b(?:discuss|discusses|discussing|discussion|debate|debating|argue|arguing|"
    r"argument|council|deliberate|weigh\s+in|weigh-in|round\s*table)\b",
    re.IGNORECASE)


def is_council_cue(text: str) -> bool:
    """True when the body asks for a discussion (""discuss X"", ""quick council"")."""
    return bool(_CUE_RX.search(text or ""))


_STOP_RX = re.compile(
    r"^(?:(?:ok|okay|alright|right|so|and)[\s,.!?;:]+)?"
    r"(?:(?:that['’]?s|that\s+is)\s+)?"
    r"(?:enough(?:\s+(?:of\s+(?:this|that|it)|already|now))?"
    r"|stop(?:\s+(?:it|now|already|there|talking|please))?"
    r"|wrap\s+(?:it|this|that)\s+up"
    r"|cut\s+(?:it|this|that)(?:\s+out)?"
    r"|halt|quit\s+it|settle\s+down)$", re.IGNORECASE)


_REPORT_RX = re.compile(
    r"\b(report|decide|decision|figure\s+(?:it|this|that|\w+)?\s*out|make\s+a\s+call"
    r"|recommend|recommendation|let\s+me\s+know|get\s+back\s+to\s+me|send\s+me)\b",
    re.IGNORECASE)


def wants_report(question: str) -> bool:
    """The owner asked the council for a decision or a report, not just a chat:
    "discuss X and send me a report", "figure out Y and decide"."""
    return bool(_REPORT_RX.search(question or ""))


def report_text(question: str, prior: Iterable[Tuple[str, str]], verdict: str,
                lead: str = LEAD) -> str:
    """The written report of one round: the question, every line, the verdict."""
    rows = ["**Council report**", "Question: " + _flat(question, 300), ""]
    for n, line in (prior or ()):
        if line:
            rows.append("- " + name_label(n) + ": " + _flat(line, 400))
    rows += ["", "Verdict (" + name_label(lead) + "): " + _flat(verdict, 400)]
    return "\n".join(rows)[:1900]


_GO_RX = re.compile(
    r"^(?:yes|yeah|yep|ok(?:ay)?|sure)?[\s,.!]*(?:go\s+(?:with\s+(?:it|that|the\s+verdict)"
    r"|ahead|for\s+it)|approve(?:d)?|do\s+it|ship\s+it|sounds\s+good|agreed|"
    r"let'?s\s+do\s+(?:it|that))\b", re.IGNORECASE)
_NO_RX = re.compile(
    r"^(?:no|nope|nah)?[\s,.!]*(?:no|drop\s+it|reject(?:ed)?|don'?t\s+do\s+(?:it|that)"
    r"|scrap\s+(?:it|that)|veto)\b[\s.!]*$", re.IGNORECASE)


def card_answer(text: str) -> str:
    """The owner's spoken answer to the council's card: "go", "no", or ""."""
    body = (text or "").strip()
    if _GO_RX.search(body):
        return "go"
    if _NO_RX.search(body):
        return "no"
    return ""


def is_stop(text: str) -> bool:
    """An exact ""stop this council"" phrase -- never ""stop the build""."""
    t = (text or "").strip().strip(" \t.!?,;:").strip()
    return bool(t) and bool(_STOP_RX.match(t))


@dataclass(frozen=True)
class CouncilPlan:
    """Who speaks, in what order, and who sums up."""
    speakers: Tuple[str, ...] = ()
    lead: str = LEAD
    capped: bool = False
    budget_s: float = BUDGET_S


def plan_council(names: Iterable[str], lead: str = LEAD,
                 max_agents: int = MAX_AGENTS,
                 budget_s: float = BUDGET_S) -> CouncilPlan:
    """Order the named bots, cap the round, and keep the lead for the verdict.

    The lead is a participant either way (it gives the verdict), so at most
    ``max_agents - 1`` other names speak. ``capped`` says names were dropped.
    """
    ordered: List[str] = []
    for name in names or ():
        name = canonical(name) or name
        if name and name not in ordered:
            ordered.append(name)
    others = [n for n in ordered if n != lead]
    cap = max(0, int(max_agents) - 1)
    speakers = tuple(others[:cap])
    return CouncilPlan(speakers=speakers, lead=lead, capped=len(others) > cap,
                       budget_s=float(budget_s))


def slot_delay(index: int, slot_s: float = SLOT_S) -> float:
    """How long bot ``index`` waits before the floor is allowed to be its."""
    return max(0, int(index)) * max(0.0, float(slot_s))


class SlotWaiter:
    """Take turns on a shared floor with no server between the bots.

    ``floor_busy`` says a bot is speaking right now; ``spoke`` counts the distinct
    other bots whose SPEAKING events were seen since :meth:`reset`; ``abort`` ends
    the wait (a human said ""enough""). The wait ends in one of three verdicts:
    ""now"" (take the floor), ""skipped"" (bot k's turn came and the floor was still
    busy), or ""aborted"" (the round was stopped).

    Clock and sleep are injected so the fallback path is testable without waiting.
    """

    def __init__(self, floor_busy: Callable[[], bool],
                 spoke: Optional[Callable[[], int]] = None,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep,
                 poll_s: float = 0.1,
                 abort: Optional[Callable[[], bool]] = None) -> None:
        self._floor_busy = floor_busy
        self._spoke = spoke
        self._clock = clock
        self._sleep = sleep
        self._poll = max(0.01, float(poll_s))
        self._abort = abort

    def wait(self, index: int, slot_s: float = SLOT_S,
             max_wait_s: Optional[float] = None) -> str:
        """Wait for turn ``index`` (0 = first). Returns now/skipped/aborted."""
        if self._aborted():
            return "aborted"
        if index <= 0:
            return "now"
        deadline = self._clock() + slot_delay(index, slot_s)
        if max_wait_s is not None:
            deadline = min(deadline, self._clock() + max(0.0, float(max_wait_s)))
        while True:
            if self._aborted():
                return "aborted"
            heard = int(self._spoke() or 0) if self._spoke is not None else 0
            if heard >= index and not self._floor_busy():
                return "now"
            remaining = deadline - self._clock()
            if remaining <= 0:
                return "skipped" if self._floor_busy() else "now"
            self._sleep(min(self._poll, remaining))

    def _aborted(self) -> bool:
        return bool(self._abort is not None and self._abort())


class CouncilRound:
    """One round's state INSIDE one bot process (there is no shared server).

    ``stop`` is set when a human says ""enough""; ``verdict_claimed`` makes sure the
    lead says the verdict once even when the pending speaker thread and the stop
    handler race to produce it.
    """

    def __init__(self, question: str, speakers: Sequence[str], lead: str = LEAD,
                 budget_s: float = BUDGET_S, now: Optional[float] = None) -> None:
        self.question = question or ""
        self.speakers = tuple(speakers)
        self.lead = lead or LEAD
        self.deadline = (time.monotonic() if now is None else float(now)) \
            + max(0.0, float(budget_s))
        #: Wall-clock start: the window the members' earlier lines are read from.
        #: ``deadline`` is monotonic, so it can never be compared to a Discord
        #: timestamp; a fixed "last 5 seconds" window dropped the first lines of a
        #: round as soon as a slot or the verdict came later than that.
        self.started_wall = time.time()
        self.stop = threading.Event()
        self.verdict_claimed = threading.Event()


_SENTENCE_RX = re.compile(r"(?<=[.!?])\s+")


def _flat(text: str, limit: int = 400) -> str:
    flat = " ".join((text or "").split()).strip()
    return flat[:max(0, int(limit))].rstrip()


def one_line(text: str, limit: int = 240) -> str:
    """The first sentence, on one line, at most ``limit`` characters.

    Spoken answers are heard: a council line that runs four sentences holds the
    floor four times as long as it was given.
    """
    flat = " ".join((text or "").split()).strip()
    if not flat:
        return ""
    first = _SENTENCE_RX.split(flat, maxsplit=1)[0].strip()
    if len(first) > limit:
        first = first[:max(0, limit - 3)].rstrip(" ,;:") + "..."
    return first


_MARKER_RX = re.compile(r"^\*\*Council\*\*\s+([A-Za-z]+):\s*(.+)$", re.DOTALL)


def council_line(name: str, line: str) -> str:
    """The text-chat record of one council line (how bots read each other)."""
    return "**Council** " + name_label(name) + ": " + _flat(line)


def prior_lines(messages: Iterable[dict], names: Iterable[str] = (),
                limit: int = MAX_AGENTS, since_iso: str = "") -> List[Tuple[str, str]]:
    """Council lines already posted to the text chat, oldest first.

    ``since_iso`` is compared as a string prefix against Discord's ISO-8601
    ``timestamp`` (same format, UTC), which keeps a previous round out of this one.
    """
    want = {canonical(n) or n for n in (names or ())}
    out: List[Tuple[str, str]] = []
    # Discord returns messages NEWEST first; read them oldest first so the lines
    # reach the next speaker in the order they were said.
    rows = list(messages or ())
    rows.reverse()
    for message in rows:
        if not isinstance(message, dict):
            continue
        stamp = str(message.get("timestamp") or "")
        if since_iso and stamp and stamp < since_iso:
            continue
        m = _MARKER_RX.match(str(message.get("content") or "").strip())
        if m is None:
            continue
        who = canonical(m.group(1))
        if want and who not in want:
            continue
        line = _flat(m.group(2))
        if line:
            out.append((who, line))
    return out[-max(1, int(limit)):]


def member_prompt(question: str, prior: Iterable[Tuple[str, str]], name: str) -> str:
    """What one council member sees: the question, and the lines before its own."""
    head = ("You are in a quick council in a voice channel. The question: "
            + (question or "").strip())
    lines = [(n, line) for n, line in (prior or ()) if line]
    if lines:
        head += "\n\nWhat the others have said so far:\n" + "\n".join(
            name_label(n) + ": " + line for n, line in lines)
    return (head + "\n\nAnswer as " + name_label(name)
            + " in ONE short spoken sentence. Do not repeat the others; add your own "
              "view in a few words.")


def verdict_prompt(question: str, prior: Iterable[Tuple[str, str]],
                   stopped: bool = False, lead: str = LEAD) -> str:
    """What the lead sees before it gives the room its one-line verdict."""
    head = ("You are " + name_label(lead) + ", the lead, closing a quick council. "
            "The question: " + (question or "").strip())
    lines = [(n, line) for n, line in (prior or ()) if line]
    if lines:
        head += "\n\nThe lines so far:\n" + "\n".join(
            name_label(n) + ": " + line for n, line in lines)
    else:
        head += "\n\nNo member managed to speak."
    if stopped:
        head += "\n\nThe human asked to wrap it up early."
    return head + "\n\nGive the room ONE short spoken sentence: your verdict."
