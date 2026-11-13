from datetime import date

import pytest
from factories import DEST_ASSIGNMENTS, SOURCE_ASSIGNMENTS, dest_entry, make_config, source_entry
from fakes import FakeHarvest

from harvest_sync.config import TaskRule
from harvest_sync.harvest import Unprocessable
from harvest_sync.reconcile import NO_MATCH
from harvest_sync.report import sync_report
from harvest_sync.sync import SyncError, Syncer, sync_window

START, END = date(2026, 11, 1), date(2026, 11, 14)


def sides(source_entries=(), dest_entries=()):
    source = FakeHarvest(account_id=1, assignments=SOURCE_ASSIGNMENTS, entries=source_entries)
    dest = FakeHarvest(account_id=2, assignments=DEST_ASSIGNMENTS, entries=dest_entries, user_id=70, roles=("member",))
    return source, dest


def sync(source, dest, apply=True, start=START, end=END, **settings):
    return Syncer(make_config(**settings), source, dest).run(start, end, apply)


def kinds(result):
    return sorted(((a.kind, a.source_id) for a in result.actions), key=lambda k: (k[0], k[1] or 0))


def test_preview_writes_nothing():
    source, dest = sides([source_entry(1), source_entry(2, "2026-11-04", task="OR-TRV", hours=2)])
    result = sync(source, dest, apply=False)
    assert kinds(result) == [("create", 1), ("create", 2)]
    assert dest.writes == []
    assert result.totals["2026-11-04"].theirs_after == 2.0  # projected
    assert not result.needs_attention


def test_apply_then_a_second_run_changes_nothing():
    source, dest = sides([source_entry(1, notes=""), source_entry(2, "2026-11-04", task="OR-TRV", hours=2, notes="Drive")])
    assert not sync(source, dest).needs_attention
    copies = sorted((e["spent_date"], e["task"]["name"], e["hours"], e["notes"]) for e in dest.entries.values())
    assert copies == [("2026-11-03", "DEV", 1.0, "CA-EE"), ("2026-11-04", "TRV", 2.0, "OR-Drive")]
    writes = len(dest.writes)

    second = sync(source, dest)
    assert kinds(second) == [("unchanged", 1), ("unchanged", 2)]
    assert len(dest.writes) == writes
    assert not second.needs_attention


def test_an_edit_on_your_side_updates_their_copy():
    source, dest = sides([source_entry(1)])
    sync(source, dest)
    source.entries[1]["hours"] = 2.5
    assert kinds(sync(source, dest)) == [("update", 1)]
    assert [e["hours"] for e in dest.entries.values()] == [2.5]


def test_deleting_on_your_side_deletes_their_copy():
    source, dest = sides([source_entry(1)])
    sync(source, dest)
    del source.entries[1]
    result = sync(source, dest)
    assert kinds(result) == [("delete", None)]
    assert result.actions[0].reason == NO_MATCH
    assert dest.entries == {}


def test_an_entry_moved_to_another_date_moves_its_copy():
    source, dest = sides([source_entry(1, "2026-11-10")])
    sync(source, dest)
    source.entries[1]["spent_date"] = "2026-11-02"
    assert kinds(sync(source, dest)) == [("create", 1), ("delete", None)]
    assert [e["spent_date"] for e in dest.entries.values()] == ["2026-11-02"]


def test_an_entry_moved_to_another_client_is_removed():
    source, dest = sides([source_entry(1)])
    sync(source, dest)
    source.entries[1]["client"] = {"id": 20, "name": "Other Client"}
    assert kinds(sync(source, dest)) == [("delete", None)]
    assert dest.entries == {}


def test_blank_notes_can_stay_blank():
    source, dest = sides([source_entry(1)])
    rules = (TaskRule(("CA-DEV",), "DEV", None),)
    assert not sync(source, dest, task_map=rules).needs_attention
    (copy,) = dest.entries.values()
    assert copy["notes"] == ""
    copy["notes"] = None  # how Harvest may hand a blank note back
    assert kinds(sync(source, dest, task_map=rules)) == [("unchanged", 1)]


