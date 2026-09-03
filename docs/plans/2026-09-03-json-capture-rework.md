# Project Plan: JSON-Capture Rework of getmyancestors

**Status:** Approved plan, ready for implementation.
**Written:** 2026-09-03, against commit `45ba79a`.
**Companion document:** `docs/reviews/2026-09-03-code-review.md` (finding IDs like CRIT-02, MED-01
referenced below are defined there).

---

## 0. How to use this plan (instructions for the implementing model)

- This file is **canonical**. Where it conflicts with the code review report, this file wins —
  the review targeted the old architecture; much of that code is deleted here.
- Implement **one phase per session**, in order. Do not start a phase until the previous phase's
  acceptance checks pass.
- End every phase by running its acceptance checks, then committing with the phase's commit
  message. Never commit with failing checks.
- **Never call the live FamilySearch API** from tests or during development. All tests use the
  fixtures in §8 with mocked HTTP. Only the human operator runs `fetch` for real.
- **Never widen or change the `requests-ratelimiter==0.7.0` pin** (`pyproject.toml` documents why).
- Do not reintroduce GEDCOM parsing/serialization, the GUI, merging, ordinances, or contributors.
  They are deliberately removed (see §2).
- New code targets Python 3.12: use modern type annotations (`str | None`, builtin generics),
  f-strings, `pathlib`, and docstrings on every public function. Keep functions small.
- If FamilySearch JSON shapes in the fixtures conflict with what old code expected, trust the
  fixtures for tests and write the loader defensively (`.get()` with defaults) — real payloads
  vary (review finding MED-06).
- Ask nothing; every decision needed is written here. If something is genuinely unspecified,
  choose the simplest option consistent with §1 and note it in the commit message.

## 1. Goal and non-goals

**Goal:** a command-line tool that (a) logs into FamilySearch, (b) walks a family tree
(ancestors / descendants / spouses) from one or more starting person IDs, (c) stores **every raw
JSON API response verbatim** in a SQLite file, and (d) loads those raw responses into clean
relational tables suitable for export into a document-transcription app. Refresh cadence is a few
times per year; each refresh is a full re-fetch, with a diff report between runs.

**Non-goals:** GEDCOM input or output; GUI; merging; LDS ordinances; contributor lists;
incremental/delta sync; the transcription-app export itself (the relational tables are the
handoff surface).

**Design rule:** the raw `api_response` rows are the source of truth. The relational tables are a
derived, disposable view — `load` must be re-runnable offline against already-captured raw data,
and improving the loader must never require re-fetching.

## 2. Target repository layout

```
getmyancestors/
    __init__.py          # __version__ only; no eager imports
    __main__.py          # delegates to cli.main()
    cli.py               # argparse with subcommands: fetch, load, diff
    session.py           # FamilySearch auth + HTTP (hardened, trimmed)
    fetch.py             # tree traversal + raw JSON capture
    db.py                # SQLite connection, schema DDL, small helpers
    load.py              # raw JSON -> relational tables
    diff.py              # reconciliation report between two runs
tests/
    conftest.py          # fixtures (see §8), tmp-path DB helper
    fixtures/*.json      # canned API responses (see §8)
    test_session.py
    test_db.py
    test_fetch.py
    test_load.py
    test_diff.py
```

**Deleted** (Phase 1): `getmyancestors/getmyancestors.py` (replaced by `cli.py` + `fetch.py`),
`mergemyancestors.py`, `fstogedcom.py`, `fstogedcom.png`, `classes/` (entire package:
`gedcom.py`, `gui.py`, `tree.py`, `translation.py`, `constants.py`, `session.py` moves up one
level), plus the `babelfish` and `diskcache` dependencies, the `mergemyancestors` and
`fstogedcom` console scripts, and the `package-data` PNG entry.

