"""Test helpers for session unit tests.

Phase 2 uses ``Session.__new__(Session)`` plus manual attribute setup to avoid
running the real FamilySearch login flow in tests.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Generator
from pathlib import Path

import pytest
import requests

from getmyancestors.db import connect, init_schema
from getmyancestors.session import DEFAULT_CLIENT_ID, DEFAULT_REDIRECT_URI, Session


@pytest.fixture(autouse=True)
def _reset_getmyancestors_logger() -> Generator[None, None, None]:
    logger = logging.getLogger("getmyancestors")
    yield
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
    logger.setLevel(logging.NOTSET)


@pytest.fixture
def session_factory() -> Callable[[], Session]:
    """Return a factory for creating a minimally initialized Session instance."""

    def _make_session() -> Session:
        session = Session.__new__(Session)
        requests.Session.__init__(session)
        session.username = "user"
        session.password = "pass"
        session.client_id = DEFAULT_CLIENT_ID
        session.redirect_uri = DEFAULT_REDIRECT_URI
        session.timeout = 7
        session.fid = None
        session.lang = None
        session.display_name = None
        session.counter = 0
        session.failed_requests = 0
        session.headers = {"User-Agent": "pytest-agent"}
        return session

    return _make_session


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    """Return a temporary SQLite database path."""
    return tmp_path / "test.sqlite3"


@pytest.fixture
def initialized_db(temp_db_path: Path):
    """Return a connected DB with schema initialized."""
    conn = connect(temp_db_path)
    init_schema(conn)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def fixtures_dir() -> Path:
    """Return the test fixture directory path."""
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def load_fixture(fixtures_dir: Path) -> Callable[[str], dict]:
    """Load one JSON fixture by filename."""

    def _load(name: str) -> dict:
        return json.loads((fixtures_dir / name).read_text(encoding="utf-8"))

    return _load
