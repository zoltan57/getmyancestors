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


def test_diff_default_picks_two_most_recent_same_seed_runs(initialized_db, capsys) -> None:
    conn = initialized_db
    old_same_seed = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, old_same_seed, total=1, failed=0)
    middle_other_seed = start_run(conn, ["getmyancestors", "fetch", "-i", "BBBB-002"])
    finish_run(conn, middle_other_seed, total=1, failed=0)
    new_same_seed = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, new_same_seed, total=1, failed=0)

    diff(conn)
    output = capsys.readouterr().out
    assert f"Comparing runs {old_same_seed} -> {new_same_seed}" in output


def test_diff_default_groups_runs_without_ids_as_self_seed(initialized_db, capsys) -> None:
    conn = initialized_db
    old_self_seed = start_run(conn, ["getmyancestors", "fetch"])
    finish_run(conn, old_self_seed, total=1, failed=0)
    middle_other_seed = start_run(conn, ["getmyancestors", "fetch", "-i", "BBBB-002"])
    finish_run(conn, middle_other_seed, total=1, failed=0)
    new_self_seed = start_run(conn, ["getmyancestors", "fetch"])
    finish_run(conn, new_self_seed, total=1, failed=0)

    diff(conn)
    output = capsys.readouterr().out
    assert f"Comparing runs {old_self_seed} -> {new_self_seed}" in output


def test_diff_default_falls_back_to_two_most_recent_overall_when_no_seed_pair(initialized_db, capsys) -> None:
    conn = initialized_db
    oldest_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, oldest_run, total=1, failed=0)
    middle_run = start_run(conn, ["getmyancestors", "fetch", "-i", "BBBB-002"])
    finish_run(conn, middle_run, total=1, failed=0)
    newest_run = start_run(conn, ["getmyancestors", "fetch", "-i", "CCCC-003"])
    finish_run(conn, newest_run, total=1, failed=0)

    diff(conn)
    output = capsys.readouterr().out
    assert f"Comparing runs {middle_run} -> {newest_run}" in output