**Preserved logic to consult before deleting** (read, then delete the old file):
- `classes/session.py` — the OAuth login flow (lines 72-161) and `get_url` status-code triage
  (lines 163-224) move to the new `session.py` nearly intact, with Phase 2 fixes.
- `classes/tree.py:650-811` — `add_indis` / `add_parents` / `add_children` / `add_spouses` define
  the traversal semantics `fetch.py` re-implements (what to extract from a persons batch, how
  generations advance). The class hierarchy around them is not kept.
- `classes/constants.py:4` — `MAX_PERSONS = 200` moves to `fetch.py`.

## 3. CLI specification (`cli.py`)

Single console script `getmyancestors` with subcommands. Use `argparse` subparsers; **do not**
copy the `parser.error = parser.exit` hack from the old CLIs (review LOW-10).

```
getmyancestors fetch --db PATH -u USERNAME [-p PASSWORD] [-i FID ...]
                     [-a N] [-d N] [-m] [--no-sources] [--no-notes] [--no-memories]
                     [--rate-limit N] [--timeout SECONDS] [-v]
getmyancestors load  --db PATH [--run ID]        # default: latest finished run
getmyancestors diff  --db PATH [--runs OLD NEW]  # default: two most recent finished runs
```

- `--db` is required everywhere; it is the single SQLite file.
- `fetch`: `-a` ascend generations (default 4), `-d` descend (default 0), `-m` fetch couple
  relationships (marriage facts). Sources/notes/memories are fetched **by default**; the
  `--no-*` flags disable them. `--rate-limit` defaults to **2** requests/second and the
  `LimiterAdapter` is always mounted (fixes review MED-02). `--timeout` (default 60) is the
  per-request timeout only — not the retry backoff. If `-p` is omitted, prompt with `getpass`;
  if `-i` is omitted, start from the logged-in user's own person ID. Validate FIDs with
  `re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{2,4}", fid)` (review LOW-02).
- `-v` prints each request to stderr. Failures are reported **regardless of `-v`** (see §5).
- Exit codes: 0 success; 1 unexpected error; 2 bad arguments / login failure; **3 = fetch
  finished but one or more requests permanently failed** (data captured is incomplete).
  `load` and `diff` never touch the network.

## 4. Database schema (`db.py`)

`db.py` exposes `connect(path) -> sqlite3.Connection` (enables `PRAGMA foreign_keys=ON`, sets
`row_factory=sqlite3.Row`) and `init_schema(conn)` (executes the DDL below with
`CREATE TABLE IF NOT EXISTS`). Timestamps are ISO-8601 UTC strings.

