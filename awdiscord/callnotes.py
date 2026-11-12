"""Calls that run themselves: join, keep notes that survive a restart, file the work.

Everything here is plain logic with no Discord import, so it is tested with fakes.
:mod:`awdiscord.voice` is the transport that calls it.

* **Opt-in per server.** ``voice_calls_on`` is true only for a server whose store has
  ``core/voice_calls`` set, or whose id is in ``AWDISCORD_VOICE_AUTO_GUILDS``. Every
  other server keeps the old behaviour: the bot joins when asked and records only when
  told.
* **Joining and leaving.** ``next_move`` decides, from who is in which voice channel,
  whether the bot joins, stays, moves or leaves.
* **A journal.** Each line of a notes session is appended to a JSONL file under the
  bot's state directory as it is heard, so a restart mid-call loses nothing: the next
  start resumes the session (people still there) or finishes it (everyone left).
* **A plan.** At the end of a call the model returns decisions, action items and open
  questions as JSON; ``parse_call_plan`` reads it and never invents an item.
* **Filing.** Action items go to whatever trackers registered a filer
  (``register_filer``); with none, the list says the items were not filed. An item
  that spends money, deletes data or publishes outside the organisation is never done:
  it is marked for a decision.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

SETTING_PLUGIN = "core"
SETTING_KEY = "voice_calls"
AUTO_GUILDS_ENV = "VOICE_AUTO_GUILDS"
#: Finished journals kept on disk (newest first); older ones are removed.
KEEP_DONE = 60
#: Action items shown in the channel before "+N more".
LIST_CAP = 5

JOIN_NOTICE = ("**Aither joined this call and is taking notes.** What is said is "
               "transcribed and kept; a summary and the action items are posted here "
               "when the call ends. Say \"Aither, off the record\" to pause, \"Aither, "
               "back on the record\" to resume, \"Aither, stop\" to end the notes.")
RESUME_NOTICE = ("**Aither restarted and is still taking notes in this call.** Nothing "
                 "said before the restart was lost. \"Aither, off the record\" pauses.")


# -- the per-server setting -------------------------------------------------------------

def auto_guilds(prefix: str = "AWDISCORD_") -> Tuple[str, ...]:
    raw = os.getenv(prefix + AUTO_GUILDS_ENV) or ""
    return tuple(g.strip() for g in raw.split(",") if g.strip().isdigit())


def voice_calls_on(guild_id: str, store=None, env_guilds: Iterable[str] = ()) -> bool:
    """This server asked for calls that run themselves. Off unless something says on.

    A store that cannot be read is OFF: the bot never starts recording a server on a
    guess.
    """
    gid = str(guild_id or "")
    if not gid:
        return False
    if gid in {str(g) for g in env_guilds}:
        return True
    if store is None:
        return False
    try:
        value = store.get(gid, SETTING_PLUGIN, SETTING_KEY, False)
    except Exception:  # noqa: BLE001 - unreadable setting = off, never a guess
        return False
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "on", "yes")
    return bool(value)


def set_voice_calls(store, guild_id: str, on: bool) -> None:
    store.set(str(guild_id), SETTING_PLUGIN, SETTING_KEY, bool(on))


# -- join / stay / leave ----------------------------------------------------------------

def next_move(*, auto: bool, bot_channel: str, humans: Dict[str, int],
              standing: Iterable[str] = (), event_channel: str = "") -> Tuple[str, str]:
    """What the bot does after a voice-state change in one server.

    ``humans`` maps each voice channel id to the people (not bots) in it. Returns
    ``("join", channel)``, ``("leave", "")`` or ``("stay", "")``.

    * not opted in: always stay (joining stays a command);
    * the bot's channel still has people: stay with them;
    * people somewhere else: join the fullest channel (the one the event came from
      wins a tie);
    * nobody anywhere: go back to a standing channel when the server has one, else
      leave.
    """
    if not auto:
        return ("stay", "")
    here = str(bot_channel or "")
    if here and humans.get(here, 0) > 0:
        return ("stay", "")
    busy = [(n, c == str(event_channel or ""), c) for c, n in humans.items() if n > 0]
    if busy:
        busy.sort(reverse=True)
        return ("join", busy[0][2])
    homes = [str(c) for c in standing if str(c) in humans]
    if homes:
        return ("stay", "") if here in homes else ("join", homes[0])
    return ("leave", "") if here else ("stay", "")


# -- the journal ------------------------------------------------------------------------

def _safe(part: str) -> str:
    return "".join(c for c in str(part) if c.isalnum() or c in "-_") or "x"


class Journal:
    """One notes session on disk: a header line, then one JSON line per utterance.

    Append-only and flushed per line; a crash keeps everything already heard. There
    is one open journal per voice channel (``<guild>-<channel>.jsonl``); ``close``
    moves it under ``done/`` with its start time in the name.
    """

    def __init__(self, root, guild_id: str, channel_id: str) -> None:
        self.dir = Path(root) / "callnotes"
        self.guild_id, self.channel_id = str(guild_id), str(channel_id)
        self.path = self.dir / (_safe(guild_id) + "-" + _safe(channel_id) + ".jsonl")
        self._lock = threading.Lock()

    def exists(self) -> bool:
        return self.path.is_file()

    def _write(self, row: dict) -> bool:
        try:
            with self._lock:
                self.dir.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                    fh.flush()
                    os.fsync(fh.fileno())
            return True
        except OSError:
            return False

    def start(self, started_at: float, started_by: str, started_by_id: str = "",
              auto: bool = False) -> bool:
        return self._write({"type": "start", "guild": self.guild_id,
                            "channel": self.channel_id, "at": started_at,
                            "by": started_by, "by_id": str(started_by_id),
                            "auto": bool(auto)})

    def line(self, wall: float, name: str, text: str, uid: str = "") -> bool:
        row = {"type": "line", "at": wall, "name": name, "text": text}
        if uid:
            row["uid"] = str(uid)
        return self._write(row)

    def mark(self, kind: str, name: str = "") -> bool:
        return self._write({"type": kind, "at": time.time(), "name": name})

    def close(self) -> Optional[Path]:
        """Move the journal under ``done/``; returns where it went (None if absent)."""
        with self._lock:
            if not self.path.is_file():
                return None
            done = self.dir / "done"
            done.mkdir(parents=True, exist_ok=True)
            header = read_journal(self.path)[0]
            stamp = time.strftime("%Y%m%d-%H%M%S",
                                  time.gmtime(float(header.get("at") or time.time())))
            target = done / (self.path.stem + "-" + stamp + ".jsonl")
            os.replace(str(self.path), str(target))
            kept = sorted(done.glob("*.jsonl"), key=lambda p: p.stat().st_mtime,
                          reverse=True)
            for old in kept[KEEP_DONE:]:
                try:
                    old.unlink()
                except OSError:
                    break
            return target


def read_journal(path) -> Tuple[dict, List[tuple], bool]:
    """``(header, [(wall, name, text)], paused)`` from one journal file.

    A torn last line (the write a crash interrupted) is skipped, not an error. The
    header gains ``people`` (spoken name -> Discord id) and ``last_at`` (the newest
    line or marker), which is how a resume tells a live call from an old file.
    """
    header: dict = {}
    lines: List[tuple] = []
    paused = False
    people: Dict[str, str] = {}
    last_at = 0.0
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return header, lines, paused
    for row in raw.splitlines():
        try:
            data = json.loads(row)
        except ValueError:
            continue
        if not isinstance(data, dict):
            continue
        kind = data.get("type")
        try:
            last_at = max(last_at, float(data.get("at") or 0.0))
        except (TypeError, ValueError):
            continue
        if kind == "start" and not header:
            header = data
        elif kind == "line":
            name = str(data.get("name") or "")
            lines.append((float(data.get("at") or 0.0), name,
                          str(data.get("text") or "")))
            if data.get("uid") and name:
                people[name.lower()] = str(data["uid"])
        elif kind == "pause":
            paused = True
        elif kind == "resume":
            paused = False
    if header:
        header = dict(header, people=people, last_at=last_at)
    return header, lines, paused


def unfinished(root) -> List[Path]:
    """Journals a previous process left open (a restart mid-call)."""
    folder = Path(root) / "callnotes"
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.jsonl") if p.is_file())


# -- spoken "do this" -------------------------------------------------------------------

_ACTION = re.compile(
    r"^(?:(?:create|make|open|file|add|log|raise|start|write)\s+(?:us\s+|me\s+)?"
    r"(?:a\s+|an\s+|the\s+)?(?:new\s+)?(?:task|ticket|issue|bug|todo|to-do|action\s+item|"
    r"reminder|card|job)\b"
    r"|action\s+item\b|todo\b|to-do\b"
    r"|(?:remind|assign|ask|tell|have|get)\s+\S+.*\s+to\s+\S+"
    r"|(?:track|file|schedule|follow\s+up\s+on|make\s+a\s+note\s+to|"
    r"put\s+(?:that|this|it)\s+on\s+the\s+board|add\s+(?:that|this|it)\s+to\s+the\s+board)\b"
    r").*$", re.IGNORECASE)


def is_action_request(text: str) -> bool:
    """A spoken instruction to file or hand off work (the wake word already removed).

    Narrow on purpose: "what tasks are open" and "how do I file an issue" stay
    questions for the agent.
    """
    t = re.sub(r"\s+", " ", (text or "").strip())
    t = re.sub(r"^[\W_]+", "", t)
    t = re.sub(r"^(?:(?:please|can you|could you|would you|will you|go ahead and|"
               r"i need you to|i want you to|now)\s+)+", "", t, flags=re.IGNORECASE)
    if not t or re.match(r"^(?:what|how|why|when|where|who|which|is|are|do|does|did)\b",
                         t, re.IGNORECASE):
        return False
    return bool(_ACTION.match(t))


# -- the plan the model returns ---------------------------------------------------------

KINDS = ("task", "engineering", "agent")

CALL_PROMPT = """You turn the transcript of a team voice call into its record.
Use only what the transcript says. Never invent a decision, an owner or a commitment.

