"""Diff reports between two finished fetch runs."""

from __future__ import annotations

import sqlite3
from typing import Any

from getmyancestors.db import parse_json_body


def _display_name(person: dict[str, Any]) -> str | None:
    """Extract one display name from a person payload."""
    for name in person.get("names", []):
        form = (name.get("nameForms") or [{}])[0]
        full_text = form.get("fullText")
        if name.get("preferred") and full_text:
            return str(full_text)
    for name in person.get("names", []):
        form = (name.get("nameForms") or [{}])[0]
        full_text = form.get("fullText")
        if full_text:
            return str(full_text)
    return None


def _run_people(conn: sqlite3.Connection, run_id: int) -> dict[str, str | None]:
    """Return fid->display_name mapping parsed from persons_batch rows for one run."""
    people: dict[str, str | None] = {}
    rows = conn.execute(
        """
        SELECT body
        FROM api_response
        WHERE run_id = ? AND kind = 'persons_batch' AND ok = 1
        ORDER BY id
        """,
        (run_id,),
    ).fetchall()
    for row in rows:
        payload = parse_json_body(row["body"])
        if payload is None:
            continue
        for person in payload.get("persons", []):
            fid = person.get("id")
            if not fid:
                continue
            people[fid] = _display_name(person)
    return people


def _resolve_run_pair(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return the two most recent finished runs with the same seed, else latest two."""
    import json

    rows = conn.execute(
        """
        SELECT run_id, cli_args
        FROM fetch_run
        WHERE finished_at IS NOT NULL
        ORDER BY run_id DESC
        """
    ).fetchall()
    if len(rows) < 2:
        raise ValueError("Need at least two finished runs for diff.")

    def seed_from_cli_args(cli_args: str | None) -> tuple[str, ...]:
        if not cli_args:
            return ("__self__",)
        try:
            tokens = json.loads(cli_args)
        except json.JSONDecodeError:
            return ("__self__",)
        if not isinstance(tokens, list):
            return ("__self__",)
        ids: list[str] = []
        idx = 0
        while idx < len(tokens):
            token = tokens[idx]
            if token not in ("-i", "--ids"):
                idx += 1
                continue
            idx += 1
            while idx < len(tokens):
                value = tokens[idx]
                if isinstance(value, str) and value.startswith("-"):
                    break
                ids.append(str(value))
                idx += 1
        if not ids:
            return ("__self__",)
        return tuple(sorted(set(ids)))

    # rows is newest-first, so each seed's run list stays sorted newest-first too.
    run_ids_by_seed: dict[tuple[str, ...], list[int]] = {}
    for row in rows:
        run_id = int(row["run_id"])
        seed = seed_from_cli_args(row["cli_args"])
        run_ids_by_seed.setdefault(seed, []).append(run_id)

    # Among seed-groups with 2+ runs, prefer the one whose own newest run is the
    # most recent overall -- not an arbitrary older seed-group that happens to
    # complete its own pair first when scanning newest-first.
    best_pair: tuple[int, int] | None = None
    best_recency = -1
    for run_ids in run_ids_by_seed.values():
        if len(run_ids) < 2:
            continue
        newest, second_newest = run_ids[0], run_ids[1]
        if newest > best_recency:
            best_recency = newest
            best_pair = (second_newest, newest)

    if best_pair is not None:
        return best_pair

    return int(rows[1]["run_id"]), int(rows[0]["run_id"])


def _run_loaded_cleanly(conn: sqlite3.Connection, run_id: int) -> bool:
    """Return true when a run exists, finished, and has zero failed requests."""
    row = conn.execute(
        """
        SELECT requests_failed, finished_at
        FROM fetch_run
        WHERE run_id = ?
        """,
        (run_id,),
    ).fetchone()
    if row is None or row["finished_at"] is None:
        return False
    return int(row["requests_failed"]) == 0


def diff(conn: sqlite3.Connection, old_run: int | None = None, new_run: int | None = None) -> None:
    """Print a plain-text diff summary between two finished runs."""
    if old_run is None or new_run is None:
        old_run, new_run = _resolve_run_pair(conn)

    old_people = _run_people(conn, old_run)
    new_people = _run_people(conn, new_run)
    old_ids = set(old_people)
    new_ids = set(new_people)

    appeared = sorted(new_ids - old_ids)
    disappeared = sorted(old_ids - new_ids)

    print(f"Comparing runs {old_run} -> {new_run}")
    print(f"Appeared ({len(appeared)}):")
    for fid in appeared:
        print(f"  + {fid}")
    print(f"Disappeared ({len(disappeared)}):")
    for fid in disappeared:
        print(f"  - {fid}")
        print(
            f"    WARNING: {fid} may have been merged in FamilySearch; "
            "review external document mappings keyed to this FID."
        )

    if _run_loaded_cleanly(conn, old_run) and _run_loaded_cleanly(conn, new_run):
        changed_names: list[tuple[str, str | None, str | None]] = []
        for fid in sorted(old_ids & new_ids):
            if old_people.get(fid) != new_people.get(fid):
                changed_names.append((fid, old_people.get(fid), new_people.get(fid)))
        print(f"Display-name changes ({len(changed_names)}):")
        for fid, old_name, new_name in changed_names:
            print(f"  * {fid}: {old_name!r} -> {new_name!r}")
    else:
        print("Display-name changes: skipped (one or both runs were incomplete).")
