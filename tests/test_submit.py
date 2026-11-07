from datetime import date

from factories import DEST_ASSIGNMENTS, SOURCE_ASSIGNMENTS, dest_entry, make_config, source_entry
from fakes import FakeHarvest

from harvest_sync.harvest import Forbidden, OutcomeUnknown
from harvest_sync.report import submit_report
from harvest_sync.submit import Submitter, last_finished_week, week_containing
from harvest_sync.sync import Syncer

TODAY = date(2026, 11, 10)  # a Tuesday
WEEK = (date(2026, 11, 2), date(2026, 11, 8))  # the last finished Monday-to-Sunday week


def sides(source_entries=(), dest_entries=()):
    source = FakeHarvest(account_id=1, assignments=SOURCE_ASSIGNMENTS, entries=source_entries)
    dest = FakeHarvest(account_id=2, assignments=DEST_ASSIGNMENTS, entries=dest_entries, user_id=70, roles=("member",))
    return source, dest


def submit(source, dest, week=WEEK, apply=True):
    cfg = make_config(submit_enabled=True)
    return Submitter(cfg, Syncer(cfg, source, dest), sleep=lambda seconds: None).run(*week, today=TODAY, apply=apply)


def submissions(dest):
    return [w for w in dest.writes if w[0] == "submit"]


def test_weeks():
    assert last_finished_week(TODAY, 0) == WEEK
    assert last_finished_week(TODAY, 6) == (date(2026, 11, 1), date(2026, 11, 7))  # weeks starting Sunday
    assert week_containing(date(2026, 11, 1), 0) == (date(2026, 10, 26), date(2026, 11, 1))


def test_preview_writes_nothing():
    source, dest = sides([source_entry(1, "2026-11-03")])
    result = submit(source, dest, apply=False)
    assert (result.status, result.hours) == ("would submit", 1.0)
    assert dest.writes == []
    assert "preview only" in submit_report(result)


def test_apply_syncs_the_week_then_submits_it_once():
    source, dest = sides([source_entry(1, "2026-11-03"), source_entry(2, "2026-11-05", hours=2)])
    result = submit(source, dest)
    assert (result.status, result.hours, result.needs_attention) == ("submitted", 3.0, False)
    assert [w[0] for w in dest.writes] == ["create", "create", "submit"]
    assert submissions(dest) == [("submit", *WEEK)]


def test_an_already_submitted_week_is_left_alone():
    source, dest = sides([source_entry(1, "2026-11-03")], [dest_entry(50, "2026-11-03", approval="submitted")])
    result = submit(source, dest)
    assert (result.status, result.needs_attention) == ("already submitted", False)
    assert dest.writes == []


def test_entries_added_after_submitting_are_flagged():
    existing = [dest_entry(50, "2026-11-03", approval="approved"), dest_entry(51, "2026-11-04")]
    source, dest = sides([source_entry(1, "2026-11-03"), source_entry(2, "2026-11-04")], existing)
    result = submit(source, dest)
    assert (result.status, result.needs_attention) == ("already submitted", True)
    assert submissions(dest) == []


def test_an_unfinished_week_is_held():
    result = submit(*sides([source_entry(1, "2026-11-09")]), week=(date(2026, 11, 9), date(2026, 11, 15)))
    assert result.status == "held"
    assert "isn't over" in result.reasons[0]


def test_held_when_the_sync_needs_attention():
    source, dest = sides([source_entry(1, "2026-11-03", task="ADMIN")])
    result = submit(source, dest)
    assert (result.status, result.needs_attention) == ("held", True)
    assert submissions(dest) == []


def test_held_while_a_timer_is_running():
    source, dest = sides([source_entry(1, "2026-11-03"), source_entry(2, "2026-11-04", running=True)])
    result = submit(source, dest)
    assert result.status == "held"
    assert "timer" in result.reasons[0]
    assert submissions(dest) == []


def test_an_empty_week_is_skipped():
    result = submit(*sides())
    assert (result.status, result.needs_attention) == ("nothing to submit", False)


def test_the_first_week_starts_at_the_cut_over():
    source, dest = sides([source_entry(1, "2026-11-01")])
    result = submit(source, dest, week=(date(2026, 10, 26), date(2026, 11, 1)))
    assert (result.period_start, result.status) == (date(2026, 11, 1), "submitted")
    assert submissions(dest) == [("submit", date(2026, 11, 1), date(2026, 11, 1))]


def test_a_week_before_the_cut_over_has_nothing_to_submit():
    result = submit(*sides(), week=(date(2026, 10, 19), date(2026, 10, 25)))
    assert result.status == "nothing to submit"


def test_a_lost_response_is_checked_not_resubmitted():
    source, dest = sides([source_entry(1, "2026-11-03")])
    real_submit = dest.submit_timesheet

    def submit_then_lose_the_response(start, end):
        real_submit(start, end)
        raise OutcomeUnknown("their account: POST /approvals/submit: Harvest returned 502", 502)

    dest.submit_timesheet = submit_then_lose_the_response
    assert submit(source, dest).status == "submitted"
    assert len(submissions(dest)) == 1


def test_an_unconfirmed_submission_is_flagged_not_retried():
    source, dest = sides([source_entry(1, "2026-11-03")])
    dest.submit_timesheet = lambda start, end: dest.writes.append(("submit", start, end))  # never takes effect
    result = submit(source, dest)
    assert (result.status, result.needs_attention) == ("failed", True)
    assert len(submissions(dest)) == 1


def test_approvals_switched_off():
    source, dest = sides([source_entry(1, "2026-11-03")])
    message = "their account: POST /approvals/submit: 403 Timesheet approvals are not enabled for this company"
    dest.fail["submit"] = Forbidden(message, 403)
    result = submit(source, dest)
    assert result.status == "failed"
    assert "not enabled" in result.reasons[0]
