"""Per-server state: which plugins are on, and each plugin's own keys.

One SQLite file (stdlib only). Values are JSON. Every read and write is scoped
by guild id, so one server can never see or change another's data.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable

_log = logging.getLogger("awdiscord.store")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS plugin_state (
    guild TEXT NOT NULL, plugin TEXT NOT NULL, enabled INTEGER NOT NULL,
    PRIMARY KEY (guild, plugin));
CREATE TABLE IF NOT EXISTS kv (
    guild TEXT NOT NULL, plugin TEXT NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (guild, plugin, key));
"""


class GuildStore:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(_SCHEMA)
            # A member's profile is personal data they can ask us to delete. With
            # secure_delete the bytes are zeroed in the page, not just unlinked
            # from the index, so a deleted row cannot be recovered from the file.
            try:
                self._db.execute("PRAGMA secure_delete=ON")
            except sqlite3.DatabaseError as exc:  # pragma: no cover - very old sqlite
                _log.warning("secure_delete unavailable, erasure is index-only: %s", exc)
            self._db.commit()

    # -- plugin enablement -----------------------------------------------------
    def plugin_state(self, guild: str, plugin: str) -> bool | None:
        with self._lock:
            row = self._db.execute(
                "SELECT enabled FROM plugin_state WHERE guild=? AND plugin=?",
                (str(guild), plugin)).fetchone()
        return None if row is None else bool(row[0])

    def set_plugin_state(self, guild: str, plugin: str, enabled: bool) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO plugin_state(guild, plugin, enabled) VALUES (?,?,?) "
                "ON CONFLICT(guild, plugin) DO UPDATE SET enabled=excluded.enabled",
                (str(guild), plugin, int(enabled)))
            self._db.commit()

    # -- plugin keys -------------------------------------------------------------
    def get(self, guild: str, plugin: str, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE guild=? AND plugin=? AND key=?",
                (str(guild), plugin, key)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, guild: str, plugin: str, key: str, value: Any) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO kv(guild, plugin, key, value) VALUES (?,?,?,?) "
                "ON CONFLICT(guild, plugin, key) DO UPDATE SET value=excluded.value",
                (str(guild), plugin, key, json.dumps(value)))
            self._db.commit()

    def update(self, guild: str, plugin: str, key: str,
               fn: Callable[[Any], tuple[Any, Any]], default: Any = None) -> Any:
        """Atomic read-modify-write of one key; returns what ``fn`` returns second.

        ``fn(current) -> (new_value, result)`` runs while the store is locked and
        inside one ``BEGIN IMMEDIATE`` transaction, so two concurrent callers
        (threads here, or another process on the same file) never both read the
        same value. ``fn`` must not call the store (the lock is not re-entrant).
        A raise from ``fn`` rolls back and writes nothing.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT value FROM kv WHERE guild=? AND plugin=? AND key=?",
                    (str(guild), plugin, key)).fetchone()
                current = default if row is None else json.loads(row[0])
                value, result = fn(current)
                self._db.execute(
                    "INSERT INTO kv(guild, plugin, key, value) VALUES (?,?,?,?) "
                    "ON CONFLICT(guild, plugin, key) DO UPDATE SET value=excluded.value",
                    (str(guild), plugin, key, json.dumps(value)))
            except BaseException:
                self._db.rollback()
                raise
            self._db.commit()
        return result

    def delete(self, guild: str, plugin: str, key: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM kv WHERE guild=? AND plugin=? AND key=?",
                             (str(guild), plugin, key))
            self._db.commit()

    def vacuum(self) -> None:
        """Reclaim the pages a deletion freed. Safe to call on any store."""
        with self._lock:
            self._db.commit()
            self._db.execute("VACUUM")

    # -- discovery ---------------------------------------------------------------
    def guilds(self) -> list[str]:
        """Every guild this store has ever written a row for (any plugin).

        Discord sends no guild list to a bot that restarted: this is how a
        periodic job knows which servers to sweep. A guild with no row at all
        has never used the bot, so there is nothing there to act on.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT guild FROM plugin_state UNION SELECT guild FROM kv "
                "ORDER BY guild").fetchall()
        return [str(r[0]) for r in rows if r[0]]

    def keys(self, guild: str, plugin: str, prefix: str = "") -> list[str]:
        with self._lock:
            rows = self._db.execute(
                "SELECT key FROM kv WHERE guild=? AND plugin=? AND key LIKE ? ESCAPE '\\' "
                "ORDER BY key",
                (str(guild), plugin,
                 prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"),
            ).fetchall()
        return [r[0] for r in rows]
