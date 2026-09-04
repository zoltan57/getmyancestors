# Technical stack: what we use, what we don't, and why

**Written:** 2026-09-04, against commit `cb55beb` (post JSON-capture rework, §
[2026-09-03-json-capture-rework.md](../plans/2026-09-03-json-capture-rework.md)).

**Purpose:** a recurring question from reviewers/contributors is "why doesn't this
use \<framework X\>?" — usually SQLAlchemy/SQLModel, Pydantic, asyncio, or a linter
suite. This document records the actual stack, the alternatives considered, and the
reasoning, so that answer doesn't have to be re-derived (or re-litigated) every time.

The short version: this tool is a small, infrequently-run (a few times a year) ETL
script — log in, capture raw JSON, load it into relational tables, diff two runs.
The stack is deliberately minimal: standard library plus a handful of small,
single-purpose dependencies. Every "why not X" below comes down to "X's benefits
don't outweigh its complexity for a tool this size and this workload."

## Runtime — Python 3.12+

In use as expected. `pyproject.toml` sets `requires-python = ">=3.12"`. The codebase
uses modern syntax throughout: builtin generic types and `str | None` unions instead
of `typing.Optional`/`typing.List`, f-strings, `pathlib.Path` instead of `os.path`,
and a docstring on every public function (see plan §0's instructions to the
implementing model).

## Persistence — raw `sqlite3`, not SQLAlchemy/SQLModel

**In use:** the standard library's `sqlite3` module, with hand-written DDL in
[`db.py`](../../getmyancestors/db.py) (`SCHEMA_SQL`, executed via
`conn.executescript`) and hand-written SQL throughout `load.py`/`diff.py`.

**Considered:** SQLAlchemy Core/ORM, or SQLModel (which wraps SQLAlchemy with
Pydantic-style models).

**Why not adopted:**

- The schema is small (9 tables) and intentionally treated as a *disposable,
  rebuildable view* over the raw `api_response` capture table — the design rule
  in the rework plan is "raw JSON is the source of truth; relational tables can be
  dropped and rebuilt by re-running `load`." `clear_relational()` in `db.py` is
  literally eight `DELETE FROM` statements in FK-safe order. An ORM's unit-of-work/
  session/migration machinery solves problems (schema evolution without data loss,
  complex query composition) that this design explicitly doesn't have, because nothing
  here needs to survive a schema change — it gets rebuilt from raw JSON instead.
- The tool runs as a short-lived CLI process a few times a year, not a long-running
  service with concurrent sessions, connection pooling needs, or complex
  transactional boundaries — the things an ORM most helps with.
- Raw SQL keeps the capture-vs-relational separation, and exactly what each loader
  step reads/writes, fully visible at the call site, which mattered during the
  rework because FamilySearch's JSON shapes are known to vary (see review finding
  MED-06) and the loader needs to be easy to audit and re-run defensively.

**When this should be revisited:** if the relational schema grows substantially, or
a second consumer needs to query/join the tables in more complex ways (e.g. the
"downstream transcription app" this tool hands off to), SQLAlchemy Core (without the
full ORM/session layer) would be the more likely next step rather than a full ORM —
it would reduce raw-SQL duplication while still fitting the "thin ETL" shape.

## Validation and settings — `argparse` + defensive parsing + `python-dotenv`, not Pydantic

**In use:** `argparse` for the CLI ([`cli.py`](../../getmyancestors/cli.py)),
`python-dotenv`'s `load_dotenv()` plus small hand-written `_env_str`/`_env_int`
helpers for environment-variable defaults/overrides (added 2026-09-04), and
defensive `dict.get(...)`-based parsing of FamilySearch JSON payloads throughout
`load.py`/`fetch.py`/`diff.py` — never a schema that raises on an unexpected shape.

**Considered:** Pydantic v2 models for FamilySearch API payloads, and
`pydantic-settings` for CLI/env configuration.

**Why not adopted:**

- The rework plan explicitly instructs: "trust the fixtures for tests and write the
  loader defensively (`.get()` with defaults) — real payloads vary" (review finding
  MED-06). A Pydantic model validating FamilySearch's GedcomX-derived JSON would
  need `Optional`/default-heavy fields nearly everywhere to avoid raising on the
  same real-world shape drift the loader is designed to tolerate — at that point it
  adds ceremony without adding real safety, since the loader's whole job is "don't
  crash on missing/unexpected fields, just skip them."
