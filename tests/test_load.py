from __future__ import annotations

import copy
import json

from getmyancestors.db import finish_run, start_run, store_response
from getmyancestors.load import load


def _insert_api_response(
    conn,
    run_id: int,
    *,
    kind: str,
    body: dict,
    subject_fid: str | None = None,
    url: str | None = None,
) -> None:
    response_url = url or f"/fixture/{kind}"
    store_response(
        conn,
        run_id=run_id,
        kind=kind,
        url=response_url,
        subject_fid=subject_fid,
        http_status=200,
        ok=1,
        body=json.dumps(body),
    )


def _table_count(conn, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"])


def _person(fid: str, name: str, *, living: bool = False, gender_type: str | None = None) -> dict:
    person: dict[str, object] = {
        "id": fid,
        "living": living,
        "names": [
            {
                "preferred": True,
                "nameForms": [{"fullText": name, "parts": []}],
            }
        ],
        "facts": [],
    }
    if gender_type is not None:
        person["gender"] = {"type": gender_type}
    return person


def test_load_builds_relational_tables_from_raw_fixtures(initialized_db, load_fixture) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite"])
    finish_run(conn, run_id, total=6, failed=0)

    persons_batch = load_fixture("persons_batch.json")
    person_sources = load_fixture("person_sources.json")
    person_notes = load_fixture("person_notes.json")
    couple = load_fixture("couple.json")
    memory = load_fixture("memory.json")
    couple_notes = {"notes": [{"subject": "Marriage note", "text": "Married in Hamburg parish."}]}

    _insert_api_response(
        conn,
        run_id,
        kind="persons_batch",
        body=persons_batch,
        url="/platform/tree/persons?pids=AAAA-001,BBBB-002,CCCC-003",
    )
    _insert_api_response(
        conn,
        run_id,
        kind="person_sources",
        body=person_sources,
        subject_fid="AAAA-001",
        url="/platform/tree/persons/AAAA-001/sources",
    )
    _insert_api_response(
        conn,
        run_id,
        kind="person_notes",
        body=person_notes,
        subject_fid="AAAA-001",
        url="/platform/tree/persons/AAAA-001/notes",
    )
    _insert_api_response(
        conn,
        run_id,
        kind="couple",
        body=couple,
        subject_fid="REL-C-1",
        url="/platform/tree/couple-relationships/REL-C-1",
    )
    _insert_api_response(
        conn,
        run_id,
        kind="couple_notes",
        body=couple_notes,
        subject_fid="REL-C-1",
        url="/platform/tree/couple-relationships/REL-C-1/notes",
    )
    _insert_api_response(
        conn,
        run_id,
        kind="memory",
        body=memory,
        subject_fid="AAAA-001",
        url="/platform/memories/memories/MEM-9",
    )

    load(conn, run_id)

    individuals = {
        row["fid"]: row
        for row in conn.execute("SELECT fid, sex, living, display_name FROM individual ORDER BY fid").fetchall()
    }
    assert set(individuals) == {"AAAA-001", "BBBB-002", "CCCC-003"}
    assert individuals["AAAA-001"]["sex"] == "M"
    assert individuals["AAAA-001"]["living"] == 0
    assert individuals["AAAA-001"]["display_name"] == "Johann Schmidt"
    assert individuals["BBBB-002"]["sex"] == "F"
    assert individuals["BBBB-002"]["living"] == 0
    assert individuals["BBBB-002"]["display_name"] == "Anna Meyer"
    assert individuals["CCCC-003"]["sex"] is None
    assert individuals["CCCC-003"]["living"] == 1
    assert individuals["CCCC-003"]["display_name"] == "Karl Schmidt"

    names = conn.execute("SELECT individual_fid, name_type, full_text FROM name ORDER BY individual_fid, id").fetchall()
    assert len(names) == 4
    assert any(
        row["individual_fid"] == "AAAA-001" and row["name_type"] == "aka" and row["full_text"] == "John Smith"
        for row in names
    )

    family = conn.execute("SELECT family_id, couple_fid, husband_fid, wife_fid FROM family").fetchone()
    assert family is not None
    assert family["couple_fid"] == "REL-C-1"
    assert family["husband_fid"] == "AAAA-001"
    assert family["wife_fid"] == "BBBB-002"
    family_id = int(family["family_id"])

    family_child = conn.execute("SELECT family_id, child_fid FROM family_child").fetchone()
    assert family_child is not None
    assert family_child["family_id"] == family_id
    assert family_child["child_fid"] == "CCCC-003"

    birth_event = conn.execute(
        """
        SELECT individual_fid, family_id, type_uri, date_formal, place_latitude, place_longitude
        FROM event
        WHERE individual_fid = 'AAAA-001' AND type_uri = 'http://gedcomx.org/Birth'
        """
    ).fetchone()
    assert birth_event is not None
    assert birth_event["date_formal"] == "+1850-03-12"
    assert birth_event["place_latitude"] == 53.55
    assert birth_event["place_longitude"] == 9.99

    occupation_event = conn.execute(
        """
        SELECT type_uri, date_original, date_formal, place_original
        FROM event
        WHERE individual_fid = 'AAAA-001' AND type_uri = 'http://gedcomx.org/Occupation'
        """
    ).fetchone()
    assert occupation_event is not None
    assert occupation_event["date_original"] is None
    assert occupation_event["date_formal"] is None
    assert occupation_event["place_original"] is None

    marriage_event = conn.execute(
        """
        SELECT individual_fid, family_id, type_uri
        FROM event
        WHERE type_uri = 'http://gedcomx.org/Marriage'
        """
    ).fetchone()
    assert marriage_event is not None
    assert marriage_event["individual_fid"] is None
    assert marriage_event["family_id"] == family_id

    assert _table_count(conn, "source") == 2
    assert _table_count(conn, "source_link") == 3
    assert _table_count(conn, "note") == 3

    memory_row = conn.execute("SELECT url FROM memory").fetchone()
    assert memory_row is not None
    assert memory_row["url"] == "https://www.familysearch.org/photos/artifacts/12345"

    baseline_counts = {
        table: _table_count(conn, table)
        for table in [
            "individual",
            "name",
            "family",
            "family_child",
            "event",
            "source",
            "source_link",
            "note",
            "memory",
        ]
    }

    load(conn, run_id)

    rerun_counts = {table: _table_count(conn, table) for table in baseline_counts}
    assert rerun_counts == baseline_counts


def test_load_creates_stub_individual_for_unfetched_parent(initialized_db, load_fixture) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite"])
    finish_run(conn, run_id, total=1, failed=0)

    persons_batch = copy.deepcopy(load_fixture("persons_batch.json"))
    persons_batch["childAndParentsRelationships"][0]["parent1"]["resourceId"] = "DDDD-004"
    _insert_api_response(
        conn,
        run_id,
        kind="persons_batch",
        body=persons_batch,
        url="/platform/tree/persons?pids=AAAA-001,BBBB-002,CCCC-003",
    )

    load(conn, run_id)

    stub = conn.execute(
        """
        SELECT fid, sex, living, display_name
        FROM individual
        WHERE fid = 'DDDD-004'
        """
    ).fetchone()
    assert stub is not None
    assert stub["sex"] is None
    assert stub["living"] is None
    assert stub["display_name"] is None


def test_load_assigns_husband_wife_by_gender_when_parent1_is_the_mother(initialized_db) -> None:
    conn = initialized_db
    run_id = start_run(conn, ["getmyancestors", "fetch", "--db", "tmp.sqlite"])
    finish_run(conn, run_id, total=1, failed=0)

    # GedcomX's parent1/parent2 are gender-neutral positional slots -- here parent1
    # is deliberately the mother, to verify husband/wife are assigned by recorded
    # gender.type rather than by blindly trusting parent1->husband.
    _insert_api_response(
        conn,
        run_id,
        kind="persons_batch",
        body={
            "persons": [
                _person("MOTHER-001", "Mother First", gender_type="http://gedcomx.org/Female"),
                _person("FATHER-001", "Father Second", gender_type="http://gedcomx.org/Male"),
                _person("CHILD-001", "Child"),
            ],
            "childAndParentsRelationships": [
                {
                    "id": "REL-CP-1",
                    "parent1": {"resourceId": "MOTHER-001"},
                    "parent2": {"resourceId": "FATHER-001"},
                    "child": {"resourceId": "CHILD-001"},
                }
            ],
        },
    )

    load(conn, run_id)

    family = conn.execute("SELECT husband_fid, wife_fid FROM family").fetchone()
    assert family is not None
    assert family["husband_fid"] == "FATHER-001"
    assert family["wife_fid"] == "MOTHER-001"


def test_load_merged_keeps_people_from_non_overlapping_finished_runs(initialized_db) -> None:
    conn = initialized_db
    old_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, old_run, total=1, failed=0)
    new_run = start_run(conn, ["getmyancestors", "fetch", "-i", "BBBB-002"])
    finish_run(conn, new_run, total=1, failed=0)

    old_batch = {
        "persons": [
            _person("AAAA-001", "Old Line Person", gender_type="http://gedcomx.org/Male"),
        ]
    }
    old_batch["persons"][0]["facts"] = [
        {
            "type": "http://gedcomx.org/Birth",
            "date": {"formal": "+1900-01-01"},
        }
    ]
    new_batch = {
        "persons": [
            _person("BBBB-002", "New Line Person", gender_type="http://gedcomx.org/Female"),
        ]
    }
    new_batch["persons"][0]["facts"] = [
        {
            "type": "http://gedcomx.org/Birth",
            "date": {"formal": "+1910-02-02"},
        }
    ]

    _insert_api_response(conn, old_run, kind="persons_batch", body=old_batch)
    _insert_api_response(conn, new_run, kind="persons_batch", body=new_batch)

    load(conn)

    individuals = conn.execute("SELECT fid, display_name FROM individual ORDER BY fid").fetchall()
    assert [(row["fid"], row["display_name"]) for row in individuals] == [
        ("AAAA-001", "Old Line Person"),
        ("BBBB-002", "New Line Person"),
    ]
    events = conn.execute("SELECT individual_fid, date_formal FROM event ORDER BY individual_fid").fetchall()
    assert [(row["individual_fid"], row["date_formal"]) for row in events] == [
        ("AAAA-001", "+1900-01-01"),
        ("BBBB-002", "+1910-02-02"),
    ]


