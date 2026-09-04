from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from getmyancestors.db import (
    clear_relational,
    connect,
    finish_run,
    init_schema,
    latest_finished_run,
    start_run,
    store_response,
)


def test_schema_initializes_on_tmp_db(temp_db_path: Path) -> None:
    conn = connect(temp_db_path)
    try:
        init_schema(conn)
        row = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='fetch_run'").fetchone()
        assert row is not None
    finally:
        conn.close()


def test_family_child_foreign_key_is_enforced(initialized_db) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["fetch", "--db", "tmp.sqlite3"])
    finish_run(conn, run_id, 1, 0)
    conn.execute(
        "INSERT INTO individual (fid, run_id) VALUES (?, ?)",
        ("PARENT-1", run_id),
    )
    conn.execute(
        "INSERT INTO family (husband_fid, wife_fid) VALUES (?, ?)",
        ("PARENT-1", None),
    )
    family_id = conn.execute("SELECT family_id FROM family").fetchone()["family_id"]

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO family_child (family_id, child_fid, rel_fid) VALUES (?, ?, ?)",
            (family_id, "UNKNOWN-CHILD", "REL-1"),
        )


def test_event_check_requires_individual_or_family(initialized_db) -> None:
    conn = initialized_db
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """
            INSERT INTO event (type_uri, value, date_original, date_formal, place_original)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("http://gedcomx.org/Birth", "x", None, None, None),
        )


def test_run_lifecycle_helpers_round_trip(initialized_db) -> None:
    conn = initialized_db
    assert latest_finished_run(conn) is None

    run_one = start_run(conn, ["getmyancestors", "fetch", "-u", "user"])
    run_two = start_run(conn, ["getmyancestors", "fetch", "-u", "user", "-i", "AAAA-001"])
    assert latest_finished_run(conn) is None

    finish_run(conn, run_one, total=3, failed=1)
    assert latest_finished_run(conn) == run_one

    store_response(
        conn,
        run_id=run_one,
        kind="current_user",
        url="/platform/users/current",
        subject_fid="AAAA-001",
        http_status=200,
        ok=1,
        body='{"users":[{"personId":"AAAA-001"}]}',
    )

    finish_run(conn, run_two, total=4, failed=0)
    assert latest_finished_run(conn) == run_two

    run_row = conn.execute("SELECT * FROM fetch_run WHERE run_id = ?", (run_one,)).fetchone()
    assert run_row is not None
    assert run_row["requests_total"] == 3
    assert run_row["requests_failed"] == 1
    assert json.loads(run_row["cli_args"]) == ["getmyancestors", "fetch", "-u", "user"]
    assert run_row["finished_at"] is not None

    api_row = conn.execute("SELECT * FROM api_response WHERE run_id = ?", (run_one,)).fetchone()
    assert api_row is not None
    assert api_row["kind"] == "current_user"
    assert api_row["ok"] == 1
    assert api_row["http_status"] == 200
    assert api_row["body"] == '{"users":[{"personId":"AAAA-001"}]}'
    assert api_row["fetched_at"] is not None

    conn.execute("INSERT INTO individual (fid, run_id) VALUES (?, ?)", ("AAAA-001", run_two))
    conn.execute("INSERT INTO source (fid) VALUES (?)", ("SRC-1",))
    conn.execute(
        "INSERT INTO source_link (source_fid, individual_fid, change_message) VALUES (?, ?, ?)",
        ("SRC-1", "AAAA-001", "citation"),
    )
    clear_relational(conn)
    assert conn.execute("SELECT COUNT(*) AS c FROM individual").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM source").fetchone()["c"] == 0
    assert conn.execute("SELECT COUNT(*) AS c FROM source_link").fetchone()["c"] == 0
