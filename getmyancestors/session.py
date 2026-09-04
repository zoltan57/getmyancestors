"""FamilySearch HTTP session and OAuth login workflow.

The login flow performs four HTTP requests to establish authentication and then
loads current-user metadata. Data requests are made against the FamilySearch
API host with bounded retries and explicit timeout handling.
"""

from __future__ import annotations

import logging
import time
import webbrowser
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests
from fake_useragent import UserAgent
from requests_ratelimiter import LimiterAdapter

DEFAULT_CLIENT_ID = "a02j000000KTRjpAAH"
DEFAULT_REDIRECT_URI = "https://misbach.github.io/fs-auth/index_raw.html"
# Cap for a single Retry-After wait: FamilySearch's throttling is a shared,
# per-user processing-time budget with no published numeric quota (see
# https://developers.familysearch.org/main/docs/throttling) -- Retry-After is
# the sanctioned mechanism for backing off, but an unreasonably large value
# (bad server response, clock skew) shouldn't stall a single request forever.
MAX_RETRY_AFTER_SECONDS = 300.0
logger = logging.getLogger(__name__)


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header value into seconds to wait, or None if absent/invalid.

    Per RFC 9110 SS10.2.3, Retry-After is either an integer number of seconds
    or an HTTP-date. FamilySearch's throttling docs specify a 429 response
    always carries this header; this returns None only when it's missing or
    unparseable, so the caller can fall back to exponential backoff.
    """
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=UTC)
    return max((retry_at - datetime.now(UTC)).total_seconds(), 0.0)


class Session(requests.Session):
    """FamilySearch session with login and JSON request helpers."""

    def __init__(
        self,
        username: str,
        password: str,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        timeout: int = 60,
        rate_limit: int = 2,
    ) -> None:
        """Initialize the session and attempt login immediately."""
        super().__init__()
        self.username = username
        self.password = password
        self.client_id = client_id or DEFAULT_CLIENT_ID
        self.redirect_uri = redirect_uri or DEFAULT_REDIRECT_URI
        self.timeout = timeout
        self.fid: str | None = None
        self.lang: str | None = None
        self.display_name: str | None = None
        self.counter = 0
        self.failed_requests: int = 0
        self.headers = {"User-Agent": UserAgent().firefox}

        adapter = LimiterAdapter(per_second=rate_limit)
        self.mount("http://", adapter)
        self.mount("https://", adapter)

        self.login()

    @property
    def logged(self) -> bool:
        """Return whether the session cookie indicates a logged-in user."""
        return bool(self.cookies.get("fssessionid"))

    def login(self, *, refresh_only: bool = False) -> None:
        """Run the FamilySearch OAuth login sequence.

        ``refresh_only=True`` is used when ``get_url`` calls back into this
        method to re-establish an expired session mid-request (on a 401): it
        skips the post-login ``set_current()`` call, which itself calls
        ``get_url`` — without this, a persistently-401ing endpoint (e.g. if
        ``/platform/users/current`` itself keeps returning 401) would cause
        unbounded mutual recursion between ``login`` and ``get_url`` instead
        of failing cleanly once ``get_url``'s own retry limit is reached.
        """
        for attempt in range(5):
            try:
                url = "https://www.familysearch.org/auth/familysearch/login"
                logger.debug("Downloading: " + url)
                self.get(url, headers=self.headers, timeout=self.timeout)
                xsrf = self.cookies["XSRF-TOKEN"]

                url = "https://ident.familysearch.org/login"
                logger.debug("Logging in: " + url)
                res = self.post(
                    url,
                    data={
                        "_csrf": xsrf,
                        "username": self.username,
                        "password": self.password,
                    },
                    headers=self.headers,
                    timeout=self.timeout,
                )
                res.raise_for_status()

                url = "https://ident.familysearch.org/cis-web/oauth2/v3/authorization"
                params = {
                    "response_type": "code",
                    "scope": "openid profile email qualifies_for_affiliate_account country",
                    "client_id": self.client_id,
                    "redirect_uri": self.redirect_uri,
                    "username": self.username,
                }
                logger.debug("Getting an authorization code: " + url)
                response = self.get(
                    url,
                    headers=self.headers,
                    params=params,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                codes = parse_qs(urlparse(response.url).query).get("code")
                if not codes:
                    webbrowser.open(response.url)
                    logger.warning("Please log in to the web page that just opened and try again.")
                    logger.warning("Login flow did not return an OAuth code.")
                    return
                code = codes[0]

                url = "https://ident.familysearch.org/cis-web/oauth2/v3/token"
                logger.debug("Exchanging for an access token: " + url)
                res = self.post(
                    url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self.client_id,
                        "code": code,
                        "redirect_uri": self.redirect_uri,
                    },
                    headers=self.headers,
                    timeout=self.timeout,
                )

                try:
                    data = res.json()
                except ValueError:
                    logger.warning("Invalid auth request")
                    continue

                if "access_token" not in data:
                    logger.warning(res.text)
                    continue
                self.headers.update({"Authorization": f"Bearer {data['access_token']}"})

            except requests.exceptions.ReadTimeout:
                logger.warning("Read timed out")
                continue
            except requests.exceptions.ConnectionError:
                logger.warning("Connection aborted")
                time.sleep(min(2**attempt, 60))
                continue
            except requests.exceptions.HTTPError:
                logger.warning("HTTPError")
                time.sleep(min(2**attempt, 60))
                continue
            except KeyError:
                logger.warning("KeyError")
                time.sleep(min(2**attempt, 60))
                continue
            except ValueError:
                logger.warning("ValueError")
                time.sleep(min(2**attempt, 60))
                continue
            if self.logged:
                if not refresh_only:
                    self.set_current()
                break
        if not self.logged:
            logger.warning("Login failed after retries; session cookie was not established.")

    def get_url(
        self, url: str, headers: dict[str, str] | None = None
    ) -> tuple[dict[str, Any] | None, str | None, int | None]:
        """Get one API URL and return parsed JSON, raw body, and HTTP status."""
        self.counter += 1
        request_headers = {"Accept": "application/x-gedcomx-v1+json"}
        if headers:
            request_headers.update(headers)
        request_headers.update(self.headers)
        base = "https://api.familysearch.org"
        last_status: int | None = None

        for attempt in range(10):
            try:
                logger.debug("Downloading: " + url)
                response = self.get(
                    base + url,
                    timeout=self.timeout,
                    headers=request_headers,
                )
            except requests.exceptions.ReadTimeout:
                logger.warning("Read timed out")
                continue
            except requests.exceptions.ConnectionError:
                logger.warning("Connection aborted")
                time.sleep(min(2**attempt, 60))
                continue

            last_status = response.status_code
            logger.debug(f"Status code: {response.status_code}")
            if response.status_code == 204:
                return None, None, response.status_code
            if response.status_code in {404, 405, 410}:
                logger.warning(url)
                return None, None, response.status_code
            if response.status_code == 500:
                logger.warning("HTTP 500 from " + url)
                time.sleep(min(2**attempt, 60))
                continue
            if response.status_code == 401:
                self.login(refresh_only=True)
                continue
            if response.status_code == 429:
                # Sanctioned mechanism per FamilySearch's throttling docs
                # (https://developers.familysearch.org/main/docs/throttling):
                # honor Retry-After rather than guessing at a request rate --
                # the throttle is a shared per-user processing-time budget
                # with no published numeric quota, so this is the only
                # documented way to know how long to wait.
                wait = _parse_retry_after(response.headers.get("Retry-After"))
                if wait is None:
                    wait = min(2**attempt, 60)
                else:
                    wait = min(wait, MAX_RETRY_AFTER_SECONDS)
                logger.warning(f"Rate limited (429) for {url}; waiting {wait:.0f}s before retry")
                time.sleep(wait)
                continue

            try:
                response.raise_for_status()
            except requests.exceptions.HTTPError:
                logger.warning("HTTPError")
                if response.status_code == 403:
                    try:
                        message = response.json()["errors"][0]["message"] or ""
                        logger.warning(f"code 403 from {url} {message}")
                        return None, None, response.status_code
                    except (ValueError, KeyError, IndexError):
                        time.sleep(min(2**attempt, 60))
                        continue
                time.sleep(min(2**attempt, 60))
                continue

            raw_text = response.text
            try:
                return response.json(), raw_text, response.status_code
            except ValueError as error:
                logger.warning(f"corrupted file from {url}, error: {error}")
                return None, raw_text, response.status_code

        self.failed_requests += 1
        logger.warning(f"Max retries exceeded for {url} (failed_requests={self.failed_requests})")
        return None, None, last_status

    def set_current(self) -> None:
        """Retrieve FamilySearch current user ID, name, and language."""
        url = "/platform/users/current"
        data, _, _ = self.get_url(url)
        if data:
            self.fid = data["users"][0]["personId"]
            self.lang = data["users"][0]["preferredLanguage"]
            self.display_name = data["users"][0]["displayName"]
