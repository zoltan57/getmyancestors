"""Load relational tables from captured raw FamilySearch API responses."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from getmyancestors.db import clear_relational, latest_finished_run, parse_json_body, sync_batch_indexes


def _sex_from_gender_type(type_uri: str | None) -> str | None:
    """Map GedcomX gender type URI suffix to a one-letter sex code."""
    if not type_uri:
        return None
    if type_uri.endswith("Male"):
        return "M"
    if type_uri.endswith("Female"):
        return "F"
    if type_uri.endswith("Unknown"):
        return "U"
    return None


def _name_type(name_entry: dict[str, Any]) -> str:
    """Map a GedcomX name entry to the relational name_type value."""
    if name_entry.get("preferred") is True:
        return "preferred"
    mapping = {
        "BirthName": "birth",
        "MarriedName": "married",
        "AlsoKnownAs": "aka",
        "Nickname": "nickname",
    }
    type_uri = name_entry.get("type", "")
    suffix = str(type_uri).rsplit("/", 1)[-1]
    return mapping.get(suffix, "other")


def _name_parts(parts: list[dict[str, Any]]) -> tuple[str | None, str | None, str | None, str | None]:
    """Extract given/surname/prefix/suffix values from GedcomX name parts."""
    bucket: dict[str, list[str]] = {"Given": [], "Surname": [], "Prefix": [], "Suffix": []}
    for part in parts:
        value = part.get("value")
        if not value:
            continue
        type_uri = str(part.get("type", ""))
        key = type_uri.rsplit("/", 1)[-1]
        if key in bucket:
            bucket[key].append(str(value))

    def joined(values: list[str]) -> str | None:
        return " ".join(values) if values else None

    return (
        joined(bucket["Given"]),
        joined(bucket["Surname"]),
        joined(bucket["Prefix"]),
        joined(bucket["Suffix"]),
    )


def _ensure_individual(conn: sqlite3.Connection, fid: str | None, run_id: int) -> None:
    """Ensure an individual row exists, creating a stub when missing."""
    if not fid:
        return
    conn.execute(
        "INSERT INTO individual (fid, run_id) VALUES (?, ?) ON CONFLICT(fid) DO NOTHING",
        (fid, run_id),
    )


def _upsert_individual(
    conn: sqlite3.Connection,
    run_id: int,
    person: dict[str, Any],
) -> None:
    """Insert or update one individual row from a person payload."""
    fid = person.get("id")
    if not fid:
        return
    display_name: str | None = None
    for name in person.get("names", []):
        name_form = (name.get("nameForms") or [{}])[0]
        if name.get("preferred") and name_form.get("fullText"):
            display_name = name_form.get("fullText")
            break
    if display_name is None:
        for name in person.get("names", []):
            name_form = (name.get("nameForms") or [{}])[0]
            if name_form.get("fullText"):
                display_name = name_form.get("fullText")
                break

    conn.execute(
        """
        INSERT INTO individual (fid, sex, living, display_name, run_id)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(fid) DO UPDATE SET
            sex = excluded.sex,
            living = excluded.living,
            display_name = excluded.display_name,
            run_id = excluded.run_id
        """,
        (
            fid,
            _sex_from_gender_type(person.get("gender", {}).get("type")),
            int(person["living"]) if "living" in person else None,
            display_name,
            run_id,
        ),
    )


def _make_family_resolver(
    conn: sqlite3.Connection,
) -> Callable[[str | None, str | None, int, str | None], int]:
    """Return a get_or_create_family(husband_fid, wife_fid, run_id, couple_fid) resolver.

    The returned closure memoizes family rows by (husband_fid, wife_fid) pair and by
    couple_fid, creating stub individuals and a new family row only the first time a
    given pair/couple is seen; later calls for the same pair/couple reuse the same
    family_id (and backfill couple_fid onto a pair-only family row when it becomes
    known).
    """
    family_pair_to_id: dict[tuple[str | None, str | None], int] = {}
    couple_to_family_id: dict[str, int] = {}

    def get_or_create_family(
        husband_fid: str | None,
        wife_fid: str | None,
        run_id: int,
        couple_fid: str | None = None,
    ) -> int:
        key = (husband_fid, wife_fid)
        if couple_fid and couple_fid in couple_to_family_id:
            family_id = couple_to_family_id[couple_fid]
            family_pair_to_id[key] = family_id
            return family_id
        if key in family_pair_to_id:
            family_id = family_pair_to_id[key]
            if couple_fid:
                conn.execute(
                    "UPDATE family SET couple_fid = COALESCE(couple_fid, ?) WHERE family_id = ?",
                    (couple_fid, family_id),
                )
                couple_to_family_id[couple_fid] = family_id
            return family_id

        if husband_fid:
            _ensure_individual(conn, husband_fid, run_id)
        if wife_fid:
            _ensure_individual(conn, wife_fid, run_id)

        conn.execute(
            "INSERT INTO family (couple_fid, husband_fid, wife_fid) VALUES (?, ?, ?)",
            (couple_fid, husband_fid, wife_fid),
        )
        family_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
        family_pair_to_id[key] = family_id
        if couple_fid:
            couple_to_family_id[couple_fid] = family_id
        return family_id

    return get_or_create_family


def _insert_name_rows(conn: sqlite3.Connection, fid: str, names: list[dict[str, Any]]) -> None:
    """Insert one name row per entry in a person's names[] array."""
    for name in names:
        name_form = (name.get("nameForms") or [{}])[0]
        parts = name_form.get("parts") or []
        given, surname, prefix, suffix = _name_parts(parts)
        conn.execute(
            """
            INSERT INTO name (
                individual_fid, name_type, given, surname, prefix, suffix, full_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid,
                _name_type(name),
                given,
                surname,
                prefix,
                suffix,
                name_form.get("fullText"),
            ),
        )


def _insert_person_event_rows(
    conn: sqlite3.Connection,
    fid: str,
    facts: list[dict[str, Any]],
    places_by_id: dict[str, dict[str, Any]],
) -> None:
    """Insert individual-scoped event/note rows from a person's facts[] array."""
    for fact in facts:
        type_uri = fact.get("type")
        if not type_uri:
            continue
        if type_uri == "http://familysearch.org/v1/LifeSketch":
            conn.execute(
                "INSERT INTO note (individual_fid, subject, text) VALUES (?, ?, ?)",
                (fid, "Life Sketch", fact.get("value")),
            )
            continue

        place = fact.get("place", {})
        place_ref = str(place.get("description", "")).lstrip("#")
        place_row = places_by_id.get(place_ref, {})
        date = fact.get("date", {})
        conn.execute(
            """
            INSERT INTO event (
                individual_fid,
                family_id,
                type_uri,
                value,
                date_original,
                date_formal,
                place_original,
                place_latitude,
                place_longitude
            ) VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                fid,
                type_uri,
                fact.get("value"),
                date.get("original"),
                date.get("formal"),
                place.get("original"),
                place_row.get("latitude"),
                place_row.get("longitude"),
            ),
        )


def _apply_person_batch_payload(
    conn: sqlite3.Connection,
    run_id: int,
    payload: dict[str, Any],
    winner_fids: set[str] | None = None,
) -> None:
    """Insert individual/name/event rows for persons in one persons_batch payload.

    When winner_fids is given, only persons whose fid is in the set are applied
    (used by the merged load path to keep only each person's latest capture);
    otherwise every person with a fid in the payload is applied.
    """
    places_by_id = {str(place.get("id")): place for place in payload.get("places", []) if place.get("id")}
    for person in payload.get("persons", []):
        fid = person.get("id")
        if not fid:
            continue
        if winner_fids is not None and fid not in winner_fids:
            continue
        _upsert_individual(conn, run_id, person)
        _insert_name_rows(conn, fid, person.get("names", []))
        _insert_person_event_rows(conn, fid, person.get("facts", []), places_by_id)


def _lookup_sex(conn: sqlite3.Connection, fid: str | None) -> str | None:
    """Return the recorded one-letter sex code for an individual, or None if unknown."""
    if not fid:
        return None
    row = conn.execute("SELECT sex FROM individual WHERE fid = ?", (fid,)).fetchone()
    return row["sex"] if row is not None else None


def _assign_parents_by_gender(
    conn: sqlite3.Connection,
    parent1_fid: str | None,
    parent2_fid: str | None,
) -> tuple[str | None, str | None]:
    """Return (husband_fid, wife_fid) for a childAndParentsRelationship's parents.

    GedcomX's `parent1`/`parent2` fields are gender-neutral positional slots -- the
    schema makes no guarantee that parent1 is the father -- so assigning
    husband=parent1/wife=parent2 unconditionally can silently swap the couple when
    the API returns the mother first. Reorder only when each parent's recorded
    `gender.type` gives positive evidence of the opposite assignment; fall back to
    the parent1->husband, parent2->wife default when sex is unknown, tied, or both
    parents share a sex, since there is no better signal available then.
    """
    sex1 = _lookup_sex(conn, parent1_fid)
    sex2 = _lookup_sex(conn, parent2_fid)
    if (sex1 == "F" or sex2 == "M") and sex1 != "M" and sex2 != "F":
        return parent2_fid, parent1_fid
    return parent1_fid, parent2_fid


def _apply_child_and_parents_relationships(
    conn: sqlite3.Connection,
    run_id: int,
    payload: dict[str, Any],
    get_or_create_family: Callable[[str | None, str | None, int, str | None], int],
    winner_rel_fids: set[str] | None = None,
) -> None:
    """Insert family/family_child rows from a payload's childAndParentsRelationships[].

    When winner_rel_fids is given, only relationships whose id is in the set are
    applied (merged load path); otherwise every relationship is applied.
    """
    for relation in payload.get("childAndParentsRelationships", []):
        rel_fid = relation.get("id")
        if winner_rel_fids is not None and (not rel_fid or rel_fid not in winner_rel_fids):
            continue
        child_id = relation.get("child", {}).get("resourceId")
        parent1_id = relation.get("parent1", {}).get("resourceId")
        parent2_id = relation.get("parent2", {}).get("resourceId")
        husband_id, wife_id = _assign_parents_by_gender(conn, parent1_id, parent2_id)

        family_id = get_or_create_family(husband_id, wife_id, run_id, None)
        if child_id:
            _ensure_individual(conn, child_id, run_id)
            conn.execute(
                """
                INSERT OR IGNORE INTO family_child (family_id, child_fid, rel_fid)
                VALUES (?, ?, ?)
                """,
                (family_id, child_id, rel_fid),
            )


def _apply_couple_relationships(
    run_id: int,
    payload: dict[str, Any],
    get_or_create_family: Callable[[str | None, str | None, int, str | None], int],
    winner_rel_fids: set[str] | None = None,
) -> None:
    """Create family rows from a payload's Couple-type relationships[].

    When winner_rel_fids is given, only relationships whose id is in the set are
    applied (merged load path); otherwise every Couple relationship is applied.
    """
    for relation in payload.get("relationships", []):
        rel_fid = relation.get("id")
        if winner_rel_fids is not None and (not rel_fid or rel_fid not in winner_rel_fids):
            continue
        if relation.get("type") != "http://gedcomx.org/Couple":
            continue
        husband = relation.get("person1", {}).get("resourceId")
        wife = relation.get("person2", {}).get("resourceId")
        if not husband and not wife:
            continue
        get_or_create_family(husband, wife, run_id, rel_fid)


def _apply_couple_response(
    conn: sqlite3.Connection,
    run_id: int,
    subject_fid: str | None,
    payload: dict[str, Any],
    get_or_create_family: Callable[[str | None, str | None, int, str | None], int],
) -> None:
    """Insert family/event/source rows from one 'couple' kind response payload."""
    relationship = (payload.get("relationships") or [{}])[0]
    couple_fid = relationship.get("id") or subject_fid
    family_id = get_or_create_family(None, None, run_id, couple_fid)

    for fact in relationship.get("facts", []):
        type_uri = fact.get("type")
        if not type_uri:
            continue
        date = fact.get("date", {})
        place = fact.get("place", {})
        conn.execute(
            """
            INSERT INTO event (
                individual_fid,
                family_id,
                type_uri,
                value,
                date_original,
                date_formal,
                place_original,
                place_latitude,
                place_longitude
            ) VALUES (NULL, ?, ?, ?, ?, ?, ?, NULL, NULL)
            """,
            (
                family_id,
                type_uri,
                fact.get("value"),
                date.get("original"),
                date.get("formal"),
                place.get("original"),
            ),
        )

    for source_link in relationship.get("sources", []):
        source_fid = source_link.get("descriptionId")
        if not source_fid:
            continue
        conn.execute(
            "INSERT INTO source (fid) VALUES (?) ON CONFLICT(fid) DO NOTHING",
            (source_fid,),
        )
        change_message = source_link.get("attribution", {}).get("changeMessage")
        conn.execute(
            """
            INSERT INTO source_link (source_fid, individual_fid, family_id, change_message)
            VALUES (?, NULL, ?, ?)
            """,
            (source_fid, family_id, change_message),
        )


def _apply_person_sources_response(
    conn: sqlite3.Connection,
    run_id: int,
    subject_fid: str | None,
    payload: dict[str, Any],
) -> None:
    """Insert source/source_link rows from one 'person_sources' kind response payload."""
    for source_description in payload.get("sourceDescriptions", []):
        source_fid = source_description.get("id")
        if not source_fid:
            continue
        title = ((source_description.get("titles") or [{}])[0]).get("value")
        citation = ((source_description.get("citations") or [{}])[0]).get("value")
        conn.execute(
            """
            INSERT INTO source (fid, title, citation, url)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(fid) DO UPDATE SET
                title = excluded.title,
                citation = excluded.citation,
                url = excluded.url
            """,
            (source_fid, title, citation, source_description.get("about")),
        )

    individual_fid = subject_fid
    if not individual_fid:
        persons = payload.get("persons", [])
        if persons:
            individual_fid = persons[0].get("id")
    if not individual_fid:
        return
    _ensure_individual(conn, individual_fid, run_id)
    person_sources = ((payload.get("persons") or [{}])[0]).get("sources", [])
    for source_link in person_sources:
        source_fid = source_link.get("descriptionId")
        if not source_fid:
            continue
        conn.execute(
            "INSERT INTO source (fid) VALUES (?) ON CONFLICT(fid) DO NOTHING",
            (source_fid,),
        )
        change_message = source_link.get("attribution", {}).get("changeMessage")
        conn.execute(
            """
            INSERT INTO source_link (source_fid, individual_fid, family_id, change_message)
            VALUES (?, ?, NULL, ?)
            """,
            (source_fid, individual_fid, change_message),
        )


def _apply_person_notes_response(
    conn: sqlite3.Connection,
    run_id: int,
    subject_fid: str | None,
    payload: dict[str, Any],
) -> None:
    """Insert note rows from one 'person_notes' kind response payload."""
    individual_fid = subject_fid
    if not individual_fid:
        persons = payload.get("persons", [])
        if persons:
            individual_fid = persons[0].get("id")
    if not individual_fid:
        return
    _ensure_individual(conn, individual_fid, run_id)
    notes = ((payload.get("persons") or [{}])[0]).get("notes", [])
    for note in notes:
        conn.execute(
            """
            INSERT INTO note (individual_fid, family_id, subject, text)
            VALUES (?, NULL, ?, ?)
            """,
            (individual_fid, note.get("subject"), note.get("text")),
        )


def _apply_couple_notes_response(
    conn: sqlite3.Connection,
    run_id: int,
    subject_fid: str | None,
    payload: dict[str, Any],
    get_or_create_family: Callable[[str | None, str | None, int, str | None], int],
) -> None:
    """Insert note rows from one 'couple_notes' kind response payload."""
    family_id = get_or_create_family(None, None, run_id, subject_fid)
    notes = payload.get("notes", [])
    for note in notes:
        conn.execute(
            """
            INSERT INTO note (individual_fid, family_id, subject, text)
            VALUES (NULL, ?, ?, ?)
            """,
            (family_id, note.get("subject"), note.get("text")),
        )


def _apply_memory_response(
    conn: sqlite3.Connection,
    run_id: int,
    subject_fid: str | None,
    payload: dict[str, Any],
) -> None:
    """Insert memory rows from one 'memory' kind response payload."""
    individual_fid = subject_fid
    if not individual_fid:
        return
    _ensure_individual(conn, individual_fid, run_id)

    for source_description in payload.get("sourceDescriptions", []):
        title = ((source_description.get("titles") or [{}])[0]).get("value")
        description = ((source_description.get("descriptions") or [{}])[0]).get("value")
        if title and description:
            combined_description = f"{title}\n{description}"
        else:
            combined_description = title or description
        conn.execute(
            """
            INSERT INTO memory (
                individual_fid,
                memory_fid,
                url,
                description,
                media_type
            ) VALUES (?, ?, ?, ?, ?)
            """,
            (
                individual_fid,
                source_description.get("id"),
                source_description.get("about"),
                combined_description,
                source_description.get("mediaType"),
            ),
        )


_RELATIONAL_ROW_COUNT_TABLES = (
    "individual",
    "name",
    "family",
    "family_child",
    "event",
    "source",
    "source_link",
    "note",
    "memory",
)


def _print_row_counts(conn: sqlite3.Connection) -> None:
    """Print a row count for each relational table (post-load summary)."""
    for table in _RELATIONAL_ROW_COUNT_TABLES:
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        print(f"{table}: {count}")


def _load_single_run(conn: sqlite3.Connection, run_id: int | None = None) -> int:
    """Load relational tables from raw responses for one completed fetch run."""
    if run_id is None:
        run_id = latest_finished_run(conn)
        if run_id is None:
            raise ValueError("No finished fetch runs available to load.")

    run_row = conn.execute(
        "SELECT run_id FROM fetch_run WHERE run_id = ? AND finished_at IS NOT NULL",
        (run_id,),
    ).fetchone()
    if run_row is None:
        raise ValueError(f"Run {run_id} is missing or not finished.")

    clear_relational(conn)
    get_or_create_family = _make_family_resolver(conn)

    def rows_for_kind(kind: str) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT id, subject_fid, body
            FROM api_response
            WHERE run_id = ? AND kind = ? AND ok = 1
            ORDER BY id
            """,
            (run_id, kind),
        ).fetchall()

    with conn:
        person_batch_payloads: list[dict[str, Any]] = []
        for row in rows_for_kind("persons_batch"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            person_batch_payloads.append(payload)
            _apply_person_batch_payload(conn, run_id, payload)

        # Relationships are applied only after every person in this run has been
        # loaded, so parent-gender lookups (see _assign_parents_by_gender) can see
        # a parent's sex even when that parent was fetched in a later batch/row
        # than the relationship referencing them.
        for payload in person_batch_payloads:
            _apply_child_and_parents_relationships(conn, run_id, payload, get_or_create_family)
            _apply_couple_relationships(run_id, payload, get_or_create_family)

        for row in rows_for_kind("couple"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_couple_response(conn, run_id, row["subject_fid"], payload, get_or_create_family)

        for row in rows_for_kind("person_sources"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_person_sources_response(conn, run_id, row["subject_fid"], payload)

        for row in rows_for_kind("person_notes"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_person_notes_response(conn, run_id, row["subject_fid"], payload)

        for row in rows_for_kind("couple_notes"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_couple_notes_response(conn, run_id, row["subject_fid"], payload, get_or_create_family)

        for row in rows_for_kind("memory"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_memory_response(conn, run_id, row["subject_fid"], payload)

    _print_row_counts(conn)
    return int(run_id)


def _load_merged(conn: sqlite3.Connection) -> int:
    """Load relational tables from latest-per-subject raw responses across finished runs."""
    latest_run_id = latest_finished_run(conn)
    if latest_run_id is None:
        raise ValueError("No finished fetch runs available to load.")

    sync_batch_indexes(conn)
    clear_relational(conn)

    get_or_create_family = _make_family_resolver(conn)
    payload_cache: dict[int, dict[str, Any] | None] = {}
    response_run_id_cache: dict[int, int] = {}

    def get_response_run_id(response_id: int) -> int:
        if response_id in response_run_id_cache:
            return response_run_id_cache[response_id]
        row = conn.execute(
            "SELECT run_id FROM api_response WHERE id = ?",
            (response_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Response {response_id} not found.")
        resolved_run_id = int(row["run_id"])
        response_run_id_cache[response_id] = resolved_run_id
        return resolved_run_id

    def get_cached_payload(response_id: int) -> dict[str, Any] | None:
        if response_id in payload_cache:
            return payload_cache[response_id]
        row = conn.execute(
            "SELECT body FROM api_response WHERE id = ?",
            (response_id,),
        ).fetchone()
        if row is None:
            payload_cache[response_id] = None
            return None
        payload = parse_json_body(row["body"])
        payload_cache[response_id] = payload
        return payload

    person_winner_rows = conn.execute(
        """
        SELECT pbm.fid, MAX(ar.id) AS winning_id
        FROM person_batch_member pbm
        JOIN api_response ar ON ar.id = pbm.response_id
        JOIN fetch_run fr ON fr.run_id = ar.run_id
        WHERE ar.kind = 'persons_batch' AND ar.ok = 1 AND fr.finished_at IS NOT NULL
              AND pbm.fid != ''
        GROUP BY pbm.fid
        """
    ).fetchall()
    people_winners_by_response: dict[int, set[str]] = {}
    for row in person_winner_rows:
        winning_id = int(row["winning_id"])
        people_winners_by_response.setdefault(winning_id, set()).add(str(row["fid"]))

    relationship_winner_rows = conn.execute(
        """
        SELECT brm.rel_fid, MAX(ar.id) AS winning_id
        FROM batch_relationship_member brm
        JOIN api_response ar ON ar.id = brm.response_id
        JOIN fetch_run fr ON fr.run_id = ar.run_id
        WHERE ar.kind = 'persons_batch' AND ar.ok = 1 AND fr.finished_at IS NOT NULL
        GROUP BY brm.rel_fid
        """
    ).fetchall()
    relationship_winners_by_response: dict[int, set[str]] = {}
    for row in relationship_winner_rows:
        winning_id = int(row["winning_id"])
        relationship_winners_by_response.setdefault(winning_id, set()).add(str(row["rel_fid"]))

    def winning_rows_by_subject_kind(kind: str) -> list[sqlite3.Row]:
        return conn.execute(
            """
            SELECT ar.run_id, ar.subject_fid, ar.body
            FROM api_response ar
            JOIN (
                SELECT subject_fid, MAX(id) AS winning_id
                FROM api_response ar2
                JOIN fetch_run fr ON fr.run_id = ar2.run_id
                WHERE ar2.kind = ? AND ar2.ok = 1 AND fr.finished_at IS NOT NULL
                GROUP BY subject_fid
            ) w ON w.winning_id = ar.id
            ORDER BY ar.id
            """,
            (kind,),
        ).fetchall()

    memory_winner_rows = conn.execute(
        """
        SELECT ar.run_id, ar.subject_fid, ar.body
        FROM api_response ar
        JOIN (
            SELECT subject_fid, url, MAX(id) AS winning_id
            FROM api_response ar2
            JOIN fetch_run fr ON fr.run_id = ar2.run_id
            WHERE ar2.kind = 'memory' AND ar2.ok = 1 AND fr.finished_at IS NOT NULL
            GROUP BY subject_fid, url
        ) w ON w.winning_id = ar.id
        ORDER BY ar.id
        """
    ).fetchall()

    with conn:
        for response_id, winner_fids in people_winners_by_response.items():
            payload = get_cached_payload(response_id)
            if payload is None:
                continue
            response_run_id = get_response_run_id(response_id)
            _apply_person_batch_payload(conn, response_run_id, payload, winner_fids)

        for response_id, winner_rel_fids in relationship_winners_by_response.items():
            payload = get_cached_payload(response_id)
            if payload is None:
                continue
            response_run_id = get_response_run_id(response_id)
            _apply_child_and_parents_relationships(
                conn, response_run_id, payload, get_or_create_family, winner_rel_fids
            )
            _apply_couple_relationships(response_run_id, payload, get_or_create_family, winner_rel_fids)

        for row in winning_rows_by_subject_kind("couple"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_couple_response(conn, int(row["run_id"]), row["subject_fid"], payload, get_or_create_family)

        for row in winning_rows_by_subject_kind("person_sources"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_person_sources_response(conn, int(row["run_id"]), row["subject_fid"], payload)

        for row in winning_rows_by_subject_kind("person_notes"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_person_notes_response(conn, int(row["run_id"]), row["subject_fid"], payload)

        for row in winning_rows_by_subject_kind("couple_notes"):
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_couple_notes_response(conn, int(row["run_id"]), row["subject_fid"], payload, get_or_create_family)

        for row in memory_winner_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            _apply_memory_response(conn, int(row["run_id"]), row["subject_fid"], payload)

    _print_row_counts(conn)
    return int(latest_run_id)


def load(conn: sqlite3.Connection, run_id: int | None = None) -> int:
    """Load relational tables from raw responses."""
    if run_id is None:
        return _load_merged(conn)
    return _load_single_run(conn, run_id)
