from factories import DEST_ASSIGNMENTS, SOURCE_ASSIGNMENTS, dest_entry, make_config, source_entry

from harvest_sync.mapping import Mapper
from harvest_sync.reconcile import NO_MATCH, create_payload, plan, update_payload

MAPPER = Mapper.build(make_config(), SOURCE_ASSIGNMENTS, DEST_ASSIGNMENTS)


def run(source, dest):
    return plan(source, dest, MAPPER)


def kinds(actions):
    return sorted(((a.kind, a.source_id) for a in actions), key=lambda k: (k[0], k[1] or 0))


def test_new_entry_is_created_with_nothing_but_the_entry():
    (action,) = run([source_entry(8, hours=2, notes="Reviewed schematics")], [])
    assert action.kind == "create"
    assert create_payload(action) == {
        "project_id": 900,
        "task_id": 91,
        "spent_date": "2026-11-03",
        "hours": 2.0,
        "notes": "CA-Reviewed schematics",
    }


def test_matching_copy_is_unchanged():
    actions = run([source_entry(1, hours=1.0)], [dest_entry(50, hours=1.004, notes="CA-EE\r\n")])
    assert kinds(actions) == [("unchanged", 1)]


def test_an_edit_updates_the_most_similar_copy():
    source = [source_entry(1), source_entry(2, task="OR-TRV", hours=2.5, notes="Drive")]
    copies = [dest_entry(50), dest_entry(51, task="TRV", hours=2, notes="OR-Drive")]
    actions = run(source, copies)
    assert [(a.kind, a.source_id, a.dest["id"]) for a in actions] == [("unchanged", 1, 50), ("update", 2, 51)]
    assert update_payload(actions[1]) == {"hours": 2.5}


def test_changed_fields_are_updated():
    (action,) = run([source_entry(1, task="OR-TRV", hours=2.5, notes="Site visit")], [dest_entry(50)])
    assert action.kind == "update"
    assert action.changes == {"task_id": (91, 92), "hours": (1.0, 2.5), "notes": ("CA-EE", "OR-Site visit")}
    assert update_payload(action) == {"project_id": 900, "task_id": 92, "hours": 2.5, "notes": "OR-Site visit"}


def test_an_entry_moved_to_another_day_is_recreated_there():
    actions = run([source_entry(1, "2026-11-04")], [dest_entry(50, "2026-11-03")])
    assert kinds(actions) == [("create", 1), ("delete", None)]
    assert actions[0].reason == NO_MATCH


def test_identical_entries_each_get_their_own_copy():
    actions = run([source_entry(1), source_entry(2)], [dest_entry(50)])
    assert kinds(actions) == [("create", 2), ("unchanged", 1)]


def test_a_copy_matching_nothing_is_deleted():
    (action,) = run([], [dest_entry(50)])
    assert (action.kind, action.source_id, action.dest["id"], action.reason) == ("delete", None, 50, NO_MATCH)


def test_duplicates_keep_the_locked_copy_and_delete_the_rest():
    actions = run([source_entry(1)], [dest_entry(50), dest_entry(51, locked=True)])
    assert [(a.kind, a.dest["id"]) for a in actions] == [("unchanged", 51), ("delete", 50)]


def test_locked_copy_is_a_conflict():
    (action,) = run([source_entry(1, hours=3)], [dest_entry(50, locked=True)])
    assert (action.kind, action.attention) == ("conflict", True)
    assert "locked" in action.reason


def test_an_edit_prefers_an_unlocked_copy():
    actions = run([source_entry(1, hours=3)], [dest_entry(50, locked=True), dest_entry(51, hours=2)])
    assert [(a.kind, a.dest["id"]) for a in actions] == [("update", 51), ("conflict", 50)]


def test_running_timer_holds_that_days_deletes_only():
    source = [source_entry(1, running=True, hours=5)]
    actions = run(source, [dest_entry(50), dest_entry(51, "2026-11-04")])
    assert [(a.kind, a.day) for a in actions] == [("skip", "2026-11-03"), ("held", "2026-11-03"), ("delete", "2026-11-04")]
    assert actions[1].attention


def test_zero_hours_lets_the_copy_go():
    actions = run([source_entry(1, hours=0)], [dest_entry(50)])
    assert kinds(actions) == [("delete", None), ("skip", 1)]


def test_unmapped_task_needs_attention_and_holds_that_days_copies():
    actions = run([source_entry(1, task="ADMIN")], [dest_entry(50)])
    assert [(a.kind, a.attention) for a in actions] == [("skip", True), ("held", True)]


def test_mismatched_note_tag_needs_attention():
    (action,) = run([source_entry(1, task="CA-OAK-DEV", notes="CA-Reviewed")], [])
    assert (action.kind, action.attention) == ("create", True)
    assert "CA-OAK" in action.warning
