# Architecture & Code Review Report

**Repository Target:** repository root (`c:\Github\getmyancestors`), commit `45ba79a`, clean working tree. Source root: `getmyancestors/`.
**Target Stack:** Python >=3.12 (`pyproject.toml:8`); `requests` 2.32+ with `requests-ratelimiter==0.7.0` (deliberate pin, `pyproject.toml:30-34`) and `fake-useragent`; argparse CLIs (`getmyancestors`, `mergemyancestors`); Tkinter GUI (`fstogedcom`) with `diskcache`; `babelfish` for language names; concurrency via `threading`/`ThreadPoolExecutor` and `asyncio.run_in_executor`; packaging via setuptools, dependencies locked with `uv.lock`.

---

## 1. Executive Summary

- **Known baseline acknowledged once:** no lint/type-check tooling, no test suite, and CI (`.github/workflows/quality-gate.yml`) runs only `uv sync --locked`, `compileall`, and CLI import checks — all three verified still true; not re-reported as findings.
- **Verification commands run:** `uv sync --locked` → pass ("Resolved 11 packages / Audited 11 packages"); `uv run python -m compileall -q getmyancestors` → pass; entry-point imports of `getmyancestors.getmyancestors` and `getmyancestors.mergemyancestors` → pass.
- The domain core (GEDCOM object model in `classes/tree.py`, parser in `classes/gedcom.py`) is cohesive and mostly sound. The two systemic weak points are the **`Session.get_url` return contract** (returns `None`, the string `"error"`, or a dict — callers index into it unchecked) and the **GUI's threading model** (worker threads mutate Tk widgets; network I/O on the main thread).
- Two Critical findings: the GUI persists the FamilySearch **password in plaintext** in a world-readable temp-dir diskcache, and the download pipeline **silently omits data** after retry exhaustion while still reporting success.
- The GUI's Merge tab **duplicates and diverges from** `mergemyancestors.py`: it overwrites instead of merging and drops the `initiatory` ordinance — silent data loss relative to the CLI.
- The advertised `--delay` CLI option is a **no-op** (`Session` stores it and never reads it), and the GUI applies no rate limiting at all, contradicting the rate-limit rationale documented in `pyproject.toml:30-34`.
- Deterministic checks: **5 of 8 fail** (timeouts, encodings, endpoint centralization, swallowed exceptions, GUI thread hygiene); bounded retries, mutable defaults, and scaffold leftovers pass. Details below.
- Highest-leverage refactors: (1) make `get_url` raise typed exceptions or return one shape; (2) extract a single shared merge routine used by both the CLI and GUI; (3) move all GUI network work off the Tk main thread and all widget updates onto it.

### Deterministic Check Results

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | Outbound HTTP timeouts | **FAIL** | Login flow calls omit `timeout=`: `classes/session.py:80` (`self.get`), `:84-92` (`self.post`), `:104` (`self.get`), `:117-126` (`self.post`). Only `get_url` passes it (`classes/session.py:175`). |
| 2 | Explicit file encodings | **FAIL** | `getmyancestors.py:197` — `open(settings_name, "w")` with no `encoding=`. All other text opens pass it (`classes/gui.py:99,233,466,481`; `argparse.FileType("w", encoding="UTF-8")` at `getmyancestors.py:111,119` and `mergemyancestors.py:26,34`). |
| 3 | Endpoint centralization | **FAIL** | `classes/tree.py:96-99` rewrites the endpoint fragment `familysearch.org/platform/memories/memories` → `www.familysearch.org/photos/artifacts` inside `Source.__init__`; `classes/tree.py:341` inlines the `http://familysearch.org/v1/LifeSketch` type URI outside `constants.py`. All true request endpoints are in `classes/session.py` (allowed). |
| 4 | No swallowed exceptions | **FAIL (narrow)** | No bare `except:` anywhere. One discarding handler: `classes/gui.py:72-73` — `except TclError: pass` in `EntryWithMenu.paste` (benign empty-clipboard case, but discards without logging or a comment). Marginal: `classes/session.py:108-113` binds `e` and never logs it (it does print a user message and exit, so not silent). |
| 5 | GUI thread hygiene | **FAIL** | (a) `classes/gui.py:437-439` — `command=Thread(target=self.quit).start` binds one `Thread` object; a second click raises `RuntimeError: threads can only be started once`. (b) Blocking network I/O on the Tk main thread: `classes/gui.py:305` (`StartIndis.add_indi` → `fs.get_url`) reached from the button command at `gui.py:357-359,387-390`; `Merge.save` (`gui.py:141`, `:158-235`) parses files and writes output on the main thread. (c) Worker threads mutate widgets directly: `download()` runs in a `Thread` (`gui.py:614-622`) and calls `self.info()`/`.config()`/`.destroy()` (`gui.py:550-554,563,608-611`); `update_gui` calls `self.master.update()` from a non-main thread (`gui.py:638-643`). |
| 6 | Bounded retries | **PASS** | `login`: `for attempt in range(5)` (`classes/session.py:76`); `get_url`: `for attempt in range(10)` (`classes/session.py:172`) with terminal warning at `:223-224`. (Silent exhaustion is reported separately as CRIT-02/MED-03.) |
| 7 | No mutable default args | **PASS** | grep for `=[]`/`={}` in `def` signatures across `getmyancestors/` → no matches; collection params default to `None` (e.g. `classes/tree.py:56,84,133`). |
| 8 | No scaffold leftovers | **PASS** | No `getmyancestors/hello.py`; no `getmyancestors/pyproject.toml`; root `pyproject.toml` (read in full) has no `[tool.uv.workspace]`. An untracked local `getmyancestors/.python-version` exists but is not in `git ls-files`, so the committed state is clean. |

---

## 2. Executive Architecture Assessment

