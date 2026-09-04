"""Command-line interface for fetch/load/diff workflows."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from getpass import getpass
from pathlib import Path

from dotenv import load_dotenv

from getmyancestors.db import connect, init_schema
from getmyancestors.diff import diff
from getmyancestors.fetch import run_fetch
from getmyancestors.load import load
from getmyancestors.logging_config import configure_logging, default_logfile_path
from getmyancestors.session import Session

FID_PATTERN = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{2,4}")
logger = logging.getLogger(__name__)


class EnvConfigError(Exception):
    """Raised when an environment-variable default cannot be parsed."""


def _fid(value: str) -> str:
    """Validate one FamilySearch person ID."""
    if not FID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"Invalid FamilySearch ID: {value}")
    return value


def _env_str(name: str) -> str | None:
    """Return a non-empty string environment variable value, or None."""
    value = os.environ.get(name)
    return value or None


def _env_int(name: str, fallback: int) -> int:
    """Return an int environment variable value, or ``fallback`` if unset."""
    value = os.environ.get(name)
    if not value:
        return fallback
    try:
        return int(value)
    except ValueError as error:
        raise EnvConfigError(f"Invalid integer in environment variable {name}: {value!r}") from error


def _env_bool(name: str) -> bool:
    """Return a bool environment variable value, defaulting to False when unset-ish."""
    value = os.environ.get(name)
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in ("", "0", "false"):
        return False
    return True


def _cap_max_persons(value: int) -> int:
    """Apply FamilySearch's documented max-persons ceiling."""
    if value > 200:
        logger.warning(f"--max-persons {value} exceeds FamilySearch's documented maximum of 200; using 200 instead.")
        return 200
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser."""
    parser = argparse.ArgumentParser(prog="getmyancestors")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch", help="log in and capture raw FamilySearch API responses into the SQLite file"
    )
    fetch_parser.add_argument(
        "--db",
        default=_env_str("FS_DB"),
        help="path to the SQLite capture database (created if missing); falls back to the FS_DB environment variable",
    )
    fetch_parser.add_argument(
        "-u",
        "--username",
        default=_env_str("FS_USERNAME"),
        help="FamilySearch.org username; falls back to the FS_USERNAME environment variable",
    )
    fetch_parser.add_argument(
        "-p",
        "--password",
        default=_env_str("FS_PASSWORD"),
        help="FamilySearch.org password; falls back to the FS_PASSWORD environment variable, "
        "then prompts if still unset (prompting is recommended, it avoids shell history)",
    )
    fetch_parser.add_argument(
        "-i",
        "--ids",
        nargs="+",
        type=_fid,
        help="one or more starting FamilySearch person IDs, e.g. AAAA-001; default: the logged-in user's own person ID",
    )
    fetch_parser.add_argument(
        "-a",
        "--ascend",
        type=int,
        default=_env_int("FS_ASCEND", 4),
        help="number of ancestor generations to walk upward from the starting person(s); "
        "falls back to the FS_ASCEND environment variable (default: 4)",
    )
    fetch_parser.add_argument(
        "-d",
        "--descend",
        type=int,
        default=_env_int("FS_DESCEND", 0),
        help="number of descendant generations to walk downward from the starting person(s); "
        "falls back to the FS_DESCEND environment variable (default: 0)",
    )
    fetch_parser.add_argument(
        "--max-persons",
        type=int,
        default=_cap_max_persons(_env_int("FS_MAX_PERSONS", 200)),
        help="maximum person IDs per /platform/tree/persons batch request; "
        "falls back to the FS_MAX_PERSONS environment variable (default: 200)",
    )
    fetch_parser.add_argument(
        "-m",
        "--marriages",
        action="store_true",
        help="also fetch spouses and couple-relationship details (marriage facts/notes)",
    )
    fetch_parser.add_argument(
        "--no-sources",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("FS_NO_SOURCES"),
        help="skip fetching each person's sources (fetched by default); "
        "falls back to the FS_NO_SOURCES environment variable",
    )
    fetch_parser.add_argument(
        "--no-notes",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("FS_NO_NOTES"),
        help="skip fetching person/couple notes (fetched by default); "
        "falls back to the FS_NO_NOTES environment variable",
    )
    fetch_parser.add_argument(
        "--no-memories",
        action=argparse.BooleanOptionalAction,
        default=_env_bool("FS_NO_MEMORIES"),
        help="skip fetching linked memories (fetched by default); "
        "falls back to the FS_NO_MEMORIES environment variable",
    )
    # No numeric rate limit is published by FamilySearch; 2/sec is a conservative
    # self-imposed default, not a documented safe value -- get_url() also honors
    # a 429 response's Retry-After header regardless of this setting. See
    # docs/decisions/2026-09-04-familysearch-api-limits.md (SS1).
    fetch_parser.add_argument(
        "--rate-limit",
        type=int,
        default=_env_int("FS_RATE_LIMIT", 2),
        help="maximum FamilySearch API requests per second (a conservative default, "
        "not a documented FamilySearch limit -- see docs/decisions/2026-09-04-familysearch-api-limits.md); "
        "falls back to the FS_RATE_LIMIT environment variable (default: 2)",
    )
    fetch_parser.add_argument(
        "--timeout",
        type=int,
        default=_env_int("FS_TIMEOUT", 60),
        help="per-request HTTP timeout in seconds; falls back to the FS_TIMEOUT environment variable (default: 60)",
    )
    fetch_parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each HTTP request to stderr as it happens"
    )
    fetch_parser.add_argument(
        "--logfile",
        help="write the full debug-level log to this file instead of the automatic default "
        "(a timestamped file next to --db); a log file is always written for fetch, "
        "regardless of -v/--verbose",
    )

    load_parser = subparsers.add_parser(
        "load", help="rebuild the relational tables from a captured run's raw JSON (no network access)"
    )
    load_parser.add_argument(
        "--db",
        default=_env_str("FS_DB"),
        help="path to the SQLite capture database; falls back to the FS_DB environment variable",
    )
    load_parser.add_argument("--run", type=int, help="run ID to load; default: the most recently finished fetch run")

    diff_parser = subparsers.add_parser(
        "diff", help="report person IDs that appeared/disappeared between two fetch runs (no network access)"
    )
    diff_parser.add_argument(
        "--db",
        default=_env_str("FS_DB"),
        help="path to the SQLite capture database; falls back to the FS_DB environment variable",
    )
    diff_parser.add_argument(
        "--runs",
        nargs=2,
        type=int,
        metavar=("OLD", "NEW"),
        help="the two run IDs to compare; default: the two most recent finished runs",
    )

    return parser


def _run_fetch_command(args: argparse.Namespace, argv: list[str]) -> int:
    """Execute the fetch subcommand."""
    args.max_persons = _cap_max_persons(int(args.max_persons))
    password = args.password if args.password is not None else getpass("Password: ")
    session = Session(
        username=args.username,
        password=password,
        timeout=args.timeout,
        rate_limit=args.rate_limit,
    )
    if not session.logged:
        return 2

    connection = connect(Path(args.db))
    try:
        init_schema(connection)
        args.password = password
        args.argv = argv
        return run_fetch(connection, session, args)
    finally:
        connection.close()


def _run_load_command(args: argparse.Namespace) -> int:
    """Execute the load subcommand."""
    connection = connect(Path(args.db))
    try:
        init_schema(connection)
        load(connection, args.run)
        return 0
    finally:
        connection.close()


def _run_diff_command(args: argparse.Namespace) -> int:
    """Execute the diff subcommand."""
    connection = connect(Path(args.db))
    try:
        init_schema(connection)
        if args.runs:
            old_run, new_run = args.runs
            diff(connection, old_run, new_run)
        else:
            diff(connection)
        return 0
    finally:
        connection.close()


def _validate_required(args: argparse.Namespace) -> str | None:
    """Return an error message if a required-but-env-fallback field is still missing."""
    if not getattr(args, "db", None):
        return "--db is required (or set the FS_DB environment variable)"
    if args.command == "fetch" and not args.username:
        return "-u/--username is required (or set the FS_USERNAME environment variable)"
    return None


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch subcommands."""
    load_dotenv()
    argv = list(sys.argv[1:] if argv is None else argv)

    try:
        parser = build_parser()
    except EnvConfigError as exc:
        propagate = logger.propagate
        logger.propagate = False
        try:
            logger.error(str(exc))
        finally:
            logger.propagate = propagate
        return 2

    args = parser.parse_args(argv)
    if args.command == "fetch" and not getattr(args, "logfile", None) and getattr(args, "db", None):
        args.logfile = default_logfile_path(args.db)
    configure_logging(verbose=getattr(args, "verbose", False), logfile=getattr(args, "logfile", None))
    if args.command == "fetch" and args.logfile:
        logger.warning(f"Writing full fetch log to {args.logfile}")

    error = _validate_required(args)
    if error:
        logger.error(error)
        return 2

    try:
        if args.command == "fetch":
            return _run_fetch_command(args, ["getmyancestors", *argv])
        if args.command == "load":
            return _run_load_command(args)
        if args.command == "diff":
            return _run_diff_command(args)
        logger.error(f"Unknown command: {args.command}")
        return 2
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 - top-level boundary: report and exit 1 rather than a traceback
        logger.error(str(exc))
        return 1
