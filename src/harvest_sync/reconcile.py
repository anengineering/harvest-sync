"""Works out what to change on the client's side. Pure functions: no API calls.

Your entries in their account that sit in a mapped project are all treated as copies made by this tool,
since you never type time into their Harvest directly. Harvest only keeps a reference back to your entry
if it carries a URL, which turns the notes into a link, so copies are matched to your entries by content.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from harvest_sync.mapping import Desired, Mapper, Skip, hours_of

HOURS_TOLERANCE = 0.005
NO_MATCH = "no entry of yours matches it any more"


@dataclass
class Action:
    kind: str  # create | update | delete | unchanged | skip | conflict | held
    source_id: int | None  # your entry; None for a copy that matches none of your entries
    desired: Desired | None = None
    dest: dict | None = None  # the client-side entry concerned
    changes: dict[str, tuple] = field(default_factory=dict)  # field -> (theirs now, wanted)
    reason: str = ""
    attention: bool = False
    error: str | None = None  # set when applying the action failed
    skipped_day: str = ""  # skip only: the date of your entry

    def __post_init__(self) -> None:
        if self.warning:
            self.attention = True

    @property
    def warning(self) -> str | None:
        return self.desired.warning if self.desired else None

    @property
    def day(self) -> str:
        if self.desired:
            return self.desired.spent_date
        return self.dest["spent_date"] if self.dest else self.skipped_day


def plan(source_entries: list[dict], copies: list[dict], mapper: Mapper) -> list[Action]:
    """Compare one window of your entries with the copies in their account for the same dates."""
    actions = []
    wanted: dict[str, list[Desired]] = {}
    held: dict[str, str] = {}  # day -> why that day's copies mustn't be deleted
    for entry in source_entries:
        mapped = mapper.map_entry(entry)
        if isinstance(mapped, Desired):
            wanted.setdefault(mapped.spent_date, []).append(mapped)
            continue
        actions.append(Action("skip", mapped.source_id, reason=mapped.reason, attention=mapped.attention, skipped_day=mapped.spent_date))
        if not mapped.remove_copy:  # one of the copies that day may be this entry's, so keep them all
            held.setdefault(mapped.spent_date, mapped.reason)
    by_day: dict[str, list[dict]] = {}
    for copy in copies:
        by_day.setdefault(copy["spent_date"], []).append(copy)
    for day in sorted(wanted.keys() | by_day.keys()):
        actions += match_day(wanted.get(day, []), by_day.get(day, []), held.get(day))
    return actions


def match_day(wanted: list[Desired], copies: list[dict], hold: str | None = None) -> list[Action]:
    """Pair one day's entries of yours with that day's copies, changing as few copies as possible."""
    actions = []
    # Exact matches first. Locked copies go first because they can't be changed anyway.
    spare = sorted(copies, key=lambda c: (not c.get("is_locked"), c["id"]))
    unmatched = []
    for want in sorted(wanted, key=lambda d: d.source_id):
        same = next((c for c in spare if not diff(c, want)), None)
        if same is None:
            unmatched.append(want)
        else:
            spare.remove(same)
            actions.append(Action("unchanged", want.source_id, desired=want, dest=same))

    # An edited entry no longer matches its copy exactly: update the most similar copy left over.
    for want in unmatched:
        if not spare:
            actions.append(Action("create", want.source_id, desired=want))
            continue
        nearest = min(spare, key=lambda c: _distance(c, want))
        spare.remove(nearest)
        changes = diff(nearest, want)
        if nearest.get("is_locked"):
            reason = f"locked on their side ({nearest.get('locked_reason') or 'locked'})"
            actions.append(Action("conflict", want.source_id, desired=want, dest=nearest, changes=changes, reason=reason, attention=True))
        else:
            actions.append(Action("update", want.source_id, desired=want, dest=nearest, changes=changes))

    for copy in spare:
        if copy.get("is_locked"):
            reason = f"should be deleted ({NO_MATCH}) but is locked on their side"
            actions.append(Action("conflict", None, dest=copy, reason=reason, attention=True))
        elif hold:
            reason = f"not deleted ({NO_MATCH}) because an entry of yours that day didn't sync: {hold}"
            actions.append(Action("held", None, dest=copy, reason=reason, attention=True))
        else:
            actions.append(Action("delete", None, dest=copy, reason=NO_MATCH))
    return actions


def diff(entry: dict, desired: Desired) -> dict[str, tuple]:
    current = {
        "spent_date": entry["spent_date"],
        "project_id": entry["project"]["id"],
        "task_id": entry["task"]["id"],
        "hours": hours_of(entry),
        "notes": _normal(entry.get("notes")),
    }
    wanted = {
        "spent_date": desired.spent_date,
        "project_id": desired.project_id,
        "task_id": desired.task_id,
        "hours": desired.hours,
        "notes": _normal(desired.notes),
    }
    changes = {}
    for key, value in wanted.items():
        if key == "hours" and abs(current[key] - value) < HOURS_TOLERANCE:
            continue
        if current[key] != value:
            changes[key] = (current[key], value)
    return changes


def create_payload(action: Action) -> dict:
    desired = action.desired
    return {
        "project_id": desired.project_id,
        "task_id": desired.task_id,
        "spent_date": desired.spent_date,
        "hours": desired.hours,
        "notes": desired.notes,
    }


def update_payload(action: Action) -> dict:
    payload = {key: wanted for key, (_, wanted) in action.changes.items()}
    if "project_id" in payload or "task_id" in payload:  # Harvest validates the pair together
        payload["project_id"] = action.desired.project_id
        payload["task_id"] = action.desired.task_id
    return payload


def _distance(copy: dict, want: Desired) -> tuple:
    """How unlike your entry a copy is: an unlocked copy of the same task and note comes first."""
    return (
        bool(copy.get("is_locked")),
        copy["task"]["id"] != want.task_id,
        copy["project"]["id"] != want.project_id,
        _normal(copy.get("notes")) != _normal(want.notes),
        abs(hours_of(copy) - want.hours),
        copy["id"],
    )


def _normal(notes: str | None) -> str:
    return (notes or "").replace("\r\n", "\n").strip()