def test_load_merged_uses_latest_capture_for_refetched_person(initialized_db) -> None:
    conn = initialized_db
    older_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, older_run, total=1, failed=0)
    newer_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, newer_run, total=1, failed=0)

    _insert_api_response(
        conn,
        older_run,
        kind="persons_batch",
        body={"persons": [_person("AAAA-001", "Original Name")]},
    )
    _insert_api_response(
        conn,
        newer_run,
        kind="persons_batch",
        body={"persons": [_person("AAAA-001", "Updated Name")]},
    )

    load(conn)

    row = conn.execute("SELECT display_name FROM individual WHERE fid = 'AAAA-001'").fetchone()
    assert row is not None
    assert row["display_name"] == "Updated Name"
    names = conn.execute("SELECT full_text FROM name WHERE individual_fid = 'AAAA-001'").fetchall()
    assert [name["full_text"] for name in names] == ["Updated Name"]


def test_load_merged_keeps_older_person_when_later_runs_do_not_refetch(initialized_db) -> None:
    conn = initialized_db
    first_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, first_run, total=1, failed=0)
    second_run = start_run(conn, ["getmyancestors", "fetch", "-i", "BBBB-002"])
    finish_run(conn, second_run, total=1, failed=0)

    _insert_api_response(
        conn,
        first_run,
        kind="persons_batch",
        body={"persons": [_person("AAAA-001", "Persisting Person")]},
    )
    _insert_api_response(
        conn,
        second_run,
        kind="persons_batch",
        body={"persons": [_person("BBBB-002", "Later Person")]},
    )

    load(conn)

    people = conn.execute("SELECT fid, display_name FROM individual ORDER BY fid").fetchall()
    assert [(row["fid"], row["display_name"]) for row in people] == [
        ("AAAA-001", "Persisting Person"),
        ("BBBB-002", "Later Person"),
    ]


