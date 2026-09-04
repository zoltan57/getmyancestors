# FamilySearch API limits: what's documented, what's arbitrary, and what's unknown

**Written:** 2026-09-04, against commit `0ddba86` (post authenticated-fetch fixes).

**Purpose:** three numbers/assumptions baked into this codebase were inherited from
the original project with no visible justification: the request rate limit
(`FS_RATE_LIMIT`, default 2/sec), the person-batch size (`MAX_PERSONS = 200`), and the
implicit assumption that this tool's 6-7 `kind` values are close to "the" FamilySearch
API surface. This document records what live research against FamilySearch's current
official developer documentation (`developers.familysearch.org`) actually confirmed
for each, so the distinction between "documented" and "someone's guess" doesn't have
to be re-derived later. Two of these were investigated directly (fetched and read the
primary source); one used a research subagent whose findings were spot-checked.

## 1. Rate limiting — arbitrary number, real (but different) documented mechanism

**The numbers in this codebase's history are arbitrary.** Traced through git
history: the very first rate limit (`5` requests/second, `f6bb22e` "adding rate
limit") shipped with no justification beyond a bare code comment. The current
default (`2` requests/second, this fork's own `d75e1bc` "harden Session" phase) was
likewise a design choice recorded in a planning doc, not cited to any FamilySearch
source. Neither `5` nor `2` was ever validated against FamilySearch's own published
limits.

**What FamilySearch actually documents** (`https://developers.familysearch.org/main/docs/throttling`,
page dated `updatedAt: 2026-07-14`, fetched and read directly):

- **No numeric requests-per-second (or per-minute, per-day) quota is published
  anywhere.** The one number given — *"requests could be given 18 seconds of
  execution time within a 1 minute window"* — is explicitly framed as an
  illustrative example ("could be"), not a guaranteed value, and the docs state
  different endpoints have different throttling windows.
- Throttling is **per-user, not per-connection/per-app**: *"even if a user has two
  different active sessions with two distinct products, their requests are all
  still throttled together."* This means the budget is shared across everything a
  user does with the API concurrently — parallelizing requests (more threads, or
  asyncio) does not increase the effective quota, it only burns through the same
  shared budget faster.
- **The sanctioned mechanism is reactive, not proactive**: a throttled request gets
  HTTP **429** plus a **`Retry-After`** header (seconds to wait) — FamilySearch's
  own guidance is to honor that header, not to self-impose a fixed requests/second
  ceiling and hope it's conservative enough.
- Every response also carries an `X-PROCESSING-TIME` header (ms) for client-side
  self-calibration, and there's a dedicated test endpoint
  (`GET /platform/throttled?processingTime=60001`) for exercising 429-handling code
  deliberately.

**Implication for asyncio:** this also answers a side question raised during the
same investigation — whether the project's earlier decision to use
`ThreadPoolExecutor` instead of asyncio (`docs/decisions/2026-09-04-technical-stack.md`)
was really just "a previous developer wasn't familiar with asyncio." It wasn't: since
the throttle is a shared per-user time budget independent of concurrency primitive,
asyncio would hit the same 429 wall at the same aggregate request rate as threads —
it has no rate-limiting advantage here. No asyncio-based FamilySearch client was
found anywhere on GitHub during this research; even FamilySearch's own official
reference sample app (`byu-osl/familytree-sample-app`) uses plain `threading` with
purely reactive backoff, not proactive rate limiting.

**What changed as a result (commit `0ddba86` and follow-up):**
`Session.get_url()` now special-cases HTTP 429: it parses `Retry-After` (accepting
both the integer-seconds and HTTP-date forms per RFC 9110 §10.2.3, via
`_parse_retry_after()`) and sleeps exactly that long — capped at
`MAX_RETRY_AFTER_SECONDS = 300` as a sanity bound against a malformed/absurd header
value — falling back to the existing exponential backoff only when the header is
missing or unparseable. `FS_RATE_LIMIT`'s `LimiterAdapter` (proactive throttle) is
still in place as a conservative, self-imposed default on top of this — not because
`2` is a documented safe number, but because proactively slowing down reduces how
often 429 is hit at all, and reactive `Retry-After` handling now correctly covers
the cases where it's hit anyway.

**Confidence:** High — primary source, fetched and read directly, current
(2026-07-14).

## 2. `MAX_PERSONS = 200` — a real, current, officially documented limit

Unlike the rate limit, **this one checks out.** Directly confirmed by fetching
`https://developers.familysearch.org/main/reference/readpersons` (the current,
live OpenAPI 3.0.1 reference for `GET /tree/persons`, page dated
`updatedAt: 2026-05-19`):

> "Read a list of persons. Invalid ids will be returned in Warning headers. **The
> maximum number of persons that can be read is 200.**"
>
> `pids` parameter: "A comma separated list of **no more than 200** person ids."

This directly supersedes the dead legacy URL
(`www.familysearch.org/developers/docs/api/tree/Persons_resource`, now 403) that
the original code comment pointed to — the content moved to
`developers.familysearch.org/main/reference/readpersons` as part of the same
site migration that affected the throttling docs (§1).

A research subagent additionally found the same `200` value independently baked
into an unrelated third-party project (`rappdw/fs-crawler`), with the same
now-dead source URL in its comment — consistent with either shared lineage or
independent empirical convergence on FamilySearch's real documented number. No
distinct/uniquely-worded error is documented for exceeding 200 (only the generic
`400 Bad Request` response is listed) — exceeding it likely produces a plain 400,
but that specific behavior is inferred, not verified.

**What this means for configurability:** `MAX_PERSONS` is a real API ceiling, not
a tuning knob — making it configurable would only let it be *lowered* (useful for
debugging/throttling further), not raised, since raising it would just start
failing against FamilySearch's own cap. Worth documenting this distinction
wherever `MAX_PERSONS` becomes user-configurable, so it isn't mistaken for a
performance setting the way `FS_RATE_LIMIT` might be.

**Confidence:** High — primary source (the live OpenAPI reference itself),
verified independently by direct fetch, current (2026-05-19).

## 3. Query "kind" population — this tool covers a known subset, not the full API

This tool's `fetch.py` implements exactly 6 request kinds (plus `current_user`):
`persons_batch`, `couple`, `couple_notes`, `person_sources`, `person_notes`,
`memory`. That is **not** close to FamilySearch's full API surface. Two pieces of
evidence, both from this same investigation:

- The *original* pre-fork version of this project (still in this repo's own git
  history, before `7067ebd` "Phase 1: prune GUI, GEDCOM, merge") called at least
  four more endpoint types this fork dropped: person change history
  (`/platform/tree/persons/{fid}/changes`), couple-relationship sources
  (`/platform/tree/couple-relationships/{id}/sources`), couple-relationship change
  history (`/platform/tree/couple-relationships/{id}/changes`), and LDS ordinances
  (a different API host/path entirely: `/service/tree/tree-data/reservations/...`).
- FamilySearch's own current documentation index
  (`https://developers.familysearch.org/main/llms.txt`) lists on the order of
  **250+ distinct documented endpoints** across the whole Platform API — person
  and couple-relationship change history, discussions, controlled vocabularies,
  date/place authorities, historical records, memories (uploads as well as reads),
  and an entirely separate "Genealogies"/user-tree API family (create/read/update/
  delete person, relationship, source description, and tree, plus match-finding)
  that this tool doesn't touch at all.

**Deliberately left open-ended, not enumerated exhaustively here:** this document
doesn't attempt to catalog the full API surface — that would go stale immediately
and isn't the point. The takeaway is narrower: **the 6/7 kinds this tool knows
about are a deliberately small, chosen subset of a much larger, well-documented
API**, so there's plenty of officially-supported room to add more kinds later
(e.g. the four the original project had, or others from the index above) without
running into an API limitation — only this tool's own scope decisions. Anyone
wanting to add a new kind should start at
`https://developers.familysearch.org/main/llms.txt` (the current, authoritative,
actively-maintained index of every documented endpoint) rather than guessing.

**Confidence:** High that the API surface is far larger than this tool's subset
(confirmed via the live, current documentation index); the exact total endpoint
count is an approximate sample-based estimate, not an exhaustively verified count,
since exhaustively counting wasn't the goal.
