"""Submit a finished week's timesheet on the client's side, after a clean sync of that week.

Harvest marks the Approvals API beta, so this relies only on whether the request succeeded, and
then confirms the result through the `approval_status` of the time entries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

from harvest_sync.config import Config
from harvest_sync.harvest import HarvestError, OutcomeUnknown
from harvest_sync.sync import Syncer, SyncResult

SUBMITTED = ("submitted", "approved")
CONFIRM_ATTEMPTS = 3
CONFIRM_WAIT = 5  # seconds between checks that the submission went through


def week_containing(day: date, week_starts_on: int) -> tuple[date, date]:
    start = day - timedelta(days=(day.weekday() - week_starts_on) % 7)
    return start, start + timedelta(days=6)


def last_finished_week(today: date, week_starts_on: int) -> tuple[date, date]:
    this_week, _ = week_containing(today, week_starts_on)
    return this_week - timedelta(days=7), this_week - timedelta(days=1)


@dataclass
class SubmitResult:
    period_start: date  # the week's start, or earliest_date if that's later
    period_end: date
    status: str = ""  # would submit | submitted | already submitted | nothing to submit | held | failed
    reasons: list[str] = field(default_factory=list)
    sync: SyncResult | None = None
    hours: float = 0.0
    attention: bool = False

    @property
    def needs_attention(self) -> bool:
        return self.attention or bool(self.sync and self.sync.needs_attention)


class Submitter:
    def __init__(self, cfg: Config, syncer: Syncer, sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg = cfg
        self.syncer = syncer
        self.dest = syncer.dest
        self._sleep = sleep

    def run(self, week_start: date, week_end: date, today: date, apply: bool) -> SubmitResult:
        result = SubmitResult(max(week_start, self.cfg.earliest_date), week_end)
        if week_end < self.cfg.earliest_date:
            return self._finish(result, "nothing to submit", f"the week ends before earliest_date ({self.cfg.earliest_date})")
        if week_end >= today:
            return self._finish(result, "held", f"the week isn't over until the end of {week_end}", attention=True)

        entries = self._entries(result)
        if any(e.get("approval_status") in SUBMITTED for e in entries):
            pending = sum(1 for e in entries if e.get("approval_status") not in SUBMITTED)
            if pending:
                why = f"{pending} entry(ies) were added after it was submitted; submit them in their Harvest"
                return self._finish(result, "already submitted", why, attention=True)
            return self._finish(result, "already submitted")

        result.sync = self.syncer.run(result.period_start, week_end, apply=apply)
        if result.sync.needs_attention:
            return self._finish(result, "held", "the sync for this week needs attention first (see below)", attention=True)
        if result.sync.running_timers or any(e.get("is_running") for e in entries):
            return self._finish(result, "held", "a timer is still running in this week; stop it first", attention=True)
        result.hours = round(sum(t.theirs_after for t in result.sync.totals.values()), 2)
        if result.hours <= 0:
            return self._finish(result, "nothing to submit", "no time recorded this week")
        if not apply:
            return self._finish(result, "would submit")

        try:
            self.dest.submit_timesheet(result.period_start, week_end)
        except OutcomeUnknown:
            pass  # don't resubmit (that notifies the approver again); check below whether it went through
        except HarvestError as e:
            return self._finish(result, "failed", str(e), attention=True)
        for attempt in range(CONFIRM_ATTEMPTS):
            entries = self._entries(result)
            if entries and all(e.get("approval_status") in SUBMITTED for e in entries):
                return self._finish(result, "submitted")
            if attempt + 1 < CONFIRM_ATTEMPTS:
                self._sleep(CONFIRM_WAIT)
        why = "Harvest didn't confirm the submission; check their Harvest before trying again"
        return self._finish(result, "failed", why, attention=True)

    def _entries(self, result: SubmitResult) -> list[dict]:
        return self.dest.time_entries(user_id=self.syncer.dest_user, start=result.period_start, end=result.period_end)

    @staticmethod
    def _finish(result: SubmitResult, status: str, reason: str = "", attention: bool = False) -> SubmitResult:
        result.status = status
        result.attention = attention
        if reason:
            result.reasons.append(reason)
        return result