def test_load_merged_keeps_relationship_when_later_person_winner_omits_it(initialized_db) -> None:
    conn = initialized_db
    early_run = start_run(conn, ["getmyancestors", "fetch", "-i", "CHILD-001"])
    finish_run(conn, early_run, total=1, failed=0)
    later_run = start_run(conn, ["getmyancestors", "fetch", "-i", "CHILD-001"])
    finish_run(conn, later_run, total=1, failed=0)

    _insert_api_response(
        conn,
        early_run,
        kind="persons_batch",
        body={
            "persons": [
                _person("CHILD-001", "Child Early"),
            ],
            "childAndParentsRelationships": [
                {
                    "id": "REL-CP-1",
                    "parent1": {"resourceId": "FATHER-001"},
                    "parent2": {"resourceId": "MOTHER-001"},
                    "child": {"resourceId": "CHILD-001"},
                }
            ],
        },
    )
    _insert_api_response(
        conn,
        later_run,
        kind="persons_batch",
        body={
            "persons": [
                _person("CHILD-001", "Child Later"),
            ],
        },
    )

    load(conn)

    family_child = conn.execute("SELECT family_id, child_fid, rel_fid FROM family_child").fetchone()
    assert family_child is not None
    assert family_child["child_fid"] == "CHILD-001"
    assert family_child["rel_fid"] == "REL-CP-1"
    family = conn.execute(
        "SELECT family_id, husband_fid, wife_fid FROM family WHERE family_id = ?",
        (family_child["family_id"],),
    ).fetchone()
    assert family is not None
    assert family["husband_fid"] == "FATHER-001"
    assert family["wife_fid"] == "MOTHER-001"


