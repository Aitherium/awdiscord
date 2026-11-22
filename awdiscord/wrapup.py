"""Wrap up a channel conversation: positions, agreement, open questions, resolutions, due-outs.

A long discussion ends with nobody writing down what was decided or who said they
would do what. ``build`` reads the channel since a point (a message link, a
duration, or a default window), asks the server's model for a structured wrap-up,
and then GROUNDS every line of the answer in the fetched history before anything
is shown:

* every item must cite message numbers that exist in the history it was given;
* a person named in an item must be the AUTHOR of a message that item cites --
  names come from message authors, never from the model;
* a due-out's date survives only when it is a date-like phrase copied from a cited
  message; otherwise the item says ``date: ask owner of item``;
* model prose carrying a first-person voice or an editorial judgement is dropped:
  the wrap-up reports stated positions, it never takes a side.

A deterministic floor runs beside the model: commitments in the first person
("I should email them", "once I have time to finish it") always become due-outs,
links are listed from the messages themselves, and when the model is unreachable
the floor alone still produces a wrap-up of quoted, cited lines.

This module reads REST and talks to a model; it never posts. ``builtin/wrapup.py``
wires the command, the message menu, the mention, the buttons and the task board.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from awdiscord import turn_context

log = logging.getLogger("awdiscord.wrapup")

DISCORD_EPOCH_MS = 1420070400000
PAGE = 100
MAX_MESSAGES = 400
DEFAULT_WINDOW_S = 12 * 3600
MAX_WINDOW_S = 7 * 86400
LINE_CHARS = 450
PROMPT_CHARS = 30000
DATE_MISSING = "date: ask owner of item"
MAX_ITEMS = 10                  # per section; the post has to stay readable
#: What the model is asked for per list: its answer must fit the agent's token budget
#: (a JSON cut off mid-object is no wrap-up at all).
PROMPT_ITEMS = 6
#: Human message types: 0 default, 19 reply, 20 slash-command result is a bot's.
HUMAN_TYPES = (0, 19)

_DURATION_RE = re.compile(
    r"^\s*(?:(\d+)\s*d(?:ays?)?)?\s*(?:(\d+)\s*h(?:ours?|rs?)?)?\s*"
    r"(?:(\d+)\s*m(?:in(?:utes?)?)?)?\s*$", re.I)
_LINK_RE = re.compile(
    r"https?://(?:\w+\.)?discord(?:app)?\.com/channels/(\d{1,25})/(\d{1,25})/(\d{1,25})")
_URL_RE = re.compile(r"https?://[^\s<>()\[\]\"']+")
#: First-person commitments. "we should" is a group sentiment, not an owner.
_COMMIT_RE = re.compile(
    r"\b(?:i\s*(?:'ll|will|shall|should(?:\s+really)?|need\s+to|plan\s+to|"
    r"am\s+going\s+to|'m\s+going\s+to|am\s+gonna|'m\s+gonna|am\s+working\s+on|"
    r"'m\s+working\s+on|started\s+working\s+on|am\s+writing\s+up|'m\s+writing\s+up|"
    r"can\s+take|volunteer)|(?:once|when)\s+i\s+have\s+time\s+to\s+\w+)\b", re.I)
_QUESTION_START_RE = re.compile(
    r"^\s*(?:which|what|who|whom|how|why|when|where|did|does|do|can|could|should|"
    r"would|is|are|will)\b", re.I)
_ASK_RE = re.compile(r"\b(?:kindly\s+offer|can\s+someone|could\s+someone|any\s+examples?|"
                     r"give\s+an\s+example|offer\s+an\s+example)\b", re.I)
#: Model prose in a first-person or editorial voice (quoted spans are removed first).
_FIRST_PERSON_RE = re.compile(
    r"\b(?:i|i'm|i'd|i've|i'll|me|my|mine|we|we're|we've|we'll|our|ours|us)\b", re.I)
_EDITORIAL_RE = re.compile(
    r"\b(?:clearly|obviously|rightly|wrongly|unfortunately|sadly|thankfully|"
    r"absurd|ridiculous|naive|misguided|great idea|good idea|bad idea|best idea|"
    r"correctly|incorrectly|is right|is wrong|are right|are wrong)\b", re.I)
#: "max asks which..." -> "Which...": the renderer already says who asks.
_ASKS_RE = re.compile(r"^\s*[\w .|'-]{1,40}?\s+(?:asks|asked|wants to know)\s*"
                      r"(?:whether|if)?\s*:?\s*", re.I)
_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”")
#: A due phrase worth keeping: a weekday, a month, a date, "tomorrow", "by EOD"...
_DATEISH_RE = re.compile(
    r"\b(?:mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|\b(?:jan|feb|mar|apr|may|jun|jul|aug|"
    r"sep|oct|nov|dec)[a-z]*\.?\s+\d{1,2}\b|\b\d{4}-\d\d-\d\d\b|\b\d{1,2}/\d{1,2}\b|"
    r"\btomorrow\b|\btonight\b|\bnext\s+(?:week|month)\b|\bend\s+of\s+(?:the\s+)?"
    r"(?:day|week|month)\b|\beod\b|\beow\b|\bthis\s+(?:week|weekend|month)\b", re.I)
_STOP = frozenset("about after again also always around because before being could "
                  "doing every going gonna maybe might other really should something "
                  "their there these thing things think those through would".split())


class WrapupError(Exception):
    """A wrap-up could not be built; the message is safe to show a member."""


# ── where the conversation starts ─────────────────────────────────────────────

def snowflake_at(epoch_s: float) -> int:
    """The smallest Discord id at ``epoch_s`` (an ``after=`` cursor for a time)."""
    return max(0, int(epoch_s * 1000) - DISCORD_EPOCH_MS) << 22


def snowflake_time(sid: Any) -> float:
    """Unix seconds a Discord id was minted at; 0 when it is not an id."""
    try:
        return ((int(str(sid)) >> 22) + DISCORD_EPOCH_MS) / 1000.0
    except (TypeError, ValueError):
        return 0.0


def parse_duration(text: str) -> int | None:
    """Seconds in ``"90m"``, ``"3h"``, ``"1d 2h"``, ``"8h30m"``; None when not a duration."""
    got = _DURATION_RE.match(str(text or ""))
    if not got or not any(got.groups()):
        return None
    d, h, m = (int(x or 0) for x in got.groups())
    seconds = d * 86400 + h * 3600 + m * 60
    return seconds or None


@dataclass
class Since:
    """Where reading starts: after message ``after`` (exclusive), plus ``anchor`` itself."""
    after: int
    anchor: str = ""            # a linked/clicked message, read and included
    label: str = ""             # "since the linked message", "the last 9h"
    channel: str = ""           # the channel a link points into ("" = this one)
    guild: str = ""


def parse_since(value: str, *, now: float | None = None,
                default_s: int = DEFAULT_WINDOW_S) -> Since:
    """A message link, a duration, or empty (the default window). Raises WrapupError."""
    now = time.time() if now is None else now
    text = str(value or "").strip()
    link = _LINK_RE.search(text)
    if link:
        guild, channel, mid = link.groups()
        return Since(after=int(mid), anchor=mid, label="since the linked message",
                     channel=channel, guild=guild)
    if text:
        seconds = parse_duration(text)
        if seconds is None:
            raise WrapupError("`since` takes a message link or a duration like 3h, 90m "
                              "or 1d.")
        seconds = min(seconds, MAX_WINDOW_S)
        return Since(after=snowflake_at(now - seconds), label=f"the last {_dur(seconds)}")
    return Since(after=snowflake_at(now - default_s), label=f"the last {_dur(default_s)}")


def _dur(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    out = "".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m")) if v)
    return out or "0m"


# ── the history ───────────────────────────────────────────────────────────────

def fetch(rest, channel: str, since: Since, cap: int = MAX_MESSAGES) -> tuple[list, bool]:
    """``(messages oldest first, truncated)`` after ``since``, the anchor included.

    Pages forward with ``after=``; stops at ``cap`` messages (``truncated`` True).
    Raises what ``rest`` raises: the caller says the history could not be read.
    """
    seen: dict[str, dict] = {}
    if since.anchor:
        try:
            first = rest.request("GET", f"/channels/{channel}/messages/{since.anchor}")
            if isinstance(first, dict) and first.get("id"):
                seen[str(first["id"])] = first
        except Exception as exc:  # noqa: BLE001 - the anchor is context; the rest reads
            log.info("wrapup: anchor message unreadable (%s)", type(exc).__name__)
    cursor = int(since.after)
    truncated = False
    while True:
        page = rest.request(
            "GET", f"/channels/{channel}/messages?limit={PAGE}&after={cursor}") or []
        if not isinstance(page, list) or not page:
            break
        for m in page:
            if isinstance(m, dict) and m.get("id"):
                seen[str(m["id"])] = m
        cursor = max(int(str(m["id"])) for m in page if isinstance(m, dict) and m.get("id"))
        if len(seen) >= cap:
            truncated = True
            break
        if len(page) < PAGE:
            break
    out = sorted(seen.values(), key=lambda m: int(str(m["id"])))
    return out[:cap], truncated


@dataclass
class Line:
    """One human message, numbered for the model: ``[n] name: text``."""
    n: int
    id: str
    author_id: str
    name: str
    text: str
    ts: float


def lines_of(messages: list[dict], bot_id: str = "") -> list[Line]:
    """Human messages as numbered lines, oldest first.

    Bots (this one included), system messages, empty messages and anything the
    abuse gate withholds are left out: a wrap-up reports the people.
    """
    out: list[Line] = []
    for m in sorted(messages or [], key=lambda x: int(str(x.get("id") or 0))):
        author = m.get("author") or {}
        if author.get("bot") or (bot_id and str(author.get("id")) == str(bot_id)):
            continue
        if int(m.get("type") or 0) not in HUMAN_TYPES:
            continue
        if not turn_context.usable(m) or turn_context.withheld(m):
            continue
        text = turn_context.neutralise(" ".join(turn_context.message_text(m).split()))
        if len(text) > LINE_CHARS:
            text = text[:LINE_CHARS - 3].rstrip() + "..."
        name = turn_context.clean_name(author.get("global_name") or author.get("username")
                                       or author.get("id"))
        out.append(Line(n=len(out) + 1, id=str(m["id"]), author_id=str(author.get("id")),
                        name=name, text=text, ts=snowflake_time(m["id"])))
    return out


def people(lines: list[Line]) -> dict[str, str]:
    """{author id: display name} in order of first message."""
    out: dict[str, str] = {}
    for ln in lines:
        out.setdefault(ln.author_id, ln.name)
    return out


# ── the model ─────────────────────────────────────────────────────────────────

SCHEMA = (
    '{"positions": [{"who": NAME, "text": "...", "cite": [N]}],\n'
    ' "agreement": [{"text": "...", "cite": [N]}],\n'
    ' "open_questions": [{"who": NAME, "text": "...", "cite": [N]}],\n'
    ' "resolutions": [{"text": "...", "cite": [N]}],\n'
    ' "due_outs": [{"owner": NAME, "action": "...", "due": "", "cite": [N]}]}')


def prompt(lines: list[Line], channel_name: str = "") -> str:
    """The instruction and the numbered transcript, kept under ``PROMPT_CHARS``."""
    names = ", ".join(f'"{n}"' for n in people(lines).values())
    head = (
        "You are a neutral facilitator writing the wrap-up of a community discussion"
        + (f" in #{channel_name}" if channel_name else "") + ". Reply with ONE JSON "
        "object and nothing else, in exactly this shape:\n" + SCHEMA + "\n\nRules:\n"
        "- positions: what each person argued, one entry per distinct idea, credited "
        "to them. Report it as their stated position (\"X says ...\", \"X proposes "
        "...\"). Healthcare, politics and similar topics are summarised as stated "
        "positions only. Cover EVERY participant who stated a view, and never credit "
        "one person with another person's point.\n"
        "- Write out what a pronoun refers to (\"email them\" -> \"email ArtCraft\") "
        "from the earlier messages; when it is unclear, quote the words instead.\n"
        "- You are neutral: never give your own opinion, never judge or rank an idea, "
        "never write I, we, our or us yourself, never call anything right, wrong, "
        "good or bad.\n"
        "- agreement: points more than one person supported.\n"
        "- open_questions: questions asked that the discussion did not settle; who = "
        "the person who asked.\n"
        "- resolutions: concrete proposals the group can accept or reject, drawn only "
        "from what was said. Phrase them as proposals (\"Contact X about Y\").\n"
        "- due_outs: ONLY where a person said THEY would or should do something. owner "
        "= that person. due = a date or deadline the person stated, copied word for "
        "word; \"\" when they stated none. Never invent a date or an owner.\n"
        f"- who and owner must be exactly one of: {names}.\n"
        "- Every entry cites the message numbers it comes from in cite.\n"
        f"- At most {PROMPT_ITEMS} entries per list, one short sentence each. "
        "No markdown.\n\nMessages:\n")
    budget = PROMPT_CHARS - len(head)
    body = [f"[{ln.n}] {ln.name}: {ln.text}" for ln in lines]
    while body and sum(len(b) + 1 for b in body) > budget:
        # Over budget: shorten the longest line first, never drop a person's words.
        i = max(range(len(body)), key=lambda k: len(body[k]))
        if len(body[i]) <= 120:
            body.pop(0)
            continue
        body[i] = body[i][: max(120, len(body[i]) * 2 // 3)].rstrip() + "..."
    return head + "\n".join(body)


def parse_json(text: str) -> dict:
    """The first JSON object in a model answer (fences and chatter around it ignored)."""
    raw = str(text or "")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        out = json.loads(raw[start:end + 1])
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


# ── grounding ─────────────────────────────────────────────────────────────────

@dataclass
class Item:
    text: str
    cite: list[str]                       # message ids
    who: str = ""                         # author id ("" for group items)
    due: str = ""
    source: str = "model"                 # model | floor


@dataclass
class Wrapup:
    channel: str
    guild: str
    channel_name: str = ""
    label: str = ""
    start: float = 0.0
    end: float = 0.0
    count: int = 0
    truncated: bool = False
    people: dict = field(default_factory=dict)          # author id -> name
    positions: list = field(default_factory=list)
    agreement: list = field(default_factory=list)
    open_questions: list = field(default_factory=list)
    resolutions: list = field(default_factory=list)
    due_outs: list = field(default_factory=list)
    links: list = field(default_factory=list)            # [{"url", "who", "cite"}]
    dropped: dict = field(default_factory=dict)          # reason -> count
    model: str = "ok"                                    # ok | unavailable: ...

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Wrapup":
        w = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        for sec in ("positions", "agreement", "open_questions", "resolutions", "due_outs"):
            setattr(w, sec, [Item(**i) if isinstance(i, dict) else i
                             for i in getattr(w, sec)])
        return w


def opinionated(text: str) -> bool:
    """Model prose in a first-person or editorial voice (quotes excluded)."""
    bare = _QUOTED_RE.sub(" ", str(text or ""))
    return bool(_FIRST_PERSON_RE.search(bare) or _EDITORIAL_RE.search(bare))


def _norm(name: str) -> str:
    return " ".join(turn_context.clean_name(name).lower().split())


def resolve_name(name: str, roster: dict[str, str]) -> str:
    """The author id a model-written name means; "" unless it is unambiguous."""
    want = _norm(name)
    if not want or want == "someone":
        return ""
    exact = [uid for uid, n in roster.items() if _norm(n) == want]
    if len(exact) == 1:
        return exact[0]
    tokens = [uid for uid, n in roster.items()
              if want in _norm(n).split() or _norm(n).startswith(want + " ")]
    return tokens[0] if len(tokens) == 1 else ""


def _cites(item: dict, by_n: dict[int, Line]) -> list[Line]:
    out: list[Line] = []
    raw = item.get("cite")
    for x in raw if isinstance(raw, list) else [raw]:
        try:
            n = int(str(x).strip().strip("[]#"))
        except (TypeError, ValueError):
            continue
        if n in by_n and by_n[n] not in out:
            out.append(by_n[n])
    return out


def _clean(text: Any, limit: int = 300) -> str:
    out = turn_context.neutralise(" ".join(str(text or "").split()))
    out = re.sub(r"<@[!&]?\d+>", "", out)              # the model never pings anyone
    out = out.replace("@everyone", "everyone").replace("@here", "here")
    return out if len(out) <= limit else out[:limit - 3].rstrip() + "..."


def _due(raw: str, cited: list[Line]) -> str:
    """The stated date, only when copied from a cited message and date-like."""
    due = " ".join(str(raw or "").split())
    if not due or not _DATEISH_RE.search(due):
        return DATE_MISSING
    if not any(due.lower() in ln.text.lower() for ln in cited):
        return DATE_MISSING
    return due[:60]


def ground(raw: dict, lines: list[Line]) -> tuple[dict[str, list[Item]], dict[str, int]]:
    """Every model item checked against the history; ``(sections, dropped counts)``."""
    by_n = {ln.n: ln for ln in lines}
    roster = people(lines)
    dropped: dict[str, int] = {}

    def drop(reason: str) -> None:
        dropped[reason] = dropped.get(reason, 0) + 1

    out: dict[str, list[Item]] = {k: [] for k in (
        "positions", "agreement", "open_questions", "resolutions", "due_outs")}
    for section, person_key in (("positions", "who"), ("agreement", ""),
                                ("open_questions", "who"), ("resolutions", ""),
                                ("due_outs", "owner")):
        rows = raw.get(section) if isinstance(raw, dict) else None
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                drop("malformed")
                continue
            text = _clean(row.get("action" if section == "due_outs" else "text"))
            cited = _cites(row, by_n)
            if not text:
                drop("empty")
                continue
            if not cited:
                drop("uncited")
                continue
            if opinionated(text):
                drop("not neutral")
                continue
            who = ""
            if person_key:
                who = resolve_name(str(row.get(person_key) or ""), roster)
                if not who:
                    drop("unknown person")
                    continue
                if who not in {ln.author_id for ln in cited}:
                    drop("person not the author of the cited message")
                    continue
            if section == "open_questions":
                text = _ASKS_RE.sub("", text, count=1).strip() or text
                text = text[:1].upper() + text[1:]
            due = _due(str(row.get("due") or ""), cited) if section == "due_outs" else ""
            if any(i.text.lower() == text.lower() for i in out[section]):
                continue
            if len(out[section]) >= MAX_ITEMS:
                drop("over the cap")
                continue
            out[section].append(Item(text=text, cite=[ln.id for ln in cited], who=who,
                                     due=due))
    return out, dropped


# ── the deterministic floor ───────────────────────────────────────────────────

def _sentence(text: str, start: int) -> str:
    """The sentence of ``text`` around index ``start``."""
    left = max(text.rfind(c, 0, start) for c in ".!?\n") + 1
    rights = [i for i in (text.find(c, start) for c in ".!?\n") if i >= 0]
    right = min(rights) if rights else len(text)
    return text[left:right].strip(" .,;:-")


def _words(text: str) -> set[str]:
    """Content-word stems (first five letters): "secure" and "security" meet."""
    return {w[:5] for w in re.findall(r"[a-z]{5,}", text.lower()) if w not in _STOP}


def commitments(lines: list[Line]) -> list[Item]:
    """First-person commitments, owner = the author, quoted verbatim."""
    out: list[Item] = []
    for ln in lines:
        got = _COMMIT_RE.search(ln.text)
        if not got:
            continue
        said = _sentence(ln.text, got.start())
        if len(said) > 160:
            said = said[:157].rstrip() + "..."
        same = next((i for i in out if i.who == ln.author_id
                     and _words(i.text) & _words(said)), None)
        if same is not None:
            same.cite.append(ln.id)
            continue
        out.append(Item(text=f"“{said}”", cite=[ln.id], who=ln.author_id,
                        due=DATE_MISSING, source="floor"))
    return out


def questions(lines: list[Line]) -> list[Item]:
    """Questions people asked, quoted (used when the model gave none)."""
    out: list[Item] = []
    for ln in lines:
        text = ln.text
        if not (text.rstrip().endswith("?") or _ASK_RE.search(text)
                or (_QUESTION_START_RE.match(text) and len(text.split()) <= 25)):
            continue
        if len(text.split()) < 3:
            continue
        said = text if len(text) <= 200 else text[:197].rstrip() + "..."
        out.append(Item(text=f"“{said}”", cite=[ln.id], who=ln.author_id,
                        source="floor"))
    return out[:MAX_ITEMS]


def quoted_positions(lines: list[Line]) -> list[Item]:
    """Each person's longest message, quoted (used when the model is unreachable)."""
    best: dict[str, Line] = {}
    for ln in lines:
        if ln.author_id not in best or len(ln.text) > len(best[ln.author_id].text):
            best[ln.author_id] = ln
    out = []
    for ln in best.values():
        said = ln.text if len(ln.text) <= 220 else ln.text[:217].rstrip() + "..."
        out.append(Item(text=f"“{said}”", cite=[ln.id], who=ln.author_id,
                        source="floor"))
    return out[:MAX_ITEMS]


