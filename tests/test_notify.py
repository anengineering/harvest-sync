import base64
from urllib.parse import parse_qs

import pytest
import requests
import responses

from harvest_sync import notify
from harvest_sync.config import EmailConfig

URL = "https://api.mailgun.net/v3/mail.example.com/messages"


def settings(**overrides):
    values = dict(
        to=("me@example.com", "you@example.com"),
        sender="sync@mail.example.com",
        domain="mail.example.com",
        api_key="secret-key",
    )
    values.update(overrides)
    return EmailConfig(**values)


@pytest.fixture
def api():
    with responses.RequestsMock() as mock:
        yield mock


@pytest.mark.parametrize(
    ("every_run", "on_problems", "problem", "expected"),
    [
        (True, True, False, True),
        (True, False, False, True),
        (False, True, False, False),
        (False, True, True, True),
        (False, False, True, False),
    ],
)
def test_should_send(every_run, on_problems, problem, expected):
    email = settings(send_every_run=every_run, send_on_problems=on_problems)
    assert notify.should_send(email, problem) is expected


def test_the_message_mailgun_receives(api):
    api.post(URL, json={"id": "<1@mail.example.com>", "message": "Queued. Thank you."})
    notify.send(settings(), "Subject", "Day  <Yours>\n")
    request = api.calls[0].request
    assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"api:secret-key").decode()
    fields = parse_qs(request.body)
    assert fields["from"] == ["sync@mail.example.com"]
    assert fields["to"] == ["me@example.com", "you@example.com"]
    assert fields["subject"] == ["Subject"]
    assert fields["text"] == ["Day  <Yours>\n"]
    assert fields["html"] == ["<pre>Day  &lt;Yours&gt;\n</pre>"]


def test_the_eu_region_uses_its_own_address(api):
    api.post("https://api.eu.mailgun.net/v3/mail.example.com/messages", json={"message": "Queued. Thank you."})
    notify.send(settings(region="eu"), "Subject", "Body")
    assert len(api.calls) == 1


def test_a_refusal_gives_mailgun_s_reason_without_the_key(api):
    api.post(URL, status=401, json={"message": "Forbidden"})
    with pytest.raises(notify.EmailError, match="Mailgun answered 401: Forbidden") as caught:
        notify.send(settings(), "Subject", "Body")
    assert "secret-key" not in str(caught.value)


def test_a_reply_that_isn_t_json_is_still_reported(api):
    api.post(URL, status=502, body="Bad Gateway")
    with pytest.raises(notify.EmailError, match="Mailgun answered 502: Bad Gateway"):
        notify.send(settings(), "Subject", "Body")


def test_being_unable_to_reach_mailgun_is_reported(api):
    api.post(URL, body=requests.ConnectionError("timed out"))
    with pytest.raises(notify.EmailError, match="couldn't reach Mailgun: timed out") as caught:
        notify.send(settings(), "Subject", "Body")
    assert "secret-key" not in str(caught.value)
