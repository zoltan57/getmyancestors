# Kickoff prompt for implementing model

Copy the block below into a fresh session for each phase, replacing `<N>` with the phase
number (1-4). Run one phase per session, review the commit, then start the next session.

---

Implement Phase <N> of the plan in `docs/plans/2026-09-04-merged-tree-and-query-config.md`.

Before writing any code:

1. Run `git status` — the working tree must be clean. If it isn't, stop and tell me.
2. Read the entire plan file, not just the Phase <N> section at the bottom. In particular:
   §0 (rules for you), §1 and its subsections §1.1/§1.2 (goal, non-goals, and the two
   design reversals made during planning — both explain *why* `diff.py`'s comparison
   mechanism stays untouched and only its default run-selection changes; do not
   "correct" the plan back toward what it explicitly rejected), §2 and its subsections
   (§2.1-§2.5 — the exact target design for each file/table/function), and §3 (testing
   guidance — read this before writing or changing any test; it documents specific
   scenarios each phase's tests must cover, including edge cases like empty-persons
   batch responses and composite-key memory resolution).
3. If this is Phase 2 or later, confirm `git log` shows every prior phase's commit (exact
   commit messages are given at the end of each phase section). If any is missing, stop
   and tell me.
4. Also confirm `git log` shows commit `85b60e8` ("Fix memory job subject_fid: owning
   person's FID, not the memory's own ID") — this plan's §2.1/§2.3 step 6 assume that
   fix is already in place (a memory row's `subject_fid` is the owning person, not the
   memory's own numeric ID). If it's missing, stop and tell me; do not re-derive or
   work around it yourself.

Then:

- Implement exactly what Phase <N> specifies — nothing more. Do not refactor, rename, or
  "improve" anything outside the phase's scope, and do not start the next phase.
- The plan is canonical. Do not ask me questions; every decision you need is written in
  the plan, including exact SQL for the new tables/queries (§2.1, §2.3, §2.4), the exact
  helper function signatures (§2.2's `sync_batch_indexes`, §2.4's `_resolve_run_pair`),
  and the exact edge-case handling (§2.2's empty/unparseable-response sentinel rows,
  §2.3's per-kind winner-resolution keys, §2.5's 200-ceiling auto-cap-and-warn behavior).
  If something is truly unspecified, pick the simplest option consistent with the plan's
  §1 and note your choice in the commit message.
- Hard rules: never call the live FamilySearch API from anything you run yourself (unit
  tests, or any sanity check) — use the existing `initialized_db`/`load_fixture`/
  `session_factory` fixtures in `tests/conftest.py`, mirroring how existing tests in
  `tests/test_load.py`/`tests/test_diff.py`/`tests/test_db.py` already do this. Do not
  widen or touch the `requests-ratelimiter==0.7.0` pin or any dependency version. Do not
  reintroduce GEDCOM, the GUI, merging, ordinances, or contributors. Do not add any of the
  FamilySearch endpoints/kinds mentioned in
  `docs/decisions/2026-09-04-familysearch-api-limits.md` §3 (person/couple change
  history, couple sources, ordinances, or anything else) — this plan only reorganizes and
  makes configurable the *existing* six kinds, it does not add new ones.
- If Phase <N> is 2, do not change `_load_single_run`'s behavior at all (it must remain
  byte-for-byte the same logic as today's `load()`, just renamed) — it's the fallback
  path anyone using `--run N` explicitly still depends on, and Phase 2's acceptance
  check checks this by name.
- If Phase <N> is 3, the acceptance check requires `diff.py`'s diff isolated to
  `_resolve_run_pair` (plus the Phase 1 `parse_json_body` import swap) — if you find
  yourself editing the appeared/disappeared/name-change comparison logic itself, stop;
  that is out of scope and explicitly rejected by §1.1.

When the implementation is done:

- Run the phase's acceptance checks exactly as listed at the top of the plan's §4 for
  every phase (`uv sync --locked`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run pytest -q`), plus the specific additional checks listed under that phase's own
  "**Acceptance:**" bullet (e.g. a `grep` check that old duplicate code is gone, or a
  manual scratch-database sanity check). Fix failures until all pass. Do not weaken or
  skip a check to make it pass — if a check seems wrong, stop and tell me instead.
- Commit all changes in a single commit using the exact commit message given in the
  phase.
- Finish with a short report: what was created/deleted/changed, the acceptance check
  results verbatim, and any simplest-option decisions you made.

---
