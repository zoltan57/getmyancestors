# Kickoff prompt for implementing model

Copy the block below into a fresh session for each phase, replacing `<N>` with the phase
number (1 or 2). Run one phase per session, review the commit, then start the next session.

---

Implement Phase <N> of the plan in `docs/plans/2026-09-04-logging-migration.md`.

Before writing any code:

1. Run `git status` — the working tree must be clean. If it isn't, stop and tell me.
2. Read the entire plan file, not just the Phase <N> section at the bottom. In particular:
   §0 (rules for you), §1 (goal/non-goals/design rule), §2 and its subsections (§2.1,
   §2.1a, §2.2, §2.3, §2.4 — the exact target design for each file), and §3 (testing
   guidance — read this before writing or changing any test; it documents specific
   pitfalls, like `capsys` vs `caplog` and handler accumulation across repeated `main()`
   calls, that are easy to get wrong).
3. If this is Phase 2, confirm `git log` shows Phase 1's commit
   (`Phase 1: migrate session.py/fetch.py diagnostics to logging`). If it doesn't, stop
   and tell me.

Then:

- Implement exactly what Phase <N> specifies — nothing more. Do not refactor, rename, or
  "improve" anything outside the phase's scope, and do not start the next phase.
- The plan is canonical. Do not ask me questions; every decision you need is written in
  the plan, including the full list of call sites to change (§2.2 enumerates every
  `write_log` call site in `session.py` by exact message text — treat that list as
  complete, not illustrative). If something is truly unspecified, pick the simplest
  option consistent with the plan's §1 and note your choice in the commit message.
- Hard rules: never call the live FamilySearch API from anything you run yourself
  (unit tests, or any sanity check) — the plan's §5/§6 acceptance and manual-verification
  steps are written so that none of them need real credentials or network access except
  the one step explicitly marked "human operator only, not for the implementing model" at
  the very end of the plan (§6) — do not run that one. Do not widen or touch the
  `requests-ratelimiter==0.7.0` pin or any dependency version. Do not reintroduce GEDCOM,
  the GUI, merging, ordinances, or contributors. Do not touch `db.py`, `load.py`, or
  `diff.py` — this plan explicitly excludes them (see §0).

When the implementation is done:

- Run the phase's acceptance checks exactly as listed at the top of the plan's §5 for
  every phase (`uv sync --locked`, `uv run python -m compileall -q getmyancestors`,
  `uv run ruff check .`, `uv run ruff format --check .`, `uv run pytest -q`), plus the
  specific additional checks listed under that phase's own "**Acceptance:**" bullet
  (e.g. `grep` checks that certain old code is gone, or a scratch-temp-directory CLI
  invocation). Fix failures until all pass. Do not weaken or skip a check to make it
  pass — if a check seems wrong, stop and tell me instead.
- Commit all changes in a single commit using the exact commit message given in the
  phase.
- Finish with a short report: what was created/deleted/changed, the acceptance check
  results verbatim, and any simplest-option decisions you made.

---
