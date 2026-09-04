from __future__ import annotations

import json

from getmyancestors.db import finish_run, start_run, store_response
from getmyancestors.diff import diff


def test_diff_reports_appeared_disappeared_and_name_changes(initialized_db, capsys) -> None:
    conn = initialized_db
    old_run = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite", "--old"])
    finish_run(conn, old_run, total=1, failed=0)
    new_run = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite", "--new"])
    finish_run(conn, new_run, total=1, failed=0)

    old_batch = {
        "persons": [
            {"id": "AAAA-001", "names": [{"preferred": True, "nameForms": [{"fullText": "Old Name"}]}]},
            {"id": "BBBB-002", "names": [{"preferred": True, "nameForms": [{"fullText": "Same Name"}]}]},
        ]
    }
    new_batch = {
        "persons": [
            {"id": "AAAA-001", "names": [{"preferred": True, "nameForms": [{"fullText": "New Name"}]}]},
            {"id": "CCCC-003", "names": [{"preferred": True, "nameForms": [{"fullText": "New Person"}]}]},
        ]
    }

    store_response(
        conn,
        run_id=old_run,
        kind="persons_batch",
        url="/platform/tree/persons?pids=AAAA-001,BBBB-002",
        subject_fid=None,
        http_status=200,
        ok=1,
        body=json.dumps(old_batch),
    )
    store_response(
        conn,
        run_id=new_run,
        kind="persons_batch",
        url="/platform/tree/persons?pids=AAAA-001,CCCC-003",
        subject_fid=None,
        http_status=200,
        ok=1,
        body=json.dumps(new_batch),
    )

    diff(conn, old_run, new_run)
    output = capsys.readouterr().out

    assert f"Comparing runs {old_run} -> {new_run}" in output
    assert "Appeared (1):" in output
    assert "  + CCCC-003" in output
    assert "Disappeared (1):" in output
    assert "  - BBBB-002" in output
    assert "may have been merged in FamilySearch" in output
    assert "Display-name changes (1):" in output
    assert "  * AAAA-001: 'Old Name' -> 'New Name'" in output
