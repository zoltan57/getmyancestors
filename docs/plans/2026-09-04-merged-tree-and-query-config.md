# Project Plan: Merged multi-lineage tree, always-on batch indexing, and configurable fetch scope

**Status:** Approved plan, ready for implementation.
**Written:** 2026-09-04, against commit `85b60e8`.
**Companion documents:** `docs/decisions/2026-09-04-familysearch-api-limits.md` (records
why `MAX_PERSONS = 200` is a real FamilySearch-documented ceiling, not a tunable
performance knob — read before Phase 4), `docs/plans/2026-09-03-json-capture-rework.md`
and `docs/plans/2026-09-04-logging-migration.md` (prior plans this one builds on;
historical context only, nothing in them should be redone).

---

## 0. How to use this plan (instructions for the implementing model)

- This file is **canonical**. Implement **one phase per session**, in order. Do not
  start a phase until the previous one's acceptance checks pass.
- End every phase by running its acceptance checks (`uv sync --locked`, `uv run ruff
  check .`, `uv run ruff format --check .`, `uv run pytest -q`), then committing with
  the phase's commit message. Never commit with failing checks.
- **Never call the live FamilySearch API** from tests. Use the existing
  `initialized_db`/`load_fixture`/`session_factory` fixtures in `tests/conftest.py`.
- Ask nothing; every decision needed is written here. If something is genuinely
  unspecified, choose the simplest option consistent with §1 and note it in the
  commit message.

## 1. Goal, non-goals, and the two design reversals from earlier discussion