Return ONLY this JSON object, no prose:
{{
  "title": "<= 70 chars, what the call was about",
  "decisions": ["each thing the call settled, one short sentence"],
  "actions": [{{"title": "<= 90 chars, imperative",
               "detail": "<= 250 chars: the numbers, names and constraints said",
               "owner": "the person's name as spoken, or an agent name, or \\"\\"",
               "kind": "task | engineering | agent",
               "needs_decision": false,
               "why": "only when needs_decision is true: what it would spend, delete "
                      "or publish"}}],
  "questions": ["each question the call left open"]
}}
kind: "engineering" = a code, bug, infrastructure or product-build item; "agent" = work
an AI agent can do alone now (research, a draft, a lookup, a summary); "task" =
anything else a person owns.
needs_decision is true when doing the item would spend money, delete or overwrite
data, or publish, post or send something outside the team.
Leave out an action the transcript shows as already filed (a line "Aither: Filed:
..."), and a question that was answered later in the call.
{focus}
Transcript:
---
{transcript}
---
"""

_RISK = (
    ("spend", re.compile(
        r"\b(?:buy|purchase|pay(?:ing)?\s+for|subscribe|subscription|renew|rent|"
        r"order\s+(?:a|an|the|more)|upgrade\s+(?:the\s+)?plan|spend|wire\s+(?:the\s+)?"
        r"(?:money|funds)|invoice\s+them|refund|hire|sign\s+(?:the\s+)?contract)\b"
        r"|[$€£]\s?\d", re.IGNORECASE)),
    ("delete", re.compile(
        r"\b(?:delete|drop\s+(?:the\s+)?(?:table|database|db|bucket)|wipe|purge|erase|"
        r"destroy|truncate|remove\s+(?:all|every|the\s+(?:data|backups?|users?|"
        r"accounts?|repo|repository|volume))|shut\s+down\s+(?:the\s+)?"
        r"(?:prod|production)|factory\s+reset|force[- ]push)\b", re.IGNORECASE)),
    ("publish", re.compile(
        r"\b(?:publish|announce|tweet|press\s+release|go\s+live|launch\s+publicly|"
        r"post\s+(?:it\s+|this\s+|that\s+)?(?:on|to)\s+(?:twitter|x|linkedin|reddit|"
        r"facebook|instagram|youtube|hacker\s*news|the\s+blog|the\s+website|social)|"
        r"email\s+(?:the\s+|our\s+|all\s+)?(?:customers?|clients?|users?|list|"
        r"investors?|partners?)|send\s+(?:it\s+|this\s+|that\s+)?to\s+(?:the\s+)?"
        r"(?:customer|client|investor|partner|press)s?|make\s+(?:it|the\s+repo)\s+"
        r"public|open[- ]source\s+(?:it|the))\b", re.IGNORECASE)),
)


def risk_of(*texts: str) -> str:
    """``spend``, ``delete``, ``publish`` or ``""`` for one action item's words."""
    blob = " ".join(str(t or "") for t in texts)
    for name, pattern in _RISK:
        if pattern.search(blob):
            return name
    return ""


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def parse_call_plan(reply: str) -> dict:
    """The model's record of a call as a dict that always has every key.

    A reply that is not JSON yields an empty, ``parsed: False`` plan: the transcript
    still ships and nothing is filed from a guess. An item is marked for a decision
    when the model said so OR its own words name spending, deleting or publishing.
    """
    raw = (reply or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    data: Any = {}
    if 0 <= start < end:
        try:
            data = json.loads(raw[start:end + 1])
        except ValueError:
            data = {}
    if not isinstance(data, dict):
        data = {}

    def _list(key: str, cap: int) -> List[str]:
        rows = data.get(key) or []
        if not isinstance(rows, list):
            return []
        return [t for t in (_text(r, 300) for r in rows)
                if t and t.lower() not in ("none", "none.", "n/a")][:cap]

    actions: List[dict] = []
    rows = data.get("actions") or []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        title = _text(row.get("title"), 90)
        if len(title) < 3:
            continue
        detail = _text(row.get("detail"), 250)
        kind = _text(row.get("kind"), 20).lower()
        risk = risk_of(title, detail)
        flagged = row.get("needs_decision") is True or bool(risk)
        actions.append({
            "title": title, "detail": detail,
            "owner": _text(row.get("owner"), 40),
            "kind": kind if kind in KINDS else "task",
            "needs_decision": flagged,
            "risk": risk or ("flagged" if flagged else ""),
            "why": _text(row.get("why"), 200)})
    decisions, questions = _list("decisions", 12), _list("questions", 12)
    return {"title": _text(data.get("title"), 80) or "Call notes",
            "decisions": decisions, "actions": actions[:25], "questions": questions,
            "parsed": bool(decisions or actions or questions or data.get("title"))}


def render_summary(plan: dict) -> str:
    """Decisions / Action items / Open questions as plain text for the chat."""
    out = ["**Decisions**"]
    out += ["- " + d for d in plan.get("decisions") or []] or ["- None"]
    out += ["", "**Action items**"]
    acts = plan.get("actions") or []
    out += ["- " + a["title"] + (" (" + a["owner"] + ")" if a.get("owner") else "")
            for a in acts] or ["- None"]
    out += ["", "**Open questions**"]
    out += ["- " + q for q in plan.get("questions") or []] or ["- None"]
    return "\n".join(out)


# -- filing -----------------------------------------------------------------------------

#: fn(guild_id, channel_id, plan, context) -> rows, or None when it is not this
#: filer's server. A row: {"title", "owner", "link", "note", "status"} where status is
#: "filed", "decision", "queued" or "failed".
_FILERS: List[Callable[..., Optional[List[dict]]]] = []
#: fn(guild_id, channel_id, title, document) -> True when it kept the summary.
_KEEPERS: List[Callable[..., bool]] = []
_reg_lock = threading.Lock()


def register_filer(fn: Callable[..., Optional[List[dict]]]) -> None:
    with _reg_lock:
        if fn not in _FILERS:
            _FILERS.append(fn)


def register_keeper(fn: Callable[..., bool]) -> None:
    with _reg_lock:
        if fn not in _KEEPERS:
            _KEEPERS.append(fn)


def registered() -> Tuple[int, int]:
    """``(filers, keepers)`` registered in this process."""
    with _reg_lock:
        return len(_FILERS), len(_KEEPERS)


def clear_registrations() -> None:
    with _reg_lock:
        del _FILERS[:]
        del _KEEPERS[:]


def file_actions(guild_id: str, channel_id: str, plan: dict,
                 context: Optional[dict] = None) -> List[dict]:
    """Hand the plan's action items to the first filer that takes this server.

    With no filer, or when every filer declines, each item comes back ``failed``
    with the reason -- the chat is told the item was NOT tracked, never left to
    assume it was. A filer that raises is reported the same way.
    """
    actions = list(plan.get("actions") or [])
    if not actions:
        return []
    with _reg_lock:
        filers = list(_FILERS)
    reason = "no tracker is connected in this server"
    for filer in filers:
        try:
            rows = filer(str(guild_id), str(channel_id), plan, dict(context or {}))
        except Exception as exc:  # noqa: BLE001 - said in the chat, per item
            reason = type(exc).__name__ + ": " + str(exc)[:120]
            continue
        if rows is not None:
            return list(rows)
    return [{"title": a["title"], "owner": a.get("owner") or "", "link": "",
             "status": "failed", "note": "not filed: " + reason} for a in actions]


def keep_summary(guild_id: str, channel_id: str, title: str, document: str) -> bool:
    """Give the call's record to the server's memory. False when nothing kept it."""
    with _reg_lock:
        keepers = list(_KEEPERS)
    kept = False
    for keeper in keepers:
        try:
            kept = bool(keeper(str(guild_id), str(channel_id), title, document)) or kept
        except Exception:  # noqa: BLE001 - the caller reports "not saved"
            continue
    return kept


def _row_line(index: int, row: dict) -> str:
    owner = str(row.get("owner") or "").strip() or "no owner yet"
    line = str(index) + ". " + str(row.get("title") or "item") + " - " + owner
    link = str(row.get("link") or "").strip()
    if link:
        line += " - <" + link + ">"
    status, note = str(row.get("status") or ""), str(row.get("note") or "").strip()
    if status == "decision":
        line += " - **needs a decision, not done**"
    if note:
        line += " (" + note + ")"
    return line


def action_list(rows: List[dict], cap: int = LIST_CAP,
                head: str = "**Action items from this call**") -> Tuple[str, str]:
    """``(the channel post, the overflow post)``; the overflow is "" under the cap."""
    if not rows:
        return "", ""
    lines = [_row_line(i, r) for i, r in enumerate(rows, 1)]
    main = [head] + lines[:cap]
    more = lines[cap:]
    if more:
        main.append("+" + str(len(more)) + " more below")
    return "\n".join(main)[:1990], "\n".join(more)[:1990]


def brain_document(plan: dict, guild_id: str, channel_id: str, started_at: float,
                   speakers: Iterable[str], transcript: str, rows: List[dict]) -> str:
    """The call's record as one memory document, with where it came from."""
    lines = ["# " + str(plan.get("title") or "Call notes"),
             "source_path: discord/" + str(guild_id) + "/" + str(channel_id),
             "kind: voice-call-notes",
             "speakers: " + (", ".join(sorted({s for s in speakers if s})) or "unknown"),
             "consent: the channel was told when note-taking started",
             "call_started: " + time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                              time.gmtime(started_at)),
             "", "Decisions:"]
    lines += ["- " + d for d in plan.get("decisions") or []] or ["- (none)"]
    lines += ["", "Action items:"]
    if rows:
        for row in rows:
            lines.append("- " + str(row.get("title") or "") + " - "
                         + (str(row.get("owner") or "") or "unowned")
                         + ((" - " + str(row["link"])) if row.get("link") else ""))
    else:
        lines.append("- (none)")
    lines += ["", "Open questions:"]
    lines += ["- " + q for q in plan.get("questions") or []] or ["- (none)"]
    lines += ["", "Transcript:", transcript[-12000:]]
    return "\n".join(lines)


# -- one tracker any server can use: a GitHub issue --------------------------------------

def github_issue(repo: str, title: str, body: str, token: str, opener=None,
                 labels: Iterable[str] = ()) -> str:
    """File one issue in ``owner/name``; returns its URL. Raises on any failure."""
    if not token:
        raise RuntimeError("no GitHub token on the bot host")
    if not re.match(r"^[\w.-]+/[\w.-]+$", repo or ""):
        raise ValueError("repo must be owner/name")
    payload: Dict[str, Any] = {"title": title[:240], "body": body[:60000]}
    wanted = [str(x) for x in labels if str(x)]
    if wanted:
        payload["labels"] = wanted
    req = urllib.request.Request(
        "https://api.github.com/repos/" + repo + "/issues",
        data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + token, "User-Agent": "awdiscord",
                 "Accept": "application/vnd.github+json"})
    with (opener or urllib.request.urlopen)(req, timeout=20) as resp:
        issue = json.loads(resp.read().decode("utf-8"))
    return str(issue.get("html_url") or "")