def test_copies_outside_the_window_are_left_alone():
    source, dest = sides([], [dest_entry(50, "2026-11-02")])
    assert kinds(sync(source, dest, start=date(2026, 11, 5))) == []
    assert list(dest.entries) == [50]


def test_changes_harvest_doesnt_keep_are_flagged():
    source, dest = sides([source_entry(1, hours=1.25)])
    create = dest.create_time_entry

    def create_but_store_other_hours(payload):
        created = create(payload)
        dest.entries[created["id"]]["hours"] = 1.0
        return created

    dest.create_time_entry = create_but_store_other_hours
    result = sync(source, dest)
    assert result.unsettled == 1
    assert result.needs_attention
    assert "comparing again still finds 1 change(s)" in sync_report(result)


def test_a_lost_create_response_never_causes_a_duplicate():
    source, dest = sides([source_entry(1)])
    dest.lose_create_response = True
    first = sync(source, dest)
    assert first.needs_attention
    assert "unclear" in first.actions[0].error

    dest.lose_create_response = False
    assert kinds(sync(source, dest)) == [("unchanged", 1)]
    assert len(dest.entries) == 1


def test_too_many_deletes_are_held():
    source, dest = sides([source_entry(1), source_entry(2)])
    sync(source, dest)
    source.entries.clear()
    result = sync(source, dest, max_deletes_per_run=1)
    assert kinds(result) == [("held", None), ("held", None)]
    assert result.needs_attention
    assert len(dest.entries) == 2


def test_deletes_can_be_switched_off():
    source, dest = sides([source_entry(1)])
    sync(source, dest)
    source.entries.clear()
    assert kinds(sync(source, dest, propagate_deletes=False)) == [("skip", None)]
    assert len(dest.entries) == 1


def test_a_locked_copy_is_reported_not_written():
    source, dest = sides([source_entry(1, hours=3)], [dest_entry(50, locked=True)])
    result = sync(source, dest)
    assert kinds(result) == [("conflict", 1)]
    assert dest.writes == []
    assert result.needs_attention


def test_your_entries_outside_the_mapped_projects_are_left_alone_and_flagged():
    source, dest = sides([source_entry(1)], [dest_entry(60, project_id=950, notes="typed in by hand")])
    result = sync(source, dest)
    assert [e["id"] for e in result.unmanaged] == [60]
    assert 60 in dest.entries
    assert result.totals["2026-11-03"].differs  # 1 h of yours against 2 h on their side
    assert result.needs_attention
    assert '!! 2026-11-03 Tue  Project 950  DEV  1.00 h  "typed in by hand"' in sync_report(result)


def test_failed_writes_are_reported_and_the_rest_still_run():
    source, dest = sides([source_entry(1), source_entry(2, "2026-11-04")])
    dest.fail["create"] = Unprocessable("their account: POST /time_entries: 422 Project is archived", 422)
    result = sync(source, dest)
    assert [bool(a.error) for a in result.actions] == [True, True]
    assert result.needs_attention


def test_start_and_end_times_on_your_side_are_copied_as_hours():
    timed = {**source_entry(1, hours=1.333333), "started_time": "9:00am", "ended_time": "10:20am"}
    source, dest = sides([timed])
    source.company_settings = {"wants_timestamp_timers": True}
    result = sync(source, dest)
    assert not result.needs_attention  # in particular, the day's totals match
    (write,) = dest.writes
    assert write[1]["hours"] == 1.33
    assert "started_time" not in write[1] and "ended_time" not in write[1]
    assert kinds(sync(source, dest)) == [("unchanged", 1)]


def test_start_and_end_time_tracking_on_their_side_is_refused():
    source, dest = sides()
    dest.company_settings = {"wants_timestamp_timers": True}
    with pytest.raises(SyncError, match="start and end times"):
        Syncer(make_config(), source, dest)


def test_totals_after_applying_are_read_back_from_harvest():
    source, dest = sides([source_entry(1, hours=1.5), source_entry(2, hours=2)])
    total = sync(source, dest).totals["2026-11-03"]
    assert (total.yours, total.theirs_before, total.theirs_after) == (3.5, 0.0, 3.5)


