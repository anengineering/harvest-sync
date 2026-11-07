import pytest

from harvest_sync.notes import NoteFormatter, location_of


@pytest.mark.parametrize(("task", "location"), [("CA-DEV", "CA"), ("CA-OAK-DEV", "CA-OAK"), (" OR-TRV ", "OR")])
def test_location_of(task, location):
    assert location_of(task) == location


@pytest.mark.parametrize("task", ["DEV", "-DEV", "CA-", ""])
def test_location_of_is_none_for_tasks_without_a_location(task):
    assert location_of(task) is None


FORMATTER = NoteFormatter(["CA", "OR", "CA-OAK"])


@pytest.mark.parametrize(
    ("note", "location", "default", "expected"),
    [
        (None, "CA", "EE", "CA-EE"),
        ("", "CA-OAK", "EE", "CA-OAK-EE"),
        ("   ", "OR", "Traveling", "OR-Traveling"),
        ("Reviewed schematics", "CA", "EE", "CA-Reviewed schematics"),
        ("  Reviewed schematics  ", "CA", "EE", "CA-Reviewed schematics"),
        ("CA-EE board bring-up", "CA", "EE", "CA-EE board bring-up"),
        ("ca - Reviewed", "CA", "EE", "CA-Reviewed"),
        ("CA-OAK-EE", "CA-OAK", "EE", "CA-OAK-EE"),
        ("CAD drawings", "CA", "EE", "CA-CAD drawings"),
        ("CA-", "CA", "EE", "CA-EE"),
        ("Line one\nline two", "CA", "EE", "CA-Line one\nline two"),
        ("Reviewed", "ca", "EE", "CA-Reviewed"),
    ],
)
def test_format(note, location, default, expected):
    assert FORMATTER.format(note, location, default) == (expected, None)


@pytest.mark.parametrize(
    ("note", "expected"),
    [
        (None, "Office"),
        ("   ", "Office"),
        ("  Paperwork  ", "Paperwork"),
        ("CA-Reviewed", "CA-Reviewed"),
        ("ca - Reviewed", "ca - Reviewed"),
    ],
)
def test_a_task_with_no_location_copies_the_note_as_written(note, expected):
    assert FORMATTER.format(note, None, "Office") == (expected, None)


@pytest.mark.parametrize(
    ("note", "location", "expected"),
    [
        (None, "CA", ""),
        ("   ", "CA", ""),
        ("CA-", "CA", ""),
        (None, None, ""),
        ("Reviewed", "CA", "CA-Reviewed"),
        ("Reviewed", None, "Reviewed"),
    ],
)
def test_with_no_default_a_blank_note_stays_blank(note, location, expected):
    assert FORMATTER.format(note, location, None) == (expected, None)


def test_no_locations_at_all():
    assert NoteFormatter([]).format("Paperwork", None, "Office") == ("Paperwork", None)


def test_tag_for_another_location_is_kept_but_flagged():
    note, warning = FORMATTER.format("OR-EE board bring-up", "CA", "EE")
    assert note == "OR-EE board bring-up"
    assert warning == "note is tagged OR but the task's location is CA"


def test_plain_ca_tag_on_an_oakland_task_is_flagged():
    note, warning = FORMATTER.format("CA-Reviewed schematics", "CA-OAK", "EE")
    assert note == "CA-Reviewed schematics"
    assert warning is not None
