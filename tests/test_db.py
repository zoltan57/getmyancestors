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
    parse_json_body,
    start_run,
    store_response,
    sync_batch_indexes,
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


def test_parse_json_body_handles_valid_and_invalid_inputs() -> None:
    assert parse_json_body('{"persons": []}') == {"persons": []}
    assert parse_json_body('{"persons": [}') is None
    assert parse_json_body('["not", "a", "dict"]') is None
    assert parse_json_body("") is None


def test_sync_batch_indexes_indexes_people_and_both_relationship_types(initialized_db) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite"])
    finish_run(conn, run_id, total=1, failed=0)
    payload = {
        "persons": [{"id": "AAAA-001"}, {"id": "BBBB-002"}],
        "childAndParentsRelationships": [{"id": "REL-CP-1"}],
        "relationships": [
            {"id": "REL-C-1", "type": "http://gedcomx.org/Couple"},
            {"id": "REL-IGNORED", "type": "http://gedcomx.org/ParentChild"},
        ],
    }
    store_response(
        conn,
        run_id=run_id,
        kind="persons_batch",
        url="/platform/tree/persons?pids=AAAA-001,BBBB-002",
        subject_fid=None,
        http_status=200,
        ok=1,
        body=json.dumps(payload),
    )

    sync_batch_indexes(conn)

    people = conn.execute("SELECT fid FROM person_batch_member ORDER BY fid").fetchall()
    rels = conn.execute("SELECT rel_fid FROM batch_relationship_member ORDER BY rel_fid").fetchall()

    assert [row["fid"] for row in people] == ["AAAA-001", "BBBB-002"]
    assert [row["rel_fid"] for row in rels] == ["REL-C-1", "REL-CP-1"]


def test_sync_batch_indexes_is_incremental_and_idempotent(initialized_db) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite"])
    finish_run(conn, run_id, total=2, failed=0)
    store_response(
        conn,
        run_id=run_id,
        kind="persons_batch",
        url="/platform/tree/persons?pids=AAAA-001",
        subject_fid=None,
        http_status=200,
        ok=1,
        body=json.dumps({"persons": [{"id": "AAAA-001"}]}),
    )

    sync_batch_indexes(conn)
    first_counts = {
        "person_batch_member": conn.execute("SELECT COUNT(*) AS c FROM person_batch_member").fetchone()["c"],
        "batch_relationship_member": conn.execute("SELECT COUNT(*) AS c FROM batch_relationship_member").fetchone()[
            "c"
        ],
    }

    sync_batch_indexes(conn)
    second_counts = {
        "person_batch_member": conn.execute("SELECT COUNT(*) AS c FROM person_batch_member").fetchone()["c"],
        "batch_relationship_member": conn.execute("SELECT COUNT(*) AS c FROM batch_relationship_member").fetchone()[
            "c"
        ],
    }
    assert second_counts == first_counts

    store_response(
        conn,
        run_id=run_id,
        kind="persons_batch",
        url="/platform/tree/persons?pids=BBBB-002",
        subject_fid=None,
        http_status=200,
        ok=1,
        body=json.dumps({"persons": [{"id": "BBBB-002"}]}),
    )
    sync_batch_indexes(conn)
    third_counts = {
        "person_batch_member": conn.execute("SELECT COUNT(*) AS c FROM person_batch_member").fetchone()["c"],
        "batch_relationship_member": conn.execute("SELECT COUNT(*) AS c FROM batch_relationship_member").fetchone()[
            "c"
        ],
    }
    assert third_counts["person_batch_member"] == first_counts["person_batch_member"] + 1
    assert third_counts["batch_relationship_member"] == first_counts["batch_relationship_member"]


def test_sync_batch_indexes_marks_empty_and_unparseable_payloads_with_sentinel(initialized_db) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite"])
    finish_run(conn, run_id, total=2, failed=0)

    store_response(
        conn,
        run_id=run_id,
        kind="persons_batch",
        url="/platform/tree/persons?pids=EMPTY-000",
        subject_fid=None,
        http_status=200,
        ok=1,
        body=json.dumps({"persons": []}),
    )
    store_response(
        conn,
        run_id=run_id,
        kind="persons_batch",
        url="/platform/tree/persons?pids=BAD-000",
        subject_fid=None,
        http_status=200,
        ok=1,
        body='{"persons": [}',
    )

    sync_batch_indexes(conn)
    sync_batch_indexes(conn)

    sentinel_rows = conn.execute(
        "SELECT response_id, fid FROM person_batch_member WHERE fid = '' ORDER BY response_id"
    ).fetchall()
    assert len(sentinel_rows) == 2
    assert conn.execute("SELECT COUNT(*) AS c FROM batch_relationship_member").fetchone()["c"] == 0