- **Verdict:** Layering is acceptable for the project's size — `session.py` (transport/auth), `tree.py` (domain model + FS mapping), `gedcom.py` (parsing), thin CLI entry points. The GUI (`classes/gui.py`) is the drift zone: it re-implements the merge pipeline, hardcodes a different session configuration (`timeout=1`, no rate limit), and owns its own translation function — three parallel implementations of things the core already provides.
- **Top systemic risks:**
  1. **Stringly-typed transport contract.** `get_url` returns `None | "error" | dict` (`classes/session.py:163-224`); 14 call sites consume it and several index into it unchecked. Any new caller inherits the trap.
  2. **Thread-unsafe shared state.** One `requests.Session` and one mutable `Tree` (dicts/lists) are shared across executor threads with no synchronization (`classes/tree.py:655-663`, `getmyancestors.py:289-304`), including concurrent re-login on 401 (`classes/session.py:193-195`).
  3. **GUI/CLI feature bifurcation.** Merge, download-loop, ID validation, and translation logic exist twice with behavioral differences (see §7); fixes land in one copy.
  4. **Failure signals routed to logs the user may not see.** `write_log` writes to stderr only when `verbose` (`classes/session.py:64-70`), so exhausted retries degrade output silently.

---

## 3. Findings by Severity

### Critical Severity

#### [CRIT-01] GUI persists FamilySearch password in plaintext in a world-readable temp directory
- **Location:** `classes/gui.py:25-26` (cache at `tempfile.gettempdir()/fstogedcom`), `gui.py:249-251` (password pre-filled from cache at startup), `gui.py:504-512` (password written when "Save Password" is checked), `gui.py:501-502` (username always cached).
- **Reachability:** Live — one checkbox click (`gui.py:259-261`); the username is cached unconditionally on every successful login.
- **Problem & Consequence:** Secret leakage. `diskcache` stores values unencrypted in a SQLite file under the shared system temp directory; on multi-user machines any local user can read the FamilySearch credential, and backup/sync tools may capture it. Temp cleanup can also silently delete it, which is the only mitigation and not a designed one.
- **Blast Radius:** Cache readers: `gui.py:27` (`lang`), `gui.py:249` (`username`), `gui.py:251` (`password`), `gui.py:260` (`save_password`). Replacing password storage with the OS keyring (or removing it) touches only `SignIn.__init__` and `Download.login`; the other cache keys are non-secret and can stay. No CLI code reads this cache.
- **Recommendation:** Store the password via the `keyring` package (`keyring.set_password("fstogedcom", username, password)`) or drop the save-password feature; keep `lang`/`username` in diskcache. At minimum, move the cache out of the shared temp dir into a user-scoped config dir with restrictive permissions and document the risk next to the checkbox.
- **Effort:** S

#### [CRIT-02] Retry exhaustion silently drops data while the run reports success
- **Location:** `classes/session.py:223-224` (`get_url` returns `None` after 10 failed attempts, logging only via `write_log`), `classes/session.py:64-70` (`write_log` emits to stderr **only if `verbose`** and to `logfile` only if one was given), `getmyancestors.py:310-324` (CLI prints a "Downloaded N individuals…" success summary and exits 0), `classes/gui.py:610` ("Success !" label).
- **Reachability:** Live — any transient FamilySearch outage, rate-limit response streak, or (in the GUI) the hardcoded 1-second timeout (`gui.py:487`) can exhaust retries mid-run.
- **Problem & Consequence:** Silent wrong output presented as authoritative. Every consumer of `get_url` treats `None` as "no data exists" (e.g. `classes/tree.py:355,375,398,414,441,542`), so persons, sources, notes, or whole relationship batches (`tree.py:669-672` — a failed batch of up to 200 persons is skipped and the loop advances at `tree.py:710`) vanish from the GEDCOM with no error, no nonzero exit code, and — without `-v` or `-l` — no visible message at all.
- **Blast Radius:** A failure counter on `Session` (`self.counter` already exists at `session.py:49,165` as precedent) is additive: increment in the max-retries branch (`session.py:223`) and read it in `getmyancestors.py:306-324` (final summary), `gui.py:608-611` (success label). No existing consumer breaks; `Tree` and `Gedcom` untouched.
- **Recommendation:** Track `self.failed_requests` in `Session`; on exhaustion also print a warning unconditionally (not just via `write_log`). In the CLI `finally` block, if `fs.counter` includes failures, print "N requests failed — output is incomplete" and exit nonzero; in the GUI, show it in the completion label. Do **not** make exhaustion raise inside `add_indis` without also handling it in the GUI worker thread (see HIGH-02) — the over-correction would be trading silent omission for a silently dead thread.
- **Effort:** M

### High Severity

#### [HIGH-01] `get_url` returns `None | "error" | dict` and callers index into it unchecked
- **Location:** Contract: `classes/session.py:163-224` (returns `None` at `:185,188,215,222,224`, the string `"error"` at `:210`, or a dict at `:219`). Crashing consumers: `getmyancestors.py:232-235` (`test["status"]` → `TypeError` when `test == "error"` — and the `"error"` sentinel exists *precisely* for this ordinances 403 case, `session.py:200-210`); `classes/gui.py:514-515` (`.get("status")` → `AttributeError` on `None` or `"error"`); `classes/tree.py:413-415` (`data["data"]` → `TypeError` on `"error"`, guarded only by truthiness); `classes/tree.py:556-559` (`sources["sourceDescriptions"]` → `TypeError` if the follow-up fetch returned `None`) and `:567-569` (`self.tree.sources[source_fid]` → `KeyError` for any quote whose description was never fetched).
- **Reachability:** Live. A non-LDS account running `getmyancestors -c` hits the `getmyancestors.py:235` crash instead of the intended "Need an LDS account" message; the same account signing into the GUI kills the login thread at `gui.py:515`, leaving the Sign In button disabled forever. The `add_marriage` paths trigger under any sources-fetch failure during `-m` downloads.
- **Blast Radius:** All 14 call sites of `get_url`: `classes/session.py:229`; `classes/tree.py:352, 374, 397, 413, 440, 541, 556, 575, 589, 669`; `getmyancestors.py:232`; `classes/gui.py:305, 515`. Ten of them handle `None` correctly today (`if data:` guards); a fix must not break those. The `"error"` sentinel is consumed nowhere — no caller compares against it, confirming it is dead-as-designed and only ever crashes.
- **Recommendation:** Replace the `"error"` return (`session.py:210`) with `None` plus a distinguishable signal (e.g. set `self.lds_error = True`, or raise a dedicated `OrdinanceAccessError` caught at `getmyancestors.py:231-237` and `gui.py:514-516`). Guard `gui.py:515` (`data = self.fs.get_url(...); lds_account = bool(data) and data.get("status") == "OK"`) and `tree.py:556-570` (skip quotes whose source description is absent). Two invariants conflict here: "partial data must not abort a long download" (the `None` convention, exercised by every `if data:` guard) vs. "failures must not be silent" (CRIT-02). The over-correction is converting `get_url` to raise on every failure — that would crash mid-download paths that today degrade gracefully. Keep `None` for "no data", add explicit signaling for the LDS case and the failure counter for silence.
- **Effort:** M

