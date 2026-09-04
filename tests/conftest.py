"""Test helpers for session unit tests.

Phase 2 uses ``Session.__new__(Session)`` plus manual attribute setup to avoid
running the real FamilySearch login flow in tests.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
import requests

from getmyancestors.db import connect, init_schema
from getmyancestors.session import DEFAULT_CLIENT_ID, DEFAULT_REDIRECT_URI, Session


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
        session.verbose = False
        session.logfile = None
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
