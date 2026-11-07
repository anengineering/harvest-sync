"""Emails the result of a scheduled run through Mailgun's HTTP API."""

from __future__ import annotations

import html

import requests

from harvest_sync.config import EmailConfig

API_URLS = {"us": "https://api.mailgun.net", "eu": "https://api.eu.mailgun.net"}
TIMEOUT = (10, 30)  # seconds to connect, and to wait for a reply


class EmailError(Exception):
    """Mailgun didn't accept the message."""


def should_send(email: EmailConfig, problem: bool) -> bool:
    return email.send_every_run or (email.send_on_problems and problem)


def send(email: EmailConfig, subject: str, body: str) -> None:
    payload = {
        "from": email.sender,
        "to": list(email.to),
        "subject": subject,
        "text": body,
        "html": f"<pre>{html.escape(body)}</pre>",  # keeps the tables lined up
    }
    url = f"{API_URLS[email.region]}/v3/{email.domain}/messages"
    try:
        response = requests.post(url, auth=("api", email.api_key), data=payload, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise EmailError(f"couldn't reach Mailgun: {e}") from e
    if not response.ok:
        raise EmailError(f"Mailgun answered {response.status_code}: {_reason(response)}")


def _reason(response: requests.Response) -> str:
    try:
        return response.json()["message"]
    except (ValueError, KeyError, TypeError):
        return response.text[:200] or response.reason