#### [HIGH-02] GUI violates Tk threading rules in both directions
- **Location:** Worker threads mutate widgets: `classes/gui.py:550-554` (`self.options.destroy()`, `self.form.destroy()`, `title.config`, `btn_valid.config` from the download thread), `:563,572,600-610` (`self.info`, final `.config`), `:638-643` (`update_gui` loop calls `self.master.update()` from a non-main thread every 100 ms); `login()` also runs in a worker (`gui.py:441`) and packs/destroys widgets (`gui.py:516-526`). Main thread does blocking network/file I/O: `gui.py:305` (`add_indi` fetch), `gui.py:158-235` (`Merge.save`). One-shot Thread bound as a command: `gui.py:437-439`.
- **Reachability:** Live. Tcl/Tk is not thread-safe; calling widget methods from non-main threads raises `RuntimeError: main thread is not in main loop` or `TclError` intermittently — the classic "GUI crashes sometimes mid-download" failure. The Quit button reliably raises `RuntimeError` on a second click. Slow network freezes the whole window during `add_indi`/merge.
- **Blast Radius:** Confined to `classes/gui.py` and `fstogedcom.py` (main loop owner). `command_in_thread` (`gui.py:614-622`) is used by three commands (`gui.py:275,441,522`); replacing the `update_gui` polling thread with `root.after`-based scheduling changes only `Download`. Core (`tree.py`, `session.py`) is unaffected.
- **Recommendation:** Route all widget updates through the main thread: workers put messages on a `queue.Queue`; `Download` polls it with `self.after(100, self._poll)` (this also replaces `update_gui` and its `self.master.update()` hack). Fix the Quit button with `command=lambda: Thread(target=self.quit).start()` or a direct call. Move `add_indi`'s fetch and `Merge.save` into `command_in_thread` workers that only enqueue UI updates.
- **Effort:** L

