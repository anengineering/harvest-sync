"""Test data shaped like Harvest API v2 responses."""

from datetime import date
from zoneinfo import ZoneInfo

from harvest_sync.config import Config, ProjectRule, TaskRule

CONFIG_TOML = """\
contact_email = "me@example.com"
timezone = "America/Los_Angeles"

[source]
account_id = 1
client = "Client Co"

[destination]
account_id = 2

[sync]
earliest_date = 2026-11-01
location_prefix = true

[[project_map]]
from = "Client Co - Retrofit"
to = "Engineering Support"

[[task_map]]
from = ["CA-DEV", "OR-DEV", "CA-OAK-DEV"]
to = "DEV"
default_note = "EE"

[[task_map]]
from = ["CA-TRV", "OR-TRV"]
to = "TRV"
default_note = "Traveling"
"""

SOURCE_TASK_IDS = {"CA-DEV": 1, "OR-DEV": 2, "CA-OAK-DEV": 3, "CA-TRV": 4, "OR-TRV": 5, "ADMIN": 6}
DEST_TASK_IDS = {"DEV": 91, "TRV": 92}


def assignment(project_id, project_name, tasks, *, client_id=10, client_name="Client Co", code=None):
    return {
        "id": project_id * 10,
        "is_active": True,
        "project": {"id": project_id, "name": project_name, "code": code},
        "client": {"id": client_id, "name": client_name},
        "task_assignments": [
            {"id": task_id * 10, "is_active": True, "billable": True, "task": {"id": task_id, "name": name}}
            for name, task_id in tasks.items()
        ],
    }


SOURCE_ASSIGNMENTS = [
    assignment(100, "Client Co - Retrofit", SOURCE_TASK_IDS, code="CC-R"),
    assignment(200, "Other client work", {"CA-DEV": 1}, client_id=20, client_name="Other Client"),
]
DEST_ASSIGNMENTS = [assignment(900, "Engineering Support", DEST_TASK_IDS, client_id=90, client_name="Client Co")]


def make_config(**overrides) -> Config:
    settings = dict(
        contact_email="me@example.com",
        timezone=ZoneInfo("America/Los_Angeles"),
        source_account_id=1,
        source_token="src-token",
        dest_account_id=2,
        dest_token="dst-token",
        earliest_date=date(2026, 11, 1),
        source_client="Client Co",
        project_map=(ProjectRule(("Client Co - Retrofit",), "Engineering Support"),),
        task_map=(
            TaskRule(("CA-DEV", "OR-DEV", "CA-OAK-DEV"), "DEV", "EE", location_prefix=True),
            TaskRule(("CA-TRV", "OR-TRV"), "TRV", "Traveling", location_prefix=True),
        ),
    )
    settings.update(overrides)
    return Config(**settings)


def source_entry(
    entry_id,
    spent_date="2026-11-03",
    task="CA-DEV",
    hours=1.0,
    notes=None,
    *,
    project=(100, "Client Co - Retrofit"),
    client=(10, "Client Co"),
    running=False,
):
    return {
        "id": entry_id,
        "spent_date": spent_date,
        "hours": hours,
        "notes": notes,
        "is_running": running,
        "is_locked": False,
        "approval_status": "unsubmitted",
        "user": {"id": 7, "name": "Me"},
        "client": {"id": client[0], "name": client[1]},
        "project": {"id": project[0], "name": project[1]},
        "task": {"id": SOURCE_TASK_IDS.get(task, 99), "name": task},
        "external_reference": None,
    }


def dest_entry(
    entry_id,
    spent_date="2026-11-03",
    task="DEV",
    hours=1.0,
    notes="CA-EE",
    *,
    locked=False,
    approval="unsubmitted",
    running=False,
    project_id=900,
):
    return {
        "id": entry_id,
        "spent_date": spent_date,
        "hours": hours,
        "notes": notes,
        "is_running": running,
        "is_locked": locked,
        "locked_reason": "Item Approved and Locked for this Time Period" if locked else None,
        "approval_status": approval,
        "user": {"id": 70, "name": "Me"},
        "project": {"id": project_id, "name": "Engineering Support" if project_id == 900 else f"Project {project_id}"},
        "task": {"id": DEST_TASK_IDS[task], "name": task},
        "external_reference": None,
    }
