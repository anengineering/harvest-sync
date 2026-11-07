import pytest
from factories import DEST_ASSIGNMENTS, SOURCE_ASSIGNMENTS, assignment, make_config, source_entry

from harvest_sync.config import ProjectRule, TaskRule
from harvest_sync.mapping import Desired, Mapper, MappingError, Skip


def build(**overrides) -> Mapper:
    return Mapper.build(make_config(**overrides), SOURCE_ASSIGNMENTS, DEST_ASSIGNMENTS)


def test_resolves_names_to_ids():
    mapper = build()
    assert (mapper.client_id, mapper.client_name) == (10, "Client Co")
    assert mapper.project_ids == {100: 900}
    assert mapper.dest_task_ids == {(900, "dev"): 91, (900, "trv"): 92}
    assert mapper.warnings == []


def test_matches_project_codes_and_ignores_case():
    mapper = build(project_map=(ProjectRule(("cc-r",), "ENGINEERING SUPPORT"),))
    assert mapper.project_ids == {100: 900}


def test_a_task_with_no_location_keeps_its_note_as_written():
    rules = (TaskRule(("CA-DEV",), "DEV", "EE", location_prefix=True), TaskRule(("ADMIN",), "TRV", "Office", location_prefix=True))
    mapper = build(task_map=rules)
    blank = mapper.map_entry(source_entry(1, task="ADMIN"))
    typed = mapper.map_entry(source_entry(2, task="ADMIN", notes="Paperwork"))
    assert (blank.notes, blank.dest_task, typed.notes) == ("Office", "TRV", "Paperwork")
    assert mapper.map_entry(source_entry(3)).notes == "CA-EE"


def test_location_prefix_off_ignores_a_hyphen_in_the_name():
    mapper = build(task_map=(TaskRule(("OR-TRV",), "TRV", "Traveling", location_prefix=False),))
    assert mapper.map_entry(source_entry(1, task="OR-TRV")).notes == "Traveling"
    assert mapper.map_entry(source_entry(2, task="OR-TRV", notes="Site visit")).notes == "Site visit"


def test_location_prefix_off_copies_a_typed_tag_without_a_warning():
    mapper = build(task_map=(TaskRule(("CA-DEV",), "DEV", "EE"),))
    desired = mapper.map_entry(source_entry(1, notes="OR-Reviewed"))
    assert (desired.notes, desired.warning) == ("OR-Reviewed", None)


def test_client_can_be_given_by_id():
    assert build(source_client=10).client_id == 10


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"source_client": None}, "source.client"),
        ({"source_client": "Nobody"}, "no client 'Nobody'"),
        ({"project_map": ()}, "project_map"),
        ({"task_map": ()}, "task_map"),
        ({"project_map": (ProjectRule(("Other client work",), "Engineering Support"),)}, "isn't one of your projects for Client Co"),
        ({"project_map": (ProjectRule(("Client Co - Retrofit",), "Nope"),)}, '"Nope" isn\'t one of the projects'),
        ({"task_map": (TaskRule(("CA-DEV",), "QA", "x"),)}, 'no active task "QA"'),
    ],
)
def test_mapping_problems(overrides, message):
    with pytest.raises(MappingError, match=message):
        build(**overrides)


def test_ambiguous_project_name():
    source = SOURCE_ASSIGNMENTS + [assignment(150, "Client Co - Retrofit", {"CA-DEV": 1}, code="CC-R2")]
    with pytest.raises(MappingError, match="more than one project"):
        Mapper.build(make_config(), source, DEST_ASSIGNMENTS)


def test_warns_about_tasks_not_assigned_and_projects_not_mapped():
    source = SOURCE_ASSIGNMENTS + [assignment(150, "Client Co - Phase 2", {"CA-DEV": 1})]
    config = make_config(task_map=(TaskRule(("CA-DEV", "WA-DEV"), "DEV", "EE"),))
    warnings = Mapper.build(config, source, DEST_ASSIGNMENTS).warnings
    assert any("WA-DEV" in w for w in warnings)
    assert any("Phase 2" in w for w in warnings)


def test_maps_an_entry():
    desired = build().map_entry(source_entry(5, task="CA-OAK-DEV", hours=1.5, notes=""))
    assert desired == Desired(
        source_id=5,
        spent_date="2026-11-03",
        project_id=900,
        task_id=91,
        hours=1.5,
        notes="CA-OAK-EE",
        source_task="CA-OAK-DEV",
        dest_task="DEV",
    )


def test_travel_entry_with_own_note():
    desired = build().map_entry(source_entry(5, task="OR-TRV", hours=0.3333, notes="Drive to site"))
    assert (desired.task_id, desired.hours, desired.notes) == (92, 0.33, "OR-Drive to site")


def test_note_tagged_with_another_location_carries_a_warning():
    desired = build().map_entry(source_entry(5, task="CA-OAK-DEV", notes="CA-Reviewed schematics"))
    assert desired.notes == "CA-Reviewed schematics"
    assert "CA-OAK" in desired.warning


def test_running_timer_is_skipped_quietly():
    skip = build().map_entry(source_entry(5, running=True))
    assert isinstance(skip, Skip)
    assert (skip.attention, skip.remove_copy) == (False, False)


def test_unmapped_task_needs_attention():
    skip = build().map_entry(source_entry(5, task="ADMIN"))
    assert isinstance(skip, Skip) and skip.attention
    assert "ADMIN" in skip.reason


def test_unmapped_project_needs_attention():
    skip = build().map_entry(source_entry(5, project=(150, "Client Co - Phase 2")))
    assert isinstance(skip, Skip) and skip.attention
    assert "Phase 2" in skip.reason


def test_zero_hours_removes_any_copy():
    skip = build().map_entry(source_entry(5, hours=0))
    assert isinstance(skip, Skip)
    assert (skip.attention, skip.remove_copy) == (False, True)
