# Kickoff prompt for implementing model

Copy the block below into a fresh session for each phase, replacing `<N>` with the phase number
(1-6). Run one phase per session, review the commit, then start the next session.

---

Implement Phase <N> of the rework plan in `docs/plans/2026-09-03-json-capture-rework.md`.

Before writing any code:

1. Run `git status` — the working tree must be clean. If it isn't, stop and tell me.
2. Read the entire plan file, not just the Phase <N> section. Pay particular attention to
   §0 (rules), the guardrails, and the sections the phase references (schema, CLI spec,
   fixtures).
3. If this is Phase 2 or later, confirm `git log` shows the previous phase's commit. If it
   doesn't, stop and tell me.

Then:

- Implement exactly what Phase <N> specifies — nothing more. Do not refactor, rename, or
  "improve" anything outside the phase's scope, and do not start the next phase.
- The plan is canonical. Do not ask me questions; every decision you need is written in the
  plan. If something is truly unspecified, pick the simplest option consistent with the plan's
  §1 and note your choice in the commit message.
- Hard rules: never call the live FamilySearch API or use real credentials; all tests use the
  fixtures in the plan's §8 with mocked HTTP. Never change the `requests-ratelimiter==0.7.0`
  pin. Do not reintroduce GEDCOM, the GUI, merging, ordinances, or contributors.

When the implementation is done:

- Run the phase's acceptance checks (they are listed at the top of the plan's §9 and inside the
  phase): `uv sync --locked`, `uv run python -m compileall -q getmyancestors`, and
  `uv run pytest -q` (Phase 2 onward). Fix failures until all pass. Do not weaken or skip a
  check to make it pass — if a check seems wrong, stop and tell me instead.
- Commit all changes in a single commit using the exact commit message given in the phase.
- Finish with a short report: what was created/deleted/changed, the acceptance check results
  verbatim, and any simplest-option decisions you made.

---