@pytest.mark.parametrize(
    ("today", "start", "end", "expected_start", "expected_end", "noted"),
    [
        (date(2026, 12, 20), None, None, date(2026, 12, 1), date(2026, 12, 20), False),  # the month so far
        (date(2026, 12, 1), None, None, date(2026, 12, 1), date(2026, 12, 1), False),  # a new month starts fresh
        (date(2026, 12, 2), None, date(2026, 11, 30), date(2026, 11, 1), date(2026, 11, 30), False),  # all of last month
        (date(2026, 12, 20), date(2026, 11, 25), None, date(2026, 11, 25), date(2026, 12, 20), False),
        (date(2026, 11, 20), date(2026, 10, 15), None, date(2026, 11, 1), date(2026, 11, 20), True),
        (date(2026, 10, 31), None, None, date(2026, 11, 1), date(2026, 10, 31), False),  # before the cut-over: empty
    ],
)
def test_window(today, start, end, expected_start, expected_end, noted):
    got_start, got_end, notes = sync_window(make_config(), today, start, end)
    assert (got_start, got_end) == (expected_start, expected_end)
    assert bool(notes) == noted


def test_summary_says_what_was_done_only_after_applying():
    source, dest = sides([source_entry(1), source_entry(2, "2026-11-04")])
    assert "Summary: 2 to create. Both sides will match once applied." in sync_report(sync(source, dest, apply=False))
    assert "Summary: 2 created. Both sides match." in sync_report(sync(source, dest))
    assert "Summary: 2 unchanged. Both sides match." in sync_report(sync(source, dest, apply=False))

    source.entries[1]["hours"] = 2.5
    del source.entries[2]
    assert "Summary: 1 updated, 1 deleted. Both sides match." in sync_report(sync(source, dest))


def test_action_lines_say_what_was_done_only_after_applying():
    source, dest = sides([source_entry(1)])
    assert '   create    2026-11-03 Tue  CA-DEV -> DEV  1.00 h  "CA-EE"' in sync_report(sync(source, dest, apply=False))
    assert '   created   2026-11-03 Tue  CA-DEV -> DEV  1.00 h  "CA-EE"' in sync_report(sync(source, dest))

    source, dest = sides([source_entry(1, task="ADMIN")])
    assert "!! skipped   2026-11-03 Tue  your entry 1" in sync_report(sync(source, dest))


def test_report_totals_the_period():
    source, dest = sides([source_entry(1, hours=1.5), source_entry(2, "2026-11-04", hours=2.25)])
    text = sync_report(sync(source, dest))
    assert (
        "\nDate        Day   Yours  Theirs\n"
        "----------  ---  ------  ------\n"
        "2026-11-03  Tue    1.50    1.50\n"
        "2026-11-04  Wed    2.25    2.25\n"
        "----------  ---  ------  ------\n"
        "Total              3.75    3.75\n"
    ) in text


def test_report_totals_a_single_day_too():
    source, dest = sides([source_entry(1, hours=1.5)])
    text = sync_report(sync(source, dest, apply=False))
    assert (
        "\nDate        Day   Yours  Theirs now  Theirs after\n"
        "----------  ---  ------  ----------  ------------\n"
        "2026-11-03  Tue    1.50        0.00          1.50\n"
        "----------  ---  ------  ----------  ------------\n"
        "Total              1.50        0.00          1.50\n"
    ) in text


def test_only_the_days_that_differ_are_flagged_in_the_table():
    source, dest = sides([source_entry(1)], [dest_entry(60, project_id=950)])
    text = sync_report(sync(source, dest))
    assert "2026-11-03  Tue    1.00    2.00  !! differs\n" in text
    assert "Total              1.00    2.00\n" in text


def test_preview_report():
    source, dest = sides([source_entry(1, notes="Reviewed schematics"), source_entry(2, task="ADMIN")])
    text = sync_report(sync(source, dest, apply=False))
    assert "preview only" in text
    assert '   create    2026-11-03 Tue  CA-DEV -> DEV  1.00 h  "CA-Reviewed schematics"' in text
    # A reason goes on the next line, lined up under the entry's details.
    assert '!! skip      2026-11-03 Tue  your entry 2\n' + " " * 29 + 'your task "ADMIN" isn\'t in task_map' in text
    assert "need attention" in text
