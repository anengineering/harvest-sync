"""Turns the names in config.toml into Harvest IDs, and decides where each of your entries goes."""

from __future__ import annotations

from dataclasses import dataclass

from harvest_sync.config import Config, TaskRule
from harvest_sync.notes import NoteFormatter


class MappingError(Exception):
    """The mapping in config.toml doesn't line up with the projects and tasks in Harvest."""


@dataclass(frozen=True)
class Desired:
    """What the client's copy of one of your entries should look like."""

    source_id: int
    spent_date: str
    project_id: int
    task_id: int
    hours: float
    notes: str
    source_task: str
    dest_task: str
    warning: str | None = None


@dataclass(frozen=True)
class Skip:
    """One of your entries that isn't synced."""

    source_id: int
    reason: str
    spent_date: str = ""
    attention: bool = False  # you need to do something about it
    remove_copy: bool = False  # its copy may go; any other skip keeps that day's copies from being deleted


@dataclass
class Mapper:
    client_id: int
    client_name: str
    project_ids: dict[int, int]  # your project ID -> their project ID
    task_rules: dict[str, TaskRule]  # your task name, casefolded -> rule
    dest_task_ids: dict[tuple[int, str], int]  # (their project ID, their task name casefolded) -> their task ID
    notes: NoteFormatter
    warnings: list[str]

    @classmethod
    def build(cls, cfg: Config, source_assignments: list[dict], dest_assignments: list[dict]) -> Mapper:
        """Resolve the config against both sides' project assignments; raises MappingError on anything unusable."""
        if cfg.source_client is None:
            raise MappingError("set source.client in config.toml to the client whose time is synced")
        if not cfg.project_map:
            raise MappingError("add at least one [[project_map]] rule to config.toml")
        if not cfg.task_map:
            raise MappingError("add at least one [[task_map]] rule to config.toml")

        client_id, client_name = _find_client(cfg.source_client, source_assignments)
        client_projects = [a for a in source_assignments if a["client"]["id"] == client_id]

        project_ids: dict[int, int] = {}
        dest_projects: dict[int, dict] = {}
        for rule in cfg.project_map:
            dest = _find_project(rule.dest, dest_assignments, "isn't one of the projects you're assigned to in their account")
            dest_projects[dest["project"]["id"]] = dest
            for name in rule.sources:
                source = _find_project(name, client_projects, f"isn't one of your projects for {client_name}")
                project_ids[source["project"]["id"]] = dest["project"]["id"]

        dest_task_ids: dict[tuple[int, str], int] = {}
        for project_id, dest in dest_projects.items():
            tasks = {
                ta["task"]["name"].strip().casefold(): ta["task"]["id"]
                for ta in dest["task_assignments"]
                if ta.get("is_active", True)
            }
            for rule in cfg.task_map:
                task_id = tasks.get(rule.dest.casefold())
                if task_id is None:
                    raise MappingError(
                        f'their project "{dest["project"]["name"]}" has no active task "{rule.dest}" assigned to you'
                    )
                dest_task_ids[(project_id, rule.dest.casefold())] = task_id

        warnings = []
        your_tasks = {ta["task"]["name"].strip().casefold() for a in client_projects for ta in a["task_assignments"]}
        for rule in cfg.task_map:
            for name in rule.sources:
                if name.casefold() not in your_tasks:
                    warnings.append(f'your task "{name}" isn\'t assigned to any of your projects for {client_name}')
        for a in client_projects:
            if a["project"]["id"] not in project_ids:
                warnings.append(
                    f'your project "{a["project"]["name"]}" for {client_name} isn\'t in project_map, so its entries won\'t sync'
                )

        return cls(
            client_id=client_id,
            client_name=client_name,
            project_ids=project_ids,
            task_rules={name.casefold(): rule for rule in cfg.task_map for name in rule.sources},
            dest_task_ids=dest_task_ids,
            notes=NoteFormatter(place for rule in cfg.task_map for name in rule.sources if (place := rule.location(name))),
            warnings=warnings,
        )

    def map_entry(self, entry: dict) -> Desired | Skip:
        source_id, day = entry["id"], entry["spent_date"]
        if entry.get("is_running"):
            return Skip(source_id, "timer still running; it will sync once stopped", day)
        project_id = self.project_ids.get(entry["project"]["id"])
        if project_id is None:
            return Skip(source_id, f'your project "{entry["project"]["name"]}" isn\'t in project_map', day, attention=True)
        task_name = entry["task"]["name"].strip()
        rule = self.task_rules.get(task_name.casefold())
        if rule is None:
            return Skip(source_id, f'your task "{task_name}" isn\'t in task_map', day, attention=True)
        hours = hours_of(entry)
        if hours <= 0:
            return Skip(source_id, "no hours recorded", day, remove_copy=True)
        notes, warning = self.notes.format(entry.get("notes"), rule.location(task_name), rule.default_note)
        return Desired(
            source_id=source_id,
            spent_date=entry["spent_date"],
            project_id=project_id,
            task_id=self.dest_task_ids[(project_id, rule.dest.casefold())],
            hours=hours,
            notes=notes,
            source_task=task_name,
            dest_task=rule.dest,
            warning=warning,
        )


def hours_of(entry: dict) -> float:
    """An entry's hours to the hundredth, as copied. Only the hours are copied, even from start and end times."""
    return round(float(entry.get("hours") or 0), 2)


def _find_client(wanted: str | int, assignments: list[dict]) -> tuple[int, str]:
    clients = {a["client"]["id"]: a["client"]["name"] for a in assignments}
    if isinstance(wanted, int):
        matches = [cid for cid in clients if cid == wanted]
    else:
        matches = [cid for cid, name in clients.items() if name.strip().casefold() == wanted.casefold()]
    if not matches:
        raise MappingError(f"no client {wanted!r} among your project assignments (run `harvest-sync list` to see them)")
    if len(matches) > 1:
        raise MappingError(f"more than one client is named {wanted!r}; use its ID instead")
    return matches[0], clients[matches[0]]


def _find_project(wanted: str, assignments: list[dict], missing: str) -> dict:
    """Find a project assignment by project name or code, ignoring case."""
    key = wanted.casefold()
    matches = [
        a
        for a in assignments
        if key in (a["project"]["name"].strip().casefold(), (a["project"].get("code") or "").strip().casefold())
    ]
    if not matches:
        raise MappingError(f'"{wanted}" {missing} (run `harvest-sync list` to see the names)')
    if len(matches) > 1:
        names = ", ".join(a["project"]["name"] for a in matches)
        raise MappingError(f'"{wanted}" matches more than one project ({names}); use the project code instead')
    return matches[0]