```sql
-- ---- capture layer (append-only; source of truth) ----
CREATE TABLE IF NOT EXISTS fetch_run (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,                     -- NULL while running / if crashed
    cli_args        TEXT NOT NULL,            -- JSON array of argv (password redacted)
    requests_total  INTEGER NOT NULL DEFAULT 0,
    requests_failed INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS api_response (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES fetch_run(run_id),
    kind        TEXT NOT NULL,   -- 'current_user' | 'persons_batch' | 'person_sources'
                                 -- | 'person_notes' | 'memory' | 'couple' | 'couple_notes'
    url         TEXT NOT NULL,   -- path portion, e.g. /platform/tree/persons?pids=...
    subject_fid TEXT,            -- person/couple/memory id the request was about; NULL for batches
    fetched_at  TEXT NOT NULL,
    http_status INTEGER,         -- last status seen; NULL if no response at all
    ok          INTEGER NOT NULL,-- 1 = body holds usable JSON; 0 = permanently failed / empty
    body        TEXT             -- raw response text, verbatim; NULL when ok = 0
);
CREATE INDEX IF NOT EXISTS idx_api_response_run_kind ON api_response(run_id, kind);

-- ---- relational layer (derived; dropped and rebuilt by `load`) ----
CREATE TABLE IF NOT EXISTS individual (
    fid          TEXT PRIMARY KEY,            -- FamilySearch person ID
    sex          TEXT,                        -- 'M' | 'F' | 'U' | NULL
    living       INTEGER,                     -- 0/1/NULL
    display_name TEXT,                        -- preferred name fullText
    run_id       INTEGER NOT NULL REFERENCES fetch_run(run_id)
);

CREATE TABLE IF NOT EXISTS name (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT NOT NULL REFERENCES individual(fid),
    name_type      TEXT NOT NULL,  -- 'preferred'|'birth'|'married'|'aka'|'nickname'|'other'
    given          TEXT,
    surname        TEXT,
    prefix         TEXT,
    suffix         TEXT,
    full_text      TEXT
);

CREATE TABLE IF NOT EXISTS family (
    family_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    couple_fid  TEXT UNIQUE,                  -- FS couple-relationship id (NULL if unknown)
    husband_fid TEXT REFERENCES individual(fid),
    wife_fid    TEXT REFERENCES individual(fid),
    UNIQUE (husband_fid, wife_fid)
);

CREATE TABLE IF NOT EXISTS family_child (
    family_id INTEGER NOT NULL REFERENCES family(family_id),
    child_fid TEXT NOT NULL REFERENCES individual(fid),
    rel_fid   TEXT,                           -- childAndParentsRelationship id
    PRIMARY KEY (family_id, child_fid)
);

CREATE TABLE IF NOT EXISTS event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT REFERENCES individual(fid),   -- person events
    family_id      INTEGER REFERENCES family(family_id), -- couple events (marriage, divorce)
    type_uri       TEXT NOT NULL,             -- verbatim, e.g. http://gedcomx.org/Birth
    value          TEXT,                      -- fact value (occupation text, etc.)
    date_original  TEXT,                      -- freetext as entered, e.g. 'abt 1850'
    date_formal    TEXT,                      -- gedcomx formal date if present, e.g. '+1850-03-12'
    place_original TEXT,
    place_latitude  REAL,
    place_longitude REAL,
    CHECK (individual_fid IS NOT NULL OR family_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS source (
    fid      TEXT PRIMARY KEY,                -- sourceDescription id
    title    TEXT,
    citation TEXT,
    url      TEXT                             -- 'about' link
);

CREATE TABLE IF NOT EXISTS source_link (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    source_fid     TEXT NOT NULL REFERENCES source(fid),
    individual_fid TEXT REFERENCES individual(fid),
    family_id      INTEGER REFERENCES family(family_id),
    change_message TEXT,                      -- attribution note / citation page text
    CHECK (individual_fid IS NOT NULL OR family_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS note (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT REFERENCES individual(fid),
    family_id      INTEGER REFERENCES family(family_id),
    subject        TEXT,
    text           TEXT,
    CHECK (individual_fid IS NOT NULL OR family_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS memory (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    individual_fid TEXT NOT NULL REFERENCES individual(fid),
    memory_fid     TEXT,
    url            TEXT,                      -- artifact URL ('about')
    description    TEXT,
    media_type     TEXT
);
```

Notes: keep the raw gedcomx `type_uri` values — do **not** map them to GEDCOM tags (the old
mapping silently dropped unknown types, `classes/tree.py:144-145`). `load` begins by `DELETE`-ing
all relational tables (children before parents to satisfy FKs) inside one transaction.

## 5. `session.py` — kept behavior and required fixes

Start from `classes/session.py` and apply exactly these changes (review IDs in parentheses):

1. Pass `timeout=self.timeout` on **all four** login-flow requests, currently lines 80, 84-92,
   104, 117-126 (MED-01).
2. Replace every `time.sleep(self.timeout)` retry sleep with exponential backoff:
   `time.sleep(min(2 ** attempt, 60))` (MED-03). `self.timeout` is only ever a request timeout.
