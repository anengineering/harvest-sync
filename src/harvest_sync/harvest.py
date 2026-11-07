"""A small client for the Harvest API v2: auth headers, paging, rate limits and retries."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import date
from typing import Any

import requests

API_URL = "https://api.harvestapp.com/v2"
ACCOUNTS_URL = "https://id.getharvest.com/api/v2/accounts"
TIMEOUT = (10, 60)  # seconds to connect, seconds to read
MAX_ATTEMPTS = 5
DEFAULT_RETRY_AFTER = 15.0
MAX_PAGES = 500

log = logging.getLogger(__name__)


class HarvestError(Exception):
    """Harvest returned an error, or couldn't be reached."""

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class AuthError(HarvestError):
    """401: the token was rejected (mistyped, revoked, or not valid for this account)."""


class Forbidden(HarvestError):
    """403: the token works, but this user isn't allowed to do that."""


class NotFound(HarvestError):
    """404."""


class Unprocessable(HarvestError):
    """422: Harvest refused the data; the message says why."""


class OutcomeUnknown(HarvestError):
    """A POST failed in a way that leaves it unclear whether Harvest applied it, so it isn't retried."""


class HarvestClient:
    def __init__(
        self,
        token: str,
        account_id: int,
        user_agent: str,
        *,
        name: str = "",
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.account_id = account_id
        self.name = name  # e.g. "their account", so error messages say which side failed
        self._session = session or requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {token}",
                "Harvest-Account-Id": str(account_id),
                "User-Agent": user_agent,
                "Accept": "application/json",
            }
        )
        self._sleep = sleep

    def me(self) -> dict:
        return self.request("GET", "/users/me")

    def accounts(self) -> list[dict]:
        """Every Harvest and Forecast account this token can reach."""
        return self.request("GET", ACCOUNTS_URL).get("accounts", [])

    def company(self) -> dict | None:
        """The account's company settings, or None when this user isn't allowed to read them."""
        try:
            return self.request("GET", "/company")
        except Forbidden:
            return None

    def approvals_enabled(self) -> bool | None:
        """Whether timesheet approval is switched on, or None when this user can't tell."""
        today = date.today().isoformat()
        try:
            self.request("GET", "/approvals", params={"period_start": today, "period_end": today})
            return True
        except Forbidden as e:
            return False if "not enabled" in str(e).lower() else None

    def project_assignments(self) -> list[dict]:
        return list(self.paginate("/users/me/project_assignments", "project_assignments"))

    def time_entries(
        self,
        *,
        user_id: int | None = None,
        client_id: int | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> list[dict]:
        params = {
            "user_id": user_id,
            "client_id": client_id,
            "from": start.isoformat() if start else None,
            "to": end.isoformat() if end else None,
        }
        return list(self.paginate("/time_entries", "time_entries", {k: v for k, v in params.items() if v is not None}))

    def create_time_entry(self, payload: dict) -> dict:
        return self.request("POST", "/time_entries", json=payload)

    def update_time_entry(self, entry_id: int, payload: dict) -> dict:
        return self.request("PATCH", f"/time_entries/{entry_id}", json=payload)

    def delete_time_entry(self, entry_id: int) -> None:
        try:
            self.request("DELETE", f"/time_entries/{entry_id}")
        except NotFound:
            pass  # already gone, e.g. a retried delete whose first attempt worked

    def submit_timesheet(self, start: date, end: date) -> dict:
        """Submit your own timesheet for approval (Harvest marks this endpoint beta)."""
        payload = {"period_start": start.isoformat(), "period_end": end.isoformat()}
        return self.request("POST", "/approvals/submit", json=payload)

    def paginate(self, path: str, key: str, params: dict | None = None) -> Iterator[dict]:
        """Yield every record, following the `links.next` URLs as Harvest recommends."""
        page = self.request("GET", path, params=params)
        for _ in range(MAX_PAGES):
            yield from page.get(key, [])
            next_url = (page.get("links") or {}).get("next")
            if not next_url:
                return
            page = self.request("GET", next_url)
        raise HarvestError(f"GET {path}: gave up after {MAX_PAGES} pages")

    def request(self, method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> Any:
        url = path if path.startswith("https://") else API_URL + path
        what = f"{method} {_short(url)}"
        if self.name:
            what = f"{self.name}: {what}"
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                response = self._session.request(method, url, params=params, json=json, timeout=TIMEOUT)
            except requests.RequestException as e:
                if method == "POST":
                    raise OutcomeUnknown(f"{what}: {e}") from e
                if last:
                    raise HarvestError(f"{what}: couldn't reach Harvest ({e})") from e
                self._backoff(what, attempt, str(e))
                continue

            status = response.status_code
            log.debug("%s -> %s", what, status)
            if status == 429:  # throttled requests aren't processed, so any method may be retried
                if last:
                    raise HarvestError(f"{what}: still rate-limited after {MAX_ATTEMPTS} attempts", status)
                wait = _retry_after(response)
                log.warning("Harvest rate limit reached; waiting %s s", wait)
                self._sleep(wait)
                continue
            if status >= 500:
                if method == "POST":
                    raise OutcomeUnknown(f"{what}: Harvest returned {status}", status)
                if last:
                    raise HarvestError(f"{what}: Harvest returned {status}", status)
                self._backoff(what, attempt, f"HTTP {status}")
                continue
            if status >= 400:
                raise _error(response, what)
            return response.json() if response.content else None
        raise AssertionError("unreachable")

    def _backoff(self, what: str, attempt: int, reason: str) -> None:
        wait = min(2**attempt, 30)
        log.warning("%s failed (%s); retrying in %s s", what, reason, wait)
        self._sleep(wait)


def _short(url: str) -> str:
    return url.removeprefix(API_URL).split("?")[0]


def _retry_after(response: requests.Response) -> float:
    try:
        return max(1.0, float(response.headers.get("Retry-After", "")))
    except ValueError:
        return DEFAULT_RETRY_AFTER


def _error(response: requests.Response, what: str) -> HarvestError:
    try:
        body = response.json()
    except ValueError:
        body = None
    detail = None
    if isinstance(body, dict):
        detail = body.get("message") or body.get("error_description") or body.get("error")
    detail = detail or response.reason or "error"
    kind = {401: AuthError, 403: Forbidden, 404: NotFound, 422: Unprocessable}.get(response.status_code, HarvestError)
    return kind(f"{what}: {response.status_code} {detail}", response.status_code)
