# getmyancestors

`getmyancestors` is a command-line tool for capturing FamilySearch tree data as raw JSON and loading that capture into relational SQLite tables for downstream transcription workflows.

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
| `-v`, `--verbose` | Print each HTTP request to stderr as it happens. Failures are always reported regardless of this flag. |

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
