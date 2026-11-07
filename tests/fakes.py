"""An in-memory stand-in for HarvestClient, for testing everything above the HTTP layer."""

import copy

from harvest_sync.harvest import OutcomeUnknown, Unprocessable


class FakeHarvest:
    def __init__(
        self,
        *,
        account_id,
        assignments,
        entries=(),
        user_id=7,
        roles=("administrator",),
        company=None,
        approvals=None,
        account_name="Test Account",
    ):
        self.account_id = account_id
        self.assignments = assignments
        self.entries = {e["id"]: copy.deepcopy(e) for e in entries}
        self.user = {"id": user_id, "first_name": "Test", "last_name": "User","access_roles": list(roles)}
        self.company_settings = company
        self.approvals = approvals
        self.account_name = account_name
        self.writes = []  # ("create", payload), ("update", id, payload), ("delete", id), ("submit", start, end)
        self.fail = {}  # operation name -> exception to raise
        self.lose_create_response = False  # create the entry, then raise OutcomeUnknown
        self._next_id = 5000

    def _check(self, operation):
        if operation in self.fail:
            raise self.fail[operation]

    def me(self):
        self._check("me")
        return dict(self.user)

    def accounts(self):
        return [{"id": self.account_id, "name": self.account_name, "product": "harvest"}]

    def company(self):
        return self.company_settings

    def approvals_enabled(self):
        return self.approvals

    def project_assignments(self):
        return copy.deepcopy(self.assignments)

    def time_entries(self, *, user_id=None, client_id=None, start=None, end=None):
        self._check("time_entries")
        found = []
        for entry in sorted(self.entries.values(), key=lambda e: (e["spent_date"], e["id"])):
            if client_id is not None and entry.get("client", {}).get("id") != client_id:
                continue
            if start is not None and entry["spent_date"] < start.isoformat():
                continue
            if end is not None and entry["spent_date"] > end.isoformat():
                continue
            found.append(copy.deepcopy(entry))
        return found

    def create_time_entry(self, payload):
        self._check("create")
        self._next_id += 1
        entry = {
            "id": self._next_id,
            "spent_date": payload["spent_date"],
            "hours": payload["hours"],
            "notes": payload.get("notes"),
            "is_running": False,
            "is_locked": False,
            "locked_reason": None,
            "approval_status": "unsubmitted",
            "user": {"id": self.user["id"], "name": "Test User"},
            "project": self._named("project", payload["project_id"]),
            "task": self._named("task", payload["task_id"]),
            "external_reference": None,
        }
        self.entries[entry["id"]] = entry
        self.writes.append(("create", payload))
        if self.lose_create_response:
            raise OutcomeUnknown("their account: POST /time_entries: Harvest returned 502", 502)
        return copy.deepcopy(entry)

    def update_time_entry(self, entry_id, payload):
        self._check("update")
        entry = self.entries[entry_id]
        if entry["is_locked"]:
            raise Unprocessable("their account: PATCH /time_entries: 422 locked", 422)
        for key, value in payload.items():
            if key == "project_id":
                entry["project"] = self._named("project", value)
            elif key == "task_id":
                entry["task"] = self._named("task", value)
            else:
                entry[key] = value
        self.writes.append(("update", entry_id, payload))
        return copy.deepcopy(entry)

    def delete_time_entry(self, entry_id):
        self._check("delete")
        self.entries.pop(entry_id, None)
        self.writes.append(("delete", entry_id))

    def submit_timesheet(self, start, end):
        self._check("submit")
        for entry in self.entries.values():
            if start.isoformat() <= entry["spent_date"] <= end.isoformat():
                entry["approval_status"] = "submitted"
        self.writes.append(("submit", start, end))
        return {"status": "submitted"}

    def _named(self, kind, wanted_id):
        for a in self.assignments:
            if kind == "project" and a["project"]["id"] == wanted_id:
                return {"id": wanted_id, "name": a["project"]["name"]}
            for ta in a["task_assignments"]:
                if kind == "task" and ta["task"]["id"] == wanted_id:
                    return {"id": wanted_id, "name": ta["task"]["name"]}
        return {"id": wanted_id, "name": f"{kind} {wanted_id}"}