def links(lines: list[Line]) -> list[dict]:
    """Every URL people posted, once, with who posted it first."""
    out: list[dict] = []
    seen: set[str] = set()
    for ln in lines:
        for url in _URL_RE.findall(ln.text):
            url = url.rstrip(".,;:!?")
            if _LINK_RE.match(url) or url.lower() in seen:
                continue
            seen.add(url.lower())
            out.append({"url": url, "who": ln.author_id, "cite": ln.id})
    return out[:MAX_ITEMS]


def merge_due_outs(grounded: list[Item], floor: list[Item]) -> list[Item]:
    """The model's grounded due-outs plus every floor commitment it missed."""
    out = list(grounded)
    for f in floor:
        covered = any(g.who == f.who and (set(g.cite) & set(f.cite)
                                          or _words(g.text) & _words(f.text))
                      for g in out)
        if not covered and len(out) < MAX_ITEMS:
            out.append(f)
    return out


# ── the whole thing ───────────────────────────────────────────────────────────

AskFn = Callable[[str], str]


def build(lines: list[Line], ask: AskFn | None, *, channel: str, guild: str,
          channel_name: str = "", label: str = "", truncated: bool = False) -> Wrapup:
    """A grounded wrap-up of ``lines``. ``ask(prompt) -> text`` reaches the model.

    ``ask`` None, raising, or answering no usable JSON = the floor alone (quoted
    positions and questions); ``Wrapup.model`` says which.
    """
    if not lines:
        raise WrapupError("There is nothing to wrap up: no messages from people in that "
                          "window.")
    w = Wrapup(channel=channel, guild=guild, channel_name=channel_name, label=label,
               start=lines[0].ts, end=lines[-1].ts, count=len(lines),
               truncated=truncated, people=people(lines))
    raw: dict = {}
    if ask is None:
        w.model = "unavailable: no model answers in this server"
    else:
        try:
            raw = parse_json(ask(prompt(lines, channel_name)))
            if not raw:
                w.model = "unavailable: the model's answer was not a wrap-up"
        except Exception as exc:  # noqa: BLE001 - the floor still answers
            w.model = f"unavailable: {type(exc).__name__}"
    sections, dropped = ground(raw, lines)
    w.dropped = dropped
    w.positions = sections["positions"] or quoted_positions(lines)
    w.agreement = sections["agreement"]
    w.open_questions = sections["open_questions"] or (
        questions(lines) if not raw else [])
    w.resolutions = sections["resolutions"]
    w.due_outs = merge_due_outs(sections["due_outs"], commitments(lines))
    w.links = links(lines)
    return w


