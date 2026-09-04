from __future__ import annotations

from collections import defaultdict
from fnmatch import fnmatch
from types import SimpleNamespace
from typing import Any

from getmyancestors.fetch import _collect_relationships, run_fetch


class FakeSession:
    def __init__(
        self,
        fixtures: dict[str, tuple[dict[str, Any], str, int]],
        fail_urls: set[str] | None = None,
    ) -> None:
        self.fixtures = fixtures
        self.fail_urls = fail_urls or set()
        self.calls: list[tuple[str, dict[str, str] | None]] = []
        self.failed_requests = 0

    def get_url(
        self, url: str, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, Any] | None, str | None, int | None]:
        self.calls.append((url, headers))
        if url in self.fail_urls:
            self.failed_requests += 1
            return None, None, None
        for pattern, (payload, raw, status) in self.fixtures.items():
            if fnmatch(url, pattern):
                return payload, raw, status
        raise AssertionError(f"Unexpected URL requested: {url}")


def _make_opts(**overrides: Any) -> SimpleNamespace:
    base = {
        "ids": ["AAAA-001"],
        "ascend": 1,
        "descend": 0,
        "marriages": True,
        "no_sources": False,
        "no_notes": False,
        "no_memories": False,
        "argv": ["getmyancestors", "fetch", "--db", "tmp.sqlite", "-u", "user"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_collect_relationship_accumulators_match_fixture(load_fixture) -> None:
    payload = load_fixture("persons_batch.json")
    fetched: set[str] = set()
    child_to_parents: dict[str, set[tuple[str | None, str | None]]] = defaultdict(set)
    parent_to_children: dict[str, set[str]] = defaultdict(set)
    couples: set[tuple[str, str, str]] = set()
    person_payloads: dict[str, dict[str, Any]] = {}

    _collect_relationships(
        payload,
        fetched=fetched,
        child_to_parents=child_to_parents,
        parent_to_children=parent_to_children,
        couples=couples,
        person_payloads=person_payloads,
    )

    assert fetched == {"AAAA-001", "BBBB-002", "CCCC-003"}
    assert child_to_parents["CCCC-003"] == {("AAAA-001", "BBBB-002")}
    assert parent_to_children["AAAA-001"] == {"CCCC-003"}
    assert parent_to_children["BBBB-002"] == {"CCCC-003"}
    assert couples == {("AAAA-001", "BBBB-002", "REL-C-1")}


def test_run_fetch_records_raw_bodies_and_expected_url_fanout(initialized_db, load_fixture) -> None:
    current_user = load_fixture("current_user.json")
    persons_batch = load_fixture("persons_batch.json")
    person_sources = load_fixture("person_sources.json")
    person_notes = load_fixture("person_notes.json")
    couple = load_fixture("couple.json")
    memory = load_fixture("memory.json")

    fixtures = {
        "/platform/users/current": (
            current_user,
            '{"users":[{"personId":"AAAA-001","preferredLanguage":"en","displayName":"Test User"}]}',
            200,
        ),
        "/platform/tree/persons?pids=AAAA-001": (
            persons_batch,
            '{"persons":[{"id":"AAAA-001"},{"id":"BBBB-002"},{"id":"CCCC-003"}]}',
            200,
        ),
        "/platform/tree/couple-relationships/REL-C-1": (couple, '{"relationships":[{"id":"REL-C-1"}]}', 200),
        "/platform/tree/couple-relationships/REL-C-1/notes": (
            person_notes,
            '{"persons":[{"id":"AAAA-001","notes":[{"subject":"Emigration","text":"Left Hamburg for New York in 1869."}]}]}',
            200,
        ),
        "/platform/tree/persons/*/sources": (
            person_sources,
            '{"persons":[{"id":"AAAA-001","sources":[{"descriptionId":"SRC-1"},{"descriptionId":"SRC-2"}]}]}',
            200,
        ),
        "/platform/tree/persons/*/notes": (
            person_notes,
            '{"persons":[{"id":"AAAA-001","notes":[{"subject":"Emigration","text":"Left Hamburg for New York in 1869."}]}]}',
            200,
        ),
        "/platform/memories/memories/MEM-9": (memory, '{"sourceDescriptions":[{"id":"MEM-9"}]}', 200),
    }
    session = FakeSession(fixtures)
    opts = _make_opts()

    exit_code = run_fetch(initialized_db, session, opts)

    assert exit_code == 0
    person_batch_calls = [url for url, _ in session.calls if url.startswith("/platform/tree/persons?pids=")]
    assert person_batch_calls == ["/platform/tree/persons?pids=AAAA-001"]

    source_calls = [url for url, _ in session.calls if url.endswith("/sources")]
    notes_calls = [url for url, _ in session.calls if "/platform/tree/persons/" in url and url.endswith("/notes")]
    memory_calls = [url for url, _ in session.calls if url.startswith("/platform/memories/memories/")]
    assert sorted(source_calls) == [
        "/platform/tree/persons/AAAA-001/sources",
        "/platform/tree/persons/BBBB-002/sources",
        "/platform/tree/persons/CCCC-003/sources",
    ]
    assert sorted(notes_calls) == [
        "/platform/tree/persons/AAAA-001/notes",
        "/platform/tree/persons/BBBB-002/notes",
        "/platform/tree/persons/CCCC-003/notes",
    ]
    assert memory_calls == ["/platform/memories/memories/MEM-9"]

    rows = initialized_db.execute("SELECT kind, url, ok, body FROM api_response ORDER BY id").fetchall()
    assert all(row["ok"] == 1 for row in rows)
    expected_raw_by_url = {
        "/platform/users/current": '{"users":[{"personId":"AAAA-001","preferredLanguage":"en","displayName":"Test User"}]}',
        "/platform/tree/persons?pids=AAAA-001": '{"persons":[{"id":"AAAA-001"},{"id":"BBBB-002"},{"id":"CCCC-003"}]}',
        "/platform/tree/couple-relationships/REL-C-1": '{"relationships":[{"id":"REL-C-1"}]}',
        "/platform/tree/couple-relationships/REL-C-1/notes": '{"persons":[{"id":"AAAA-001","notes":[{"subject":"Emigration","text":"Left Hamburg for New York in 1869."}]}]}',
        "/platform/tree/persons/AAAA-001/sources": '{"persons":[{"id":"AAAA-001","sources":[{"descriptionId":"SRC-1"},{"descriptionId":"SRC-2"}]}]}',
        "/platform/tree/persons/BBBB-002/sources": '{"persons":[{"id":"AAAA-001","sources":[{"descriptionId":"SRC-1"},{"descriptionId":"SRC-2"}]}]}',
        "/platform/tree/persons/CCCC-003/sources": '{"persons":[{"id":"AAAA-001","sources":[{"descriptionId":"SRC-1"},{"descriptionId":"SRC-2"}]}]}',
        "/platform/tree/persons/AAAA-001/notes": '{"persons":[{"id":"AAAA-001","notes":[{"subject":"Emigration","text":"Left Hamburg for New York in 1869."}]}]}',
        "/platform/tree/persons/BBBB-002/notes": '{"persons":[{"id":"AAAA-001","notes":[{"subject":"Emigration","text":"Left Hamburg for New York in 1869."}]}]}',
        "/platform/tree/persons/CCCC-003/notes": '{"persons":[{"id":"AAAA-001","notes":[{"subject":"Emigration","text":"Left Hamburg for New York in 1869."}]}]}',
        "/platform/memories/memories/MEM-9": '{"sourceDescriptions":[{"id":"MEM-9"}]}',
    }
    for row in rows:
        assert row["body"] == expected_raw_by_url[row["url"]]


def test_run_fetch_records_failed_response_and_returns_3(initialized_db, load_fixture) -> None:
    current_user = load_fixture("current_user.json")
    persons_batch = load_fixture("persons_batch.json")
    person_sources = load_fixture("person_sources.json")
    person_notes = load_fixture("person_notes.json")
    couple = load_fixture("couple.json")
    memory = load_fixture("memory.json")

    fixtures = {
        "/platform/users/current": (current_user, '{"users":[{"personId":"AAAA-001"}]}', 200),
        "/platform/tree/persons?pids=AAAA-001": (
            persons_batch,
            '{"persons":[{"id":"AAAA-001"},{"id":"BBBB-002"},{"id":"CCCC-003"}]}',
            200,
        ),
        "/platform/tree/couple-relationships/REL-C-1": (couple, '{"relationships":[{"id":"REL-C-1"}]}', 200),
        "/platform/tree/couple-relationships/REL-C-1/notes": (person_notes, '{"persons":[{"id":"AAAA-001"}]}', 200),
        "/platform/tree/persons/*/sources": (person_sources, '{"persons":[{"id":"AAAA-001"}]}', 200),
        "/platform/tree/persons/*/notes": (person_notes, '{"persons":[{"id":"AAAA-001"}]}', 200),
        "/platform/memories/memories/MEM-9": (memory, '{"sourceDescriptions":[{"id":"MEM-9"}]}', 200),
    }
    fail_url = "/platform/tree/persons/BBBB-002/notes"
    session = FakeSession(fixtures, fail_urls={fail_url})
    opts = _make_opts()

    exit_code = run_fetch(initialized_db, session, opts)

    assert exit_code == 3
    run_row = initialized_db.execute("SELECT requests_failed FROM fetch_run ORDER BY run_id DESC LIMIT 1").fetchone()
    assert run_row["requests_failed"] == 1
    failed_row = initialized_db.execute(
        "SELECT ok, http_status, body FROM api_response WHERE url = ? ORDER BY id DESC LIMIT 1",
        (fail_url,),
    ).fetchone()
    assert failed_row["ok"] == 0
    assert failed_row["http_status"] is None
    assert failed_row["body"] is None
