"""Ops: let the bot's operators ask about their own machines.

On by default, and its tools are offered ONLY to operators
(``AWDISCORD_OPERATORS``): anyone else in the server never sees them.

  status_files()       what status records exist, and how old each is
  read_status(name)    one record (JSON is returned compact; text as-is)

The records are whatever the operator mounts at ``AWDISCORD_STATUS_DIR``
(read-only): disk sensors, health summaries, backup reports. The bot reads
them; it never runs anything on the host. No directory = the tools say so.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from awdiscord.plugins import Plugin, Tool

MAX_CHARS = 6000
SUFFIXES = (".json", ".txt", ".md", ".log")


def _root() -> Path | None:
    raw = os.getenv("AWDISCORD_STATUS_DIR", "").strip()
    return Path(raw) if raw else None


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in SUFFIXES)


def status_files() -> str:
    root = _root()
    if root is None or not root.is_dir():
        return "No status directory is configured (AWDISCORD_STATUS_DIR)."
    now = time.time()
    rows = [f"- {p.relative_to(root).as_posix()} ({_age(now - p.stat().st_mtime)} old, "
            f"{p.stat().st_size} bytes)" for p in _files(root)]
    return "\n".join(rows[:60]) or "The status directory is empty."


def read_status(name: str) -> str:
    root = _root()
    if root is None or not root.is_dir():
        return "No status directory is configured (AWDISCORD_STATUS_DIR)."
    target = (root / name).resolve()
    if (root.resolve() not in target.parents or not target.is_file()
            or target.suffix not in SUFFIXES):
        return f"No status record named {name!r}. Call status_files to list them."
    body = target.read_text(encoding="utf-8", errors="replace")
    if target.suffix == ".json":
        try:
            parsed = json.loads(body)
        except ValueError:      # not valid JSON: send the raw text as-is
            parsed = body
        if parsed is not body:
            body = json.dumps(parsed, separators=(",", ":"))
    age = _age(time.time() - target.stat().st_mtime)
    if len(body) > MAX_CHARS:     # the newest lines of a log matter most
        body = "...\n" + body[-MAX_CHARS:] if target.suffix in (".log", ".txt") \
            else body[:MAX_CHARS] + "\n..."
    return f"[{name}, written {age} ago]\n{body}"


def _age(seconds: float) -> str:
    s = int(max(0, seconds))
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if s >= n:
            return f"{s // n}{unit}"
    return f"{s}s"


PLUGIN = Plugin(
    name="ops",
    version="0.1.0",
    description="Operators can ask about their machines (disk, health) from status records",
    default_enabled=True,
    # No persona text: it would reach every member, and naming tools a member
    # is not offered makes the model talk about them. The guidance rides in the
    # tool descriptions, which only operators' requests carry.
    tools=[
        Tool("status_files", "List the operator's status records (disk sensors, health "
             "summaries) with their age. Use this FIRST for any question about their "
             "machines, disks, services or backups, then read_status, and answer with "
             "the record's numbers and age - never generic advice when a record answers.",
             status_files, operators_only=True),
        Tool("read_status", "Read one status record by the name status_files gave.",
             read_status, {"type": "object", "properties": {"name": {"type": "string"}},
                           "required": ["name"]}, operators_only=True),
    ],
)
