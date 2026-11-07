"""A local index of pages rendered into Discord cards.

Discord has no artifact store. What it does have is a slash command whose string
option can carry *server-side autocomplete* — type a word, the bot answers with
the names it knows, and that list is the picker. This module is what answers.

The ledger is a single JSON file, offline, stdlib only, in the same shape as
``awrise``'s wake record: small enough to read on every keystroke, honest about
being local. It records the page's digest as well as its path, so a card that no
longer matches the file it came from can be noticed rather than assumed.

Nothing here talks to the network. Sharing is a separate step (``awshare`` for
integrity, ``awseal`` for provenance) and stays out of this file on purpose.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

INDEX_ENV = "AWDISCORD_ARTIFACT_DIR"
INDEX_FILE = "index.json"

#: Discord rejects an autocomplete response with more than 25 choices.
MAX_CHOICES = 25

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class ArtifactIndexError(Exception):
    """The index could not be read or written."""


def default_root() -> Path:
    """Where the index lives: an explicit override, then the bot's state dir.

    The bot runs in a container whose only durable volume is its state dir
    (``AWDISCORD_STATE_DIR``). Falling back to the home directory there would
    put the index in the image's writable layer, where it vanishes on the
    next restart while every save reports success.
    """
    override = os.getenv(INDEX_ENV)
    if override:
        return Path(override)
    state = os.getenv("AWDISCORD_STATE_DIR")
    if state:
        return Path(state) / "artifacts"
    return Path.home() / ".aither" / "artifacts"


def slug(text: str, limit: int = 60) -> str:
    """A stable, human-typeable name: ``"From wesyn to awdk"`` -> ``from-wesyn-to-awdk``."""
    cleaned = _SLUG_RE.sub("-", (text or "").lower()).strip("-")
    return cleaned[:limit].strip("-") or "artifact"


def sha256_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Record:
    name: str
    title: str
    path: str
    sha256: str = ""
    bytes: int = 0
    created_at: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Record":
        return cls(
            name=str(data.get("name", "")),
            title=str(data.get("title", "")),
            path=str(data.get("path", "")),
            sha256=str(data.get("sha256", "")),
            bytes=int(data.get("bytes", 0) or 0),
            created_at=str(data.get("created_at", "")),
        )

    def is_stale(self) -> bool:
        """True when the file behind this record changed or vanished."""
        try:
            return sha256_file(self.path) != self.sha256
        except OSError:
            return True


def record_for(path, title: str = "", name: str = "") -> Record:
    """Build a record for ``path``. Raises ArtifactIndexError if it is unreadable."""
    target = Path(path)
    try:
        size = target.stat().st_size
        digest = sha256_file(target)
    except OSError as exc:
        raise ArtifactIndexError(f"cannot read {path}: {exc}") from exc
    resolved_title = title or target.stem
    return Record(
        name=name or slug(resolved_title),
        title=resolved_title,
        path=str(target.resolve()),
        sha256=digest,
        bytes=size,
        created_at=_now(),
    )


class ArtifactIndex:
    """The JSON ledger. Every method is safe to call on a missing index."""

    def __init__(self, root=None) -> None:
        self.root = Path(root) if root is not None else default_root()

    @property
    def path(self) -> Path:
        return self.root / INDEX_FILE

    # -- storage ---------------------------------------------------------
    def _load(self) -> list[Record]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ArtifactIndexError(f"cannot read {self.path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise ArtifactIndexError(f"{self.path} is not valid JSON: {exc}") from exc
        items = data.get("artifacts") if isinstance(data, dict) else data
        if not isinstance(items, list):
            raise ArtifactIndexError(f"{self.path} has no artifacts list")
        return [Record.from_dict(item) for item in items if isinstance(item, dict)]

    def _write(self, records: list[Record]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = {"schema": "awdiscord.artifacts/v1",
                       "artifacts": [r.to_dict() for r in records]}
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            raise ArtifactIndexError(f"cannot write {self.path}: {exc}") from exc

    # -- operations ------------------------------------------------------
    def save(self, record: Record) -> Record:
        """Add or replace by name. The most recent save wins."""
        records = [r for r in self._load() if r.name != record.name]
        records.append(record)
        records.sort(key=lambda r: (r.created_at, r.name))
        self._write(records)
        return record

    def list(self) -> list[Record]:
        records = self._load()
        records.sort(key=lambda r: (r.created_at, r.name), reverse=True)
        return records

    def get(self, name: str) -> Record | None:
        wanted = (name or "").strip().lower()
        for record in self._load():
            if record.name.lower() == wanted:
                return record
        return None

    def find(self, query: str, limit: int = MAX_CHOICES) -> list[Record]:
        """Ranked search: exact name, then prefix, then substring of name or title."""
        needle = (query or "").strip().lower()
        records = self.list()
        if not needle:
            return records[:limit]
        exact, prefix, contains = [], [], []
        for record in records:
            name = record.name.lower()
            title = record.title.lower()
            if name == needle:
                exact.append(record)
            elif name.startswith(needle):
                prefix.append(record)
            elif needle in name or needle in title:
                contains.append(record)
        return (exact + prefix + contains)[:limit]

    def remove(self, name: str) -> bool:
        records = self._load()
        kept = [r for r in records if r.name.lower() != (name or "").strip().lower()]
        if len(kept) == len(records):
            return False
        self._write(kept)
        return True

    def choices(self, query: str = "", limit: int = MAX_CHOICES) -> list[dict]:
        """Autocomplete payloads, shaped for Discord's type-8 response."""
        out = []
        for record in self.find(query, limit):
            note = "stale" if record.is_stale() else record.title
            out.append({"name": f"{record.name} - {note}"[:100], "value": record.name})
        return out
