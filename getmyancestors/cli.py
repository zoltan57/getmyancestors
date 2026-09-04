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

    fetch_parser = subparsers.add_parser("fetch")
    fetch_parser.add_argument("--db", required=True)
    fetch_parser.add_argument("-u", "--username", required=True)
    fetch_parser.add_argument("-p", "--password")
    fetch_parser.add_argument("-i", "--ids", nargs="+", type=_fid)
    fetch_parser.add_argument("-a", "--ascend", type=int, default=4)
    fetch_parser.add_argument("-d", "--descend", type=int, default=0)
    fetch_parser.add_argument("-m", "--marriages", action="store_true")
    fetch_parser.add_argument("--no-sources", action="store_true")
    fetch_parser.add_argument("--no-notes", action="store_true")
    fetch_parser.add_argument("--no-memories", action="store_true")
    fetch_parser.add_argument("--rate-limit", type=int, default=2)
    fetch_parser.add_argument("--timeout", type=int, default=60)
    fetch_parser.add_argument("-v", "--verbose", action="store_true")

    load_parser = subparsers.add_parser("load")
    load_parser.add_argument("--db", required=True)
    load_parser.add_argument("--run", type=int)

    diff_parser = subparsers.add_parser("diff")
    diff_parser.add_argument("--db", required=True)
    diff_parser.add_argument("--runs", nargs=2, type=int, metavar=("OLD", "NEW"))

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