# ── rendering ─────────────────────────────────────────────────────────────────

KEYCAPS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣",
           "5️⃣", "6️⃣", "7️⃣", "8️⃣",
           "9️⃣"]
STATUS_MARK = {"open": "☐ open", "claimed": "⏳ claimed", "done": "✅ done"}
EMBED_COLOR = 0x5865F2
CHIME_IN = ("**Chime in:** reply here to add, correct or disagree with anything above. "
            "React with a number to support a proposed resolution. Owners can claim or "
            "close their due-outs with the buttons.")


def jump(guild: str, channel: str, mid: str) -> str:
    return f"https://discord.com/channels/{guild}/{channel}/{mid}"


def _refs(w: Wrapup, ids: list[str], mention: bool) -> str:
    if not ids:
        return ""
    if not mention:
        return " (" + ", ".join(f"msg {i[-6:]}" for i in ids[:3]) + ")"
    return " (" + ", ".join(f"[src]({jump(w.guild, w.channel, i)})" for i in ids[:2]) + ")"


def _who(w: Wrapup, uid: str, mention: bool) -> str:
    if not uid:
        return ""
    return f"<@{uid}>" if mention else f"**{w.people.get(uid, 'someone')}**"


def _fit(lines: list[str], limit: int) -> str:
    out: list[str] = []
    used = 0
    for i, ln in enumerate(lines):
        if used + len(ln) + 1 > limit - 20:
            out.append(f"(+{len(lines) - i} more)")
            break
        out.append(ln)
        used += len(ln) + 1
    return "\n".join(out)


