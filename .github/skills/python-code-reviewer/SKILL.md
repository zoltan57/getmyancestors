---
name: python-code-reviewer
description: Perform an evidence-based, senior architect code review for Python codebases. Use when asked to review Python repositories, perform architectural or code audits, or evaluate code against modern Python best practices.
---

# Python Code Reviewer

Perform thorough, evidence-based code reviews for Python projects. Every finding must cite concrete file paths and line ranges, avoid speculation, and include recommended fixes.

## When to Use

- Performing an architectural or code quality review of a Python codebase.
- Auditing any Python application, library, or CLI tool regardless of framework.
- Generating structured Markdown review reports in `./docs/reviews`.

## Technical Stack Scope

Determine the actual stack from the repository itself (`pyproject.toml`/`requirements.txt`,
lockfiles, entry points) rather than assuming one. Common areas to check when present:

- **Runtime:** Python version floor declared by the project (`requires-python`).
- **Web/CLI/GUI frameworks:** whatever the project actually uses (e.g. FastAPI, Flask, Django,
  Tkinter, argparse/click-based CLIs).
- **Persistence:** any ORM/database layer in use, if applicable.
- **Validation & Settings:** dataclasses, Pydantic, or plain config modules — whatever the project uses.
- **Concurrency:** threading, multiprocessing, or asyncio, if used.
- **External integrations:** HTTP clients, third-party APIs, rate limiting, caching.
- **Quality & Testing:** whatever test runner/linter/type-checker the project has configured.

