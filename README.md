# getmyancestors

`getmyancestors` is a command-line tool for capturing FamilySearch tree data as raw JSON and loading that capture into relational SQLite tables for downstream transcription workflows.

## Design rule

Raw capture rows in `api_response` are the source of truth. Relational tables are a derived view that can be dropped and rebuilt by running `load` again, without re-fetching.

## Install

From the repository root:

```bash
uv sync --locked
```

## Commands

All commands require `--db` and use the same SQLite file.

### fetch

Capture FamilySearch API responses into raw tables.

```bash
getmyancestors fetch --db family.sqlite -u USERNAME -i AAAA-001 -a 4 -d 1 -m -v
```

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
