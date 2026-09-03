---
name: Review Python Architecture
description: Run an evidence-based architectural code review using the Python Architect Reviewer agent and python-code-reviewer skill.
agent: Python Architect Reviewer
---

# Instructions

Execute a comprehensive, evidence-based code review of the target codebase.

## Target Scope
- **Review Target:** the repository root, unless the invoker names a narrower path; review that path instead.
- **Source Root:** `getmyancestors/`
- **Docs Root:** none currently; the review report is the first artifact under `docs/reviews/`.
- **Focus Areas:** determine the actual focus areas from the repository (currently a `requests`-based FamilySearch/GEDCOM client with a Tkinter GUI); adjust as the codebase evolves.

## Execution Rules
1. Map repository layout, dependency manifests, and configuration files from the project root before inspecting modules.
2. Read real code modules under `getmyancestors/` (or the specified target path); cite exact file paths and line ranges for every finding.
3. Validate issues by running whatever lint/type/test commands the project actually has configured (check `pyproject.toml`, `requirements.txt`, CI workflows). If none exist, report that as a finding rather than assuming a toolchain.
4. Check for duplication, divergent implementations, and extractable helpers.
5. Format the entire review following the standardized 10-section template defined in the `python-code-reviewer` skill.
6. Write the final report to `./docs/reviews/<YYYY-MM-DD>-code-review.md`, using today's date. This path is defined by the skill; do not write the report anywhere else.