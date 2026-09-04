"""Central logging configuration for the getmyancestors CLI."""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

PACKAGE_LOGGER_NAME = "getmyancestors"


def configure_logging(*, verbose: bool = False, logfile: str | Path | None = None) -> None:
    """Configure the shared getmyancestors logger's level and handlers.

    Safe to call more than once per process (e.g. across repeated ``main()``
    calls in a test session): any handlers this function previously attached
    are removed first, so handlers never accumulate and messages are never
    emitted more than once per call.
    """
    logger = logging.getLogger(PACKAGE_LOGGER_NAME)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    logger_level = logging.DEBUG if (verbose or logfile is not None) else logging.WARNING
    logger.setLevel(logger_level)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setLevel(logging.DEBUG if verbose else logging.WARNING)
    formatter = logging.Formatter(
        "[%(asctime)s] %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    if logfile is not None:
        file_handler = logging.FileHandler(logfile, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)


def default_logfile_path(db_path: str | Path) -> Path:
    """Return the auto-generated log file path for a fetch against ``db_path``.

    One file per invocation, named after the database file plus a
    filesystem-safe UTC timestamp, in the same directory as the database —
    e.g. ``family.sqlite`` -> ``family.sqlite.20260904T170300Z.log``. Colons
    are avoided (invalid in Windows filenames), unlike the ISO-8601 timestamps
    stored inside the database itself (see ``db.py``'s ``_utc_now_iso``).
    """
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = Path(db_path)
    return path.with_name(path.name + f".{timestamp}.log")
