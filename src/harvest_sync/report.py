"""Plain-text reports. Lines marked !! need a person to look at them."""

from __future__ import annotations

from collections import Counter

from harvest_sync.reconcile import Action
from harvest_sync.submit import SubmitResult
from harvest_sync.sync import CHANGES, DayTotal, SyncResult

KINDS = ("create", "update", "delete", "unchanged", "skip", "conflict", "held", "failed")
APPLIED_LABELS = {"create": "created", "update": "updated", "delete": "deleted", "skip": "skipped"}
PREVIEW_LABELS = {"create": "to create", "update": "to update", "delete": "to delete", "skip": "to skip"}
TOTAL_FIELDS = ("yours", "theirs_before", "theirs_after")
DAY_WIDTH = 10
APPLIED_COLUMNS = (("Yours", "yours", 6), ("Theirs", "theirs_after", 6))  # title, DayTotal field, width
PREVIEW_COLUMNS = (("Yours", "yours", 6), ("Theirs now", "theirs_before", 10), ("Theirs after", "theirs_after", 12))
NOTE_WIDTH = 60


def sync_report(result: SyncResult) -> str:
    mode = "changes applied" if result.applied else "preview only, nothing written (add --apply to make these changes)"
    lines = [f"Sync {result.start} to {result.end}: {mode}"]
    lines += [f"   -- {note}" for note in result.notes]

    shown = [a for a in result.actions if a.kind != "unchanged" or a.attention or a.error]
    if shown:
        lines.append("")
        ordered = sorted(shown, key=lambda a: (a.day, a.source_id is None, a.source_id or 0))
        lines += [_action_line(a, result.applied) for a in ordered]
    if result.unsettled:
        lines.append("")
        lines.append(
            f"!! After applying, comparing again still finds {result.unsettled} change(s) to make: "
            "their Harvest may have stored something other than what was sent."
        )

    if result.unmanaged:
        lines.append("")
        lines.append("Your entries in their Harvest outside the mapped projects (left alone):")
        for e in result.unmanaged:
            lines.append(
                f"!! {e['spent_date']}  {e['project']['name']}  {e['task']['name']}  {_hours(e.get('hours'))}  {_note(e.get('notes'))}"
            )

    if result.totals:
        lines += ["", *_totals_table(result)]

    counts = Counter("failed" if a.error else a.kind for a in result.actions)
    labels = APPLIED_LABELS if result.applied else PREVIEW_LABELS
    summary = ", ".join(f"{counts[k]} {labels.get(k, k)}" for k in KINDS if counts[k]) or "nothing to do"
    if result.needs_attention:
        summary += f". {result.attention_count} line(s) marked !! need attention."
    elif not result.applied and any(a.kind in CHANGES for a in result.actions):
        summary += ". Both sides will match once applied."
    else:
        summary += ". Both sides match."
    lines += ["", f"Summary: {summary}"]
    return "\n".join(lines)


def submit_report(result: SubmitResult) -> str:
    period = f"{result.period_start} to {result.period_end}"
    headline = {
        "would submit": f"Would submit {result.hours:.2f} h for {period} (preview only; add --apply to submit).",
        "submitted": f"Submitted {result.hours:.2f} h for {period} for approval.",
        "already submitted": f"{period} is already submitted.",
        "nothing to submit": f"Nothing to submit for {period}.",
        "held": f"Not submitting {period}.",
        "failed": f"Submitting {period} failed.",
    }[result.status]
    flag = "!! " if result.attention else ""
    lines = [flag + headline] + [f"   {reason}" for reason in result.reasons]
    if result.sync:
        lines += ["", sync_report(result.sync)]
    return "\n".join(lines)


def _totals_table(result: SyncResult) -> list[str]:
    columns = APPLIED_COLUMNS if result.applied else PREVIEW_COLUMNS

    def row(day: str, total: DayTotal) -> str:
        return f"{day:<{DAY_WIDTH}}" + "".join(f"  {getattr(total, name):{width}.2f}" for _, name, width in columns)

    heading = f"{'Day':<{DAY_WIDTH}}" + "".join(f"  {title:>{width}}" for title, _, width in columns)
    rule = "-" * DAY_WIDTH + "".join("  " + "-" * width for _, _, width in columns)
    days = sorted(result.totals.items())
    # The whole period, to check against your invoice; always shown so it's always in the same place.
    total = DayTotal(*(round(sum(getattr(t, f) for _, t in days), 2) for f in TOTAL_FIELDS))
    rows = [row(day, t) + ("  !! differs" if t.differs else "") for day, t in days]
    return [heading, rule, *rows, rule, row("Total", total)]


def _action_line(a: Action, applied: bool) -> str:
    flag = "!!" if a.attention or a.error else "  "
    kind = "failed" if a.error else APPLIED_LABELS.get(a.kind, a.kind) if applied else a.kind
    if a.desired and a.changes:
        changes = []
        for key, (old, new) in a.changes.items():
            if key == "hours":
                changes.append(f"hours {old:.2f} -> {new:.2f}")
            elif key == "notes":
                changes.append(f"notes {_note(old)} -> {_note(new)}")
            elif key == "spent_date":
                changes.append(f"date {old} -> {new}")
            elif key == "task_id":
                changes.append(f"task {a.dest['task']['name']} -> {a.desired.dest_task}")
            else:
                changes.append(f"project {a.dest['project']['name']} -> their project {new}")
        detail = f"{a.desired.source_task} -> {a.desired.dest_task}  " + "; ".join(changes)
    elif a.desired:
        detail = f"{a.desired.source_task} -> {a.desired.dest_task}  {_hours(a.desired.hours)}  {_note(a.desired.notes)}"
    elif a.dest:
        detail = f"their {a.dest['task']['name']}  {_hours(a.dest.get('hours'))}  {_note(a.dest.get('notes'))}"
    else:
        detail = f"your entry {a.source_id}"
    line = f"{flag} {kind:<9} {a.day:<10}  {detail}"
    for extra in (a.reason, a.warning, a.error):
        if extra:
            line += f"\n{'':25}{extra}"
    return line


def _hours(hours: float | None) -> str:
    return f"{float(hours or 0):.2f} h"


def _note(text: str | None) -> str:
    text = " / ".join((text or "").split("\n")).strip()
    if len(text) > NOTE_WIDTH:
        text = text[: NOTE_WIDTH - 3] + "..."
    return f'"{text}"'
