"""Location tags on notes: with location_prefix on, an entry on your CA-DEV task reaches the client as "CA-<note>"."""

from __future__ import annotations

import re
from collections.abc import Iterable


def location_of(task_name: str) -> str | None:
    """The location part of a task code: everything before the last "-" (CA-OAK-DEV -> CA-OAK), or None."""
    head, sep, tail = task_name.strip().rpartition("-")
    if not sep or not head.strip() or not tail.strip():
        return None
    return head.strip()


class NoteFormatter:
    """Builds the note sent to the client: "<location>-<note>" with no spaces around the "-", or just the note."""

    def __init__(self, locations: Iterable[str]) -> None:
        self._canonical = {location.casefold(): location for location in locations}
        # Longest first, so a note starting "CA-OAK-" is read as CA-OAK rather than CA.
        ordered = sorted(self._canonical.values(), key=len, reverse=True)
        alternatives = "|".join(re.escape(location) for location in ordered)
        self._tag = re.compile(rf"({alternatives})\s*-\s*(.*)", re.IGNORECASE | re.DOTALL) if ordered else None

    def format(self, note: str | None, location: str | None, default_note: str | None) -> tuple[str, str | None]:
        """Return the note to send, plus a warning when you tagged it with a different location.

        With no default_note, a blank note stays blank: there's no "<location>-" on its own.
        """
        text = (note or "").strip()
        if location is None:  # a task with no location: the note is copied as written
            return text or default_note or "", None
        location = self._canonical.get(location.casefold(), location)
        tag, warning = location, None
        match = self._tag.fullmatch(text) if self._tag and text else None
        if match:
            tag = self._canonical[match.group(1).casefold()]
            text = match.group(2).strip()
            if tag.casefold() != location.casefold():
                warning = f"note is tagged {tag} but the task's location is {location}"
        body = text or default_note
        return (f"{tag}-{body}" if body else ""), warning
