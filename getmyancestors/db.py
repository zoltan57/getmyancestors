"""SQLite connection, schema initialization, and run/capture helpers."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_SQL = """
-- ---- capture layer (append-only; source of truth) ----
CREATE TABLE IF NOT EXISTS fetch_run (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    cli_args        TEXT NOT NULL,
    requests_total  INTEGER NOT NULL DEFAULT 0,
    requests_failed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_response (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES fetch_run(run_id),
    kind        TEXT NOT NULL,
    url         TEXT NOT NULL,
    subject_fid TEXT,
    fetched_at  TEXT NOT NULL,
    http_status INTEGER,
    ok          INTEGER NOT NULL,
    body        TEXT
);
CREATE INDEX IF NOT EXISTS idx_api_response_run_kind ON api_response(run_id, kind);

-- ---- append-only indexes for persons_batch payload members ----
CREATE TABLE IF NOT EXISTS person_batch_member (
    response_id INTEGER NOT NULL REFERENCES api_response(id),
    fid         TEXT NOT NULL,
    PRIMARY KEY (response_id, fid)
);
CREATE INDEX IF NOT EXISTS idx_person_batch_member_fid ON person_batch_member(fid);

CREATE TABLE IF NOT EXISTS batch_relationship_member (
    response_id INTEGER NOT NULL REFERENCES api_response(id),
    rel_fid     TEXT NOT NULL,
    PRIMARY KEY (response_id, rel_fid)
);
CREATE INDEX IF NOT EXISTS idx_batch_relationship_member_rel_fid
    ON batch_relationship_member(rel_fid);

-- ---- relational layer (derived; dropped and rebuilt by `load`) ----
CREATE TABLE IF NOT EXISTS individual (
    fid          TEXT PRIMARY KEY,
    sex          TEXT,
    living       INTEGER,
    display_name TEXT,
    run_id       INTEGER NOT NULL REFERENCES fetch_run(run_id)
);

CREATE TABLE IF NOT EXISTS name (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT NOT NULL REFERENCES individual(fid),
    name_type      TEXT NOT NULL,
    given          TEXT,
    surname        TEXT,
    prefix         TEXT,
    suffix         TEXT,
    full_text      TEXT
);

CREATE TABLE IF NOT EXISTS family (
    family_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    couple_fid  TEXT UNIQUE,
    husband_fid TEXT REFERENCES individual(fid),
    wife_fid    TEXT REFERENCES individual(fid),
    UNIQUE (husband_fid, wife_fid)
);

CREATE TABLE IF NOT EXISTS family_child (
    family_id INTEGER NOT NULL REFERENCES family(family_id),
    child_fid TEXT NOT NULL REFERENCES individual(fid),
    rel_fid   TEXT,
    PRIMARY KEY (family_id, child_fid)
);

CREATE TABLE IF NOT EXISTS event (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid  TEXT REFERENCES individual(fid),
    family_id       INTEGER REFERENCES family(family_id),
    type_uri        TEXT NOT NULL,
    value           TEXT,
    date_original   TEXT,
    date_formal     TEXT,
    place_original  TEXT,
    place_latitude  REAL,
    place_longitude REAL,
    CHECK (individual_fid IS NOT NULL OR family_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS source (
    fid      TEXT PRIMARY KEY,
    title    TEXT,
    citation TEXT,
    url      TEXT
);

CREATE TABLE IF NOT EXISTS source_link (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_fid     TEXT NOT NULL REFERENCES source(fid),
    individual_fid TEXT REFERENCES individual(fid),
    family_id      INTEGER REFERENCES family(family_id),
    change_message TEXT,
    CHECK (individual_fid IS NOT NULL OR family_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS note (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT REFERENCES individual(fid),
    family_id      INTEGER REFERENCES family(family_id),
    subject        TEXT,
    text           TEXT,
    CHECK (individual_fid IS NOT NULL OR family_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS memory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT NOT NULL REFERENCES individual(fid),
    memory_fid     TEXT,
    url            TEXT,
    description    TEXT,
    media_type     TEXT
);
"""


def _utc_now_iso() -> str:
    """Return current UTC timestamp as an ISO-8601 string."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def connect(path: str | Path) -> sqlite3.Connection:
    """Create a SQLite connection with FK checks and row access by column name."""
    connection = sqlite3.connect(Path(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def init_schema(conn: sqlite3.Connection) -> None:
    """Create all capture and relational tables if they do not already exist."""
    conn.executescript(SCHEMA_SQL)
    conn.commit()


def start_run(conn: sqlite3.Connection, argv: list[str]) -> int:
    """Insert a fetch run row and return its run ID."""
    cursor = conn.execute(
        """
        INSERT INTO fetch_run (started_at, cli_args, requests_total, requests_failed)
        VALUES (?, ?, 0, 0)
        """,
        (
            _utc_now_iso(),
            json.dumps(argv),
        ),
    )
    conn.commit()
    return int(cursor.lastrowid)


def finish_run(conn: sqlite3.Connection, run_id: int, total: int, failed: int) -> None:
    """Mark a run finished and persist final request counters."""
    conn.execute(
        """
        UPDATE fetch_run
        SET finished_at = ?, requests_total = ?, requests_failed = ?
        WHERE run_id = ?
        """,
        (_utc_now_iso(), total, failed, run_id),
    )
    conn.commit()


def store_response(
    conn: sqlite3.Connection,
    run_id: int,
    kind: str,
    url: str,
    subject_fid: str | None,
    http_status: int | None,
    ok: int,
    body: str | None,
) -> None:
    """Store one raw API response row for a run."""
    conn.execute(
        """
        INSERT INTO api_response
            (run_id, kind, url, subject_fid, fetched_at, http_status, ok, body)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, kind, url, subject_fid, _utc_now_iso(), http_status, ok, body),
    )
    conn.commit()


def latest_finished_run(conn: sqlite3.Connection) -> int | None:
    """Return the newest finished run ID, or None when no finished run exists."""
    row = conn.execute(
        """
        SELECT run_id
        FROM fetch_run
        WHERE finished_at IS NOT NULL
        ORDER BY run_id DESC
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return None
    return int(row["run_id"])


def parse_json_body(body: str | None) -> dict[str, Any] | None:
    """Parse one JSON body into a dictionary, returning None on invalid input."""
    if not body:
        return None
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        return data
    return None


def sync_batch_indexes(conn: sqlite3.Connection) -> None:
    """Incrementally index persons_batch payload membership for future relational loads."""
    rows = conn.execute(
        """
        SELECT id, body
        FROM api_response
        WHERE kind = 'persons_batch'
          AND ok = 1
          AND id NOT IN (SELECT DISTINCT response_id FROM person_batch_member)
        ORDER BY id
        """
    ).fetchall()

    with conn:
        for row in rows:
            response_id = int(row["id"])
            payload = parse_json_body(row["body"])
            if payload is None:
                conn.execute(
                    "INSERT OR IGNORE INTO person_batch_member (response_id, fid) VALUES (?, ?)",
                    (response_id, ""),
                )
                continue

            person_fids = {str(person.get("id")) for person in payload.get("persons", []) if person.get("id")}
            relationship_fids = {
                str(relation.get("id"))
                for relation in payload.get("childAndParentsRelationships", [])
                if relation.get("id")
            }
            relationship_fids.update(
                str(relation.get("id"))
                for relation in payload.get("relationships", [])
                if relation.get("type") == "http://gedcomx.org/Couple" and relation.get("id")
            )

            if not person_fids and not relationship_fids:
                # Sentinel marks this response as indexed so future syncs never reparse it.
                conn.execute(
                    "INSERT OR IGNORE INTO person_batch_member (response_id, fid) VALUES (?, ?)",
                    (response_id, ""),
                )
                continue

            conn.executemany(
                "INSERT OR IGNORE INTO person_batch_member (response_id, fid) VALUES (?, ?)",
                [(response_id, fid) for fid in sorted(person_fids)],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO batch_relationship_member (response_id, rel_fid) VALUES (?, ?)",
                [(response_id, rel_fid) for rel_fid in sorted(relationship_fids)],
            )


def clear_relational(conn: sqlite3.Connection) -> None:
    """Delete all derived relational rows in FK-safe order inside one transaction."""
    with conn:
        conn.execute("DELETE FROM family_child")
        conn.execute("DELETE FROM event")
        conn.execute("DELETE FROM source_link")
        conn.execute("DELETE FROM note")
        conn.execute("DELETE FROM memory")
        conn.execute("DELETE FROM name")
        conn.execute("DELETE FROM family")
        conn.execute("DELETE FROM source")
        conn.execute("DELETE FROM individual")
