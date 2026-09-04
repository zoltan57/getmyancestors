"""Load relational tables from captured raw FamilySearch API responses."""

from __future__ import annotations

import sqlite3
from typing import Any

from getmyancestors.db import clear_relational, latest_finished_run, parse_json_body


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


def load(conn: sqlite3.Connection, run_id: int | None = None) -> int:
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

    family_pair_to_id: dict[tuple[str | None, str | None], int] = {}
    couple_to_family_id: dict[str, int] = {}

    def get_or_create_family(
        husband_fid: str | None,
        wife_fid: str | None,
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

    person_batch_rows = conn.execute(
        """
        SELECT id, body
        FROM api_response
        WHERE run_id = ? AND kind = 'persons_batch' AND ok = 1
        ORDER BY id
        """,
        (run_id,),
    ).fetchall()

    with conn:
        for row in person_batch_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue

            places_by_id = {str(place.get("id")): place for place in payload.get("places", []) if place.get("id")}

            for person in payload.get("persons", []):
                _upsert_individual(conn, run_id, person)
                fid = person.get("id")
                if not fid:
                    continue

                for name in person.get("names", []):
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

                for fact in person.get("facts", []):
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

            for relation in payload.get("childAndParentsRelationships", []):
                child_id = relation.get("child", {}).get("resourceId")
                father_id = relation.get("parent1", {}).get("resourceId")
                mother_id = relation.get("parent2", {}).get("resourceId")

                family_id = get_or_create_family(father_id, mother_id)
                if child_id:
                    _ensure_individual(conn, child_id, run_id)
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO family_child (family_id, child_fid, rel_fid)
                        VALUES (?, ?, ?)
                        """,
                        (family_id, child_id, relation.get("id")),
                    )

            for relation in payload.get("relationships", []):
                if relation.get("type") != "http://gedcomx.org/Couple":
                    continue
                couple_id = relation.get("id")
                husband = relation.get("person1", {}).get("resourceId")
                wife = relation.get("person2", {}).get("resourceId")
                if not husband and not wife:
                    continue
                get_or_create_family(husband, wife, couple_id)

        couple_rows = conn.execute(
            """
            SELECT subject_fid, body
            FROM api_response
            WHERE run_id = ? AND kind = 'couple' AND ok = 1
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        for row in couple_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            relationship = (payload.get("relationships") or [{}])[0]
            couple_fid = relationship.get("id") or row["subject_fid"]
            family_id = get_or_create_family(None, None, couple_fid)

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

        person_source_rows = conn.execute(
            """
            SELECT subject_fid, body
            FROM api_response
            WHERE run_id = ? AND kind = 'person_sources' AND ok = 1
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        for row in person_source_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue

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

            individual_fid = row["subject_fid"]
            if not individual_fid:
                persons = payload.get("persons", [])
                if persons:
                    individual_fid = persons[0].get("id")
            if not individual_fid:
                continue
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

        person_note_rows = conn.execute(
            """
            SELECT subject_fid, body
            FROM api_response
            WHERE run_id = ? AND kind = 'person_notes' AND ok = 1
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        for row in person_note_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            individual_fid = row["subject_fid"]
            if not individual_fid:
                persons = payload.get("persons", [])
                if persons:
                    individual_fid = persons[0].get("id")
            if not individual_fid:
                continue
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

        couple_note_rows = conn.execute(
            """
            SELECT subject_fid, body
            FROM api_response
            WHERE run_id = ? AND kind = 'couple_notes' AND ok = 1
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        for row in couple_note_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            couple_fid = row["subject_fid"]
            family_id = get_or_create_family(None, None, couple_fid)
            notes = payload.get("notes", [])
            for note in notes:
                conn.execute(
                    """
                    INSERT INTO note (individual_fid, family_id, subject, text)
                    VALUES (NULL, ?, ?, ?)
                    """,
                    (family_id, note.get("subject"), note.get("text")),
                )

        memory_rows = conn.execute(
            """
            SELECT subject_fid, body
            FROM api_response
            WHERE run_id = ? AND kind = 'memory' AND ok = 1
            ORDER BY id
            """,
            (run_id,),
        ).fetchall()
        for row in memory_rows:
            payload = parse_json_body(row["body"])
            if payload is None:
                continue
            individual_fid = row["subject_fid"]
            if not individual_fid:
                continue
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

    row_count_tables = [
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
    for table in row_count_tables:
        count = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        print(f"{table}: {count}")

    return int(run_id)