def sections(w: Wrapup, statuses: dict | None = None,
             mention: bool = True) -> list[tuple[str, list[str]]]:
    """``[(title, lines)]`` in post order. ``statuses`` = {due-out number: (state, by)}."""
    statuses = statuses or {}
    pos = [f"• {_who(w, i.who, mention)}: {i.text}{_refs(w, i.cite, mention)}"
           for i in w.positions]
    agree = [f"• {i.text}{_refs(w, i.cite, mention)}" for i in w.agreement]
    qs = [f"• {_who(w, i.who, mention)} asks: {i.text}{_refs(w, i.cite, mention)}"
          for i in w.open_questions]
    res = [f"{KEYCAPS[k] if k < len(KEYCAPS) else '-'} {i.text}"
           f"{_refs(w, i.cite, mention)}" for k, i in enumerate(w.resolutions)]
    due = []
    for k, i in enumerate(w.due_outs, 1):
        state, by = statuses.get(k, ("open", ""))
        mark = STATUS_MARK.get(state, state)
        if by and state != "open":
            mark += f" by {_who(w, by, mention) if by in w.people or mention else by}"
        due.append(f"`D{k}` {_who(w, i.who, mention)} → {i.text} · "
                   f"*{i.due or DATE_MISSING}* · {mark}{_refs(w, i.cite, mention)}")
    lnk = [f"• <{x['url']}> ({_who(w, x['who'], mention)})" if mention
           else f"- {x['url']} ({w.people.get(x['who'], 'someone')})" for x in w.links]
    return [("Where people stand", pos),
            ("Points of agreement", agree or ["(none stated yet)"]),
            ("Open questions", qs or ["(none)"]),
            ("Proposed resolutions (react with the number to support one)",
             res or ["(none proposed yet)"]),
            ("Due-outs", due or ["(nobody committed to an action)"]),
            ("Links mentioned", lnk or ["(none)"])]


