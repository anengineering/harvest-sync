import pytest


@pytest.fixture(autouse=True)
def clean_token_env(monkeypatch):
    """Start every test without tokens, and undo anything a test's .env file loads."""
    for var in ("HARVEST_SOURCE_TOKEN", "HARVEST_DEST_TOKEN", "HARVEST_MAILGUN_API_KEY"):
        monkeypatch.setenv(var, "")  # records the original value, so teardown restores it
        monkeypatch.delenv(var)