3. Remove the ordinances-specific 403 branch and its `"error"` string return (old lines
   200-215); `get_url` returns `dict | None`, nothing else (HIGH-01). Wrap the remaining 403
   message introspection in `try/except (ValueError, KeyError, IndexError)` falling back to the
   generic retry path (MED-06).
4. Remove `no_api` and its `https://familysearch.org` base — the only non-API endpoint was
   ordinances. Single base: `https://api.familysearch.org`.
5. In the missing-OAuth-code branch (old lines 106-113): keep `webbrowser.open` and the printed
   instruction, but `return` instead of `sys.exit(2)` — the caller checks `self.logged`
   (HIGH-04). After the retry loop, if not logged, `write_log` a clear reason.
6. Add `self.failed_requests: int = 0`; increment it in the max-retries-exceeded path and print
   a warning to stderr **unconditionally** (not via verbose-gated `write_log`) (CRIT-02).
7. Remove the unused `delay` parameter and attribute (MED-02); remove `logfile=False` in favor
   of `logfile: TextIO | None = None`; keep `verbose`, `timeout`, `rate_limit` (now with default
   `2` and the adapter always mounted), `client_id`, `redirect_uri`.
8. Drop the `_()` translation method and the `translations` import (translation.py is deleted).
9. Annotate everything; module docstring explains the login flow.

`get_url` keeps its shape: bounded retries (10), 204/404/405/410 → `None`, 500 → backoff+retry,
401 → `self.login()` + retry, JSON parse failure → `None` with warning. New: it should also
return the raw text alongside the parsed dict — recommended signature
`get_url(path, headers=None) -> tuple[dict | None, str | None, int | None]` returning
(parsed, raw_text, http_status) so `fetch.py` can store the verbatim body. Update all callers.

## 6. `fetch.py` — traversal and capture

`run_fetch(conn, session, opts) -> int` (returns the exit code per §3). `MAX_PERSONS = 200`.

Algorithm (reference: old `classes/tree.py:650-811`, then delete it):

1. Insert a `fetch_run` row (redact the password in `cli_args`). All `api_response` rows carry
   this `run_id`.
2. `GET /platform/users/current` → store (`kind='current_user'`); yields the default seed FID
   when `-i` is absent.
3. Maintain `fetched: set[str]`, plus relationship accumulators:
   `child_to_parents: dict[str, set[tuple[father, mother]]]`,
   `parent_to_children`, and `couples: set[tuple[p1, p2, couple_fid]]`.
4. **Batch fetch**: for a frontier of FIDs, chunk by `MAX_PERSONS`, request
   `/platform/tree/persons?pids=<comma-joined>` with header
   `Accept: application/x-gedcomx-v1+json`, store raw (`kind='persons_batch'`), then parse just
   enough to advance: `persons[].id` → mark fetched; `childAndParentsRelationships[]` →
   `parent1.resourceId` / `parent2.resourceId` / `child.resourceId` (any may be absent) into the
   accumulators; `relationships[]` with `type == "http://gedcomx.org/Couple"` →
   (`person1.resourceId`, `person2.resourceId`, `id`) into `couples`.
5. **Ancestors**: repeat batch-fetch on the not-yet-fetched parents of the current frontier,
   `ascend` times. **Descendants**: same with children, `descend` times, then fetch spouses of
   the whole fetched set if `-m`.
6. **Couple details** (`-m`): for each couple id where both partners were fetched,
   `GET /platform/tree/couple-relationships/{id}` (`kind='couple'`) and
   `/platform/tree/couple-relationships/{id}/notes` (`kind='couple_notes'`, unless `--no-notes`).
7. **Per-person extras** for every fetched person: `/platform/tree/persons/{fid}/sources`
   (`kind='person_sources'`, unless `--no-sources`); `/platform/tree/persons/{fid}/notes`
   (`kind='person_notes'`, unless `--no-notes`); and for each `evidence[].id` in the person's
   JSON (memory id = the part before the first `-`), `/platform/memories/memories/{id}`
   (`kind='memory'`, unless `--no-memories`). Run these with
   `concurrent.futures.ThreadPoolExecutor(max_workers=5)` — **no asyncio** (review MED-05); the
   rate limiter throttles globally. Wrap each worker so one exception is recorded as a failed
   response, not a crashed run.
