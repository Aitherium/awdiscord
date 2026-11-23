"""Notepads: one member's notes in one server, kept on disk, plus the call transcripts.

Plain logic with no Discord import (the ``/notepad`` commands live in
:mod:`awdiscord.builtin.notepad`), so it is tested with a temp directory.

* **Where.** ``<state>/notepad/<guild>/<user>.json``; ``<state>`` is the bot's state
  directory (``AWDISCORD_STATE_DIR``, the mounted ``/state`` volume in the container).
  One file per member per server: a note is never readable from another server, and
  only the member whose file it is can change it.
* **Atomic.** A save writes a temp file beside the real one, fsyncs it and renames it
  over, so a crash leaves the old notes or the new ones, never half a file.
* **Bounded.** A note's text is at most :data:`MAX_TEXT` characters: that is what one
  Discord modal field holds, so whatever is saved can always be re-opened and edited
  without silently losing its tail.
* **Transcripts.** The voice notes journals (:mod:`awdiscord.callnotes`) are read
  for ONE guild only: a file counts only when its name starts with that guild's id
  AND its own header says that guild. A note records which calls it carries; the
  transcript is re-read (and re-checked) whenever the note is shared.
"""
from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from awdiscord import callnotes

_log = logging.getLogger("awdiscord.notepad")

#: One Discord text-input field holds 4000 characters; a note never holds more.
MAX_TEXT = 4000
MAX_NOTES = 50
MAX_NAME = 40
#: Calls one note may carry (a Discord message takes ten files; one is the note).
MAX_CALLS = 9
DEFAULT_NAME = "scratch"
_NAME_OK = re.compile(r"[^a-z0-9 _-]+")


class NotepadError(Exception):
    """A refusal the member reads as it is."""


def state_root() -> Path:
    """The bot's state directory: the same one the gateway and voice notes use."""
    raw = (os.getenv("AWDISCORD_STATE_DIR") or "").strip()
    return Path(raw) if raw else Path.home() / ".awdiscord"


def clean_name(raw) -> str:
    """``"My Plan!"`` -> ``"my plan"``. Empty means the default note."""
    name = _NAME_OK.sub("", str(raw or "").strip().lower())
    name = " ".join(name.split())[:MAX_NAME].strip()
    return name or DEFAULT_NAME


def _digits(value) -> str:
    text = str(value or "")
    if not text.isdigit():
        raise NotepadError("notes need a server and a member")
    return text


class Notepad:
    """The notes of one member in one server."""

    _locks: Dict[str, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, root, guild_id, user_id) -> None:
        self.guild = _digits(guild_id)
        self.user = _digits(user_id)
        self.path = Path(root) / "notepad" / self.guild / (self.user + ".json")
        with Notepad._locks_guard:
            self._lock = Notepad._locks.setdefault(str(self.path), threading.Lock())

    # -- disk -----------------------------------------------------------------------
    def _read(self) -> dict:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "guild": self.guild, "user": self.user, "notes": {}}
        except OSError as exc:
            raise NotepadError(f"could not read your notes ({type(exc).__name__})") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise NotepadError("your notes file is damaged; nothing was changed") from exc
        if not isinstance(data, dict) or not isinstance(data.get("notes"), dict):
            raise NotepadError("your notes file is damaged; nothing was changed")
        return data

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".notes-", suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, str(self.path))
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError as exc:
                _log.warning("notepad: could not remove temp file %s: %s", tmp, exc)
            raise

    # -- notes ----------------------------------------------------------------------
    def all(self) -> Dict[str, dict]:
        with self._lock:
            return dict(self._read()["notes"])

    def get(self, name) -> Optional[dict]:
        return self.all().get(clean_name(name))

    def save(self, name, text: str) -> dict:
        name, text = clean_name(name), str(text or "")
        if len(text) > MAX_TEXT:
            raise NotepadError(f"a note holds {MAX_TEXT} characters; this one is {len(text)}")
        with self._lock:
            data = self._read()
            notes = data["notes"]
            if name not in notes and len(notes) >= MAX_NOTES:
                raise NotepadError(f"you already have {MAX_NOTES} notes here; "
                                   f"delete one with `/notepad delete` first")
            now = time.time()
            note = dict(notes.get(name) or {"created": now, "calls": []})
            note.update(text=text, updated=now)
            notes[name] = note
            self._write(data)
            return dict(note)

    def delete(self, name) -> bool:
        name = clean_name(name)
        with self._lock:
            data = self._read()
            if name not in data["notes"]:
                return False
            del data["notes"][name]
            self._write(data)
            return True

    def set_calls(self, name, call_ids: List[str], text: Optional[str] = None) -> dict:
        """Replace the calls a note carries (and optionally its text), creating it."""
        name = clean_name(name)
        ids = list(dict.fromkeys(str(c) for c in call_ids))[:MAX_CALLS]
        with self._lock:
            data = self._read()
            notes = data["notes"]
            if name not in notes and len(notes) >= MAX_NOTES:
                raise NotepadError(f"you already have {MAX_NOTES} notes here")
            now = time.time()
            note = dict(notes.get(name) or {"created": now, "text": ""})
            note["calls"] = ids
            if text is not None:
                if len(text) > MAX_TEXT:
                    raise NotepadError(f"a note holds {MAX_TEXT} characters")
                note["text"] = text
            note["updated"] = now
            notes[name] = note
            self._write(data)
            return dict(note)


