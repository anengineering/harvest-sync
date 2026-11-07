from datetime import date

import pytest
import responses
from responses import matchers

from harvest_sync.harvest import (
    ACCOUNTS_URL,
    API_URL,
    AuthError,
    HarvestClient,
    HarvestError,
    OutcomeUnknown,
    Unprocessable,
)


@pytest.fixture
def api():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(sleeps):
    return HarvestClient("tok", 123, "harvest-sync (me@example.com)", sleep=sleeps.append)


def test_sends_the_required_headers(api, client):
    headers = {"Authorization": "Bearer tok", "Harvest-Account-Id": "123", "User-Agent": "harvest-sync (me@example.com)"}
    api.get(f"{API_URL}/users/me", json={"id": 7}, match=[matchers.header_matcher(headers)])
    assert client.me() == {"id": 7}


def test_follows_next_links(api, client):
    first = {"user_id": "5", "from": "2026-11-01", "to": "2026-11-14"}
    api.get(
        f"{API_URL}/time_entries",
        match=[matchers.query_param_matcher(first)],
        json={"time_entries": [{"id": 1}], "links": {"next": f"{API_URL}/time_entries?page=2&user_id=5"}},
    )
    api.get(
        f"{API_URL}/time_entries",
        match=[matchers.query_param_matcher({"page": "2", "user_id": "5"})],
        json={"time_entries": [{"id": 2}], "links": {"next": None}},
    )
    entries = client.time_entries(user_id=5, start=date(2026, 11, 1), end=date(2026, 11, 14))
    assert [e["id"] for e in entries] == [1, 2]


def test_waits_for_retry_after_when_rate_limited(api, client, sleeps):
    api.get(f"{API_URL}/users/me", status=429, headers={"Retry-After": "7"})
    api.get(f"{API_URL}/users/me", json={"id": 7})
    assert client.me() == {"id": 7}
    assert sleeps == [7.0]


def test_post_is_retried_after_rate_limit(api, client):
    api.post(f"{API_URL}/time_entries", status=429, headers={"Retry-After": "1"})
    api.post(f"{API_URL}/time_entries", status=201, json={"id": 9})
    assert client.create_time_entry({"hours": 1}) == {"id": 9}


def test_get_is_retried_after_a_server_error(api, client, sleeps):
    api.get(f"{API_URL}/users/me", status=502)
    api.get(f"{API_URL}/users/me", json={"id": 7})
    assert client.me() == {"id": 7}
    assert sleeps == [2]


def test_post_is_not_retried_after_a_server_error(api, client, sleeps):
    api.post(f"{API_URL}/time_entries", status=502)
    with pytest.raises(OutcomeUnknown):
        client.create_time_entry({"hours": 1})
    assert len(api.calls) == 1
    assert sleeps == []


def test_gives_up_after_repeated_server_errors(api, client):
    api.get(f"{API_URL}/users/me", status=503)
    with pytest.raises(HarvestError, match="503"):
        client.me()
    assert len(api.calls) == 5


def test_errors_carry_harvests_explanation(api, client):
    body = {"error": "invalid_token", "error_description": "The access token provided is expired, revoked, malformed or invalid."}
    api.get(f"{API_URL}/users/me", status=401, json=body)
    with pytest.raises(AuthError, match="revoked"):
        client.me()


def test_unprocessable(api, client):
    api.patch(f"{API_URL}/time_entries/5", status=422, json={"message": "Cannot update a locked time entry"})
    with pytest.raises(Unprocessable, match="locked"):
        client.update_time_entry(5, {"hours": 2})


def test_deleting_an_entry_that_is_already_gone_is_fine(api, client):
    api.delete(f"{API_URL}/time_entries/5", status=404, json={"message": "Not found"})
    client.delete_time_entry(5)


def test_company_is_none_when_not_allowed(api, client):
    api.get(f"{API_URL}/company", status=403, json={"message": "Forbidden"})
    assert client.company() is None


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, {"approvals": []}, True),
        (403, {"message": "Timesheet approvals are not enabled for this company"}, False),
        (403, {"message": "Forbidden"}, None),
    ],
)
def test_approvals_enabled(api, client, status, body, expected):
    api.get(f"{API_URL}/approvals", status=status, json=body)
    assert client.approvals_enabled() is expected


def test_submit_timesheet(api, client):
    expected = {"period_start": "2026-11-02", "period_end": "2026-11-08"}
    api.post(f"{API_URL}/approvals/submit", json={"status": "submitted"}, match=[matchers.json_params_matcher(expected)])
    assert client.submit_timesheet(date(2026, 11, 2), date(2026, 11, 8)) == {"status": "submitted"}


def test_accounts(api, client):
    api.get(ACCOUNTS_URL, json={"user": {"id": 1}, "accounts": [{"id": 123, "name": "My Company", "product": "harvest"}]})
    assert client.accounts()[0]["name"] == "My Company"
