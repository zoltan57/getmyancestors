from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
import requests

from getmyancestors.session import Session


class DummyResponse:
    def __init__(
        self,
        *,
        status_code: int = 200,
        text: str = "",
        json_data: dict[str, Any] | None = None,
        url: str = "https://api.familysearch.org/platform/tree/persons?pids=AAAA-001",
        json_error: Exception | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.url = url
        self._json_error = json_error

    def json(self) -> dict[str, Any]:
        if self._json_error is not None:
            raise self._json_error
        return self._json_data or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"HTTP {self.status_code}")


def test_login_passes_timeout_on_all_http_calls(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    calls: list[tuple[str, str, int | None]] = []

    def fake_get(url: str, **kwargs: Any) -> DummyResponse:
        calls.append(("get", url, kwargs.get("timeout")))
        if "auth/familysearch/login" in url:
            session.cookies.set("XSRF-TOKEN", "xsrf")
            session.cookies.set("fssessionid", "cookie")
            return DummyResponse(status_code=200, text="ok", url=url)
        if "oauth2/v3/authorization" in url:
            return DummyResponse(
                status_code=200,
                text="ok",
                url="https://ident.familysearch.org/callback?code=AUTHCODE",
            )
        raise AssertionError(f"Unexpected GET URL: {url}")

    def fake_post(url: str, **kwargs: Any) -> DummyResponse:
        calls.append(("post", url, kwargs.get("timeout")))
        if url.endswith("/login"):
            return DummyResponse(status_code=200, text="ok", url=url)
        if url.endswith("/token"):
            return DummyResponse(status_code=200, json_data={"access_token": "token"}, url=url)
        raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(session, "get", fake_get)
    monkeypatch.setattr(session, "post", fake_post)
    monkeypatch.setattr(session, "set_current", lambda: None)

    session.login()

    assert len(calls) == 4
    assert all(timeout == session.timeout for _, _, timeout in calls)


def test_get_url_uses_exponential_backoff(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    attempts = {"count": 0}
    sleep_calls: list[int] = []

    def fake_get(*args: Any, **kwargs: Any) -> DummyResponse:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise requests.exceptions.ConnectionError("boom")
        return DummyResponse(status_code=200, text='{"ok": true}', json_data={"ok": True})

    monkeypatch.setattr(session, "get", fake_get)
    monkeypatch.setattr("getmyancestors.session.time.sleep", sleep_calls.append)

    data, raw_text, status = session.get_url("/platform/tree/persons?pids=AAAA-001")

    assert data == {"ok": True}
    assert raw_text == '{"ok": true}'
    assert status == 200
    assert sleep_calls == [1, 2]


def test_get_url_404_and_exhausted_retries_shape_and_failure_counter(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    monkeypatch.setattr(
        session,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=404, text="not found"),
    )
    data, raw_text, status = session.get_url("/platform/tree/persons/AAAA-001")
    assert (data, raw_text, status) == (None, None, 404)
    assert session.failed_requests == 0

    session.failed_requests = 0
    monkeypatch.setattr(
        session,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500, text="server error"),
    )
    monkeypatch.setattr("getmyancestors.session.time.sleep", lambda *args, **kwargs: None)
    data, raw_text, status = session.get_url("/platform/tree/persons/BBBB-002")
    assert (data, raw_text, status) == (None, None, 500)
    assert session.failed_requests == 1


def test_get_url_403_non_json_body_does_not_raise(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    monkeypatch.setattr(
        session,
        "get",
        lambda *args, **kwargs: DummyResponse(
            status_code=403,
            text="forbidden html",
            json_error=ValueError("not json"),
        ),
    )
    monkeypatch.setattr("getmyancestors.session.time.sleep", lambda *args, **kwargs: None)

    data, raw_text, status = session.get_url("/platform/tree/persons/CCCC-003")

    assert data is None
    assert raw_text is None
    assert status == 403
    assert session.failed_requests == 1


def test_get_url_401_triggers_one_relogin_then_retry(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    session = session_factory()
    responses = iter(
        [
            DummyResponse(status_code=401, text="unauthorized"),
            DummyResponse(status_code=200, text='{"users": []}', json_data={"users": []}),
        ]
    )
    login_calls: list[bool] = []

    def fake_login(*, refresh_only: bool = False) -> None:
        login_calls.append(refresh_only)

    monkeypatch.setattr(session, "get", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(session, "login", fake_login)

    data, raw_text, status = session.get_url("/platform/users/current")

    assert login_calls == [True]
    assert data == {"users": []}
    assert raw_text == '{"users": []}'
    assert status == 200


def test_login_refresh_only_skips_set_current(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test: a 401-triggered re-login must not re-fetch the current user.

    Before this fix, ``login()`` unconditionally called ``set_current()`` (which
    calls ``get_url()``) after every successful login, and ``get_url()`` called
    ``login()`` again on any 401. A persistently-401ing endpoint (e.g. if
    ``/platform/users/current`` itself always returned 401) caused unbounded
    mutual recursion between the two methods instead of a clean, bounded failure.
    """
    session = session_factory()
    set_current_calls = {"count": 0}

    def fake_set_current() -> None:
        set_current_calls["count"] += 1

    def fake_get(url: str, **kwargs: Any) -> DummyResponse:
        if "auth/familysearch/login" in url:
            session.cookies.set("XSRF-TOKEN", "xsrf")
            session.cookies.set("fssessionid", "cookie")
            return DummyResponse(status_code=200, text="ok", url=url)
        if "oauth2/v3/authorization" in url:
            return DummyResponse(
                status_code=200, text="ok", url="https://ident.familysearch.org/callback?code=AUTHCODE"
            )
        raise AssertionError(f"Unexpected GET URL: {url}")

    def fake_post(url: str, **kwargs: Any) -> DummyResponse:
        if url.endswith("/login"):
            return DummyResponse(status_code=200, text="ok", url=url)
        if url.endswith("/token"):
            return DummyResponse(status_code=200, json_data={"access_token": "token"}, url=url)
        raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(session, "get", fake_get)
    monkeypatch.setattr(session, "post", fake_post)
    monkeypatch.setattr(session, "set_current", fake_set_current)

    session.login(refresh_only=True)
    assert session.logged
    assert set_current_calls["count"] == 0

    session.login()
    assert set_current_calls["count"] == 1


def test_get_url_persistent_401_does_not_recurse_forever(
    session_factory: Callable[[], Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression test for the same recursion bug, exercised through get_url().

    Simulates an endpoint (e.g. ``/platform/users/current``) that always
    returns 401 while re-login always otherwise succeeds: this must terminate
    via get_url's own bounded retry loop rather than recursing without limit.
    """
    session = session_factory()
    login_flow_calls = {"count": 0}

    def fake_get(url: str, **kwargs: Any) -> DummyResponse:
        if "auth/familysearch/login" in url:
            session.cookies.set("XSRF-TOKEN", "xsrf")
            session.cookies.set("fssessionid", "cookie")
            login_flow_calls["count"] += 1
            return DummyResponse(status_code=200, text="ok", url=url)
        if "oauth2/v3/authorization" in url:
            return DummyResponse(
                status_code=200, text="ok", url="https://ident.familysearch.org/callback?code=AUTHCODE"
            )
        if "api.familysearch.org" in url:
            return DummyResponse(status_code=401, text="unauthorized", url=url)
        raise AssertionError(f"Unexpected GET URL: {url}")

    def fake_post(url: str, **kwargs: Any) -> DummyResponse:
        if url.endswith("/login"):
            return DummyResponse(status_code=200, text="ok", url=url)
        if url.endswith("/token"):
            return DummyResponse(status_code=200, json_data={"access_token": "token"}, url=url)
        raise AssertionError(f"Unexpected POST URL: {url}")

    monkeypatch.setattr(session, "get", fake_get)
    monkeypatch.setattr(session, "post", fake_post)

    data, raw_text, status = session.get_url("/platform/users/current")

    assert (data, raw_text, status) == (None, None, 401)
    assert session.failed_requests == 1
    assert session.logged
    # get_url retries a bounded number of times (its own attempt cap), each
    # triggering exactly one refresh-only re-login -- proving this terminates
    # instead of recursing without limit.
    assert 1 <= login_flow_calls["count"] <= 10


def test_get_url_debug_message_requires_debug_caplog_level(
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = session_factory()
    monkeypatch.setattr(
        session,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=200, text='{"ok": true}', json_data={"ok": True}),
    )

    session.get_url("/platform/tree/persons?pids=AAAA-001")
    assert "Status code: 200" not in caplog.text

    caplog.clear()
    caplog.set_level(logging.DEBUG, logger="getmyancestors")
    session.get_url("/platform/tree/persons?pids=AAAA-001")
    assert "Status code: 200" in caplog.text


def test_get_url_logs_max_retries_exceeded_warning(
    session_factory: Callable[[], Session],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    session = session_factory()
    monkeypatch.setattr(
        session,
        "get",
        lambda *args, **kwargs: DummyResponse(status_code=500, text="server error"),
    )
    monkeypatch.setattr("getmyancestors.session.time.sleep", lambda *args, **kwargs: None)

    data, raw_text, status = session.get_url("/platform/tree/persons/DDDD-004")

    assert (data, raw_text, status) == (None, None, 500)
    assert session.failed_requests == 1
    warning_records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any(
        record.message == "Max retries exceeded for /platform/tree/persons/DDDD-004 (failed_requests=1)"
        for record in warning_records
    )