A dependency pinned to an exact version is often a deliberate stability decision (check
`CHANGELOG`/`README`/comments before assuming it's a defect) — do not recommend widening a pin
without evidence it's unintentional.

## Review Workflow

1. **Map the Repository First:** Inspect entry points, package layout, configurations, dependency manifests, and any project-specific rule files (e.g. `AGENTS.md`, `CLAUDE.md`, `.github/skills/`, `.github/agents/`). Resolve the declared Python floor (`requires-python`) and record it — every syntax recommendation later in the review must be valid on that floor. Also sanity-check the floor itself: flag it if the declared version is past end-of-life or if pinned dependencies require a higher version than the floor claims to support. Project-specific conventions override generic advice.
2. **Establish Canonical Authority First:** Read whatever architecture/contract documentation the repo actually has (`README`, `docs/*`) and any active agent/skill/prompt instructions before evaluating source behavior. If no such docs exist, say so once and derive intended architecture from the code's own repeatable conventions instead.
3. **Read Representative Modules:** Sample across all layers (routes/pages, UI components, services, workers, persistence, provider adapters, settings, tests) before drawing conclusions.
4. **Run Drift Analysis:** Compare documented intended behavior versus repository ground truth; identify both implementation drift and undocumented-but-repeatable conventions that should be formalized.
5. **Run Dead-Code/Orphan Sweep:** Identify candidate orphan modules/functions/classes with zero inbound references, then verify expected exceptions (entrypoints, framework/plugin registration, dynamic imports/reflection, CLI hooks, test-only utilities) before marking as orphaned.
6. **Assess Boundary and Coupling Health:** Evaluate UI/service/persistence/provider dependency flow, identify circular dependencies, leaky abstractions, and transaction ownership ambiguity.
7. **Assess Invariant Placement:** For each hard rule, decide whether it belongs in docs (rationale), instructions (active steering), skills (periodic audit procedure), or deterministic tests (enforcement).
8. **Verify Claims:** Determine the project's actual lint/type-check/test commands from its own config (`pyproject.toml`, `tox.ini`, CI workflows, `README`) rather than assuming a toolchain. Run them rather than guessing, and record the exact commands and their outcomes in the report. If no such tooling is configured, say so explicitly — as a finding, unless the invoking prompt already declares that gap as a known baseline.
9. **Validate Recommendations Against Consumers:** A recommendation is a claim about the future and must be verified like any other. Before recommending a change to a shared symbol — a class field, an exception attribute, a helper's return value, a function signature — enumerate **every** consumer of that symbol (`grep` the whole repo, including tests) and confirm the fix is safe for each one. Record the consumers in the finding's **Blast Radius**. A fix that is correct for the path that produced the finding can silently break a second consumer.
10. **Prioritize Hot Paths:** Focus deeply on the code paths most exercised or most consequential for this specific project (e.g. network calls, data parsing/serialization, file I/O, long-running loops) — identify these from the repo rather than assuming a request/worker shape.
11. **Enforce Read-Only Safety:** Do not modify code unless explicitly instructed.

## Core Review Areas

### 1. Python Best Practices
- **Type Annotations:** Ensure completeness and idiomatic syntax for the Python floor resolved in workflow step 1 — never recommend syntax the floor cannot run (`X | None` and builtin generics need 3.10 without `from __future__ import annotations`; `Self` needs 3.11). If modern syntax is desirable but blocked by a stale floor, recommend raising the floor as its own finding rather than recommending the syntax. Avoid unparameterized containers or bare `Any`.
- **Error Handling:** Identify bare/broad `except`, swallowed exceptions, missing `raise ... from`, and exceptions used for control flow.
- **Resource Management:** Verify context managers for files, network clients, and locks. Check for leaked handles, sockets, or processes.
- **Data Modeling:** Check proper use of dataclasses vs. plain dicts/tuples vs. whatever validation library the project uses. Eliminate mutable default arguments and stringly-typed payloads.
- **Idioms & Clean Code:** Verify `pathlib` usage over `os.path`, comprehensions vs manual loops, removal of dead code, and elimination of magic numbers.

### 2. Framework & Library Usage
- Identify every framework/library the project depends on directly (web, GUI, ORM, HTTP client, CLI, packaging) and evaluate usage against that library's documented best practices and current major version.
- Flag deprecated APIs, legacy version patterns, or usage that fights the framework's intended structure.
- Verify separation of concerns matching the project's own layering (e.g. business logic not embedded in UI callbacks or route handlers, if such layers exist).

### 3. Persistence & External I/O (when present)
- **Session/Connection Lifecycle:** Enforce explicit open/commit/rollback/close boundaries for DB connections, HTTP sessions, or file handles.
- **Query/Request Optimization:** Detect N+1 patterns, repeated calls inside loops, and missing caching/rate-limiting where the project already has such mechanisms available.
- **Portability:** Check cross-platform/cross-environment assumptions relevant to the project's declared support matrix.

### 4. Concurrency (when present)
- **Task/Thread Lifecycle:** Flag unreferenced background tasks/threads that risk being garbage collected or leaked, missing cancellation/join handling, and lack of graceful shutdown.
- **Synchronization:** Check for appropriate use of locks, queues, or async primitives, and backoff/retry logic around flaky operations.

### 5. External Integrations / Adapters (when present)
- **Encapsulation:** Verify integration-specific details (headers, endpoints, payload formats, credentials) do not leak into unrelated layers.
- **Client Lifecycle:** Reuse pooled clients with proper connection handling and timeouts. Validate external responses rather than trusting raw payloads.

### 6. Testing & Quality Tooling
- **Test Isolation:** Verify tests do not rely on live external services, real clocks, or shared global state.
- **Test Setup:** Check test runner configuration and fixture lifecycle for the project's actual test framework (if any).
- **Project Test Contract:** Derive marker/strictness/async rules from the project's own config (`pyproject.toml`, `pytest.ini`, `tox.ini`) rather than assuming a specific contract. If no test suite exists, report that as a finding (unless the invoking prompt declares it as a known baseline) rather than skipping the section.

### 7. Duplication & Consolidation
- Identify repeated code blocks, candidate helper abstractions, divergent patterns for identical operations, and duplicated domain constants.

### 8. Orphaned/Dead Code Audit
- Find candidate orphan modules/functions/classes with no inbound references.
- Validate each candidate against dynamic wiring exceptions (entrypoints, plugin registration, reflection/dynamic imports, CLI hooks, test utilities).
- Report outcomes as: removed orphan, retained-with-justification, or uncertain-follow-up.

### 9. Architecture & Governance
- **Architectural Drift:** Compare intended architecture (from docs/instructions if present, otherwise inferred repeatable conventions) against implementation behavior and cite concrete drift points.
- **Systemic Health:** Evaluate domain cohesion, dependency direction, lifecycle consistency, and operational reliability seams.
- **Invariant Routing:** Recommend the correct enforcement layer per rule (docs vs instructions vs skills vs tests).
- **Meta-Tooling Alignment:** Recommend updates for instruction files and skills when repository patterns or contracts evolve.

## Severity Rubric

Severity reflects concrete consequence, never style preference or effort to fix.

- **Critical:** Data loss/corruption; secret leakage; silent wrong output presented as authoritative.
- **High:** Architectural boundary violated; runtime failure or unhandled exception on a hot path; documented invariant contradicted by implementation.
- **Medium:** Correctness risk under load or edge conditions (missing retry/backoff, leaked resource, missing timeout); drift between docs and code with no immediate runtime impact.
- **Low:** Maintainability, typing completeness, duplication, naming, or dead code with no behavioral risk.

### Reachability

Severity states how bad the consequence is; **Reachability** states whether it can happen today.
They are independent, and a finding is not complete without both. Record one of:

- **Live:** reachable in the current configuration and deployment.
- **Latent:** the defective code is present but unreachable because of a current setting, single-
  instance deployment, or absent caller. **State the exact condition that unblocks it.**
- **Theoretical:** requires a combination the project has explicitly ruled out.

Latent findings carry a scheduling constraint that severity alone cannot express: a latent defect
must usually be fixed *before* the change that makes it live, not after. Say so explicitly in the
finding and reflect the ordering in the §9 action plan — for example, "fix the retry-category gate
before raising `worker_max_retries` above 0," or "handle this `IntegrityError` before deploying a
second worker replica." Do not downgrade severity merely because a finding is latent.

### Conflicting invariants

When a fix sits between two invariants that pull in opposite directions, say so in the
**Recommendation** and name both, along with the test that guards each. Flag explicitly what the
over-correction would be, because the simplest-looking fix usually satisfies one invariant by
silently destroying the other. A recommendation that resolves one side without naming the other is
incomplete and will be implemented incorrectly.

## Output Report Structure & Template

Generate Markdown reports at `./docs/reviews/<YYYY-MM-DD>-code-review.md` following this exact
template structure (create `docs/reviews/` if it does not exist). Reports are dated,
non-canonical artifacts: `docs/reviews/**` is explicitly **not** part of the canonical authority
set that the canonical-authority check resolves against.

Report discipline:

- **Empty severities stay empty.** If a severity level has no findings, write
  `No findings at this severity.` under its heading. Never pad a level with inflated or
  restated findings to fill space.
- **Bound the detail.** Fully detail at most 8 findings per severity level; roll any remainder
  into a one-line-each summary list at the end of that level. Depth goes to the highest-severity,
  live-reachability findings first.
- **Known baselines are not findings.** If the invoking prompt declares known baseline gaps
  (e.g. "no test suite exists yet"), acknowledge them in one line in §1 and do not re-report
  them as fresh findings.

```markdown
# Architecture & Code Review Report

**Repository Target:** fill in with the actual repository root (or the narrower path reviewed).
**Target Stack:** fill in with the frameworks/libraries actually found in the repository (e.g. Python version, web/GUI framework, persistence, concurrency model, key dependencies).

---

## 1. Executive Summary
- 5-10 bullets on overall health, top risks, and high-leverage refactors.

---

## 2. Executive Architecture Assessment
- High-level verdict on domain cohesion, boundary clarity, and architecture fitness.
- Top 3-5 systemic risks or bottlenecks.

---

## 3. Findings by Severity

### Critical Severity
#### [CRIT-01] Title
- **Location:** `path/to/file.py:lines`
- **Reachability:** Live / Latent (state the exact condition that unblocks it) / Theoretical
- **Problem & Consequence:** Concrete consequence, not a style opinion.
- **Blast Radius:** Every consumer of the symbols the recommendation changes, each confirmed
  safe. Write `None — change is local` only after actually searching. If the fix touches a
  shared field or helper, list the call sites (including tests and evidence/logging paths).
- **Recommendation:** Fix with before/after sketch. If two invariants conflict here, name both,
  name the test guarding each, and state what the over-correction would be.
- **Effort:** S / M / L

### High Severity
#### [HIGH-01] Title
...

### Medium Severity
#### [MED-01] Title
...

### Low Severity
#### [LOW-01] Title
...

---

## 4. Architectural Drift & Gap Analysis

`Direction` is `doc->code` (implementation must change to match documented intent) or
`code->doc` (an undocumented but repeatable convention that should be formalized).

| Area / Component | Direction | Documented / Intended Rule | Actual Implementation State | Severity | Recommended Resolution |
| :--- | :--- | :--- | :--- | :--- | :--- |

---

## 5. Invariant Inventory & Routing Recommendations
| Invariant / Constraint | Current Location | Recommended Target Layer | Rationale |
| :--- | :--- | :--- | :--- |

---

## 6. Stack-Specific Analysis
- Python Best Practices
- Framework & Library Usage (list the specific frameworks found)
- Persistence & External I/O (if present)
- Concurrency (if present)
- External Integrations / Adapters (if present)
- Testing & Quality Tooling

---

## 7. Duplication & Consolidation Report
| Pattern / Duplication | Locations | Proposed Canonical Home | Estimated Lines Removed |
| :--- | :--- | :--- | :--- |

### Proposed Canonical Abstractions
- Code signatures and implementation homes.

---

## 8. Meta-Tooling & Instruction Update Recommendations
- Required updates to docs, instructions, skills, or tests to keep enforcement current.

---

## 9. Prioritized Dependency-Ordered Action Plan
1. **Phase 1: Blocking fixes**
2. **Phase 2: Enforcement hardening**
3. **Phase 3: Reliability & concurrency**
4. **Phase 4: Consolidation & refactoring**
5. **Phase 5: Non-blocking governance/documentation depth**

---

## 10. Preserved Strengths
- Existing patterns worth maintaining.