8. A request whose `get_url` returns `(None, ...)` after retries is stored with `ok=0` and
   counted in `requests_failed`. 404-style `None`s (person deleted) also store `ok=0` with their
   status — the loader just skips them.
9. Finalize the `fetch_run` row (`finished_at`, counters). Print a one-line summary:
   `N requests, M failed` — and if `M > 0`, a prominent stderr warning that captured data is
   incomplete; return exit code 3 in that case (CRIT-02).

## 7. `load.py` and `diff.py`

**`load(conn, run_id)`** — pure function of stored rows, no network:

1. Resolve `run_id` (default: latest with `finished_at` non-NULL; error if none).
2. Clear relational tables in one transaction.
3. From `persons_batch` bodies: `individual` (sex from `gender.type` URI suffix Male/Female/
   Unknown → M/F/U), `name` rows for every entry in `names[]` (type from the gedcomx type URI:
   BirthName→birth, MarriedName→married, AlsoKnownAs→aka, Nickname→nickname; `preferred: true`
   → preferred; else other; parts from `nameForms[0].parts[]` by part type URI, `full_text`
   from `nameForms[0].fullText`), and person `event` rows from `facts[]` (`type_uri` verbatim,
   `date.original`, `date.formal`, `place.original`; lat/long by joining
   `place.description` — strip a leading `#` — against the batch's top-level `places[]` id).
   `http://familysearch.org/v1/LifeSketch` facts become `note` rows (subject `Life Sketch`)
   instead of events.
4. Families: one `family` row per distinct (father, mother) pair seen in
   `childAndParentsRelationships` **plus** per couple in `relationships[]` (attach `couple_fid`;
   treat person1 as husband, person2 as wife — matching old behavior). `family_child` rows from
   the child relationships. Parents/children referencing persons that were never fetched
   (`ok=0` or beyond the generation horizon): still create the `individual` row as a **stub**
   (fid only, NULL elsewhere) so FKs hold — stubs are distinguishable by `display_name IS NULL`.
5. From `couple` bodies: family `event` rows from `relationships[0].facts[]`; from the sources
   listed there, `source_link` rows with `family_id`.
6. From `person_sources` bodies: `source` rows (deduped by id) from `sourceDescriptions[]`
   (title/citation/`about` url), `source_link` rows joining `persons[0].sources[]`
   (`descriptionId` + attribution `changeMessage`).
7. From `person_notes` / `couple_notes`: `note` rows (`subject`, `text`).
8. From `memory` bodies: for each `sourceDescriptions[]` entry, `memory` rows (`about` URL,
   title + description text, `mediaType`); the `subject_fid` on the api_response row gives the
   individual. All field access uses `.get()` — a missing optional field must never abort a load.
9. Print row counts per table.

**`diff(conn, old_run, new_run)`** — compares the set of person FIDs present in each run's
`persons_batch` bodies (parse ids only; no relational dependency). Report: appeared, disappeared,
and (if both runs loaded cleanly) display-name changes. Disappeared FIDs get a warning that
FamilySearch may have merged the person and any external document mappings keyed to that FID
need review. Plain text to stdout.

## 8. Test fixtures (canonical API shapes)

Create these under `tests/fixtures/`. They are minimal but structurally faithful; **all loader
and fetch tests run against these, never the live API.** In tests, monkeypatch
`Session.get_url` (or the underlying `requests.Session.get`) to serve them.

`persons_batch.json` — response for `/platform/tree/persons?pids=AAAA-001,BBBB-002,CCCC-003`:

```json
{
  "persons": [
    {
      "id": "AAAA-001",
      "living": false,
      "gender": {"type": "http://gedcomx.org/Male"},
      "names": [
        {
          "preferred": true,
          "type": "http://gedcomx.org/BirthName",
          "nameForms": [{
            "fullText": "Johann Schmidt",
            "parts": [
              {"type": "http://gedcomx.org/Given", "value": "Johann"},
              {"type": "http://gedcomx.org/Surname", "value": "Schmidt"}
            ]
          }],
          "attribution": {"changeMessage": "imported"}
        },
        {
          "preferred": false,
          "type": "http://gedcomx.org/AlsoKnownAs",
          "nameForms": [{"fullText": "John Smith", "parts": [
            {"type": "http://gedcomx.org/Given", "value": "John"},
            {"type": "http://gedcomx.org/Surname", "value": "Smith"}
          ]}]
        }
      ],
      "facts": [
        {
          "type": "http://gedcomx.org/Birth",
          "date": {"original": "12 March 1850", "formal": "+1850-03-12"},
          "place": {"original": "Hamburg, Germany", "description": "#PLACE-1"},
          "attribution": {"changeMessage": "from church record"}
        },
        {"type": "http://gedcomx.org/Occupation", "value": "Carpenter"}
      ],
      "evidence": [{"id": "MEM-9-ABC"}]
    },
    {
      "id": "BBBB-002",
      "living": false,
      "gender": {"type": "http://gedcomx.org/Female"},
      "names": [{"preferred": true, "nameForms": [{"fullText": "Anna Meyer", "parts": [
        {"type": "http://gedcomx.org/Given", "value": "Anna"},
        {"type": "http://gedcomx.org/Surname", "value": "Meyer"}
      ]}]}],
      "facts": [{"type": "http://gedcomx.org/Death", "date": {"original": "1901"}}]
    },
    {
      "id": "CCCC-003",
      "living": true,
      "names": [{"preferred": true, "nameForms": [{"fullText": "Karl Schmidt", "parts": [
        {"type": "http://gedcomx.org/Given", "value": "Karl"},
        {"type": "http://gedcomx.org/Surname", "value": "Schmidt"}
      ]}]}],
      "facts": []
    }
  ],
  "places": [{"id": "PLACE-1", "latitude": 53.55, "longitude": 9.99}],
  "childAndParentsRelationships": [
    {
      "id": "REL-CP-1",
      "parent1": {"resourceId": "AAAA-001"},
      "parent2": {"resourceId": "BBBB-002"},
      "child": {"resourceId": "CCCC-003"}
    }
  ],
  "relationships": [
    {
      "id": "REL-C-1",
      "type": "http://gedcomx.org/Couple",
      "person1": {"resourceId": "AAAA-001"},
      "person2": {"resourceId": "BBBB-002"}
    }
  ]
}
```

Deliberate edge cases embedded above (the loader tests must cover them): a fact with **no**
`attribution` and no date/place (Occupation); a person with **no** `gender` (CCCC-003); a name
entry with **no** `type` key; `place.description` carrying a leading `#`.

`person_sources.json` — for `/platform/tree/persons/AAAA-001/sources`:

```json
{
  "persons": [{"id": "AAAA-001", "sources": [
    {"descriptionId": "SRC-1", "attribution": {"changeMessage": "1850 baptism register, p. 14"}},
    {"descriptionId": "SRC-2"}
  ]}],
  "sourceDescriptions": [
    {"id": "SRC-1", "about": "https://familysearch.org/ark:/61903/1:1:XXXX",
     "titles": [{"value": "Hamburg Baptisms 1850"}],
     "citations": [{"value": "Ev. Kirche Hamburg, Taufen 1850, Nr. 112"}]},
    {"id": "SRC-2", "titles": [{"value": "Family bible"}]}
  ]
}
```

`person_notes.json`:

```json
{"persons": [{"id": "AAAA-001", "notes": [
  {"subject": "Emigration", "text": "Left Hamburg for New York in 1869."},
  {"text": "Note without subject."}
]}]}
```

`couple.json` — for `/platform/tree/couple-relationships/REL-C-1`:

```json
{"relationships": [{
  "id": "REL-C-1",
  "facts": [{"type": "http://gedcomx.org/Marriage",
             "date": {"original": "3 June 1872", "formal": "+1872-06-03"},
             "place": {"original": "Hamburg"}}],
  "sources": [{"descriptionId": "SRC-1",
               "attribution": {"changeMessage": "marriage register"}}]
}]}
```

`memory.json` — for `/platform/memories/memories/MEM-9`:

```json
{"sourceDescriptions": [{
  "id": "MEM-9",
  "about": "https://www.familysearch.org/photos/artifacts/12345",
  "mediaType": "image/jpeg",
  "titles": [{"value": "Portrait of Johann Schmidt"}],
  "descriptions": [{"value": "Taken circa 1880."}]
}]}
```

`current_user.json`:

```json
{"users": [{"personId": "AAAA-001", "preferredLanguage": "en",
            "displayName": "Test User"}]}
```

## 9. Phases

Every phase ends with: `uv sync --locked` passes (run `uv lock` after dependency changes),
`uv run python -m compileall -q getmyancestors` passes, `uv run pytest -q` passes (from Phase 2
on), then one commit.

### Phase 1 — Prune to the keeper core
- Delete: `getmyancestors/mergemyancestors.py`, `fstogedcom.py`, `fstogedcom.png`,
  `classes/gedcom.py`, `classes/gui.py`, `classes/translation.py`, `classes/tree.py`,
  `classes/constants.py`. Move `classes/session.py` → `getmyancestors/session.py` **unchanged**
  except deleting the `translations` import and the `_()` method; delete the `classes/` package.
- Temporarily gut `getmyancestors/getmyancestors.py` down to a stub `main()` that prints
  "rework in progress" and exits 1 (it is replaced in Phase 4); `__init__.py` becomes
  `__version__ = "2.0.0.dev0"` with **no other imports**; `__main__.py` imports lazily inside
  `if __name__ == "__main__":`-guarded code or a function call.
- `pyproject.toml`: remove `babelfish` and `diskcache` from dependencies; remove the
  `mergemyancestors` and `fstogedcom` script entries; remove `[tool.setuptools.package-data]`;
  bump nothing else. Run `uv lock` then `uv sync --locked`.
- `.github/workflows/quality-gate.yml`: the two entry-point import checks become
  `import getmyancestors.session` and `import getmyancestors` (the old module paths are gone).
- **Acceptance:** `uv sync --locked` + compileall + the two imports succeed;
  `grep -ri "gedcom\|tkinter\|babelfish\|diskcache" getmyancestors/` returns only incidental
  comment hits or nothing.
- Commit: `Phase 1: prune GUI, GEDCOM, merge, and their dependencies`

### Phase 2 — Harden session.py; introduce pytest
- Apply §5 items 1-9 to `session.py`.
- `uv add --dev pytest`; create `tests/` with `test_session.py`: monkeypatch
  `requests.Session.get`/`post` (or `Session.get`/`post` on the instance) to assert —
  (a) every login call passes `timeout=`; (b) retry backoff sleeps grow (patch `time.sleep`,
  record args); (c) `get_url` returns `(None, None, status)` on 404 and after exhausted retries
  while `failed_requests` increments; (d) a 403 with a non-JSON body does not raise; (e) 401
  triggers exactly one re-login then a retry. Construct `Session.__new__(Session)` +
  manual attribute setup (or a `login=False` test hook) to avoid running the real login in unit
  tests — choose one approach and document it in `conftest.py`.
- Add `uv run pytest -q` as a step in `quality-gate.yml`.
- Commit: `Phase 2: harden Session (timeouts, backoff, single return shape, failure counter)`

### Phase 3 — Database layer
- Implement `db.py` per §4. Helpers: `start_run(conn, argv) -> int`,
  `finish_run(conn, run_id, total, failed)`, `store_response(conn, run_id, kind, url,
  subject_fid, http_status, ok, body)`, `latest_finished_run(conn) -> int | None`,
  `clear_relational(conn)`.
- `test_db.py`: schema creates on a tmp-path DB; FKs enforced (inserting a `family_child` with an
  unknown fid raises `IntegrityError`); `event` CHECK rejects a row with neither individual nor
  family; run lifecycle helpers round-trip.
- Commit: `Phase 3: SQLite capture + relational schema`

### Phase 4 — Fetcher and CLI
- Implement `fetch.py` per §6 and `cli.py` per §3; delete the Phase-1 stub
  `getmyancestors/getmyancestors.py` and point the `getmyancestors` console script at
  `getmyancestors.cli:main` in `pyproject.toml` (then `uv lock`).
- `test_fetch.py`: fake Session whose `get_url` serves §8 fixtures by URL pattern and records
  calls. Assert: seed AAAA-001 with `-a 1` requests the persons batch once and marks all three
  fids fetched; parents/children/couple accumulators match the fixture; extras fan out to
  sources/notes/memory URLs exactly once per person; a fixture-forced failure (`get_url` returns
  `(None, None, None)`) produces an `api_response` row with `ok=0`, increments
  `requests_failed`, and `run_fetch` returns 3; all raw bodies land verbatim in `api_response`.
- Update `quality-gate.yml` import checks to `import getmyancestors.cli` and
  `import getmyancestors.fetch`.
- Commit: `Phase 4: JSON-capture fetcher and CLI`

### Phase 5 — Loader
- Implement `load.py` per §7 and wire the `load` subcommand.
- `test_load.py`: insert §8 fixtures as `api_response` rows for a synthetic run, run
  `load`, assert — 3 `individual` rows with correct sex/living/display_name; 3 `name` rows for
  AAAA-001+BBBB-002+CCCC-003 with the aka variant present; 1 `family` with `couple_fid='REL-C-1'`
  linking AAAA-001/BBBB-002; 1 `family_child` for CCCC-003; person events include Birth with
  `date_formal='+1850-03-12'` and lat/long from PLACE-1, plus the valueless-date Occupation;
  the family Marriage event exists with `family_id` set and `individual_fid` NULL; 2 `source`
  rows and 3 `source_link` rows (two person, one family); 3 `note` rows (two person notes +
  none from LifeSketch in this fixture); 1 `memory` row with the artifact URL. Then run `load`
  a second time and assert identical counts (idempotence). Also: a `persons_batch` body with a
  person referenced as parent but never fetched yields a stub `individual` row.
- Commit: `Phase 5: raw-JSON loader for relational tables`

### Phase 6 — Diff report, README, cleanup
- Implement `diff.py` per §7 and the `diff` subcommand; `test_diff.py` with two synthetic runs
  (one fid disappears, one appears) asserting the report content.
- Rewrite `README.md`: purpose, install (`uv sync`), the three subcommands with examples, the
  schema at a glance, the raw-capture design rule, the exit codes (especially 3 = incomplete),
  and the refresh workflow (fetch → check exit code → load → diff → review disappeared FIDs
  before updating external document mappings).
- Set `__version__ = "2.0.0"`. Final sweep: no dead imports (`uv run python -m pyflakes` is NOT
  configured — just grep imports per file), all public functions annotated and docstringed.
- Commit: `Phase 6: run diff report and documentation`

## 10. Manual verification (human operator, after Phase 5)

Not for the implementing model. Run a small real fetch
(`getmyancestors fetch --db test.sqlite -u ... -i <known fid> -a 2 -m -v`), confirm exit code 0,
`load`, and spot-check a known person's names/events/sources in the DB against the
FamilySearch website before trusting a full-tree run.
