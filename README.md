# getmyancestors

`getmyancestors` is a command-line tool for capturing FamilySearch tree data as raw JSON and loading that capture into relational SQLite tables for downstream transcription workflows.

## About this fork

This is a fork of [Linekio/getmyancestors](https://github.com/Linekio/getmyancestors), repurposed to a different form and purpose:

- **Original project:** a Tkinter GUI (`fstogedcom`) plus a CLI, both of which log into FamilySearch and export a family tree directly to a GEDCOM file, with a separate tool for merging multiple GEDCOM files together.
- **This fork:** a CLI-only tool that captures raw FamilySearch API responses into a SQLite database as the source of truth, then derives relational tables and run-to-run diffs from that capture — it no longer produces GEDCOM output at all.

Removed from the original:

- The Tkinter GUI (`fstogedcom`)
- GEDCOM file generation/export
- The GEDCOM merge tool

Added in this fork:

- `fetch`/`load`/`diff` CLI subcommands backed by a capture-then-derive SQLite pipeline
- Raw JSON capture (`api_response`) as the source of truth, with relational tables rebuilt from it on demand via `load`
- A `diff` command comparing two captured runs (appeared/disappeared FIDs, display-name changes)
- `.env`-based configuration and a `logging`-based diagnostics/verbose/logfile system (see [Logging](#logging) below)

If you're looking for the original GEDCOM/GUI tool, use the upstream repository linked above instead.

## Design rule

Raw capture rows in `api_response` are the source of truth. Relational tables are a derived view that can be dropped and rebuilt by running `load` again, without re-fetching.

## Install

From the repository root:

```bash
uv sync --locked
```

## Configuration (.env)

Every `--db`/`-u`/`-p` flag (plus a few `fetch` tuning options) can be supplied via
environment variables instead of the command line — useful for repeated or scripted
runs. Copy [`.env.example`](.env.example) to `.env` in the repository root and fill
in values; `.env` is loaded automatically and is already gitignored, so real
credentials never get committed. A CLI flag always overrides the matching
environment variable.

| Environment variable | Equivalent flag | Applies to |
| --- | --- | --- |
| `FS_USERNAME` | `-u`/`--username` | `fetch` |
| `FS_PASSWORD` | `-p`/`--password` | `fetch` |
| `FS_DB` | `--db` | `fetch`, `load`, `diff` |
| `FS_RATE_LIMIT` | `--rate-limit` | `fetch` |
| `FS_TIMEOUT` | `--timeout` | `fetch` |
| `FS_ASCEND` | `-a`/`--ascend` | `fetch` |
| `FS_DESCEND` | `-d`/`--descend` | `fetch` |

`--db` and `-u`/`--username` are the only values that must come from *somewhere*
(flag or environment variable) — omitting both is a bad-argument error (exit code
`2`). If `-p`/`--password` is not supplied either way, you're prompted via `getpass`
instead, which is the more secure option since it never touches shell history or a
plaintext file.

## Logging

Diagnostics are emitted with Python's `logging` module to stderr. By default, only
warning/error diagnostics are shown on the console.

With `-v`/`--verbose`, debug-level request diagnostics are also shown on the
console.

For `fetch`, a full debug-level log file is always written, regardless of `-v`.
By default it is auto-named next to `--db` (for example
`family.sqlite.20260904T170300Z.log`). Use `--logfile PATH` to override that
location and filename.

## Commands

All commands require `--db` and use the same SQLite file.

### fetch

Log in and capture FamilySearch API responses into the raw `api_response` table.

```bash
getmyancestors fetch --db family.sqlite -u USERNAME -i AAAA-001 -a 4 -d 1 -m -v
```

| Flag | Meaning |
| --- | --- |
| `--db PATH` | Path to the SQLite capture database (created if it doesn't exist). Required via flag or `FS_DB`. |
| `-u`, `--username USERNAME` | FamilySearch.org username. Required via flag or `FS_USERNAME`. |
| `-p`, `--password PASSWORD` | FamilySearch.org password. Falls back to `FS_PASSWORD`, then prompts if still unset (prompting is recommended — a flag value can leak into shell history/process listings). |
| `-i`, `--ids FID [FID ...]` | One or more starting FamilySearch person IDs (format `AAAA-001`). Default: the logged-in user's own person ID. |
| `-a`, `--ascend N` | Number of ancestor generations to walk upward from the starting person(s). Default: `4` (or `FS_ASCEND`). |
| `-d`, `--descend N` | Number of descendant generations to walk downward from the starting person(s). Default: `0` (or `FS_DESCEND`). |
| `-m`, `--marriages` | Also fetch each fetched person's spouse(s) and couple-relationship details (marriage/divorce facts and notes). Off by default. |
| `--no-sources` | Skip fetching each person's sources. Sources are fetched by default. |
| `--no-notes` | Skip fetching person/couple notes. Notes are fetched by default. |
| `--no-memories` | Skip fetching linked memories (photos/documents attached to a person). Memories are fetched by default. |
| `--rate-limit N` | Maximum FamilySearch API requests per second. Default: `2` (or `FS_RATE_LIMIT`). |
| `--timeout SECONDS` | Per-request HTTP timeout. Default: `60` (or `FS_TIMEOUT`). |
| `-v`, `--verbose` | Print each HTTP request to stderr as it happens (via `logging` at debug level). Failures are always reported regardless of this flag. |
| `--logfile PATH` | Write the full debug-level fetch log to this path instead of the automatic default (`<db>.<timestamp>.log` next to `--db`). A log file is always written for `fetch`, even without `-v`. |

Run `getmyancestors fetch --help` (or `load --help` / `diff --help`) for the same descriptions from the CLI itself.

### load

Load relational tables from a captured run (default: latest finished run).

```bash
getmyancestors load --db family.sqlite
getmyancestors load --db family.sqlite --run 12
```

### diff

Compare two captured runs (default: two most recent finished runs).

```bash
getmyancestors diff --db family.sqlite
getmyancestors diff --db family.sqlite --runs 11 12
```

## Schema at a glance

Capture layer:

- `fetch_run`: one row per fetch execution with counters and timings.
- `api_response`: one row per request, including kind/url/status and raw JSON body text.

Relational layer (rebuilt by `load`):

- `individual`, `name`
- `family`, `family_child`
- `event`
- `source`, `source_link`
- `note`
- `memory`

## Exit codes

- `0`: success
- `1`: unexpected error
- `2`: bad arguments or login failure
- `3`: fetch completed, but one or more requests permanently failed (captured data is incomplete)

## Refresh workflow

1. `getmyancestors fetch --db family.sqlite ...`
2. Check exit code (especially code `3` for incomplete capture).
3. `getmyancestors load --db family.sqlite`
4. `getmyancestors diff --db family.sqlite`
5. Review disappeared FIDs before updating any external document mappings keyed to FamilySearch IDs.