# -- call transcripts ---------------------------------------------------------------

def _call(path: Path, guild: str, live: bool) -> Optional[dict]:
    header, lines, _ = callnotes.read_journal(path)
    if str(header.get("guild") or "") != guild:
        return None                     # the name said this guild; the file did not
    people = {str(v) for v in (header.get("people") or {}).values() if v}
    if header.get("by_id"):
        people.add(str(header["by_id"]))
    return {"id": path.stem, "path": path, "live": live, "guild": guild,
            "channel": str(header.get("channel") or ""),
            "started": float(header.get("at") or 0.0), "by": str(header.get("by") or ""),
            "lines": lines, "people": people}


def calls(root, guild_id) -> List[dict]:
    """This guild's call journals, newest first (live ones included)."""
    guild = _digits(guild_id)
    folder = Path(root) / "callnotes"
    prefix = guild + "-"
    found: List[dict] = []
    for sub, live in ((folder, True), (folder / "done", False)):
        if not sub.is_dir():
            continue
        for path in sub.glob(prefix + "*.jsonl"):
            if path.is_file():
                call = _call(path, guild, live)
                if call is not None:
                    found.append(call)
    found.sort(key=lambda c: (c["started"], c["id"]), reverse=True)
    return found


def visible_calls(root, guild_id, user_id, manager: bool) -> List[dict]:
    """The calls this member may read: ones they spoke in or started, or all for a manager."""
    user = str(user_id or "")
    return [c for c in calls(root, guild_id) if manager or user in c["people"]]


def pick(found: List[dict], which) -> List[dict]:
    """``latest`` (default), ``all``, a number from ``/notepad calls``, or a call id."""
    key = str(which or "latest").strip().lower()
    if not found:
        raise NotepadError("there are no call transcripts you can read in this server yet")
    if key in ("latest", "newest", "last", ""):
        return found[:1]
    if key == "all":
        return found[:MAX_CALLS]
    if key.isdigit():
        index = int(key)
        if 1 <= index <= len(found):
            return [found[index - 1]]
        raise NotepadError(f"there is no call #{index}; `/notepad calls` lists "
                           f"{len(found)}")
    for call in found:
        if call["id"].lower() == key:
            return [call]
    raise NotepadError(f"no call `{which}` here; `/notepad calls` lists them")


def _stamp(at: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(at or 0))


def describe(call: dict) -> str:
    state = " (live)" if call["live"] else ""
    chan = f" in <#{call['channel']}>" if call["channel"] else ""
    return f"{_stamp(call['started'])}{chan}{state}, {len(call['lines'])} lines"


def transcript_markdown(call: dict) -> str:
    head = [f"# Call transcript{' (still running)' if call['live'] else ''}",
            "", f"- started: {_stamp(call['started'])}"]
    if call["channel"]:
        head.append(f"- voice channel id: {call['channel']}")
    if call["by"]:
        head.append(f"- notes started by: {call['by']}")
    head.append(f"- lines: {len(call['lines'])}")
    head.append("")
    body = [f"**[{time.strftime('%H:%M:%S', time.gmtime(at))}] {name or 'someone'}:** {text}"
            for at, name, text in call["lines"]]
    return "\n".join(head + body) + "\n"


def transcript_filename(call: dict) -> str:
    stamp = time.strftime("%Y%m%d-%H%M", time.gmtime(call["started"] or 0))
    return f"call-{stamp}-{callnotes._safe(call['channel'] or 'voice')}.md"


def ref(call: dict) -> str:
    """What a note stores for a call. A live call's file is renamed when it ends
    (``<guild>-<channel>-<start stamp>``), so its ref also carries its start time."""
    if call["live"]:
        return f"{call['id']}@{int(call['started'])}"
    return call["id"]


def resolve_attached(root, guild_id, refs: List[str]) -> tuple[List[dict], List[str]]:
    """The note's calls as they are NOW, re-checked for this guild; and the missing refs."""
    found = calls(root, guild_id)
    have: List[dict] = []
    gone: List[str] = []
    for item in refs:
        stem, _, at = str(item).partition("@")
        if at:
            when = int(at) if at.isdigit() else -1
            call = next((c for c in found if int(c["started"]) == when
                         and (c["id"] == stem or c["id"].startswith(stem + "-"))), None)
        else:
            call = next((c for c in found if c["id"] == stem), None)
        if call is None:
            gone.append(str(item))
        elif call not in have:
            have.append(call)
    return have, gone
