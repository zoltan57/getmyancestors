"""Command-line interface for fetch/load/diff workflows."""

from __future__ import annotations

import argparse
import re
import sys
from getpass import getpass
from pathlib import Path

from getmyancestors.db import connect, init_schema
from getmyancestors.diff import diff
from getmyancestors.fetch import run_fetch
from getmyancestors.load import load
from getmyancestors.session import Session

FID_PATTERN = re.compile(r"[A-Z0-9]{4}-[A-Z0-9]{2,4}")


def _fid(value: str) -> str:
    """Validate one FamilySearch person ID."""
    if not FID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(f"Invalid FamilySearch ID: {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser."""
    parser = argparse.ArgumentParser(prog="getmyancestors")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser(
        "fetch", help="log in and capture raw FamilySearch API responses into the SQLite file"
    )
    fetch_parser.add_argument(
        "--db", required=True, help="path to the SQLite capture database (created if missing)"
    )
    fetch_parser.add_argument("-u", "--username", required=True, help="FamilySearch.org username")
    fetch_parser.add_argument(
        "-p",
        "--password",
        help="FamilySearch.org password; omit to be prompted (recommended, avoids shell history)",
    )
    fetch_parser.add_argument(
        "-i",
        "--ids",
        nargs="+",
        type=_fid,
        help="one or more starting FamilySearch person IDs, e.g. AAAA-001; "
        "default: the logged-in user's own person ID",
    )
    fetch_parser.add_argument(
        "-a",
        "--ascend",
        type=int,
        default=4,
        help="number of ancestor generations to walk upward from the starting person(s) (default: 4)",
    )
    fetch_parser.add_argument(
        "-d",
        "--descend",
        type=int,
        default=0,
        help="number of descendant generations to walk downward from the starting person(s) (default: 0)",
    )
    fetch_parser.add_argument(
        "-m",
        "--marriages",
        action="store_true",
        help="also fetch spouses and couple-relationship details (marriage facts/notes)",
    )
    fetch_parser.add_argument(
        "--no-sources", action="store_true", help="skip fetching each person's sources (fetched by default)"
    )
    fetch_parser.add_argument(
        "--no-notes", action="store_true", help="skip fetching person/couple notes (fetched by default)"
    )
    fetch_parser.add_argument(
        "--no-memories", action="store_true", help="skip fetching linked memories (fetched by default)"
    )
    fetch_parser.add_argument(
        "--rate-limit", type=int, default=2, help="maximum FamilySearch API requests per second (default: 2)"
    )
    fetch_parser.add_argument(
        "--timeout", type=int, default=60, help="per-request HTTP timeout in seconds (default: 60)"
    )
    fetch_parser.add_argument(
        "-v", "--verbose", action="store_true", help="print each HTTP request to stderr as it happens"
    )

    load_parser = subparsers.add_parser(
        "load", help="rebuild the relational tables from a captured run's raw JSON (no network access)"
    )
    load_parser.add_argument("--db", required=True, help="path to the SQLite capture database")
    load_parser.add_argument(
        "--run", type=int, help="run ID to load; default: the most recently finished fetch run"
    )

    diff_parser = subparsers.add_parser(
        "diff", help="report person IDs that appeared/disappeared between two fetch runs (no network access)"
    )
    diff_parser.add_argument("--db", required=True, help="path to the SQLite capture database")
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
    password = args.password if args.password is not None else getpass("Password: ")
    session = Session(
        username=args.username,
        password=password,
        verbose=args.verbose,
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


def main(argv: list[str] | None = None) -> int:
    """Parse CLI arguments and dispatch subcommands."""
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "fetch":
            return _run_fetch_command(args, ["getmyancestors", *argv])
        if args.command == "load":
            return _run_load_command(args)
        if args.command == "diff":
            return _run_diff_command(args)
        print(f"Unknown command: {args.command}", file=sys.stderr)
        return 2
    except SystemExit:
        raise
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
