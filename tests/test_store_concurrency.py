"""Two GuildStores over one file in one process must never fail on a lock.

Live 2026-11-23 (0.6.55): the copilot pack opened its own ``GuildStore`` over the
bot's ``/state/guilds.db``; its run-card threads and the bot then held two sqlite
connections to one rollback-journal file, and every write that waited 5 s behind the
other connection died with ``OperationalError: database is locked``.
"""
from __future__ import annotations

import sqlite3
import threading
import time

from awdiscord import store as store_mod
from awdiscord.store import GuildStore


def test_same_file_shares_one_connection(tmp_path):
    a = GuildStore(tmp_path / "guilds.db")
    b = GuildStore(tmp_path / "sub" / ".." / "guilds.db")
    assert a._db is b._db and a._lock is b._lock
    b.set("g", "p", "k", 1)
    assert a.get("g", "p", "k") == 1


def test_memory_stores_stay_separate():
    a, b = GuildStore(), GuildStore()
    a.set("g", "p", "k", 1)
    assert b.get("g", "p", "k") is None


def test_file_store_runs_in_wal(tmp_path):
    s = GuildStore(tmp_path / "guilds.db")
    mode = s._db.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"
    s.set("g", "p", "k", "x")
    s.delete("g", "p", "k")
    s.vacuum()
    assert s.get("g", "p", "k") is None


def test_write_held_past_the_old_timeout_does_not_lock_out_the_other_store(tmp_path):
    """A writes inside a transaction for longer than sqlite's old 5 s default; B
    writes meanwhile. Two connections: B raises "database is locked". One shared
    connection: B waits on the lock and lands after A."""
    path = tmp_path / "guilds.db"
    a, b = GuildStore(path), GuildStore(path)
    inside, errors = threading.Event(), []

    def slow(cur):
        inside.set()
        time.sleep(8)
        return (cur or 0) + 1, None

    def writer_a():
        try:
            a.update("g", "runs", "n", slow, default=0)
        except sqlite3.OperationalError as exc:  # pragma: no cover - the bug
            errors.append(exc)

    def writer_b():
        try:
            b.set("g", "gitops", "seen", True)
        except sqlite3.OperationalError as exc:  # the bug: "database is locked"
            errors.append(exc)

    ta = threading.Thread(target=writer_a)
    ta.start()
    assert inside.wait(5)
    tb = threading.Thread(target=writer_b)
    tb.start()
    ta.join(30)
    tb.join(30)
    assert errors == []
    assert a.get("g", "runs", "n") == 1 and a.get("g", "gitops", "seen") is True


def test_another_process_connection_waits_instead_of_failing(tmp_path):
    """A foreign connection (another process) on the same file: busy_timeout and WAL
    let a short competing write wait rather than raise."""
    path = tmp_path / "guilds.db"
    s = GuildStore(path)
    other = sqlite3.connect(str(path), timeout=store_mod.BUSY_TIMEOUT_S)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO kv VALUES ('g','x','k','1')")
    done = []
    t = threading.Thread(target=lambda: (s.set("g", "p", "k", 2), done.append(1)))
    t.start()
    time.sleep(0.3)
    reader = sqlite3.connect(str(path), timeout=0.1)  # WAL: a reader is never blocked
    assert reader.execute("SELECT count(*) FROM kv WHERE plugin='x'").fetchone()[0] == 0
    reader.close()
    other.commit()
    other.close()
    t.join(10)
    assert done == [1] and s.get("g", "p", "k") == 2