def headline(w: Wrapup) -> str:
    where = f"<#{w.channel}>" if w.channel else "this channel"
    span = (f"<t:{int(w.start)}:t> to <t:{int(w.end)}:t>" if w.start and w.end else
            w.label)
    return f"**Wrap-up of {where}**, {span} · {len(w.people)} people, {w.count} messages"


def footer(w: Wrapup) -> str:
    parts = ["Every line links the message it came from. Written by Aither; it takes "
             "no side."]
    if w.truncated:
        parts.append(f"Only the first {w.count} messages of the window were read.")
    if w.model != "ok":
        parts.append("The model was unreachable, so positions are quoted directly.")
    return "-# " + " ".join(parts)


def payload(w: Wrapup, statuses: dict | None = None) -> dict:
    """The one top-level post: headline + chime-in in content, sections as embeds."""
    secs = sections(w, statuses, mention=True)
    budgets = [2000, 700, 900, 900, 1300, 600]          # 6400 > 6000: _fit trims below
    embeds = []
    total = 0
    for (title, rows), budget in zip(secs, budgets):
        room = min(budget, 5800 - total)
        if room < 80:
            break
        desc = _fit(rows, room)
        total += len(title) + len(desc)
        embeds.append({"title": title, "description": desc, "color": EMBED_COLOR})
    content = f"{headline(w)}\n{CHIME_IN}\n{footer(w)}"
    return {"content": content[:2000], "embeds": embeds[:10],
            "allowed_mentions": {"parse": []}}