**Goal:** today, `load` rebuilds the relational tables from **exactly one** fetch
run, wiping and replacing them every time. Fetching a second, unrelated lineage
(e.g. your father's line, then your wife's father's line) into the same database
silently destroys the first lineage's relational view — only the raw
`api_response` capture survives. This plan makes `load`'s **default** behavior
merge the most recent successful capture **per person, across every finished run**,
so multiple lineages accumulate into one permanent, ever-growing tree instead of
one replacing the other. It also fixes a real batch-size limitation (only
`persons_batch` rows lack a per-person key today) via two small append-only index
tables, and makes fetch's optional-content toggles and batch size configurable via
`.env`, consistent with the project's existing `FS_*` pattern.

**Non-goals:**
- **No pruning/expiry.** A person's last-known data stays in the merged view
  forever once captured, even if a later fetch doesn't touch them again. This
  matches the already-documented "raw capture is permanent" philosophy for
  `api_response` (README's Design rule) and is the simplest, least-surprising
  choice. Explicit removal of a FID from the merged view is a deliberate,
  separate future feature — not built here.
- **`diff.py`'s comparison mechanism does not change.** Two design reversals were
  made during planning discussion (both explained below) — read them before
  touching `diff.py`.
- This plan does not add any of the four FamilySearch endpoints found in the
  original pre-fork project (person/couple change history, couple sources,
  ordinances) or any of the ~250 other documented endpoints
  (`docs/decisions/2026-09-04-familysearch-api-limits.md` §3). It only makes the
  **existing 6/7 kinds'** optional ones toggle-able via `.env`, and documents how
  a future kind would plug into the same pattern.

### 1.1 First reversal: `diff.py` is *not* redefined around the merged view

An earlier discussion suggested `diff` should compare "the merged view as of run
N" vs "run N-1" once `load` merges across runs. **This is wrong and must not be
implemented:** under the never-delete merge policy above, a FID that was ever
successfully captured stays in the merged view forever — nothing proactively
removes it. A merged-view diff's "disappeared" side would therefore be
**structurally always empty**, defeating the one thing `diff` originally existed
for (catching a person FamilySearch merged/removed, so external document mappings
keyed to that FID can be fixed).

**Correct design:** `diff.py`'s comparison mechanism (`_run_people`, appeared/
disappeared/name-change logic) is untouched by this plan — it keeps comparing two
explicit runs' own directly-reachable populations, exactly as today. Only its
**default run-selection** changes (§2.4) — see the second reversal below for why.

### 1.2 Second reversal: `diff`'s default must become same-seed-aware

`diff`'s current default (`_resolve_run_pair`: "the two most recent finished
runs") assumes consecutive runs are usually the same lineage refetched — a
reasonable assumption when this tool only ever tracked one tree, but false once a
database regularly accumulates *different* lineages (father, then father-in-law,
...). Under that pattern, "the two most recent runs" are often unrelated
lineages, and a diff between them is ~100% appeared/disappeared noise, not
signal — `diff` would still *work*, but its default would stop being useful.

**Correct design:** default to the two most recent finished runs that share the
same starting seed (`-i`/`--ids`, already recorded verbatim in `fetch_run.
cli_args` — see `_redact_argv` in `fetch.py`). `--runs OLD NEW` remains available
as an explicit override for comparing any two specific runs regardless of seed.

## 2. Target design

### 2.1 Two new append-only index tables (`db.py`)

`persons_batch` is the one kind whose `api_response` rows have no single-subject
key (`subject_fid` is `NULL` — a batch covers up to 200 people at once, per
`docs/decisions/2026-09-04-familysearch-api-limits.md` §2). Every other kind
already carries a usable per-subject key directly on `api_response.subject_fid`
(verified: `couple`/`couple_notes` → couple ID; `person_sources`/`person_notes` →
person FID; `memory` → owning person's FID, as of commit `85b60e8`). Two new
tables close this gap for data embedded inside `persons_batch` payloads:

```sql
-- One row per (response, person) pair found in a persons_batch payload's
-- "persons" array. Append-only, never cleared -- a pure historical index over
-- api_response, not part of the relational layer that clear_relational() wipes.
CREATE TABLE IF NOT EXISTS person_batch_member (
    response_id INTEGER NOT NULL REFERENCES api_response(id),
    fid         TEXT NOT NULL,
    PRIMARY KEY (response_id, fid)
);
CREATE INDEX IF NOT EXISTS idx_person_batch_member_fid ON person_batch_member(fid);

-- One row per (response, relationship) pair found in a persons_batch payload's
-- "childAndParentsRelationships" or Couple-type "relationships" arrays. Same
-- append-only nature as person_batch_member.
CREATE TABLE IF NOT EXISTS batch_relationship_member (
    response_id INTEGER NOT NULL REFERENCES api_response(id),
    rel_fid     TEXT NOT NULL,
    PRIMARY KEY (response_id, rel_fid)
);
CREATE INDEX IF NOT EXISTS idx_batch_relationship_member_rel_fid
    ON batch_relationship_member(rel_fid);
```

- `rel_fid` is `relation.get("id")` for both a `childAndParentsRelationships`
  entry and a `relationships` entry whose `type` is
  `"http://gedcomx.org/Couple"` — both already have their own GedcomX `id`,
  exactly like `family_child.rel_fid` and `family.couple_fid` already store
  today (see `load.py`'s existing `get_or_create_family`/`family_child` insert
  code — this plan reuses those exact identity keys, it does not invent new
  ones).
- These tables are added to `SCHEMA_SQL` in `db.py` (`CREATE TABLE IF NOT
  EXISTS`, so `init_schema()` picking them up on an existing database is
  automatic — no migration step needed beyond running `init_schema()` again,
  which every `cli.py` command already does on every invocation).

### 2.2 Indexing function: `sync_batch_indexes()` (new, in `db.py`)

```python
def sync_batch_indexes(conn: sqlite3.Connection) -> None:
    """Populate person_batch_member/batch_relationship_member for any
    persons_batch response not yet indexed. Idempotent and incremental --
    safe to call on every `load`, only parses responses it hasn't seen before.
    """
```

- Query candidates: `SELECT id, body FROM api_response WHERE kind =
  'persons_batch' AND ok = 1 AND id NOT IN (SELECT DISTINCT response_id FROM
  person_batch_member)` (a response with zero persons still counts as
  "indexed" once attempted — see the empty-payload edge case below).
- For each candidate: parse the body once (reuse `load.py`'s existing
  `_parse_json` — move it to `db.py` as a shared helper both modules import,
  since it's needed in three places after this plan: `load.py`, `diff.py`
  already has its own copy, and now `db.py` — **do not** leave three duplicate
  copies; consolidate into one `db.py`-level `parse_json_body()` and update
  `load.py`/`diff.py` to import it, deleting their private copies).
- Insert one `person_batch_member` row per `payload["persons"][].id`.
- Insert one `batch_relationship_member` row per `payload
  ["childAndParentsRelationships"][].id` and per `payload["relationships"][]`
  entry whose `type == "http://gedcomx.org/Couple"`, using its `.id`.
- **Edge case — a response with zero persons/relationships (e.g. a payload
  that somehow parsed but contained an empty list) must still be marked
  "indexed"** so it isn't re-parsed on every future `load` call forever. Insert
  a sentinel row `(response_id, fid='')` into `person_batch_member` for such
  responses (excluded from real lookups via `WHERE fid != ''` in every query
  that reads this table — document this exclusion inline at every read site).
  A response whose body fails to parse (`_parse_json` returns `None`) gets the
  same sentinel treatment — never worth reparsing.
- Called as the **first step** of `load()` (§2.3), not exposed as a separate
  CLI command — keeps `load` self-contained and matches "load never touches
  the network, only derives from what's already captured."

### 2.3 `load()`: merged-by-default, single-run-exact when `--run` is given

**Critical design choice: `--run N` keeps its exact current meaning unchanged.**
When `run_id` is given explicitly, `load()` runs exactly today's existing
code path (rename the current function body to `_load_single_run(conn, run_id)`,
unchanged in behavior) — a pure, isolated snapshot of only that one run's own
captured rows, no merging, no bridge-table lookups. This preserves 100% backward
compatibility for anyone who explicitly asks for one run.

**When `run_id` is omitted (the new default),** `load()` calls a new
`_load_merged(conn)` instead:

1. Call `sync_batch_indexes(conn)` (§2.2).
2. `clear_relational(conn)` — unchanged; the relational tables are always fully
   rebuilt, just from the merged winner-set instead of one run's raw rows.
3. **Person data** (individual/name/event-from-facts): resolve the winning
   `api_response.id` per FID —
   ```sql
   SELECT pbm.fid, MAX(ar.id) AS winning_id
   FROM person_batch_member pbm
   JOIN api_response ar ON ar.id = pbm.response_id
   JOIN fetch_run fr ON fr.run_id = ar.run_id
   WHERE ar.kind = 'persons_batch' AND ar.ok = 1 AND fr.finished_at IS NOT NULL
         AND pbm.fid != ''
   GROUP BY pbm.fid
   ```
   Group the winners by `winning_id` (so each distinct response body is parsed
   **once**, even when several FIDs share the same winning response — e.g. a
   3-person batch where all 3 were last touched together), then for each
   response, process **only** the person entries matching FIDs that won on
   that response — not every person in the batch (a person who appears in the
   same raw batch payload but whose own most-recent capture is a *different*,
   later response must not be re-applied from this older one). Existing
   per-person logic (`_upsert_individual`, name/fact-to-event insertion,
   `places_by_id` lookup) is reused unchanged, just invoked per-winning-entry
   instead of per-every-entry-in-a-run's batches.
4. **Relationships** (family/family_child from `childAndParentsRelationships`,
   family from Couple-type `relationships`): identical pattern via
   `batch_relationship_member`, resolving one winning response per `rel_fid`,
   parsing each distinct winning response once, applying only the matching
   relationship entry through the existing `get_or_create_family`/
   `family_child` insert logic.
5. **`couple`/`couple_notes`/`person_sources`/`person_notes`** (already keyed by
   `subject_fid` directly): resolve one winning row per `subject_fid` —
   ```sql
   SELECT ar.* FROM api_response ar
   JOIN (
       SELECT subject_fid, MAX(id) AS winning_id
       FROM api_response ar2
       JOIN fetch_run fr ON fr.run_id = ar2.run_id
       WHERE ar2.kind = ? AND ar2.ok = 1 AND fr.finished_at IS NOT NULL
       GROUP BY subject_fid
   ) w ON w.winning_id = ar.id
   ```
   then apply the existing per-kind load logic to this winner-set instead of a
   single run's rows.
6. **`memory`** — same pattern, but the resolution key is the **composite**
   `(subject_fid, url)`, not `subject_fid` alone: unlike the other per-subject
   kinds, one person legitimately has *many* memory rows (one per distinct
   memory item), and each is its own independently-capturable thing — a
   person's later fetch must not make an *older, different* memory
   "disappear," only let a *repeat* fetch of the *same* memory (same URL, same
   owning person) be superseded by its own later capture. Group by
   `(subject_fid, url)` in the winner query above instead of `subject_fid`
   alone.
7. Row-count summary print is unchanged.

Add a small in-function cache (plain `dict[int, dict | None]`) mapping
`response_id -> parsed payload` shared across steps 3-4, since the same
`persons_batch` response can win for both person-level and relationship-level
data simultaneously — parse each distinct response body at most once per
`load()` call.

### 2.4 `diff.py`: same-seed-aware default run selection only

Per §1.2, only `_resolve_run_pair()` changes:

```python
def _resolve_run_pair(conn: sqlite3.Connection) -> tuple[int, int]:
    """Return the two most recent finished runs that share a starting seed,
    falling back to the two most recent finished runs overall if no such
    pair exists (e.g. only one lineage has ever been fetched)."""
```

- A run's "seed" is derived from its stored `cli_args` (JSON list, e.g.
  `["getmyancestors", "fetch", "--db", ..., "-i", "GM4H-3RC"]`) — extract the
  values following `-i`/`--ids` up to the next flag (reuse `argparse`'s own
  parsing is overkill here; a simple manual scan of the token list is
  sufficient since `cli_args` is this tool's own recorded invocation, not
  untrusted input). Normalize to a sorted tuple for comparison (order of `-i`
  values shouldn't matter for "same seed").
- A run with **no** `-i` (i.e. it started from the logged-in user's own
  person ID) is its own seed group, keyed on the literal sentinel `("__self__",
  )` — two such runs are still "the same seed" (both started from "whoever was
  logged in"), which is the common single-lineage-refetch case this tool was
  originally built around and must keep working with **zero** behavior change
  for a user who never passes `-i` at all.
- Walk finished runs newest-first, group by seed, return the newest two within
  whichever seed-group has at least two runs and is itself the most recently
  active (i.e. prefer the seed-group containing the single newest run, and
  look for its most recent same-seed predecessor — not an arbitrary older
  seed-group that happens to have more historical runs).
- If no seed-group has 2+ runs (e.g. every lineage has only been fetched
  once), fall back to today's exact behavior (two most recent runs overall)
  and let the existing `ValueError("Need at least two finished runs for
  diff.")` fire under the same conditions as today.
- `--runs OLD NEW` (explicit) bypasses all of this entirely, unchanged.

### 2.5 Configurable fetch scope: `MAX_PERSONS` and the optional-content toggles

- **`MAX_PERSONS`** moves from a `fetch.py` module constant to a `--max-persons`
  CLI flag on the `fetch` subparser, `_env_int("FS_MAX_PERSONS", 200)` default,
  matching the existing `FS_ASCEND`/`FS_TIMEOUT` pattern. Per
  `docs/decisions/2026-09-04-familysearch-api-limits.md` §2, `200` is a real
  FamilySearch-documented ceiling, not a tunable performance value — **auto-cap
  any value above 200 back down to 200**, logging `logger.warning(f"--max-
  persons {value} exceeds FamilySearch's documented maximum of 200; using 200
  instead.")` rather than either silently allowing a value guaranteed to start
  failing, or hard-erroring on it. Values below 200 are honored as given (a
  legitimate way to fetch smaller batches, e.g. for debugging).
- **`--no-sources`/`--no-notes`/`--no-memories`** gain `.env` backing —
  `FS_NO_SOURCES`/`FS_NO_NOTES`/`FS_NO_MEMORIES` — via a new small
  `_env_bool(name: str) -> bool` helper in `cli.py` (treat unset, `""`, `"0"`,
  `"false"` case-insensitive as `False`; anything else truthy as `True`,
  mirroring `_env_str`'s "empty string means unset" convention). A CLI flag
  still always overrides its environment variable, unchanged from every other
  `FS_*` variable. `argparse`'s `action="store_true"` can't take a variable
  `default` directly from a bool the usual way it does for
  `type=int`/`_env_int` — use `default=_env_bool("FS_NO_SOURCES")` (etc.)
  exactly like the existing int/str defaults; `store_true` still works fine
  with a non-`False` starting default, it simply means "already true unless
  the flag flips it" is not what `store_true` does — **use
  `action=argparse.BooleanOptionalAction`** instead (stdlib, Python 3.9+, already
  satisfies this project's `>=3.12` floor) so both `--no-sources`/`--no-no-
  sources`-style negation and an environment-derived default compose
  correctly. Update the flag's `help` text to mention the new environment
  variable, matching every other flag's phrasing.
- **Not in scope:** building out the four additional kinds found in the
  original pre-fork project, or any other endpoint from
  `docs/decisions/2026-09-04-familysearch-api-limits.md` §3. This plan only
  makes the *existing* six kinds' already-optional ones environment-
  configurable. A future kind should follow the same shape: a `request(...)`
  call site in `fetch.py`, a `kind` string, a `subject_fid` that's either a
  natural per-subject key or (if embedded in a batch, like `persons_batch`) a
  new bridge/index table following §2.1/§2.2's pattern.

## 3. Testing guidance

- **`tests/test_db.py`:** add tests for `sync_batch_indexes()` — indexes a
  fresh `persons_batch` response correctly (persons + both relationship
  types), is idempotent (second call makes no changes / doesn't reprocess),
  and correctly sentinel-marks an empty-persons or unparseable-body response
  so it's never reprocessed. Add a test for the consolidated
  `parse_json_body()` helper (valid dict, invalid JSON, non-dict JSON, empty
  string — mirroring the existing per-module `_parse_json` tests being
  removed/consolidated).
- **`tests/test_load.py`:** the existing single-run test
  (`test_load_builds_relational_tables_from_raw_fixtures`) should switch to
  calling `load(conn, run_id=<that run's id>)` explicitly, asserting it still
  exercises the **unchanged** `_load_single_run` path (§2.3) — do not delete
  this test; it's now the single-run-exact regression test. Add new tests for
  the merged default (`load(conn, run_id=None)` or bare `load(conn)`):
  - Two finished runs, each fetching a different, non-overlapping seed
    (simulating father's-line then father-in-law's-line) — after merged
    `load()`, both lineages' people/names/events appear simultaneously in the
    relational tables.
  - A person fetched in an earlier run, then re-fetched with different data
    (e.g. an updated name) in a later run — merged `load()` reflects only the
    **later** data for that person, proving "latest wins," not "both
    accumulate."
  - A person fetched only in an earlier run, untouched by any later run —
    still appears in the merged relational view with their original data,
    proving the never-delete/never-expire policy (§1).
  - A relationship (parent-child link) captured in an early run's batch,
    where the same child later appears in a *different* later batch that
    doesn't re-include that relationship's parent link — the relationship
    persists (via `batch_relationship_member`'s own independent winner
    resolution, not tied to whichever batch response "won" for the child
    person individually).
  - A memory item captured for a person, plus a second, different memory item
    for the same person captured in a different run — merged `load()` shows
    **both** memory rows for that person (composite-key resolution, §2.3
    step 6), not just the most recent one.
- **`tests/test_diff.py`:** add tests for the new same-seed default pairing:
  three finished runs — two sharing seed `["AAAA-001"]`, one with a different
  seed `["BBBB-002"]` in between them chronologically — default `diff()` (no
  `--runs`) picks the two same-seed runs, skipping the differently-seeded one
  in between. Add a test for the "no `-i` at all" sentinel grouping (two runs
  that both omitted `-i`, i.e. both used the logged-in user's own ID, are
  treated as the same seed group) — this is the scenario that must keep
  working with zero behavior change for existing single-lineage users. Add a
  test for the fallback (no seed-group has 2+ runs) reproducing today's exact
  "two most recent overall" behavior.
- **`tests/test_cli.py`:** add tests for `--max-persons` capping (value above
  200 logs a warning and is capped; value below 200 is honored as-is) and for
  `FS_NO_SOURCES`/`FS_NO_NOTES`/`FS_NO_MEMORIES` environment-variable
  defaults being overridden by their respective CLI flags.

## 4. Phases

Every phase ends with: `uv sync --locked` passes, `uv run ruff check .` and `uv run
ruff format --check .` both pass, `uv run pytest -q` passes, then one commit.

### Phase 1 — Shared JSON parsing + append-only batch index tables

- Consolidate `_parse_json`/`_parse_json` (in `load.py` and `diff.py`) into one
  `parse_json_body()` in `db.py`; update both call sites to import it, delete
  the duplicate private copies.
- Add `person_batch_member`/`batch_relationship_member` to `SCHEMA_SQL`.
- Add `sync_batch_indexes()` per §2.2. Not yet called from anywhere except
  tests — wiring into `load()` happens in Phase 2.
- Tests per §3's `test_db.py` guidance.
- **Acceptance:** all four end-of-phase checks pass; `grep -n "_parse_json"
  getmyancestors/load.py getmyancestors/diff.py` returns nothing (both now
  import `parse_json_body` from `db.py`).
- Commit: `Phase 1: shared JSON parsing + append-only batch index tables`

### Phase 2 — Merged-by-default `load()`

- Rename current `load()` body to `_load_single_run()` (unchanged behavior).
- Implement `_load_merged()` per §2.3, calling `sync_batch_indexes()` first.
- `load(conn, run_id=None)` dispatches to `_load_merged()`; `load(conn,
  run_id=<int>)` dispatches to `_load_single_run()` (exact current behavior,
  used to reject a missing/unfinished run exactly as today).
- Tests per §3's `test_load.py` guidance.
- **Acceptance:** all four end-of-phase checks pass; a manual sanity check
  (fetch two different small seeds into the same scratch `--db`, then run
  bare `load` with no `--run`) shows both lineages' individuals present
  together in the `individual` table afterward.
- Commit: `Phase 2: load() merges latest-per-person across all finished runs by default`

### Phase 3 — Same-seed-aware `diff` default

- Implement the new `_resolve_run_pair()` per §2.4. No other change to
  `diff.py`.
- Tests per §3's `test_diff.py` guidance.
- **Acceptance:** all four end-of-phase checks pass; `git diff --stat
  getmyancestors/diff.py` shows changes isolated to `_resolve_run_pair` (plus
  the Phase 1 `parse_json_body` import swap) — nothing else in `diff.py`
  should differ from before this plan.
- Commit: `Phase 3: diff defaults to same-seed run pairs instead of most-recent-overall`

### Phase 4 — Configurable `MAX_PERSONS` and optional-content toggles

- Move `MAX_PERSONS` to `--max-persons`/`FS_MAX_PERSONS` per §2.5, with the
  200-ceiling auto-cap-and-warn behavior.
- Add `_env_bool()` and wire `FS_NO_SOURCES`/`FS_NO_NOTES`/`FS_NO_MEMORIES`
  into the existing `--no-sources`/`--no-notes`/`--no-memories` flags via
  `argparse.BooleanOptionalAction`.
- Update `.env.example` with the three new optional variables and
  `FS_MAX_PERSONS`, following the existing table format.
- Update README's `fetch` flags table and `.env` section.
- Tests per §3's `test_cli.py` guidance.
- **Acceptance:** all four end-of-phase checks pass; `getmyancestors fetch
  --help` shows `--max-persons` and the negatable
  `--no-sources`/`--no-no-sources`-style flags.
- Commit: `Phase 4: configurable MAX_PERSONS and optional-content toggles via .env`

## 5. Manual verification (after Phase 4, human operator)

Not for the implementing model — the only step here that could involve real
data volume worth eyeballing manually. Fetch two genuinely different small
lineages (e.g. your father's line, then your wife's father's line) into the
same real database, run bare `load`, and confirm both family lines are
simultaneously present and correctly linked in the relational tables. Then
re-fetch one of the two lineages again and confirm `load` picks up the new
data for that lineage without losing the other, untouched one.
