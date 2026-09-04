"""Family tree traversal and raw FamilySearch JSON capture."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from getmyancestors.db import finish_run, start_run, store_response

logger = logging.getLogger(__name__)


def _chunks(items: Sequence[str], size: int) -> Iterable[list[str]]:
    """Yield list chunks of at most ``size``."""
    for index in range(0, len(items), size):
        yield list(items[index : index + size])


def _redact_argv(argv: list[str]) -> list[str]:
    """Return argv with any password value redacted."""
    redacted: list[str] = []
    skip_next = False
    for index, token in enumerate(argv):
        if skip_next:
            skip_next = False
            continue
        if token == "-p":
            redacted.extend(["-p", "******"])
            skip_next = True
            continue
        if token.startswith("--password="):
            redacted.append("--password=******")
            continue
        if token == "--password":
            redacted.append("--password")
            if index + 1 < len(argv):
                redacted.append("******")
                skip_next = True
            continue
        redacted.append(token)
    return redacted


def _collect_relationships(
    payload: dict[str, Any],
    fetched: set[str],
    child_to_parents: dict[str, set[tuple[str | None, str | None]]],
    parent_to_children: dict[str, set[str]],
    couples: set[tuple[str, str, str]],
    person_payloads: dict[str, dict[str, Any]],
) -> None:
    """Update fetch relationship accumulators from one persons-batch payload."""
    for person in payload.get("persons", []):
        person_id = person.get("id")
        if not person_id:
            continue
        fetched.add(person_id)
        person_payloads[person_id] = person

    for relation in payload.get("childAndParentsRelationships", []):
        child_id = relation.get("child", {}).get("resourceId")
        father_id = relation.get("parent1", {}).get("resourceId")
        mother_id = relation.get("parent2", {}).get("resourceId")
        if not child_id:
            continue
        child_to_parents[child_id].add((father_id, mother_id))
        if father_id:
            parent_to_children[father_id].add(child_id)
        if mother_id:
            parent_to_children[mother_id].add(child_id)

    for relation in payload.get("relationships", []):
        if relation.get("type") != "http://gedcomx.org/Couple":
            continue
        couple_id = relation.get("id")
        person1 = relation.get("person1", {}).get("resourceId")
        person2 = relation.get("person2", {}).get("resourceId")
        if couple_id and person1 and person2:
            couples.add((person1, person2, couple_id))


def run_fetch(conn: Any, session: Any, opts: Any) -> int:
    """Run a fetch session and store all raw request results in SQLite.

    Captures a deliberately small, chosen subset of the FamilySearch Platform
    API: current_user, persons_batch, couple, couple_notes, person_sources,
    person_notes, memory. FamilySearch documents a far larger API surface (see
    docs/decisions/2026-09-04-familysearch-api-limits.md SS3 for what's known and
    how to find more) -- this is a scope choice, not an API limitation.
    """
    argv = list(getattr(opts, "argv", ["getmyancestors", "fetch"]))
    run_id = start_run(conn, _redact_argv(argv))
    requests_total = 0
    requests_failed = 0
    max_persons = int(getattr(opts, "max_persons", 200))
    if max_persons > 200:
        logger.warning(
            f"--max-persons {max_persons} exceeds FamilySearch's documented maximum of 200; using 200 instead."
        )
        max_persons = 200

    fetched: set[str] = set()
    child_to_parents: dict[str, set[tuple[str | None, str | None]]] = defaultdict(set)
    parent_to_children: dict[str, set[str]] = defaultdict(set)
    couples: set[tuple[str, str, str]] = set()
    person_payloads: dict[str, dict[str, Any]] = {}

    def record_response(
        kind: str,
        url: str,
        subject_fid: str | None,
        data: dict[str, Any] | None,
        raw_text: str | None,
        http_status: int | None,
    ) -> None:
        nonlocal requests_total, requests_failed
        # A 204 No Content is a legitimate, successful "this resource has no
        # data" result (e.g. a person with no sources/notes) -- get_url()
        # returns data=None for it by design, since there's no JSON body to
        # parse, but that must not be counted the same as a genuine failure
        # (timeout, 404, 500, etc, which also return data=None).
        ok = 1 if (data is not None or http_status == 204) else 0
        if not ok:
            requests_failed += 1
        requests_total += 1
        store_response(
            conn=conn,
            run_id=run_id,
            kind=kind,
            url=url,
            subject_fid=subject_fid,
            http_status=http_status,
            ok=ok,
            body=raw_text if ok else None,
        )

    def request(
        kind: str,
        url: str,
        subject_fid: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any] | None:
        try:
            data, raw_text, http_status = session.get_url(url, headers=headers)
        except Exception:  # noqa: BLE001 - one bad request must be recorded, not crash the run (plan §6.7)
            data, raw_text, http_status = None, None, None
        record_response(kind, url, subject_fid, data, raw_text, http_status)
        return data

    def fetch_person_batch(frontier: set[str]) -> None:
        if not frontier:
            return
        pending = sorted(frontier - fetched)
        for chunk in _chunks(pending, max_persons):
            pids = ",".join(chunk)
            url = f"/platform/tree/persons?pids={pids}"
            payload = request(
                kind="persons_batch",
                url=url,
                headers={"Accept": "application/x-gedcomx-v1+json"},
            )
            if payload:
                _collect_relationships(
                    payload,
                    fetched=fetched,
                    child_to_parents=child_to_parents,
                    parent_to_children=parent_to_children,
                    couples=couples,
                    person_payloads=person_payloads,
                )

    try:
        current_user = request(kind="current_user", url="/platform/users/current")
        start_fids = list(getattr(opts, "ids", []) or [])
        if not start_fids and current_user:
            users = current_user.get("users", [])
            if users and users[0].get("personId"):
                start_fids = [users[0]["personId"]]
        seeds = set(start_fids)

        fetch_person_batch(seeds)

        ancestor_frontier = set(seeds)
        for _ in range(int(getattr(opts, "ascend", 4))):
            next_frontier: set[str] = set()
            for child_id in ancestor_frontier:
                for father_id, mother_id in child_to_parents.get(child_id, set()):
                    if father_id and father_id not in fetched:
                        next_frontier.add(father_id)
                    if mother_id and mother_id not in fetched:
                        next_frontier.add(mother_id)
            if not next_frontier:
                break
            fetch_person_batch(next_frontier)
            ancestor_frontier = next_frontier

        descendant_frontier = set(seeds)
        for _ in range(int(getattr(opts, "descend", 0))):
            next_frontier: set[str] = set()
            for parent_id in descendant_frontier:
                for child_id in parent_to_children.get(parent_id, set()):
                    if child_id not in fetched:
                        next_frontier.add(child_id)
            if not next_frontier:
                break
            fetch_person_batch(next_frontier)
            descendant_frontier = next_frontier

        if bool(getattr(opts, "marriages", False)):
            spouse_frontier: set[str] = set()
            for person1, person2, _ in couples:
                if person1 in fetched and person2 not in fetched:
                    spouse_frontier.add(person2)
                if person2 in fetched and person1 not in fetched:
                    spouse_frontier.add(person1)
            fetch_person_batch(spouse_frontier)

        if bool(getattr(opts, "marriages", False)):
            for person1, person2, couple_id in sorted(couples):
                if person1 not in fetched or person2 not in fetched:
                    continue
                request("couple", f"/platform/tree/couple-relationships/{couple_id}", couple_id)
                if not bool(getattr(opts, "no_notes", False)):
                    request(
                        "couple_notes",
                        f"/platform/tree/couple-relationships/{couple_id}/notes",
                        couple_id,
                    )

        jobs: list[tuple[str, str, str | None]] = []
        for fid in sorted(fetched):
            if not bool(getattr(opts, "no_sources", False)):
                jobs.append(("person_sources", f"/platform/tree/persons/{fid}/sources", fid))
            if not bool(getattr(opts, "no_notes", False)):
                jobs.append(("person_notes", f"/platform/tree/persons/{fid}/notes", fid))
            if not bool(getattr(opts, "no_memories", False)):
                memory_ids: set[str] = set()
                for evidence in person_payloads.get(fid, {}).get("evidence", []):
                    evidence_id = evidence.get("id")
                    if not evidence_id:
                        continue
                    memory_ids.add(str(evidence_id).rsplit("-", 1)[0])
                for memory_id in sorted(memory_ids):
                    # subject_fid is the *owning person's* FID (consistent with
                    # person_sources/person_notes), not the memory's own numeric
                    # ID -- the memory ID is still recoverable from the URL/body
                    # for anyone who needs it. Using the memory ID here was a
                    # pre-existing bug: load.py's memory-loading step reads
                    # subject_fid expecting the owning individual, which caused
                    # bogus "individual" rows keyed by memory ID instead of a
                    # real FamilySearch person ID.
                    jobs.append(("memory", f"/platform/memories/memories/{memory_id}", fid))

        def worker(
            job: tuple[str, str, str | None],
        ) -> tuple[str, str, str | None, dict[str, Any] | None, str | None, int | None]:
            kind, url, subject = job
            try:
                data, raw_text, http_status = session.get_url(url)
            except Exception:  # noqa: BLE001 - one worker's exception must not crash the whole run (plan §6.7)
                data, raw_text, http_status = None, None, None
            return kind, url, subject, data, raw_text, http_status

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker, job) for job in jobs]
            for future in as_completed(futures):
                kind, url, subject, data, raw_text, http_status = future.result()
                record_response(kind, url, subject, data, raw_text, http_status)

        finish_run(conn, run_id, requests_total, requests_failed)
    except Exception:
        finish_run(conn, run_id, requests_total, requests_failed)
        raise

    print(f"{requests_total} requests, {requests_failed} failed")
    if requests_failed > 0:
        logger.warning("Captured data is incomplete because requests failed.")
        return 3
    return 0
