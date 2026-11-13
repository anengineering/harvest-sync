import tomllib
from datetime import date
from pathlib import Path

import pytest
from factories import CONFIG_TOML, DEST_ASSIGNMENTS, SOURCE_ASSIGNMENTS, assignment, source_entry
from fakes import FakeHarvest

from harvest_sync import __version__, cli
from harvest_sync.harvest import AuthError


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    monkeypatch.setenv("HARVEST_SOURCE_TOKEN", "src-token")
    monkeypatch.setenv("HARVEST_DEST_TOKEN", "dst-token")
    monkeypatch.setenv("HARVEST_MAILGUN_API_KEY", "secret-key")
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TOML, encoding="utf-8")
    return path


@pytest.fixture
def sides(monkeypatch):
    source = FakeHarvest(account_id=1, assignments=SOURCE_ASSIGNMENTS, account_name="My Company")
    dest = FakeHarvest(account_id=2, assignments=DEST_ASSIGNMENTS, roles=("member",), account_name="Client Co")
    monkeypatch.setattr(cli, "make_clients", lambda cfg: (source, dest))
    return source, dest


def run(config_file, *args):
    return cli.main(["--config", str(config_file), *args])


def test_output_starts_with_the_name_and_version(config_file, sides, capsys):
    run(config_file, "check")
    assert capsys.readouterr().out.startswith(f"\nharvest-sync {__version__}\n\n")


def test_a_run_that_fails_still_shows_the_name_and_version(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "check"]) == cli.EXIT_FAILED
    assert capsys.readouterr().out == f"\nharvest-sync {__version__}\n\n"


def test_version_comes_from_pyproject():
    with (Path(__file__).parents[1] / "pyproject.toml").open("rb") as f:
        assert __version__ == tomllib.load(f)["project"]["version"]


def test_check_passes(config_file, sides, capsys):
    assert run(config_file, "check") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert 'token works: Test User (member) in "Client Co" (account 2)' in out
    assert 'client "Client Co" (ID 10)' in out
    assert "your CA-DEV, OR-DEV, CA-OAK-DEV -> their DEV (blank notes become CA-EE, OR-EE, CA-OAK-EE)" in out
    assert "assuming durations" in out
    assert out.rstrip().endswith("Ready to sync.")


def test_check_shows_what_blank_notes_become(config_file, sides, capsys):
    text = CONFIG_TOML.replace('default_note = "EE"', "require_note = false").replace('default_note = "Traveling"\n', "")
    config_file.write_text(text, encoding="utf-8")
    assert run(config_file, "check") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "their DEV (blank notes stay blank)" in out
    assert "their TRV (blank notes become CA-TBD, OR-TBD)" in out


def test_check_with_locations_off(config_file, sides, capsys):
    config_file.write_text(CONFIG_TOML.replace("location_prefix = true\n", ""), encoding="utf-8")
    assert run(config_file, "check") == cli.EXIT_OK
    assert "their DEV (blank notes become EE)" in capsys.readouterr().out


def test_check_flags_a_missing_task_on_their_side(config_file, sides, capsys):
    sides[1].assignments = [assignment(900, "Engineering Support", {"DEV": 91}, client_id=90)]
    assert run(config_file, "check") == cli.EXIT_ATTENTION
    assert 'no active task "TRV"' in capsys.readouterr().out


def test_check_accepts_start_and_end_times_on_your_side(config_file, sides, capsys):
    sides[0].company_settings = {"wants_timestamp_timers": True, "week_start_day": "Monday"}
    assert run(config_file, "check") == cli.EXIT_OK
    assert "time tracking: start and end times; their copies get just the hours" in capsys.readouterr().out


def test_check_flags_start_and_end_time_tracking_on_their_side(config_file, sides, capsys):
    sides[1].company_settings = {"wants_timestamp_timers": True, "week_start_day": "Monday"}
    assert run(config_file, "check") == cli.EXIT_ATTENTION
    assert "start and end times" in capsys.readouterr().out


def test_check_reports_a_rejected_token(config_file, sides, capsys):
    sides[0].fail["me"] = AuthError("your account: GET /users/me: 401 invalid_token", 401)
    assert run(config_file, "check") == cli.EXIT_ATTENTION
    out = capsys.readouterr().out
    assert "401 invalid_token" in out
    assert "this token can reach: My Company (1)" in out
    assert "skipped until both tokens work" in out