def plain(w: Wrapup, statuses: dict | None = None) -> str:
    """The same wrap-up as plain text (the CLI dry run, logs)."""
    when = w.label or time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(w.start))
    out = [f"Wrap-up of #{w.channel_name or w.channel}, {when} - "
           f"{len(w.people)} people, {w.count} messages (model: {w.model})"]
    for title, rows in sections(w, statuses, mention=False):
        out.append(f"\n## {title}")
        out.extend(rows)
    out.append("\n" + CHIME_IN)
    if w.dropped:
        out.append("-# grounding dropped: " + ", ".join(
            f"{k} x{v}" for k, v in sorted(w.dropped.items())))
    return "\n".join(out)


def ask_patiently(agent, text: str, *, user: str = "", patience_s: float = 780.0,
                  pause_s: float = 15.0, on_wait: Callable[[], None] | None = None,
                  sleep: Callable[[float], None] | None = None,
                  clock: Callable[[], float] | None = None) -> str:
    """``agent.ask`` that waits out a busy model queue instead of failing.

    A busy/timeout error (``dispatch._is_busy``) is retried every ``pause_s`` until
    ``patience_s`` has passed; ``on_wait`` runs once, on the first wait, so the
    caller can edit in a progress line. Any other error raises at once.
    """
    from awdiscord.dispatch import _is_busy
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    deadline = clock() + patience_s
    told = False
    while True:
        try:
            return agent.ask(text, user=user)
        except Exception as exc:  # noqa: BLE001 - busy is retried, the rest re-raised
            if not _is_busy(exc) or clock() + pause_s > deadline:
                raise
            if not told and on_wait is not None:
                told = True
                on_wait()
            sleep(pause_s)
