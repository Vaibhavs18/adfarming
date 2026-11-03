"""SQLite schema + migrations. Connection per call, WAL, explicit transactions."""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_DB = "data/studio.db"

MIGRATIONS: list[str] = [
    # v1
    """
    CREATE TABLE clients (
        id TEXT PRIMARY KEY,
        name TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL REFERENCES clients(id),
        brief TEXT,
        created_at TEXT NOT NULL
    );
    CREATE TABLE events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        kind TEXT NOT NULL,
        actor TEXT NOT NULL CHECK (actor IN ('human','agent','system')),
        payload TEXT NOT NULL DEFAULT '{}',
        reason TEXT
    );
    CREATE INDEX idx_events_run ON events(run_id, id);
    CREATE TRIGGER events_no_update BEFORE UPDATE ON events
        BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
    CREATE TRIGGER events_no_delete BEFORE DELETE ON events
        BEGIN SELECT RAISE(ABORT, 'events is append-only'); END;
    CREATE TABLE gates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        step TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending','approved','steered','edited','rejected','back')),
        requested_at TEXT NOT NULL,
        answered_at TEXT,
        answer TEXT
    );
    CREATE INDEX idx_gates_run ON gates(run_id, id);
    CREATE TABLE memory_items (
        id TEXT PRIMARY KEY,
        client_id TEXT NOT NULL REFERENCES clients(id),
        kind TEXT NOT NULL CHECK (kind IN
            ('brand_voice','hard_rule','performance','asset','preference','rejection')),
        text TEXT NOT NULL,
        norm TEXT NOT NULL,
        source TEXT NOT NULL CHECK (source IN ('human','agent_inferred')),
        status TEXT NOT NULL CHECK (status IN
            ('proposed','approved','rejected','superseded')),
        evidence TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        supersedes TEXT,
        pinned INTEGER NOT NULL DEFAULT 0,
        decided_at TEXT,
        decided_by TEXT,
        decision_note TEXT,
        edited_from TEXT
    );
    CREATE INDEX idx_memory_client ON memory_items(client_id, status);
    CREATE TABLE memory_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        client_id TEXT NOT NULL,
        ts TEXT NOT NULL,
        action TEXT NOT NULL,
        changes TEXT NOT NULL,       -- JSON [{id, before_status, after_status}]
        undone INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE cost_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL,
        tool TEXT NOT NULL,
        usd REAL NOT NULL,
        ts TEXT NOT NULL
    );
    CREATE INDEX idx_cost_run ON cost_ledger(run_id);
    """,
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def db_path() -> Path:
    return Path(os.environ.get("ADFARMING_DB") or DEFAULT_DB)


def _migrate(conn: sqlite3.Connection) -> None:
    ver = conn.execute("PRAGMA user_version").fetchone()[0]
    for i in range(ver, len(MIGRATIONS)):
        conn.execute("BEGIN IMMEDIATE")
        try:
            # re-check under the write lock (another process may have migrated)
            if conn.execute("PRAGMA user_version").fetchone()[0] > i:
                conn.execute("COMMIT")
                continue
            for stmt in _split(MIGRATIONS[i]):
                conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {i + 1}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def _split(script: str) -> list[str]:
    """Split on ';' but keep CREATE TRIGGER ... END; bodies whole."""
    out, buf, in_trigger = [], [], False
    for line in script.splitlines():
        buf.append(line)
        s = line.strip().upper()
        if s.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if s.endswith("END;"):
                out.append("\n".join(buf)); buf, in_trigger = [], False
        elif s.endswith(";"):
            out.append("\n".join(buf)); buf = []
    if "".join(buf).strip():
        out.append("\n".join(buf))
    return out


def connect(path: str | os.PathLike | None = None) -> sqlite3.Connection:
    p = Path(path) if path else db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    _migrate(conn)
    return conn


@contextmanager
def tx(conn: sqlite3.Connection | None = None):
    """Write transaction (BEGIN IMMEDIATE). Pass an existing conn to join it."""
    if conn is not None:
        yield conn  # caller owns the transaction
        return
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
            c.execute("COMMIT")
        except BaseException:
            c.execute("ROLLBACK")
            raise
    finally:
        c.close()


@contextmanager
def read():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def ensure_client(conn: sqlite3.Connection, client_id: str, name: str | None = None) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO clients(id,name,created_at) VALUES (?,?,?)",
        (client_id, name or client_id, now()),
    )