def test_list(config_file, sides, capsys):
    assert run(config_file, "list") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert 'Your account: "My Company" (account 1)' in out
    assert "Client Co - Retrofit [code CC-R]" in out
    assert "tasks: ADMIN, CA-DEV, CA-OAK-DEV, CA-TRV, OR-DEV, OR-TRV" in out
    assert "tasks: DEV, TRV" in out


def test_sync_previews_then_applies(config_file, sides, capsys):
    source, dest = sides
    source.entries = {1: source_entry(1, notes="Reviewed schematics")}
    window = ["--from", "2026-11-01", "--to", "2026-11-14"]

    assert run(config_file, "sync", *window) == cli.EXIT_OK
    assert "preview only" in capsys.readouterr().out
    assert dest.writes == []

    assert run(config_file, "sync", *window, "--apply") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "changes applied" in out and "Both sides match." in out
    assert [e["notes"] for e in dest.entries.values()] == ["CA-Reviewed schematics"]

    assert run(config_file, "sync", *window, "--apply") == cli.EXIT_OK
    out = capsys.readouterr().out
    assert "2026-11-14: no changes made" in out and "Summary: 1 unchanged. Both sides match." in out


def test_sync_with_problems_exits_1(config_file, sides, capsys):
    sides[0].entries = {1: source_entry(1, task="ADMIN")}
    assert run(config_file, "sync", "--from", "2026-11-01", "--to", "2026-11-14") == cli.EXIT_ATTENTION


def test_submit_apply_needs_it_switched_on(config_file, sides, capsys):
    assert run(config_file, "submit", "--apply") == cli.EXIT_FAILED
    assert "submitting is switched off" in capsys.readouterr().err


def test_submit_preview(config_file, sides, capsys, monkeypatch):
    monkeypatch.setattr(cli, "today", lambda cfg: date(2026, 11, 10))
    sides[0].entries = {1: source_entry(1, "2026-11-03")}
    assert run(config_file, "submit", "--week-of", "2026-11-04") == cli.EXIT_OK
    assert "Would submit 1.00 h for 2026-11-02 to 2026-11-08" in capsys.readouterr().out
    assert sides[1].writes == []


EMAIL = '\n[email]\nto = "me@example.com"\nfrom = "sync@mail.example.com"\nmailgun_domain = "mail.example.com"\n'
WINDOW = ["--from", "2026-11-01", "--to", "2026-11-14"]


@pytest.fixture
def emails(monkeypatch):
    sent = []
    monkeypatch.setattr(cli.notify, "send", lambda email, subject, body: sent.append((subject, body)))
    return sent


def with_email(config_file, extra=""):
    config_file.write_text(CONFIG_TOML + EMAIL + extra, encoding="utf-8")


def test_every_run_is_emailed_by_default(config_file, sides, emails, capsys):
    with_email(config_file)
    sides[0].entries = {1: source_entry(1)}
    assert run(config_file, "sync", *WINDOW, "--apply", "--email") == cli.EXIT_OK
    ((subject, body),) = emails
    assert subject == "harvest-sync sync OK: Client Co"
    assert body.startswith(f"harvest-sync {__version__}\n\n")
    assert "Summary: 1 created. Both sides match." in body


def test_without_the_flag_nothing_is_emailed(config_file, sides, emails, capsys):
    with_email(config_file)
    assert run(config_file, "sync", *WINDOW) == cli.EXIT_OK
    assert emails == []


def test_with_every_run_off_a_clean_run_sends_nothing(config_file, sides, emails, capsys):
    with_email(config_file, "send_every_run = false\n")
    sides[0].entries = {1: source_entry(1)}
    assert run(config_file, "sync", *WINDOW, "--apply", "--email") == cli.EXIT_OK
    assert emails == []


def test_with_every_run_off_a_problem_is_still_emailed(config_file, sides, emails, capsys):
    with_email(config_file, "send_every_run = false\n")
    sides[0].entries = {1: source_entry(1, task="ADMIN")}
    assert run(config_file, "sync", *WINDOW, "--apply", "--email") == cli.EXIT_ATTENTION
    ((subject, body),) = emails
    assert subject == "harvest-sync sync needs attention: Client Co"
    assert 'your task "ADMIN" isn\'t in task_map' in body