- `argparse` fully covers the CLI's needs (subcommands, `type=` validators like the
  FamilySearch-ID regex check, `store_true` flags). `pydantic-settings` would mostly
  duplicate that for the marginal benefit of validating env vars — which the small
  `_env_int`/`_env_str` helpers in `cli.py` already do, with a clear
  `EnvConfigError` message on a malformed value (e.g. `FS_RATE_LIMIT=abc`) mapped to
  exit code 2, matching the CLI's existing "bad arguments" exit code semantics.

**When this should be revisited:** if `load.py` needs to validate/normalize a much
wider variety of FamilySearch payload shapes than today's fixtures cover, a
lightweight `TypedDict`-based typing pass (no runtime validation) might help
readability without reintroducing the "raises on unexpected shape" risk that a full
Pydantic model would.

## Concurrency — `concurrent.futures.ThreadPoolExecutor`, not asyncio

**In use:** `fetch.py`'s per-person "extras" fan-out (sources/notes/memories) uses
`ThreadPoolExecutor(max_workers=5)`, submitting one job per request and collecting
results via `as_completed`.

**Considered:** an asyncio-based worker loop (e.g. with `aiohttp`/`httpx`).

**Why not adopted:** this was an explicit, written decision in the rework plan (§6,
citing review finding MED-05: "no asyncio"), not an oversight. `requests` and the
`requests_ratelimiter` `LimiterAdapter` mounted on `Session` are both synchronous;
going asyncio would mean replacing the whole HTTP client (`aiohttp`/`httpx`) and its
rate-limiting integration, for a workload that's:

- I/O-bound (waiting on FamilySearch's API), which threads handle just as well as
  coroutines for this concurrency level;
- globally rate-limited anyway (2 requests/second by default) — the bottleneck is
  the rate limiter, not the concurrency primitive;
- only ever running 5-way concurrent fan-out for extras requests, not hundreds of
  simultaneous connections where asyncio's lower per-connection overhead would
  matter.

`ThreadPoolExecutor` gets the same practical throughput with far less code and no
new dependency.

**When this should be revisited:** if a future requirement needs much higher
concurrency (hundreds of simultaneous requests) or the HTTP client itself becomes
the bottleneck — unlikely given FamilySearch's own rate limits.

## Quality and tests — pytest only; Ruff/ty/pytest-asyncio not yet configured

**In use:** `pytest` (dev dependency), plus entry-point import checks in
[`quality-gate.yml`](../../.github/workflows/quality-gate.yml) (a prior
`python -m compileall` step was dropped in the logging-migration follow-up
review — it was redundant with the entry-point import checks and the test
suite, which between them import every module in the package).

**Not in use, and why:**

- **`pytest-asyncio`** — irrelevant; there's no asyncio code (see above).
- **Ruff, `ty`** — these are a real, acknowledged gap, not a deliberate rejection.
  The quality-gate workflow's own header comment says so: "no lint or type-check
  tooling configured yet ... add those steps here as that tooling lands." The
  rework plan explicitly deferred this (§9 Phase 6: "`uv run python -m pyflakes` is
  NOT configured — just grep imports per file").

**Status:** Ruff (lint + format) is being added following this document (see commit
history after 2026-09-04). `ty` (Astral's type checker) may follow once it's judged
stable enough for this project's needs — see the CI workflow for what's actually
wired in at any given time, since this document describes the *reasoning*, not a
guaranteed up-to-date status.

## Summary table

| Area | Considered | In use | Why |
| --- | --- | --- | --- |
| Runtime | — | Python 3.12+ | Modern syntax, no legacy constraints. |
| Persistence | SQLAlchemy / SQLModel | stdlib `sqlite3` + hand-written SQL | Small, disposable/rebuildable schema; short-lived process; raw SQL keeps the capture/relational split auditable. |
| Validation/settings | Pydantic v2 + pydantic-settings | `argparse` + `.get()`-defensive parsing + `python-dotenv` | Strict validation conflicts with tolerating variable real-world FamilySearch payload shapes (MED-06); argparse + small helpers already cover CLI/env needs. |
| Concurrency | asyncio worker loop | `concurrent.futures.ThreadPoolExecutor` | Explicit plan decision (MED-05): sync HTTP stack, rate-limiter is the real bottleneck, only 5-way fan-out needed. |
| Quality/tests | Ruff, ty, pytest, pytest-asyncio | pytest only (Ruff landing next) | pytest-asyncio irrelevant (no asyncio); lint/type-check tooling is an acknowledged gap being closed incrementally. |