def test_load_merged_keeps_multiple_memories_for_same_person_across_runs(initialized_db) -> None:
    conn = initialized_db
    first_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, first_run, total=1, failed=0)
    second_run = start_run(conn, ["getmyancestors", "fetch", "-i", "AAAA-001"])
    finish_run(conn, second_run, total=1, failed=0)

    _insert_api_response(
        conn,
        first_run,
        kind="memory",
        subject_fid="AAAA-001",
        url="/platform/memories/memories/MEM-1",
        body={
            "sourceDescriptions": [
                {
                    "id": "MEM-1",
                    "about": "https://www.familysearch.org/photos/artifacts/1",
                    "mediaType": "image/jpeg",
                    "titles": [{"value": "First"}],
                }
            ]
        },
    )
    _insert_api_response(
        conn,
        second_run,
        kind="memory",
        subject_fid="AAAA-001",
        url="/platform/memories/memories/MEM-2",
        body={
            "sourceDescriptions": [
                {
                    "id": "MEM-2",
                    "about": "https://www.familysearch.org/photos/artifacts/2",
                    "mediaType": "image/jpeg",
                    "titles": [{"value": "Second"}],
                }
            ]
        },
    )

    load(conn)

    memories = conn.execute(
        "SELECT individual_fid, memory_fid, url FROM memory WHERE individual_fid = 'AAAA-001' ORDER BY memory_fid"
    ).fetchall()
    assert [(row["individual_fid"], row["memory_fid"], row["url"]) for row in memories] == [
        ("AAAA-001", "MEM-1", "https://www.familysearch.org/photos/artifacts/1"),
        ("AAAA-001", "MEM-2", "https://www.familysearch.org/photos/artifacts/2"),
    ]
