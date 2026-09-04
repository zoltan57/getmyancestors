# Project Plan: Migrate ad-hoc `print`/`write_log` diagnostics to `logging`

**Status:** Approved plan, ready for implementation.
**Written:** 2026-09-04, against commit `d10ef5ed71a4b63db6322b9618cecfa73d890666`.
**Companion documents:** `docs/plans/2026-09-03-json-capture-rework.md` (the rework this
builds on — do not re-read it before starting; it is historical context only, nothing
in it should be redone), `docs/decisions/2026-09-04-technical-stack.md` (records why
this project has no lint/type-check tooling beyond what's listed there as of its
writing — logging is not covered there and does not need an entry added).

---

## 0. How to use this plan (instructions for the implementing model)

- This file is **canonical**. Implement **one phase per session**, in order. Do not
  start Phase 2 until Phase 1's acceptance checks pass.
- End every phase by running its acceptance checks, then committing with the phase's
  commit message. Never commit with failing checks.
- **Never call the live FamilySearch API** from tests. Nothing in this plan touches
  network behavior — it only changes how diagnostics are reported. If a test needs a
  `Session` instance, use the existing `session_factory` fixture in
  `tests/conftest.py` (updated per Phase 1 below), not a real login.
- This plan does **not** touch `db.py`, `load.py`, or `diff.py` — none of them emit
  any diagnostic output today beyond the `print()` calls that are each subcommand's
  actual *data product* (row counts, the diff report), and those stay as `print()`
  (see §1's design rule). Do not add unused loggers to those modules "for
  consistency" — only change what this plan says to change.
- Ask nothing; every decision needed is written here. If something is genuinely
  unspecified, choose the simplest option consistent with §1 and note it in the
  commit message.

## 1. Goal, non-goals, and the design rule

**Goal:** replace `Session.write_log` (a hand-rolled timestamp-prefix-and-write
method gated by a `verbose: bool` attribute) and the scattered `print(..., file=sys.
stderr)` diagnostic calls in `cli.py`/`fetch.py` with the standard library's
`logging` module, configured once in one place, so verbosity and optional file
logging work consistently across the whole CLI instead of each module inventing its
own mechanism.

**Non-goals:** this plan does not add a `--log-level` flag, does not add structured
(JSON) logging, does not change what `-v`/`--verbose` *means* to a user, and does not
change the content of any message (only how it's emitted). It does not add logging
to `db.py`/`load.py`/`diff.py` (see §0).

**Design rule:** **stdout is reserved for each subcommand's data product; everything
else goes through `logging` to stderr.** Concretely:

- `fetch`'s one-line summary (`"N requests, M failed"`), `load`'s per-table row
  counts, and `diff`'s full report are the tool's actual output — meant to be piped,
  redirected, or read directly. These stay exactly as `print()` calls to stdout, with
  no changes in this plan.
- Everything else — login progress, per-request HTTP diagnostics, retry/backoff
  notices, the two CRIT-02 "unconditional" failure warnings, and CLI argument
  errors — is a diagnostic, not a data product. All of it moves to `logging`,
  written to stderr by default, optionally duplicated to a file with `--logfile`.

## 2. Target design

### 2.1 New module: `getmyancestors/logging_config.py`

One new, small module, consistent with the existing one-small-module-per-concern
layout (`db.py`, `fetch.py`, `load.py`, `diff.py`, `session.py`, `cli.py`):

```python
"""Central logging configuration for the getmyancestors CLI."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

PACKAGE_LOGGER_NAME = "getmyancestors"


def configure_logging(*, verbose: bool = False, logfile: str | Path | None = None) -> None:
    """Configure the shared getmyancestors logger's level and handlers.

    Safe to call more than once per process (e.g. across repeated ``main()``
    calls in a test session): any handlers this function previously attached
    are removed first, so handlers never accumulate and messages are never
    emitted more than once per call.
    """
```

- `configure_logging()` gets `logging.getLogger(PACKAGE_LOGGER_NAME)` (**not** the
  bare root logger — scoping to the `"getmyancestors"` namespace means this package
  never reconfigures logging for whatever else happens to share the process, e.g. if
  it's ever imported as a library rather than run as the CLI).
- It removes every handler currently on that logger (`for h in list(logger.
  handlers): logger.removeHandler(h)`) before adding new ones. **This is required,
  not optional** — without it, handlers accumulate across repeated calls within one
  process (which is exactly what happens when a test module calls `cli.main()` more
  than once), and every log message gets emitted once per accumulated handler.
- Sets `logger.setLevel(logging.DEBUG if verbose else logging.WARNING)`. This
  reproduces the old behavior exactly: previously, `write_log` wrote almost every
  message only `if self.verbose`, while a couple of specific call sites wrote to
  stderr unconditionally. Mapping "everything `write_log` used to gate on
  `verbose`" to `logger.debug(...)`/`logger.info(...)` and "the couple of
  unconditional stderr writes" to `logger.warning(...)` means: default level
  `WARNING` shows only the warnings (matching old unconditional behavior), and
  `-v`'s `DEBUG` level shows everything (matching old verbose-gated behavior, since
  `DEBUG` is the lowest severity and so includes `INFO`/`WARNING`/etc. too).
- Always attaches a `logging.StreamHandler(sys.stderr)` — created fresh inside this
  function call, **not** at module import time. Its level matches the logger's level
  from this call (`DEBUG` if `verbose` else `WARNING`). Use a simple formatter, e.g.
  `logging.Formatter("[%(asctime)s] %(levelname)s %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")` — close to the old `write_log` line shape but now including
  level and logger name, which the old ad-hoc format didn't have.
  - **Why this must be created inside the function, not at import time:** a
    `StreamHandler()` created without an explicit `stream=` argument binds to
    whatever `sys.stderr` *is at construction time*. pytest's `capsys` fixture
    replaces `sys.stderr` per-test. If the handler were built once at import time
    (before `capsys` ever patches anything), it would keep writing to the
    original, real stderr forever, and `capsys.readouterr().err` in every test
    would see nothing. Building it fresh inside `configure_logging()`, called from
    `cli.main()` during each test, means it correctly binds to whatever `sys.
    stderr` is current at that moment (the `capsys`-patched one, inside a test).
- If `logfile` is given, also attaches a `logging.FileHandler(logfile, encoding=
  "utf-8")` **always at `DEBUG` level, regardless of `verbose`** — this matches the
  old `Session.logfile` behavior exactly (the file got every `write_log` line
  whether or not `verbose` was set; only the console output was gated).
- Does not call `logging.basicConfig()` anywhere, and does not set
  `logger.propagate = False` (leave default `True` — this is what lets pytest's
  `caplog` fixture, which attaches its own handler higher up the hierarchy, still
  see every record regardless of what `configure_logging()` did or didn't set up in
  a given test).

### 2.2 `session.py`

- Drop the `verbose: bool = False` and `logfile: TextIO | None = None` constructor
  parameters and the corresponding `self.verbose`/`self.logfile` attributes
  entirely. Drop the `TextIO` import (no longer used). Remove the `write_log`
  method.
- Add `import logging` and `logger = logging.getLogger(__name__)` at module level
  (this resolves to `"getmyancestors.session"`, a child of the `"getmyancestors"`
  logger `configure_logging()` sets up — see §2.1).
- Replace every `self.write_log(...)` call with a `logger` call, split by severity
  exactly as follows (this is the full list — check it against the current file,
  don't guess at additional ones):
  - `logger.debug(...)` — routine progress, never actionable on its own:
    `"Downloading: " + url` (both occurrences, in `login()` and `get_url()`),
    `"Logging in: " + url`, `"Getting an authorization code: " + url`,
    `"Exchanging for an access token: " + url`, `f"Status code: {response.
    status_code}"`.
  - `logger.warning(...)` — something went wrong on this attempt but the retry
    loop is handling it, or the request is being abandoned as unrecoverable:
    `"Login flow did not return an OAuth code."`, `"Invalid auth request"`,
    `res.text` (the "access_token not in data" branch), `"Read timed out"` (both
    occurrences), `"Connection aborted"` (both occurrences), `"HTTPError"` (both
    occurrences), `"KeyError"`, `"ValueError"`, `"Login failed after retries;
    session cookie was not established."`, `"WARNING: " + url` (404/405/410
    branch — drop the redundant `"WARNING: "` string prefix now that the level
    itself conveys that), `"WARNING: HTTP 500 from " + url` (same prefix-drop),
    `f"WARNING: code 403 from {url} {message}"` (same), `f"WARNING: corrupted
    file from {url}, error: {error}"` (same).
  - The two **CRIT-02 "unconditional" messages** at the bottom of `get_url` (the
    `sys.stderr.write(warning)` call and the following `self.write_log(f"WARNING:
    max retries exceeded for {url}")` call) collapse into **one**
    `logger.warning(f"Max retries exceeded for {url} (failed_requests={self.
    failed_requests})")` call. Delete the now-redundant `sys.stderr.write(...)`
    entirely. **Remove `import sys` too** — verified against the current file:
    `sys` is used in exactly two places in `session.py` (the `write_log` method's
    `sys.stderr.write(log)`, and this bottom-of-`get_url` `sys.stderr.write
    (warning)`), both of which are deleted by this phase, so nothing else in the
    file needs the import afterward.
  - The `print("Please log in to the web page that just opened and try again.")`
    call in the missing-OAuth-code branch becomes `logger.warning("Please log in
    to the web page that just opened and try again.")`. This is an intentional,
    documented behavior change: it moves from unconditional stdout to
    unconditional-by-default (`WARNING` shows at the default level) stderr,
    consistent with this plan's design rule that stdout is reserved for data
    products (§1). `webbrowser.open(response.url)` right before it is unchanged.
- Do not change any return values, retry counts, sleep durations, or control flow —
  only how each message is emitted.

### 2.3 `fetch.py`

- Add `import logging` and `logger = logging.getLogger(__name__)` at module level.
- The `sys.stderr.write("WARNING: captured data is incomplete because requests
  failed.\n")` call (currently right before `return 3`) becomes `logger.warning
  ("Captured data is incomplete because requests failed.")`. **Remove `import
  sys` too** — verified against the current file: `sys` is used in exactly this
  one place in `fetch.py`, so nothing else needs the import afterward.
- The `print(f"{requests_total} requests, {requests_failed} failed")` line is a
  data-product summary (§1) — **do not change it**.
- No other changes to `fetch.py`.

### 2.4 `cli.py`

- Add `import logging` and `from getmyancestors.logging_config import
  configure_logging`.
- In `main()`, call `configure_logging(verbose=getattr(args, "verbose", False),
  logfile=getattr(args, "logfile", None))` **immediately after `args = parser.
  parse_args(argv)`**, before `_validate_required(args)` — so that even the
  "missing required argument" error path is subject to the user's `-v`/`--logfile`
  choice (if the `fetch` subcommand — the only one that has these two flags, see
  next bullet — supplied them; `load`/`diff` always get the default `verbose=False,
  logfile=None`, i.e. plain stderr-only `WARNING`-level output, since neither
  subcommand has these flags).
- Add `--logfile PATH` to the `fetch` subparser only (matching where `-v` already
  lives), with `help="also write every log message to this file, in addition to
  the console (regardless of -v/--verbose)"`. Do not add `--logfile` to `load`/
  `diff` — they don't do anything that benefits from it today (see §0), and adding
  an unused flag would be scope creep.
- Convert every remaining `print(..., file=sys.stderr)` call to a `logger.error
  (...)` call using a module-level `logger = logging.getLogger(__name__)`:
  - The `_validate_required` failure message (`f"error: {error}"` — drop the
    `"error: "` prefix now that the level conveys it: just `logger.error(error)`).
  - The `EnvConfigError` handler around `build_parser()` (`str(exc)` — becomes
    `logger.error(str(exc))`; note `configure_logging()` **cannot** have run yet at
    this point, since it happens after `parse_args()`, which can't run before
    `build_parser()` succeeds — so this one path stays without a configured
    handler until Python's own `logging.lastResort` fallback prints it. That's
    fine and intentional: this failure mode means an env var like `FS_RATE_LIMIT`
    had a bad value, `build_parser()` itself raised before we even know `-v`/
    `--logfile`, and `logging.lastResort` prints `WARNING`+ to stderr by default
    with no configuration needed — the message still reaches the user. Do not
    try to call `configure_logging()` before `build_parser()` to "fix" this; it's
    already correct.
  - The `f"Unknown command: {args.command}"` message.
  - The final `except Exception as exc:` handler's `str(exc)` message (keep the
    existing `# noqa: BLE001` comment on that line — it's still a deliberate
    top-level boundary).
- Remove the now-unused `import sys` only if `sys.argv`/`sys.exit` etc. are no
  longer referenced anywhere in `cli.py`. **Verified against the current file:
  `sys.argv` is still used in `main()`'s `argv = list(sys.argv[1:] if argv is None
  else argv)` line, so `import sys` stays** — only the four `print(..., file=sys.
  stderr)` calls listed above go away, not the import itself.
- In `_run_fetch_command`, remove the `verbose=args.verbose` keyword argument from
  the `Session(...)` call (Session's constructor no longer accepts it per §2.2).
  Do not remove `args.verbose` from argparse — it's still a real user-facing flag,
  just no longer threaded through to `Session`; it's consumed by `configure_logging
  ()` in `main()` instead.

## 3. Testing guidance (read before writing or changing any test)

- **`capsys` vs `caplog`:** tests that call `cli.main(...)` end-to-end (most of
  `tests/test_cli.py`) can keep using `capsys.readouterr().err` to assert on
  messages — `configure_logging()`'s `StreamHandler` is built fresh on each
  `main()` call using the then-current `sys.stderr`, which is the one `capsys` has
  already patched inside the test (see §2.1's explanation of why). Tests that
  exercise `session.py` or `fetch.py` in isolation (constructing a `Session` via
  `session_factory` or calling `run_fetch()` directly, without going through `cli.
  main()`) should use pytest's built-in `caplog` fixture instead, since no
  `StreamHandler` has necessarily been configured in that scenario — `caplog`
  attaches its own handler above the `getmyancestors` logger in the hierarchy and
  sees every record regardless. Call `caplog.set_level(logging.DEBUG, logger=
  "getmyancestors")` in any test that needs to see `DEBUG`-level messages (the
  default `caplog` capture level is `WARNING`).
- **Handler leakage across tests:** add an autouse fixture to `tests/conftest.py`
  that resets the `"getmyancestors"` logger after every test — e.g.:

  ```python
  @pytest.fixture(autouse=True)
  def _reset_getmyancestors_logger() -> Generator[None, None, None]:
      logger = logging.getLogger("getmyancestors")
      yield
      for handler in list(logger.handlers):
          logger.removeHandler(handler)
      logger.setLevel(logging.NOTSET)
  ```

  This needs `import logging` and `Generator` added to the existing
  `from collections.abc import Callable` import line in `tests/conftest.py`
  (`from collections.abc import Callable, Generator`).

  This is a safety net on top of `configure_logging()`'s own handler-clearing (§2.1)
  — it specifically protects tests that construct a `Session`/call `run_fetch()`
  directly without ever going through `configure_logging()`, so a handler
  (attached in a prior test) can't linger and produce surprising output in a later
  one.
- **`tests/conftest.py`'s `session_factory` fixture:** delete the `session.verbose
  = False` and `session.logfile = None` lines (lines 31-32 as of this writing) —
  those attributes no longer exist on `Session` per §2.2. No replacement is needed;
  the module logger requires no per-instance setup.
- **`tests/test_session.py`:** the existing five tests do not assert on `write_log`
  output directly (confirmed: no `verbose`/`logfile`/`stderr` references in that
  file as of this writing) — they assert on return values, `failed_requests`, sleep
  call args, and login-retry counts, none of which this plan changes. They should
  keep passing unmodified. Add new tests:
  - A `DEBUG`-level message (e.g. from a successful `get_url` call, which logs
    `"Status code: ..."`) appears in `caplog` when `caplog.set_level(logging.DEBUG,
    logger="getmyancestors")` is set, and does **not** appear with no explicit
    `caplog.set_level` call (default `WARNING` capture level).
  - The "max retries exceeded" warning (previously asserted only indirectly via
    `failed_requests`) now also appears in `caplog.records` at `WARNING` level,
    even with no `caplog.set_level` call — proving the CRIT-02 always-visible
    requirement survived the migration.
- **`tests/test_fetch.py`:** add one test asserting the "Captured data is
  incomplete..." message appears in `caplog.records` at `WARNING` level (default
  capture level, no `set_level` needed) when `run_fetch` returns 3, alongside the
  existing assertions on `requests_failed`/`api_response` rows.
- **`tests/test_cli.py`:** the three existing tests asserting on `capsys.
  readouterr().err` (`"FS_DB"`, `"FS_USERNAME"`, `"FS_RATE_LIMIT"` — see §2.4's
  `_validate_required`/`EnvConfigError` handling) must keep passing with no
  changes to their assertions, proving the `print` → `logger.error` conversion
  preserved visible behavior. Add new tests:
  - `-v`/`--verbose` on `fetch` causes a `DEBUG`-level message to reach `capsys`'s
    stderr capture when a fake `Session.get_url` is exercised (reuse
    `FakeSession`/fixtures from `test_fetch.py` if convenient, or a minimal stand-
    in) — proving `configure_logging()` is actually wired to `-v`.
  - `--logfile PATH` on `fetch` results in the log file existing and containing at
    least one line after a `run_fetch` call, using a real `tmp_path` file.
  - Calling `cli.main([...])` twice in the same test does **not** duplicate log
    lines (assert `capsys.readouterr().err.count(...)` is 1, not 2) — this is the
    handler-accumulation regression test for §2.1's "must remove old handlers
    first" requirement.

## 4. Documentation updates

- `README.md`: add a short "Logging" section (after "Configuration (.env)", before
  "Commands" — or wherever reads best once written) explaining: default behavior
  (warnings only, to stderr), `-v`/`--verbose` (also logs every HTTP request at
  debug detail), `--logfile PATH` (additionally writes everything, regardless of
  `-v`, to the given file — handy for an unattended fetch you want to review
  later). Update the existing `fetch` flags table's `-v` row if its wording no
  longer matches (it currently says "print each HTTP request to stderr as it
  happens" — still accurate, but consider adding a one-line mention that it's now
  backed by Python's `logging` module at `DEBUG` level, for anyone grepping the
  README for that fact). Add a `--logfile` row to the same table.
- `getmyancestors fetch --help` output changes automatically once the new argparse
  argument is added (§2.4) — no separate action needed, but do sanity-check the
  rendered help text reads sensibly during Phase 2's acceptance check.

## 5. Phases

Every phase ends with: `uv sync --locked` passes, `uv run python -m compileall -q
getmyancestors` passes, `uv run ruff check .` and `uv run ruff format --check .`
both pass (this repo now has Ruff wired into CI — see
`docs/decisions/2026-09-04-technical-stack.md` and commit `d10ef5e`), `uv run
pytest -q` passes, then one commit.

### Phase 1 — `logging_config.py` + `session.py` + `fetch.py`

- Add `getmyancestors/logging_config.py` per §2.1.
- Migrate `session.py` per §2.2 (drop `verbose`/`logfile` ctor params and
  `write_log`; add module logger; convert every call site).
- Migrate `fetch.py`'s one `sys.stderr.write` per §2.3.
- Update `tests/conftest.py`'s `session_factory` fixture per §3 (delete the two
  now-invalid attribute-assignment lines) and add the autouse logger-reset fixture
  per §3.
- Add the new `test_session.py`/`test_fetch.py` tests per §3. Do **not** touch
  `cli.py` yet — `Session(...)` in `cli.py` still passes `verbose=args.verbose` at
  the end of this phase, which will now fail (`Session.__init__` no longer accepts
  it). That's expected and is fixed in Phase 2 — to keep Phase 1 shippable on its
  own despite this, temporarily change that one call site to stop passing
  `verbose=args.verbose` (just delete the keyword argument) as part of Phase 1,
  even though the rest of `cli.py`'s logging migration (§2.4) waits for Phase 2.
  This keeps `uv run pytest -q` green at the end of Phase 1 without pulling all of
  Phase 2's scope forward.
- **Acceptance:** all four end-of-phase checks in the paragraph above pass; `grep
  -n "write_log\|self.verbose\|self.logfile" getmyancestors/session.py` returns
  nothing; `grep -n "sys.stderr.write" getmyancestors/fetch.py` returns nothing.
- Commit: `Phase 1: migrate session.py/fetch.py diagnostics to logging`

### Phase 2 — Wire `-v`/`--logfile` through `cli.py`

- Apply all of §2.4's remaining changes (the `Session(...)` keyword-argument
  removal was already done in Phase 1; everything else — `--logfile` argparse
  argument, the `configure_logging()` call in `main()`, and the `print` → `logger.
  error` conversions — happens now).
- Add the new `test_cli.py` tests per §3.
- Apply the README changes per §4.
- **Acceptance:** all four end-of-phase checks pass; `getmyancestors fetch --help`
  shows both `-v`/`--verbose` and `--logfile`; `grep -rn "print(.*file=sys.stderr"
  getmyancestors/cli.py` returns nothing; a manual sanity check (not a unit test,
  run from a scratch temp directory since it creates a SQLite file as a side
  effect) that `python -c "from getmyancestors.cli import main; main(['load',
  '--db', 'nonexistent.sqlite'])"` prints a `logging`-formatted error line
  (`[...] ERROR getmyancestors.cli: ...`) to stderr, not a bare message — confirms
  the formatter from §2.1 is actually active end-to-end.
- Commit: `Phase 2: wire -v/--logfile through cli.py via logging_config`

## 6. Manual verification (human operator, after Phase 2)

Not for the implementing model. Run a small real fetch with `-v --logfile run.log`,
confirm the console shows per-request detail and `run.log` contains the same lines
(plus anything below the console's configured threshold, if `-v` was omitted —
not applicable here since `-v` was passed, but worth remembering that `--logfile`
alone, without `-v`, should still capture debug-level detail to the file while the
console stays warnings-only).
