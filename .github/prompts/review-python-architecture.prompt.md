---
name: Review Python Architecture
description: Run an evidence-based architectural code review using the Python Architect Reviewer agent and python-code-reviewer skill.
agent: Python Architect Reviewer
---

# Instructions

Execute a comprehensive, evidence-based code review of the target codebase.

## Target Scope

- **Review Target:** `${input:target:repository root}` — if this variable is empty or unresolved,
  review the repository root; if the invoker names a narrower path, review that path instead.
- **Source Root:** `getmyancestors/`
- **Focus Areas:** determine the actual focus areas from the repository itself; do not trust this
  file's description over the code. (At last update: a `requests`-based FamilySearch/GEDCOM
  client with argparse CLIs and a Tkinter GUI.)

## Known Baseline — acknowledge once, do not re-report as findings

These gaps are already known. Per the skill's report-discipline rules, note them in one line in
§1 of the report and move on; spend finding depth on what is *not* already known.

- No lint or type-check tooling is configured (no ruff/mypy/ty config anywhere).
- No test suite exists (no `tests/` directory, no pytest config).
- CI (`.github/workflows/quality-gate.yml`) runs only: package install, `compileall`, and
  CLI entry-point import checks.

If any of these turns out to no longer be true, that supersedes this list — verify, then treat
the baseline entry as stale and say so.

## Deterministic Checks

Run each of these and record pass/fail with file:line evidence in the report. They are
mechanical on purpose: two runs of this review on the same commit must agree on all of them.

| # | Check | How to verify | Pass condition |
|---|---|---|---|
| 1 | Outbound HTTP timeouts | grep `self.get(`/`self.post(`/`requests.` in `getmyancestors/classes/session.py` | Every outbound call passes an explicit `timeout=` |
| 2 | Explicit file encodings | grep `open(` across `getmyancestors/` | Every text-mode `open()` passes `encoding=` |
| 3 | Endpoint centralization | grep `familysearch.org` and `https://` across `getmyancestors/` | FamilySearch URLs/endpoint fragments appear only in `classes/constants.py` or `classes/session.py` |
| 4 | No swallowed exceptions | grep `except` across `getmyancestors/` | No bare `except:`; no handler that discards the exception without logging or re-raising |
| 5 | GUI thread hygiene | read Tkinter callbacks in `classes/gui.py` and `fstogedcom.py` | No blocking network I/O on the Tk main thread; worker threads do not mutate widgets directly; no `Thread` object bound once as a `command=` (a thread cannot be started twice) |
| 6 | Bounded retries | read the retry loops in `classes/session.py` | Every retry loop has a maximum attempt count or total deadline |
| 7 | No mutable default args | grep `=[]`/`={}` in `def` signatures | None present |
| 8 | No scaffold leftovers | check `getmyancestors/hello.py`, `getmyancestors/pyproject.toml`, root `[tool.uv.workspace]` | No placeholder files or manifests that duplicate/conflict with the root `pyproject.toml` |

## Execution Rules

1. Map repository layout, dependency manifests, and configuration files from the project root
   before inspecting modules. Resolve the declared Python floor per the skill's workflow step 1.
2. Read real code modules under `getmyancestors/` (or the specified target path); cite exact
   file paths and line ranges for every finding.
3. Validate toolchain claims against what the project actually has configured (`pyproject.toml`,
   `uv.lock`, CI workflows), remembering the Known Baseline above.
4. Run every Deterministic Check and report each result.
5. Check for duplication, divergent implementations, and extractable helpers.
6. Format the entire review following the standardized 10-section template defined in the
   `python-code-reviewer` skill, including its report-discipline rules (empty severities,
   per-severity detail cap).
7. Write the final report to `./docs/reviews/<YYYY-MM-DD>-code-review.md`, using today's date
   and creating the directory if needed. This path is defined by the skill; do not write the
   report anywhere else.
