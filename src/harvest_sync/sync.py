"""One sync run: fetch both sides, plan, apply safety limits, apply the changes, verify."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from harvest_sync.config import Config
from harvest_sync.harvest import AuthError, HarvestClient, HarvestError, OutcomeUnknown
from harvest_sync.mapping import Mapper, hours_of
from harvest_sync.reconcile import Action, create_payload, plan, update_payload

HOURS_TOLERANCE = 0.005
CHANGES = ("create", "update", "delete")


class SyncError(Exception):
    """The sync can't run at all."""


@dataclass
class DayTotal:
    yours: float = 0.0
    theirs_before: float = 0.0
    theirs_after: float = 0.0  # projected in a preview; re-read from Harvest after applying

    @property
    def differs(self) -> bool:
        return abs(self.yours - self.theirs_after) >= HOURS_TOLERANCE


@dataclass
class SyncResult:
    start: date
    end: date
    applied: bool
    actions: list[Action] = field(default_factory=list)
    unmanaged: list[dict] = field(default_factory=list)  # your entries in their account outside the mapped projects
    totals: dict[str, DayTotal] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    running_timers: int = 0  # your timers still running in the window
    unsettled: int = 0  # changes a re-plan still wanted after applying

    @property
    def attention_count(self) -> int:
        """How many lines of the report are marked !!."""
        flagged = sum(1 for a in self.actions if a.attention or a.error)
        differs = sum(1 for t in self.totals.values() if t.differs)
        return flagged + len(self.unmanaged) + differs + (1 if self.unsettled else 0)

    @property
    def needs_attention(self) -> bool:
        return self.attention_count > 0


def sync_window(cfg: Config, today: date, start: date | None = None, end: date | None = None) -> tuple[date, date, list[str]]:
    """The dates to sync: the month so far (or `end`'s month up to `end`), never before earliest_date.

    Invoices go out monthly, so a month only has to match by its end; then the next one starts fresh.
    """
    notes = []
    end = end or today
    if start is None:
        start = max(end.replace(day=1), cfg.earliest_date)
    elif start < cfg.earliest_date:
        notes.append(f"starting at earliest_date ({cfg.earliest_date}) rather than {start}")
        start = cfg.earliest_date
    return start, end, notes


class Syncer:
    def __init__(self, cfg: Config, source: HarvestClient, dest: HarvestClient) -> None:
        self.cfg = cfg
        self.source = source
        self.dest = dest
        self.source_user = source.me()["id"]
        self.dest_user = dest.me()["id"]
        company = dest.company()
        if company and company.get("wants_timestamp_timers"):
            raise SyncError("their account tracks start and end times; harvest-sync can only copy to an account that tracks durations")
        self.mapper = Mapper.build(cfg, source.project_assignments(), dest.project_assignments())
        self.managed_projects = set(self.mapper.project_ids.values())

    def run(self, start: date, end: date, apply: bool) -> SyncResult:
        source_entries = self.source.time_entries(
            user_id=self.source_user, client_id=self.mapper.client_id, start=start, end=end
        )
        dest_entries = self.dest.time_entries(user_id=self.dest_user, start=start, end=end)
        copies, unmanaged = self._split(dest_entries)
        result = SyncResult(start, end, apply, unmanaged=unmanaged)
        result.running_timers = sum(1 for e in source_entries if e.get("is_running"))
        result.actions = plan(source_entries, copies, self.mapper)
        self._limit_deletes(result.actions)

        dest_after = None
        if apply:
            for action in result.actions:
                self._apply(action)
            dest_after = self.dest.time_entries(user_id=self.dest_user, start=start, end=end)
            if not any(a.error for a in result.actions):
                # Plan again against what Harvest now holds: anything left to do means Harvest didn't take
                # the changes as sent, and the next run would change the same entries again.
                again = plan(source_entries, self._split(dest_after)[0], self.mapper)
                self._limit_deletes(again)
                result.unsettled = sum(1 for a in again if a.kind in CHANGES)
        result.totals = _totals(start, end, source_entries, dest_entries, result.actions, dest_after)
        return result

    def _split(self, entries: list[dict]) -> tuple[list[dict], list[dict]]:
        """Your entries in the mapped projects are this tool's copies; any others are left alone."""
        copies = [e for e in entries if e["project"]["id"] in self.managed_projects]
        return copies, [e for e in entries if e["project"]["id"] not in self.managed_projects]

    def _limit_deletes(self, actions: list[Action]) -> None:
        deletes = [a for a in actions if a.kind == "delete"]
        if not self.cfg.propagate_deletes:
            for a in deletes:
                a.kind, a.reason = "skip", f"not deleted ({a.reason}) because propagate_deletes is off"
        elif len(deletes) > self.cfg.max_deletes_per_run:
            for a in deletes:
                a.kind, a.attention = "held", True
                a.reason = (
                    f"not deleted ({a.reason}): this run would delete {len(deletes)} entries, "
                    f"more than max_deletes_per_run ({self.cfg.max_deletes_per_run})"
                )

    def _apply(self, action: Action) -> None:
        try:
            if action.kind == "create":
                action.dest = self.dest.create_time_entry(create_payload(action))
            elif action.kind == "update":
                self.dest.update_time_entry(action.dest["id"], update_payload(action))
            elif action.kind == "delete":
                self.dest.delete_time_entry(action.dest["id"])
        except AuthError:
            raise
        except OutcomeUnknown as e:
            action.error = f"{e}; unclear whether it was created, so the next run checks before creating it again"
        except HarvestError as e:
            action.error = str(e)

def _totals(
    start: date,
    end: date,
    source_entries: list[dict],
    dest_before: list[dict],
    actions: list[Action],
    dest_after: list[dict] | None,
) -> dict[str, DayTotal]:
    days = {(start + timedelta(days=n)).isoformat(): DayTotal() for n in range((end - start).days + 1)}
    # Every side is counted at the precision that's copied, so a timed entry never differs by a rounding.
    for e in source_entries:
        if not e.get("is_running") and e["spent_date"] in days:
            days[e["spent_date"]].yours += hours_of(e)
    for e in dest_before:
        if e["spent_date"] in days:
            days[e["spent_date"]].theirs_before += hours_of(e)

    if dest_after is None:  # a preview: project what the client side will hold
        projected = {e["id"]: (e["spent_date"], hours_of(e)) for e in dest_before}
        for a in actions:
            if a.kind == "create":
                projected[f"new {a.source_id}"] = (a.desired.spent_date, a.desired.hours)
            elif a.kind == "update":
                projected[a.dest["id"]] = (a.desired.spent_date, a.desired.hours)
            elif a.kind == "delete":
                projected.pop(a.dest["id"], None)
        after = projected.values()
    else:
        after = [(e["spent_date"], hours_of(e)) for e in dest_after]
    for day, hours in after:
        if day in days:
            days[day].theirs_after += hours

    for total in days.values():
        total.yours, total.theirs_before, total.theirs_after = (
            round(total.yours, 2),
            round(total.theirs_before, 2),
            round(total.theirs_after, 2),
        )
    return {day: t for day, t in days.items() if t.yours or t.theirs_before or t.theirs_after}