#### [HIGH-03] GUI Merge tab silently loses data that the CLI merge preserves
- **Location:** `classes/gui.py:186-218` vs `mergemyancestors.py:69-110`. The GUI copy assigns instead of merging: `gui.py:188-197` (`name`, `birthnames`, `nicknames`, `aka`, `married`, `gender`, `facts`, `notes`, `sources`, `memories` all `=` where the CLI uses `|=` / set-if-absent, `mergemyancestors.py:69-92`), `gui.py:198-200` (baptism/confirmation/endowment overwritten unconditionally vs guarded at `mergemyancestors.py:83-90`), and **`initiatory` is absent entirely** from the GUI copy (present at `mergemyancestors.py:87-88`). Family facts/notes/sources likewise overwritten (`gui.py:215-217`) vs unioned (`mergemyancestors.py:104-109`).
- **Reachability:** Live — any GUI merge of files with overlapping individuals (the tool's primary use case) takes last-file-wins on facts/notes/sources and drops `initiatory` ordinances; output is written and a success dialog shown (`gui.py:233-235`).
- **Blast Radius:** Exactly two consumers of the merge algorithm: `mergemyancestors.main` (`mergemyancestors.py:52-125`) and `Merge.save` (`gui.py:158-235`). An extracted `merge_gedcom_into_tree(tree, ged, indi_counter, fam_counter)` (natural home: `classes/tree.py` or a new `classes/merge.py`) is safe for both — the CLI's semantics are the correct ones and the GUI has no deliberate divergence (the omission of `initiatory` while its three sibling ordinances are present indicates copy-drift, not intent).
- **Recommendation:** Extract the CLI merge loop into one shared function; have both entry points call it. This also removes the duplicated note-renumbering block (`mergemyancestors.py:112-121` ≡ `gui.py:220-229`).
- **Effort:** M

#### [HIGH-04] `Session.login` calls `sys.exit(2)` / `print` / `webbrowser.open` from library code — silently kills the GUI login thread
- **Location:** `classes/session.py:106-113` (on a missing OAuth `code`, opens a browser, prints to stdout, `sys.exit(2)`). Callers: CLI `getmyancestors.py:214-226`; GUI worker thread `classes/gui.py:441,469-495`; and re-login on 401 from download threads at `session.py:193-195`.
- **Reachability:** Live whenever FamilySearch requires interactive verification (new device, expired session, 2FA challenge). In the GUI, `SystemExit` raised in a non-main thread is swallowed by `threading`, the stdout message is invisible, and the Sign In button stays disabled — the app is stuck until force-quit. When triggered via the 401 re-login path inside a download worker, one downloader thread dies mid-run and its work is silently lost (compounding CRIT-02).
- **Blast Radius:** `login()` is called from `Session.__init__` (`session.py:58`) and the 401 handler (`session.py:194`). Its only success signal today is the `logged` property, already checked at `getmyancestors.py:225` and `gui.py:489` — so replacing `sys.exit(2)` with `return` (leaving `logged == False`) is safe for both existing callers; the CLI keeps exiting via its own check, and the GUI shows its error dialog instead of hanging. The `webbrowser.open`/message behavior should move behind a flag or callback so the CLI keeps it.
- **Recommendation:** In the missing-code branch: `self.write_log(...)`, optionally open the browser only when a CLI-owned flag is set, and `return` instead of `sys.exit(2)`. Improve the CLI's silent `sys.exit(2)` at `getmyancestors.py:226` to print why (see MED-03).
- **Effort:** S

### Medium Severity

#### [MED-01] Login-flow HTTP requests have no timeout
- **Location:** `classes/session.py:80, 84-92, 104, 117-126` (no `timeout=`); contrast `session.py:175`.
- **Reachability:** Live — a black-holed connection during any of the four auth requests hangs the process (CLI) or the login thread (GUI) indefinitely; the `range(5)` retry loop never advances because the call never returns.
- **Blast Radius:** None — change is local; adding `timeout=self.timeout` to four calls inside `login()` alters no signatures (searched: no other callers of these raw `get`/`post` invocations).
- **Recommendation:** Pass `timeout=self.timeout` on all four calls. Note the GUI constructs `Session(..., timeout=1)` (`gui.py:487`), so after this fix GUI logins get 1-second timeouts — raise that constant in the same change (see MED-03).
- **Effort:** S

#### [MED-02] Advertised `--delay` option is a no-op and the GUI applies no rate limit at all
- **Location:** `getmyancestors.py:154-160` (help: "Delay between requests in seconds [0.1]"), `:223` (passed to `Session`); `classes/session.py:37,47` — `self.delay` is stored and **never read** (whole-repo grep: only those two lines). The GUI never passes `rate_limit` (`gui.py:482-488`), so the `LimiterAdapter` (`session.py:53-56`) is never mounted there — contradicting `pyproject.toml:30-34`, which justifies the `requests-ratelimiter==0.7.0` pin as "enforces the FamilySearch request rate".
- **Reachability:** Live — every run without `--rate-limit` is unthrottled regardless of `--delay`; every GUI run is unthrottled while spawning up to `min(32, cpu+4)` default-executor threads (`gui.py:582-597`).
- **Blast Radius:** `delay` consumers: `getmyancestors.py:155-160,223` and `session.py:37,47` only. Implementing it as `time.sleep(self.delay)` inside `get_url` (before `session.py:175`) or removing the option both touch only those sites. Enabling a default `rate_limit` in the GUI touches `gui.py:482-488` only.
- **Recommendation:** Either implement the delay in `get_url` or delete the option; give the GUI a sensible default `rate_limit`. Respect the pinned-dependency note in `pyproject.toml` — the limiter is the sanctioned mechanism, so preferring `rate_limit` over `delay` and deleting `delay` is the cleaner resolution.
- **Effort:** S

#### [MED-03] Retry design conflates request timeout with backoff, and login failure is unexplained
- **Location:** `classes/session.py:145,149,153,157,191,216` — `time.sleep(self.timeout)` uses the *request timeout* as the *retry backoff* (CLI default: 60 s sleeps; 10 attempts can stall ~10 min on a persistent 500). `gui.py:487` sets `timeout=1`, giving GUI users 1-second sleeps but also 1-second read timeouts once MED-01 is fixed. Docstring contradiction: `session.py:24` says "time before retry a request", CLI help `getmyancestors.py:87` says "Timeout in seconds". After 5 failed login attempts, `login()` falls through silently (`session.py:76-161`) and the CLI exits with no message (`getmyancestors.py:225-226`).
- **Reachability:** Live under any degraded-network condition.
- **Blast Radius:** `self.timeout` readers: `session.py:145,149,153,157,175,181,191,216`. Splitting into `timeout` (request) and a small exponential backoff is internal to `Session`; constructor signature keeps `timeout` so both callers (`getmyancestors.py:214-224`, `gui.py:482-488`) are unaffected.
- **Recommendation:** Separate concerns: keep `timeout=` for requests; back off with something like `min(2 ** attempt, 60)`. Print an actionable message before the CLI's `sys.exit(2)` on failed login, and align the docstring with reality.
- **Effort:** M

#### [MED-04] Shared mutable state accessed from worker threads without synchronization
- **Location:** Executor fan-out: `classes/tree.py:655-663` (`add_data` per person), `getmyancestors.py:289-304` (`ThreadPoolExecutor`, default 10 workers), `classes/gui.py:582-597`. Races: check-then-set on `tree.sources` (`tree.py:363-368` — two threads can both miss the key and create duplicate `Source` objects with different `num`s, each appending notes via `tree.py:63-64`); `Note.__init__` appends to the shared `tree.notes` list while `get_contributors` iterates it (`tree.py:450-454`); one `requests.Session` shared by all threads including concurrent `self.login()` on 401 (`session.py:193-195`), which mutates shared `self.headers` (`session.py:138`) and cookies mid-flight.
- **Reachability:** Live at default concurrency; consequences range from duplicate `SOUR` records in output to corrupted auth state after a token expiry under load. Low per-run probability, rises with tree size and `--concurrency`.
- **Blast Radius:** `tree.sources` writers: `tree.py:365,564` and reader `gedcom.py:252-255`; `tree.notes` writers: `tree.py:63-64` (all Note creations); `login()` callers per HIGH-04. A `threading.Lock` around the sources check-then-set and a lock-guarded "single re-login in flight" flag are internal changes; no signatures move.
- **Recommendation:** Add a `Lock` on `Tree` for the sources registry (`with self._lock: if id not in self.sources: ...`), and serialize re-login inside `Session` (double-checked flag) so only one thread refreshes the token.
- **Effort:** M

#### [MED-05] Ad-hoc asyncio lifecycle: loops created per call, never closed, and cross-function coupling
- **Location:** `classes/tree.py:666-667` — `add_indis` creates and globally installs a **new event loop on every call** (once per generation per download) and never closes any of them; `tree.py:778` (`add_spouses`) and `gui.py:599` call `asyncio.get_event_loop()`, which only works because `add_indis` happened to run first **in the same thread** — in a thread where it didn't, Python 3.12 raises (non-main thread) or warns (deprecated). The async layer adds nothing: `run_in_executor` on the default executor just wraps blocking calls that `getmyancestors.py:289-304` already handles with a plain `ThreadPoolExecutor`.
- **Reachability:** Live for the resource leak (loops accumulate per generation); Latent for the `get_event_loop` failure — unblocked by any refactor that calls `add_spouses`/GUI `download_stuff` before `add_indis`, or by a future Python release removing the deprecated implicit-loop behavior.
- **Blast Radius:** `add_datas`/`add` inner coroutines are private to `tree.py`; `download_stuff` private to `gui.py`. Replacing all three with `concurrent.futures.ThreadPoolExecutor` (as `getmyancestors.py` already does) removes the `asyncio` import from both files; no public API changes. This latent defect should be fixed **before** any GUI-threading rework (HIGH-02), which is exactly the kind of change that reorders the call sequence and makes it live.
- **Recommendation:** Drop asyncio: `with ThreadPoolExecutor(max_workers=n) as ex: list(ex.map(...))` in `add_indis`, `add_spouses`, and `Download.download`. This also unifies the three divergent concurrency patterns (§7).
- **Effort:** M

#### [MED-06] Unguarded access to optional FamilySearch payload fields aborts downloads
- **Location:** `classes/tree.py:153` and `:234` — `data["attribution"]` accessed unconditionally (`attribution` is optional in GEDCOM X); `tree.py:194` — `data["about"]` guarded only by `"links" in data`; `tree.py:367` — `quotes[source["id"]]` can `KeyError` if a source description lacks a matching quote entry; `session.py:202-213` — inside the 403 handler, `r.json()["errors"][0]["message"]` assumes a JSON error body and raises uncaught (`ValueError`/`KeyError`) on an HTML or differently-shaped 403, escaping the retry loop entirely.
- **Reachability:** Live, data-dependent — a single fact or name without `attribution` anywhere in the tree raises `KeyError` inside an executor future, which propagates at `getmyancestors.py:303-304` (`future.result()`) and aborts the run (the `finally` at `getmyancestors.py:306-309` still writes a partial file, presented with the normal summary — feeding CRIT-02).
- **Blast Radius:** `Fact.__init__` consumers: `tree.py:350,545` and `gedcom.py:188` (constructs with `data=None` — unaffected). `Name.__init__`: `tree.py:322-331`, `gedcom.py:153,174` (`data=None` — unaffected). `Memorie.__init__`: `tree.py:385`, `gedcom.py:277` (`data=None`). Switching to `data.get("attribution", {})` is safe for all.
- **Recommendation:** Use `.get()` with defaults for `attribution`/`about`/quote lookups; in the 403 handler, wrap the JSON introspection in `try/except` and fall back to the generic retry path.
- **Effort:** S

#### [MED-07] `Merge.save` crashes on cancel and cannot merge twice
- **Location:** `classes/gui.py:164-168` — `asksaveasfilename` cancel returns `""`, but unlike `Download.save` (`gui.py:464-465`, which guards), `Merge.save` proceeds to parse everything and then `open("", "w")` at `gui.py:233` raises. Second defect: `FilesToMerge.add_file` opens handles once (`gui.py:99-101`); after a merge consumes them (`gui.py:175-176`), the files remain listed but their handles are at EOF — a second Merge click silently produces an empty/incomplete GEDCOM.
- **Reachability:** Live — cancel is a normal user action; re-merging after adjusting the file list is a normal workflow.
- **Blast Radius:** `self.files` consumers: `gui.py:88` (dup check via `.name`), `:101,115-116` (add/remove), `:160,175`. Storing filenames instead of open handles and opening inside `save()` with a context manager is safe for all four sites (the dup check simplifies).
- **Recommendation:** Add the `if not filename: return` guard *before* parsing; store paths, not handles, and open per-merge.
- **Effort:** S

#### [MED-08] Merging GEDCOMs that lack `_FSFTID` collapses all such individuals into one
- **Location:** `mergemyancestors.py:63-68` and `classes/gui.py:180-185` key merged individuals by `fid`; individuals without a `_FSFTID` tag parse with `fid=None` (`gedcom.py:113-114` never fires), so every such person maps to `tree.indi[None]`, silently fusing unrelated people; families likewise key on `(None, None)`.
- **Reachability:** Latent — unblocked the moment a user feeds a GEDCOM not produced by this software. The GUI warns against exactly that (`gui.py:132-135`); the CLI (`mergemyancestors.py`) carries no such warning in `--help` or the README (`README.md:77-81`).
- **Blast Radius:** Fix is local to the two merge loops (or the shared function after HIGH-03): fall back to a per-file synthetic key when `fid is None`. No other consumer keys `tree.indi` by `None`.
- **Recommendation:** Skip-with-warning or synthesize unique keys for `fid=None` individuals; at minimum document the constraint in the CLI help. Fix before or together with the HIGH-03 extraction so the shared routine is born correct.
- **Effort:** S

### Low Severity

#### [LOW-01] Settings file written without explicit encoding
- **Location:** `getmyancestors.py:197`.
- **Reachability:** Live on Windows (locale encoding varies; `PYTHONUTF8` not guaranteed).
- **Problem & Consequence:** Non-ASCII usernames/paths in the echoed settings can raise `UnicodeEncodeError` or produce mojibake. (Deterministic check 2's only failure.)
- **Blast Radius:** None — change is local (searched: the `.settings` file is written here and read nowhere in the repo).
- **Recommendation:** `open(settings_name, "w", encoding="utf-8")`.
- **Effort:** S

#### [LOW-02] FamilySearch ID regex is unanchored and duplicated in three places
- **Location:** `getmyancestors.py:171`, `classes/gui.py:299`, `classes/gui.py:544` — `re.match(r"[A-Z0-9]{4}-[A-Z0-9]{3}", fid)` accepts any string with a valid 8-char *prefix* (e.g. `ABCD-123XYZ-junk` passes).
- **Reachability:** Live (validation weaker than intended); consequence is only a later "Individual not found".
- **Blast Radius:** The three call sites above; a shared `is_valid_fid()` in `classes/constants.py` is safe for all (each currently treats the result as boolean).
- **Recommendation:** Single helper using `re.fullmatch(r"[A-Z0-9]{4}-[A-Z0-9]{3}[0-9]?", fid)` (FS IDs can be 4-3 or 4-4; verify against current ID format before widening) — at minimum, anchor with `fullmatch`.
- **Effort:** S

#### [LOW-03] FamilySearch URL fragments outside the sanctioned modules
- **Location:** `classes/tree.py:96-99` (memories→photos endpoint rewrite in `Source.__init__`), `classes/tree.py:341` (`http://familysearch.org/v1/LifeSketch` inline; its siblings live in `constants.py:23,32,36`).
- **Reachability:** Live (maintainability: an FS URL change requires editing `tree.py`). Deterministic check 3's failure.
- **Blast Radius:** The rewrite pair is used once; the LifeSketch URI once. Moving both to `constants.py` touches only these lines plus imports at `tree.py:13-18`.
- **Recommendation:** Add `MEMORIES_URL_REWRITE` and `FACT_LIFESKETCH` constants in `classes/constants.py`.
- **Effort:** S

#### [LOW-04] Python 2 / legacy relics and dead code on a 3.12 floor
- **Location:** `getmyancestors.py:2` and `mergemyancestors.py:1` (`from __future__ import print_function`); `mergemyancestors.py:38-41` (Python 3.4 argparse `TypeError` guard — unreachable); `mergemyancestors.py:12` (`sys.path.append(os.path.dirname(sys.argv[0]))` — package installs make this useless and it can shadow modules); `getmyancestors.py:6` (`unquote` imported, never used — whole-repo grep confirms); `classes/session.py:95` (f-string with no placeholders); stale "(4 Jul 2016)" in both CLI descriptions (`getmyancestors.py:19`, `mergemyancestors.py:17`); `# coding: utf-8` headers (`fstogedcom.py:2`, `translation.py:1`).
- **Reachability:** Live but behavior-neutral except the `sys.path` append (mild shadowing risk).
- **Blast Radius:** None — all deletions are local; nothing imports these names.
- **Recommendation:** Delete all listed relics.
- **Effort:** S

#### [LOW-05] No type annotations anywhere in the package
- **Location:** Package-wide; e.g. `classes/session.py:27-38`, `classes/tree.py:56,84,133,288`, `classes/gedcom.py:18`.
- **Reachability:** Live (maintainability only). The 3.12 floor supports `X | None` and builtin generics natively, so modern syntax is unblocked.
- **Blast Radius:** Annotations are additive; the one caution is `Session.logfile: bool | IO[str]` and `Note.__init__(text: str = "")` reveal existing type confusion (`logfile=False` as sentinel, `session.py:34`) — annotate honestly first, refactor sentinels second.
- **Recommendation:** Annotate `session.py` and `tree.py` public surfaces first; adopt `mypy`/`ruff` as the CI comment (`quality-gate.yml:4-7`) anticipates.
- **Effort:** L

#### [LOW-06] Normal GUI quit uses `os._exit(1)`
- **Location:** `classes/gui.py:237-240` (`Merge.quit`), `gui.py:529-535` (`Download.quit`).
- **Reachability:** Live — every quit exits with status 1 (reads as failure to shells/launchers) and bypasses `diskcache` finalization and buffered-file flushing (`Merge.quit` closes nothing; `Download.quit` closes only the logfile).
- **Blast Radius:** None — change is local; the `os._exit` exists to kill non-daemon download threads, which the HIGH-02 rework (daemon threads or cooperative shutdown) obsoletes.
- **Recommendation:** After HIGH-02, mark worker threads `daemon=True` and quit via `root.destroy()`; if `os._exit` must remain interim, use status 0.
- **Effort:** S

Remaining Low findings (one line each):

- **[LOW-07]** `except TclError: pass` with no comment in paste handler — `classes/gui.py:72-73` (deterministic check 4's only true hit; add a comment or log at debug).
- **[LOW-08]** Package `__init__` eagerly imports both CLI modules while `tree.py:12` imports the package back for `__version__` — works only because `__version__` is read lazily (`tree.py:852`); fragile circular import (`getmyancestors/__init__.py:1-4`).
- **[LOW-09]** Log/file lifecycle: CLI never closes `args.logfile`/`args.outfile`; GUI reopens `download.log` per login without closing the prior handle and writes it CWD-relative, which fails in read-only working dirs (`gui.py:481`); `write_log` never flushes (`session.py:64-70`).
- **[LOW-10]** `parser.error = parser.exit` hack duplicated in both CLIs (`getmyancestors.py:164`, `mergemyancestors.py:45`) mangles argparse error reporting (the error message becomes the exit *status*); `mergemyancestors.py:48` then prints that message as `e.code`.
- **[LOW-11]** `translation.py` ships pseudo-locale placeholder strings under `eo` (e.g. `translation.py:6` `"[Ļîƒé Šķéţçĥ---- П國カ내]"`) that render for Esperanto-locale accounts.
- **[LOW-12]** `Tree.__init__` crashes with an opaque `babelfish` error if login half-succeeded (`fs.lang` is `None`) — `classes/tree.py:648`; guarded today only by the `fs.logged` checks at both call sites (`getmyancestors.py:225`, `gui.py:489`).

---

## 4. Architectural Drift & Gap Analysis

| Area / Component | Direction | Documented / Intended Rule | Actual Implementation State | Severity | Recommended Resolution |
| :--- | :--- | :--- | :--- | :--- | :--- |
| Rate limiting | doc->code | `pyproject.toml:30-34`: LimiterAdapter "enforces the FamilySearch request rate" (rationale for the ==0.7.0 pin) | Adapter mounted only when `--rate-limit` passed (`session.py:53-56`); GUI never passes it; `--delay` is a no-op (MED-02) | Medium | Default `rate_limit` in GUI; implement or delete `--delay` |
| CLI `--delay` help text | doc->code | "Delay between requests in seconds [0.1]" (`getmyancestors.py:157-159`) | `Session.delay` stored, never read (`session.py:47`) | Medium | See MED-02 |
| Merge semantics | doc->code | CLI merge (union/set-if-absent, `mergemyancestors.py:69-110`) is the intended algorithm | GUI copy overwrites and drops `initiatory` (`gui.py:186-218`) | High | Extract shared merge (HIGH-03) |
| `Session.timeout` meaning | code->doc | Constructor docstring: "time before retry" (`session.py:24`); CLI help: "Timeout in seconds" (`getmyancestors.py:87`) | Used as both request timeout and inter-retry sleep (`session.py:145,175`) | Medium | Split parameters (MED-03), fix docstring |
| Merge input contract | code->doc | GUI warns merges are only reliable for this software's files (`gui.py:132-135`) | CLI help and README (`README.md:77-81`) omit the constraint; `fid=None` collapse (MED-08) | Medium | Document in CLI help/README; harden per MED-08 |
| Endpoint centralization | code->doc | Repeatable convention: request endpoints live in `session.py`, type URIs in `constants.py` | Two strays in `tree.py:96-99,341` | Low | Move to constants (LOW-03); formalize the rule (§5) |
| GEDCOM output contract | code->doc | Emits GEDCOM 5.5.1, UTF-8, 255-byte lines with CONC/CONT (`tree.py:22-44,846-850`) | Consistent but nowhere documented or tested | Low | Formalize as tested invariant (§5) |

---

## 5. Invariant Inventory & Routing Recommendations

| Invariant / Constraint | Current Location | Recommended Target Layer | Rationale |
| :--- | :--- | :--- | :--- |
| `requests-ratelimiter` stays ==0.7.0 until retested | `pyproject.toml:30-34` comment + `uv.lock` | Docs (present) + deterministic test asserting the pin | A lockfile upgrade sweep could widen it accidentally; a one-line test makes the intent enforceable |
| Every outbound HTTP call passes `timeout=` | Nowhere (violated, MED-01) | Deterministic test / lint rule + this review's check 1 | Mechanical, greppable; belongs in CI once fixed |
| FS request endpoints only in `session.py`; type URIs only in `constants.py` | Convention (2 strays) | Skill/prompt check (exists: check 3) + deterministic test | Already codified in the review prompt; a grep-based test closes the loop |
| GEDCOM lines ≤255 bytes, CONC continuation at 248 | `tree.py:22-44` (`cont()`) | Unit test | Pure function, trivially testable, silently corrupts output if wrong |
| Widgets touched only from the Tk main thread | Nowhere (violated, HIGH-02) | Instructions (`.github` agent/skill) + code structure (queue + `after`) | Not greppable; needs active steering plus a structural fix |
| Credentials never persisted in plaintext | Nowhere (violated, CRIT-01) | Instructions + code (keyring) | Policy invariant; document in contributor instructions after the fix |
| Python floor 3.12; recommendations must run on it | `pyproject.toml:8`, `README.md:10` | Docs (present) + CI (present via setup-uv 3.12) | Already dual-enforced; keep |
| `get_url` returns one shape; failures are counted, never silent | Violated (HIGH-01/CRIT-02) | Code contract + docstring + test | The single highest-recurrence trap for future callers |

---

## 6. Stack-Specific Analysis

- **Python Best Practices:** No mutable default arguments (check 7 pass). No type annotations (LOW-04/LOW-05). `%`-formatting and `os.path` used throughout where f-strings/`pathlib` are available on the 3.12 floor — cosmetic, fold into any modernization pass. Exception handling is narrow in `session.py`'s retry loops (specific `requests` exceptions) but two broad `except Exception` sites exist (`session.py:108,220` — the second logs and returns, acceptable for a tail guard). Python 2 relics detailed in LOW-04.
- **Framework & Library Usage:** `requests` — subclassing `Session` is reasonable; note `self.headers = {...}` at `session.py:50` replaces the `CaseInsensitiveDict` requests installs with a plain dict (works because headers are also passed per-request, but drops case-insensitivity for any future `self.headers` lookups). `requests-ratelimiter` — correct mount pattern (`session.py:53-56`) but effectively optional (MED-02). `fake-useragent` — used once at construction (`session.py:50`), consistent with its documented purpose in `pyproject.toml:23-24`. Tkinter — threading model violates Tk's single-thread rule (HIGH-02). `diskcache` — module-level `Cache` at import time (`gui.py:26`) means merely importing `classes.gui` performs disk I/O; acceptable for a GUI-only module but worth knowing (CI deliberately skips importing it, `quality-gate.yml:38-40`). `babelfish` — single call site (`tree.py:648`), unguarded against `None` (LOW-12).
- **Persistence & External I/O:** All FS API access funnels through `Session.get_url` — good chokepoint, undermined by its return contract (HIGH-01). Batch fetching via `MAX_PERSONS=200` (`constants.py:4`, `tree.py:669-671`) is the right shape. File I/O mostly uses context managers and explicit encodings except `getmyancestors.py:197` (LOW-01) and GUI handle lifetimes (MED-07, LOW-09).
- **Concurrency:** Three divergent patterns for the same fan-out (asyncio-wrapped executor in `tree.py:655-663,763-773` and `gui.py:582-597`; plain `ThreadPoolExecutor` in `getmyancestors.py:289-304`) — consolidate on the executor (MED-05). Shared-state races detailed in MED-04. No graceful cancellation anywhere; GUI compensates with `os._exit` (LOW-06).
- **External Integrations / Adapters:** OAuth flow encapsulated in `Session.login` but leaks process-control (`sys.exit`, `webbrowser`, stdout `print`) into library code (HIGH-04). Response validation is trust-heavy (MED-06). The `DEFAULT_REDIRECT_URI` pointing at a third-party GitHub Pages page (`session.py:16`) is inherited upstream behavior worth a hard look: the auth redirect (carrying the OAuth code) goes to a page outside the project's control — document why it is trusted, or make it configurable-only.
- **Testing & Quality Tooling:** Known baseline (no lint/type/tests) acknowledged in §1; not re-reported. The eight deterministic checks in `.github/prompts/review-python-architecture.prompt.md:33-47` are the de-facto quality contract today — converting the greppable ones (1, 2, 3, 7) into a tiny pytest module would be the cheapest possible first test suite.

---

## 7. Duplication & Consolidation Report

| Pattern / Duplication | Locations | Proposed Canonical Home | Estimated Lines Removed |
| :--- | :--- | :--- | :--- |
| GEDCOM merge algorithm (incl. note renumbering) | `mergemyancestors.py:52-125`, `classes/gui.py:169-234` | `classes/tree.py` (or new `classes/merge.py`) — `merge_gedcom_into_tree()` | ~60 |
| Generation-download loop (ancestors/descendants/spouses) | `getmyancestors.py:239-304`, `classes/gui.py:537-606` | `Tree.download(...)` method with a progress callback | ~50 |
| `get_notes` / `get_contributors` (Indi vs Fam) | `classes/tree.py:395-402` vs `:572-582`; `:436-454` vs `:584-605` | Module-level helpers taking the URL + target set | ~30 |
| Translation lookup `_()` | `classes/session.py:235-241`, `classes/gui.py:30-34` | `classes/translation.py` — `translate(string, lang)` | ~10 |
| FS ID validation regex | `getmyancestors.py:171`, `classes/gui.py:299`, `classes/gui.py:544` | `classes/constants.py` — `is_valid_fid()` | ~6 |
| `parser.error = parser.exit` argparse hack | `getmyancestors.py:163-168`, `mergemyancestors.py:44-50` | Delete (use argparse's default error handling) | ~10 |

### Proposed Canonical Abstractions

- `merge_gedcom_into_tree(tree: Tree, ged: Gedcom, indi_counter: int, fam_counter: int) -> tuple[int, int]` in `classes/tree.py` — CLI semantics (union / set-if-absent, all five ordinances), consumed by `mergemyancestors.main` and `Merge.save`.
- `Tree.download(starting_fids, ascend, descend, spouses, ordinances, contributors, notes, concurrency, progress=lambda msg: None)` — owns the generation loops and the executor; CLI passes a stderr printer, GUI passes a queue-enqueuing callback (dovetails with the HIGH-02 fix).
- `translate(string: str, lang: str | None) -> str` in `classes/translation.py`; `Session._` and `gui._` become thin bindings.
- `is_valid_fid(fid: str) -> bool` in `classes/constants.py` using `re.fullmatch`.

---

## 8. Meta-Tooling & Instruction Update Recommendations

- **Promote deterministic checks 1, 2, 3, and 7 into a pytest module** (pure grep/AST assertions, no network) and add `uv run pytest` to `quality-gate.yml` — this converts the review prompt's mechanical checks into enforcement and seeds the missing test suite. Update the prompt's Known Baseline when that lands (it self-declares this supersession rule at `.github/prompts/review-python-architecture.prompt.md:30-31`).
- **Clarify check 3's pass condition** in `.github/prompts/review-python-architecture.prompt.md:42` to distinguish *request endpoints* from *GEDCOM X type-identifier URIs* — this review had to judge that boundary (constants.py's `gedcomx.org` keys are clearly fine; `tree.py:341` is a judgment call). One sentence removes the ambiguity for the next run.
- **Add a GUI-threading rule to `.github/agents/python-reviewer.agent.md`** (or a contributor doc): "widgets are touched only from the Tk main thread; workers communicate via queue + `after`" — check 5 detects violations, but active steering prevents them.
- **After fixing CRIT-01**, record the "no plaintext credential persistence" invariant in the instructions layer so the save-password feature doesn't regress.
- **`quality-gate.yml:41-43`** could additionally import `getmyancestors.classes.tree` and `classes.gedcom` explicitly (cheap, no Tk needed) so core-module import errors are caught even if the CLI modules stop importing them transitively.

---

## 9. Prioritized Dependency-Ordered Action Plan

1. **Phase 1: Blocking fixes**
   - CRIT-01 — keyring or removal of password persistence (`gui.py:504-512`).
   - HIGH-01 — retire the `"error"` sentinel; guard `gui.py:515`, `getmyancestors.py:235`, `tree.py:413,556-569`.
   - HIGH-04 — remove `sys.exit` from `Session.login` (prerequisite for any GUI login reliability work).
   - MED-01 — add `timeout=` to the four login calls (do together with MED-03's GUI `timeout=1` bump so the fix doesn't create 1-second login timeouts).
2. **Phase 2: Enforcement hardening**
   - CRIT-02 — failure counter + unconditional end-of-run warning + nonzero exit.
   - MED-06 — `.get()` guards on optional payload fields; harden the 403 JSON introspection.
   - LOW-01/LOW-02/LOW-03 — encoding, `fullmatch` ID helper, URL constants; then land the pytest module from §8 so checks 1-3/7 stay green.
3. **Phase 3: Reliability & concurrency**
   - MED-05 — replace asyncio with `ThreadPoolExecutor` (fix this **before** the HIGH-02 GUI rework: the GUI rework reorders call sequences, which is the exact condition that makes the latent `get_event_loop` defect live).
   - MED-04 — sources-registry lock; single-flight re-login.
   - MED-03 — split timeout/backoff; explain login failures.
   - HIGH-02 — queue + `after()` GUI threading model; fix the Quit button; daemonize workers (then LOW-06 falls out).
4. **Phase 4: Consolidation & refactoring**
   - HIGH-03 + MED-08 — extract the shared merge routine with CLI semantics and a `fid=None` policy.
   - MED-07 — Merge tab filename guard + path-based file list.
   - MED-02 — implement-or-delete `--delay`; default GUI rate limit.
   - §7 remaining consolidations (`Tree.download`, `translate`, argparse-hack removal).
5. **Phase 5: Non-blocking governance/documentation depth**
   - LOW-04/LOW-05 — relic removal, annotations, then mypy/ruff in CI.
   - §8 instruction updates; document merge input constraints and the redirect-URI trust decision; LOW-08 through LOW-12.

---

## 10. Preserved Strengths

- **Deliberate, documented dependency pinning**: the `requests-ratelimiter==0.7.0` pin ships with its rationale and an upgrade protocol in `pyproject.toml:22-34`; the fake-useragent purpose is likewise documented. This is exactly the pattern the skill asks reviewers to respect.
- **Honest, minimal CI**: `quality-gate.yml` states precisely what it can and cannot verify (`quality-gate.yml:3-7`) instead of pretending coverage — and `uv sync --locked` catching lockfile drift is a real gate.
- **Single transport chokepoint**: all API traffic goes through `Session.get_url` with per-request auth headers, batching (`MAX_PERSONS`), and status-code triage — the right structure to hang the Phase 1/2 fixes on.
- **Careful GEDCOM byte-length handling**: `cont()` (`tree.py:22-44`) does UTF-8-aware 255/248-byte line splitting with word-boundary backoff — subtle domain logic done correctly and worth a guard test.
- **Domain constants centralized**: fact-tag mappings and their reversals live in one place (`constants.py`) and are shared by writer and parser, keeping the round-trip consistent.
- **Round-trip design**: `gedcom.py` parses exactly what `tree.py` prints (including `_FSFTID` linkage), which is what makes a shared merge routine (HIGH-03) straightforward to extract.