def test_with_both_off_even_a_problem_sends_nothing(config_file, sides, emails, capsys):
    with_email(config_file, "send_every_run = false\nsend_on_problems = false\n")
    sides[0].entries = {1: source_entry(1, task="ADMIN")}
    assert run(config_file, "sync", *WINDOW, "--apply", "--email") == cli.EXIT_ATTENTION
    assert emails == []


def test_a_run_that_fails_is_emailed(config_file, sides, emails, capsys):
    with_email(config_file, "send_every_run = false\n")
    sides[0].fail["me"] = AuthError("your account: GET /users/me: 401 invalid_token", 401)
    assert run(config_file, "sync", *WINDOW, "--apply", "--email") == cli.EXIT_FAILED
    ((subject, body),) = emails
    assert subject == "harvest-sync sync FAILED: Client Co"
    assert "401 invalid_token" in body and "tokens are managed at" in body


def test_a_bug_is_emailed_when_unattended_and_raised_otherwise(config_file, sides, emails, monkeypatch, capsys):
    with_email(config_file, "send_every_run = false\n")

    def broken(*args):
        raise KeyError("unexpected")

    monkeypatch.setattr(cli, "sync_window", broken)
    assert run(config_file, "sync", *WINDOW, "--email") == cli.EXIT_FAILED
    ((subject, body),) = emails
    assert "FAILED" in subject and "KeyError" in body
    with pytest.raises(KeyError):
        run(config_file, "sync", *WINDOW)


def test_failing_to_send_is_reported_but_doesn_t_hide_the_result(config_file, sides, monkeypatch, capsys):
    with_email(config_file)

    def refuse(*args):
        raise cli.notify.EmailError("connection timed out")

    monkeypatch.setattr(cli.notify, "send", refuse)
    sides[0].entries = {1: source_entry(1)}
    assert run(config_file, "sync", *WINDOW, "--apply", "--email") == cli.EXIT_ATTENTION
    captured = capsys.readouterr()
    assert "couldn't send the email: connection timed out" in captured.err
    assert "Summary: 1 created." in captured.out
    assert len(sides[1].entries) == 1


def test_the_flag_does_nothing_without_an_email_section(config_file, sides, emails, capsys):
    assert run(config_file, "sync", *WINDOW, "--email") == cli.EXIT_OK
    assert emails == []


def test_a_config_that_won_t_load_can_t_be_emailed_and_says_so(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "sync", "--email"]) == cli.EXIT_FAILED
    assert "couldn't email this failure" in capsys.readouterr().err


def test_test_email_command(config_file, sides, emails, capsys):
    with_email(config_file)
    assert run(config_file, "test-email") == cli.EXIT_OK
    ((subject, _),) = emails
    assert subject == "harvest-sync test email"
    assert "Sent a test email to me@example.com." in capsys.readouterr().out


def test_test_email_needs_settings_and_reports_failure(config_file, sides, monkeypatch, capsys):
    assert run(config_file, "test-email") == cli.EXIT_FAILED
    assert "no [email] section" in capsys.readouterr().err

    def refuse(*args):
        raise cli.notify.EmailError("Mailgun answered 401: Forbidden")

    with_email(config_file)
    monkeypatch.setattr(cli.notify, "send", refuse)
    assert run(config_file, "test-email") == cli.EXIT_FAILED
    assert "couldn't send the test email: Mailgun answered 401: Forbidden" in capsys.readouterr().err


def test_check_shows_the_email_settings(config_file, sides, capsys):
    assert run(config_file, "check") == cli.EXIT_OK
    assert "--  not set up; scheduled runs won't email anything" in capsys.readouterr().out
    with_email(config_file)
    run(config_file, "check")
    assert "after every run with --email, to me@example.com through Mailgun (mail.example.com)" in capsys.readouterr().out
    with_email(config_file, "send_every_run = false\n")
    run(config_file, "check")
    assert "only when a run with --email has a problem" in capsys.readouterr().out


def test_missing_config_exits_2(tmp_path, capsys):
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "check"]) == cli.EXIT_FAILED
    assert "config file not found" in capsys.readouterr().err


def test_log_file_gets_the_report(config_file, sides, tmp_path):
    log_file = tmp_path / "logs" / "sync.log"
    run(config_file, "--log-file", str(log_file), "list")
    logged = log_file.read_text(encoding="utf-8")
    assert "Engineering Support" in logged
    assert f"  harvest-sync {__version__}\n" in logged
