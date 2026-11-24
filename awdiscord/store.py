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


#: How long a write waits for another connection (another process) to let go.
BUSY_TIMEOUT_S = 30.0

# One connection (and one lock) per file per process. A pack that builds its own
# ``GuildStore`` over the bot's ``guilds.db`` (the copilot does, at load time) used to
# get a SECOND connection: its pollers and the bot's then fought over the file's
# rollback-journal lock and lost after 5 s with "database is locked". Sharing the
# connection turns that into a wait on one in-process lock.
_SHARED: dict[str, tuple[sqlite3.Connection, threading.Lock]] = {}
_SHARED_LOCK = threading.Lock()


def _open(path: str | Path) -> tuple[sqlite3.Connection, threading.Lock, bool]:
    """``(connection, lock, fresh)`` for ``path``; files are shared per process."""
    if str(path) == ":memory:":
        db = sqlite3.connect(":memory:", check_same_thread=False)
        return db, threading.Lock(), True
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    key = str(p.resolve())
    with _SHARED_LOCK:
        hit = _SHARED.get(key)
        if hit is not None:
            return hit[0], hit[1], False
        db = sqlite3.connect(key, check_same_thread=False, timeout=BUSY_TIMEOUT_S)
        # WAL: readers never block the writer and the writer never blocks readers,
        # so another PROCESS on the same file (a CLI, a second bot) waits less too.
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError as exc:  # pragma: no cover - fs without shm support
            _log.warning("WAL unavailable, staying on the rollback journal: %s", exc)
        db.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_S * 1000)}")
        lock = threading.Lock()
        _SHARED[key] = (db, lock)
        return db, lock, True


class GuildStore:
    def __init__(self, path: str | Path = ":memory:"):
        self._db, self._lock, fresh = _open(path)
        if not fresh:
            return
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

    # -- writes ------------------------------------------------------------------
    def _write(self, sql: str, args: tuple) -> None:
        """One statement in its own ``BEGIN IMMEDIATE`` transaction.

        Taking the write lock FIRST means a competing connection makes this wait
        (``busy_timeout``) instead of failing at once: a deferred transaction that
        has already read cannot be retried by sqlite's busy handler.
        """
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                self._db.execute(sql, args)
            except BaseException:
                self._db.rollback()
                raise
            self._db.commit()

    # -- plugin enablement -----------------------------------------------------
    def plugin_state(self, guild: str, plugin: str) -> bool | None:
        with self._lock:
            row = self._db.execute(
                "SELECT enabled FROM plugin_state WHERE guild=? AND plugin=?",
                (str(guild), plugin)).fetchone()
        return None if row is None else bool(row[0])

    def set_plugin_state(self, guild: str, plugin: str, enabled: bool) -> None:
        self._write(
            "INSERT INTO plugin_state(guild, plugin, enabled) VALUES (?,?,?) "
            "ON CONFLICT(guild, plugin) DO UPDATE SET enabled=excluded.enabled",
            (str(guild), plugin, int(enabled)))

    # -- plugin keys -------------------------------------------------------------
    def get(self, guild: str, plugin: str, key: str, default: Any = None) -> Any:
        with self._lock:
            row = self._db.execute(
                "SELECT value FROM kv WHERE guild=? AND plugin=? AND key=?",
                (str(guild), plugin, key)).fetchone()
        return default if row is None else json.loads(row[0])

    def set(self, guild: str, plugin: str, key: str, value: Any) -> None:
        self._write(
            "INSERT INTO kv(guild, plugin, key, value) VALUES (?,?,?,?) "
            "ON CONFLICT(guild, plugin, key) DO UPDATE SET value=excluded.value",
            (str(guild), plugin, key, json.dumps(value)))

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
        self._write("DELETE FROM kv WHERE guild=? AND plugin=? AND key=?",
                    (str(guild), plugin, key))

    def vacuum(self) -> None:
        """Reclaim the pages a deletion freed. Safe to call on any store."""
        with self._lock:
            self._db.commit()
            self._db.execute("VACUUM")
            # Under WAL an erased row's old page can still sit in the -wal file:
            # fold it into the (zeroed) main file and truncate the log.
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

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
