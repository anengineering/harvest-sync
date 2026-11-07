from datetime import date
from pathlib import Path

import pytest

from harvest_sync.config import ConfigError, load_config

EXAMPLE = Path(__file__).resolve().parents[1] / "config.example.toml"

BASE = """\
contact_email = "me@example.com"
timezone = "America/Los_Angeles"

[source]
account_id = 1
client = "Client Co"

[destination]
account_id = 2

[sync]
earliest_date = 2026-11-01

[[task_map]]
from = ["CA-DEV", "CA-OAK-DEV"]
to = "DEV"
default_note = "EE"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def tokens(monkeypatch):
    monkeypatch.setenv("HARVEST_SOURCE_TOKEN", "src-token")
    monkeypatch.setenv("HARVEST_DEST_TOKEN", "dst-token")


def test_example_config_loads(tokens):
    cfg = load_config(EXAMPLE)
    assert cfg.earliest_date == date(2026, 11, 1)
    assert cfg.timezone.key == "America/Los_Angeles"
    assert cfg.source_client == "Client Co"
    assert cfg.task_map[0].sources == ("Development", "Testing")
    assert (cfg.task_map[1].dest, cfg.task_map[1].default_note) == ("Travel", "Traveling")
    assert not cfg.task_map[0].location_prefix
    assert cfg.project_map[0].sources == ("Your project name",)
    assert (cfg.week_starts_on, cfg.submit_enabled) == (0, False)
    assert cfg.user_agent == "harvest-sync (you@example.com)"


def test_defaults(tmp_path, tokens):
    cfg = load_config(write(tmp_path, BASE))
    assert (cfg.propagate_deletes, cfg.max_deletes_per_run) == (True, 10)
    assert cfg.project_map == ()


EMAIL = """
[email]
to = "me@example.com"
from = "sync@mail.example.com"
mailgun_domain = "mail.example.com"
"""


@pytest.fixture
def mailgun_key(monkeypatch):
    monkeypatch.setenv("HARVEST_MAILGUN_API_KEY", "secret-key")


def test_no_email_section_means_no_email_and_no_key_needed(tmp_path, tokens):
    assert load_config(write(tmp_path, BASE)).email is None


def test_email_defaults_send_every_run_and_problems(tmp_path, tokens, mailgun_key):
    email = load_config(write(tmp_path, BASE + EMAIL)).email
    assert (email.send_every_run, email.send_on_problems) == (True, True)
    assert (email.to, email.sender, email.domain, email.region, email.api_key) == (
        ("me@example.com",), "sync@mail.example.com", "mail.example.com", "us", "secret-key",
    )
    assert "secret-key" not in repr(email)


def test_email_options(tmp_path, tokens, mailgun_key):
    extra = 'mailgun_region = "EU"\nsend_every_run = false\nsend_on_problems = false\n'
    email = load_config(write(tmp_path, BASE + EMAIL + extra)).email
    assert email.region == "eu"
    assert (email.send_every_run, email.send_on_problems) == (False, False)


def test_mailgun_key_comes_only_from_the_environment(tmp_path, tokens, mailgun_key, monkeypatch):
    with pytest.raises(ConfigError, match="unknown setting email.api_key"):
        load_config(write(tmp_path, BASE + EMAIL + 'api_key = "secret-key"\n'))
    monkeypatch.delenv("HARVEST_MAILGUN_API_KEY")
    with pytest.raises(ConfigError, match="HARVEST_MAILGUN_API_KEY"):
        load_config(write(tmp_path, BASE + EMAIL))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (EMAIL + 'mailgun_region = "mars"\n', "email.mailgun_region must be one of us, eu"),
        (EMAIL.replace("me@example.com", "nobody"), "must be an email address"),
        (EMAIL.replace('mailgun_domain = "mail.example.com"\n', ""), "missing setting email.mailgun_domain"),
        (EMAIL + "send_every_run = 1\n", "email.send_every_run must be true or false"),
    ],
)
def test_bad_email_settings(tmp_path, tokens, mailgun_key, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, BASE + text))


def test_default_note_is_tbd_unless_set(tmp_path, tokens):
    assert load_config(write(tmp_path, BASE)).task_map[0].default_note == "EE"
    assert load_config(write(tmp_path, BASE.replace('default_note = "EE"\n', ""))).task_map[0].default_note == "TBD"


def test_notes_can_be_made_optional(tmp_path, tokens):
    text = BASE.replace('default_note = "EE"', "require_note = false")
    assert load_config(write(tmp_path, text)).task_map[0].default_note is None


LOCATIONS_ON = BASE.replace("earliest_date = 2026-11-01", "earliest_date = 2026-11-01\nlocation_prefix = true")


def test_locations_are_off_by_default(tmp_path, tokens):
    (rule,) = load_config(write(tmp_path, BASE)).task_map
    assert rule.location("CA-DEV") is None


def test_locations_can_be_turned_on_and_task_names_needn_t_have_one(tmp_path, tokens):
    (rule,) = load_config(write(tmp_path, LOCATIONS_ON)).task_map
    assert (rule.location("CA-DEV"), rule.location("DEV")) == ("CA", None)


def test_a_rule_can_override_the_location_setting(tmp_path, tokens):
    extra = '\n[[task_map]]\nfrom = "{name}"\nto = "ADMIN"\nlocation_prefix = {value}\n'
    (_, office) = load_config(write(tmp_path, LOCATIONS_ON + extra.format(name="Non-Billable", value="false"))).task_map
    assert office.location("Non-Billable") is None
    (_, travel) = load_config(write(tmp_path, BASE + extra.format(name="OR-TRV", value="true"))).task_map
    assert travel.location("OR-TRV") == "OR"


def test_files_may_start_with_a_byte_order_mark(tmp_path):
    bom = b"\xef\xbb\xbf"
    (tmp_path / ".env").write_bytes(bom + b"HARVEST_SOURCE_TOKEN=from-file\nHARVEST_DEST_TOKEN=from-file\n")
    path = tmp_path / "config.toml"
    path.write_bytes(bom + BASE.encode("utf-8"))
    cfg = load_config(path)
    assert (cfg.contact_email, cfg.source_token, cfg.dest_token) == ("me@example.com", "from-file", "from-file")


def test_config_not_saved_as_utf8_gets_a_clear_error(tmp_path, tokens):
    path = tmp_path / "config.toml"
    path.write_text(BASE, encoding="utf-16")
    with pytest.raises(ConfigError, match="isn't saved as UTF-8"):
        load_config(path)


def test_env_file_not_saved_as_utf8_gets_a_clear_error(tmp_path, tokens):
    (tmp_path / ".env").write_text("HARVEST_DEST_TOKEN=from-file\n", encoding="utf-16")
    with pytest.raises(ConfigError, match="isn't saved as UTF-8"):
        load_config(write(tmp_path, BASE))


def test_earliest_date_may_be_quoted(tmp_path, tokens):
    cfg = load_config(write(tmp_path, BASE.replace("2026-11-01", '"2026-11-01"')))
    assert cfg.earliest_date == date(2026, 11, 1)


def test_tokens_come_from_env_file_but_environment_wins(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("HARVEST_SOURCE_TOKEN=from-file\nHARVEST_DEST_TOKEN=from-file\n", encoding="utf-8")
    monkeypatch.setenv("HARVEST_DEST_TOKEN", "from-env")
    cfg = load_config(write(tmp_path, BASE))
    assert (cfg.source_token, cfg.dest_token) == ("from-file", "from-env")


def test_missing_token(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_SOURCE_TOKEN", "src-token")
    with pytest.raises(ConfigError, match="HARVEST_DEST_TOKEN"):
        load_config(write(tmp_path, BASE))


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (BASE + '\n[submit]\nenable = true\n', "unknown setting submit.enable"),
        (BASE.replace("earliest_date = 2026-11-01", 'earliest_date = 2026-11-01\nmax_deletes_per_run = "10"'), "sync.max_deletes_per_run must be a whole number"),
        (BASE.replace("earliest_date = 2026-11-01", "earliest_date = 2026-11-01\npropagate_deletes = 1"), "sync.propagate_deletes must be true or false"),
        (BASE.replace("earliest_date = 2026-11-01", ""), "missing setting sync.earliest_date"),
        (BASE.replace("earliest_date = 2026-11-01", "earliest_date = 2026-11-01T08:00:00"), "without a time"),
        (BASE.replace('default_note = "EE"', 'default_note = "EE"\nlocation_prefix = "no"'), "task_map\\[1\\].location_prefix must be true or false"),
        (BASE.replace("earliest_date = 2026-11-01", 'earliest_date = 2026-11-01\nlocation_prefix = "no"'), "sync.location_prefix must be true or false"),
        (BASE.replace('default_note = "EE"', 'default_note = "EE"\nrequire_note = "no"'), "task_map\\[1\\].require_note must be true or false"),
        (BASE.replace('default_note = "EE"', 'default_note = "EE"\nrequire_note = false'), "task_map\\[1\\].default_note can't be used with require_note = false"),
        (BASE + '\n[[task_map]]\nfrom = "ca-dev"\nto = "TRV"\ndefault_note = "x"\n', "more than one"),
        (BASE.replace('default_note = "EE"', 'default_note = "  "'), "task_map\\[1\\].default_note can't be blank"),
        (BASE.replace("America/Los_Angeles", "Mars/Olympus_Mons"), "isn't a known time zone"),
        (BASE + '\n[submit]\nweek_starts_on = "funday"\n', "week_starts_on"),
        (BASE.replace("me@example.com", "nobody"), "email address"),
        (BASE.replace("account_id = 2", "account_id = 0"), "destination.account_id must be at least 1"),
        (BASE.replace("[[task_map]]", "[task_map]"), r"\[\[task_map\]\]"),
    ],
)
def test_invalid_config(tmp_path, tokens, text, message):
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))
