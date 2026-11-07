"""Loads config.toml and the two access tokens, and checks them before anything runs."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from harvest_sync.notes import location_of

SOURCE_TOKEN_VAR = "HARVEST_SOURCE_TOKEN"
DEST_TOKEN_VAR = "HARVEST_DEST_TOKEN"
MAILGUN_KEY_VAR = "HARVEST_MAILGUN_API_KEY"
MAILGUN_REGIONS = ("us", "eu")
WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
DEFAULT_NOTE = "TBD"


class ConfigError(Exception):
    """The config file or environment is missing something or has an invalid value."""


@dataclass(frozen=True)
class ProjectRule:
    sources: tuple[str, ...]
    dest: str


@dataclass(frozen=True)
class TaskRule:
    sources: tuple[str, ...]
    dest: str
    default_note: str | None  # None: require_note = false, so a blank note stays blank
    location_prefix: bool = False  # true: a name with a "-" is <location>-<task>, and the location goes on its notes

    def location(self, task_name: str) -> str | None:
        return location_of(task_name) if self.location_prefix else None


@dataclass(frozen=True)
class EmailConfig:
    to: tuple[str, ...]
    sender: str
    domain: str  # your sending domain in Mailgun
    api_key: str = field(repr=False)
    region: str = MAILGUN_REGIONS[0]
    send_every_run: bool = True  # meant for a test period, then switched off
    send_on_problems: bool = True  # a failed run or anything marked !!, whatever send_every_run says


@dataclass(frozen=True)
class Config:
    contact_email: str
    timezone: ZoneInfo
    source_account_id: int
    source_token: str
    dest_account_id: int
    dest_token: str
    earliest_date: date
    source_client: str | int | None = None
    propagate_deletes: bool = True
    max_deletes_per_run: int = 10
    submit_enabled: bool = False
    week_starts_on: int = 0  # 0 = Monday, as in date.weekday()
    project_map: tuple[ProjectRule, ...] = ()
    task_map: tuple[TaskRule, ...] = ()
    email: EmailConfig | None = None  # None: no [email] section, so nothing is ever emailed

    @property
    def user_agent(self) -> str:
        return f"harvest-sync ({self.contact_email})"


def load_config(path: Path, env_file: Path | None = None) -> Config:
    path = Path(path)
    # utf-8-sig also accepts the byte-order mark that some Windows editors and PowerShell 5.1 add.
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (start from config.example.toml)") from None
    except UnicodeDecodeError:
        raise ConfigError(f"{path} isn't saved as UTF-8; save it as UTF-8 and try again") from None
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path} isn't valid TOML: {e}") from None

    if env_file is not None and not Path(env_file).is_file():
        raise ConfigError(f"env file not found: {env_file}")
    env_path = env_file or path.parent / ".env"
    try:
        load_dotenv(env_path, override=False, encoding="utf-8-sig")  # real environment variables win over the file
    except UnicodeDecodeError:
        raise ConfigError(f"{env_path} isn't saved as UTF-8; save it as UTF-8 and try again") from None

    root = _Section(raw)
    contact_email = root.text("contact_email")
    if "@" not in contact_email:
        raise ConfigError("contact_email must be an email address; Harvest requires one in the User-Agent header")
    timezone = _timezone(root.text("timezone"))

    source = root.section("source")
    source_account_id = source.number("account_id", minimum=1)
    client = source.get("client", (str, int), None)
    if isinstance(client, str):
        client = client.strip()
        if not client:
            raise ConfigError("source.client can't be blank")
    source.finish()

    destination = root.section("destination")
    dest_account_id = destination.number("account_id", minimum=1)
    destination.finish()

    sync = root.section("sync")
    earliest_date = _date(sync, "earliest_date")
    propagate_deletes = sync.get("propagate_deletes", bool, True)
    max_deletes_per_run = sync.number("max_deletes_per_run", 10)
    location_prefix = sync.get("location_prefix", bool, False)
    sync.finish()

    submit = root.section("submit")
    submit_enabled = submit.get("enabled", bool, False)
    week_start = submit.text("week_starts_on", "monday").lower()
    if week_start not in WEEKDAYS:
        raise ConfigError(f'submit.week_starts_on must be a day name such as "monday", not {week_start!r}')
    submit.finish()

    email = _email(root.section("email")) if root.has("email") else None
    project_map = tuple(_project_rule(table) for table in root.tables("project_map"))
    task_map = tuple(_task_rule(table, location_prefix) for table in root.tables("task_map"))
    root.finish()
    _no_repeats("project_map", (name for rule in project_map for name in rule.sources))
    _no_repeats("task_map", (name for rule in task_map for name in rule.sources))

    return Config(
        contact_email=contact_email,
        timezone=timezone,
        source_account_id=source_account_id,
        source_token=_token(SOURCE_TOKEN_VAR),
        dest_account_id=dest_account_id,
        dest_token=_token(DEST_TOKEN_VAR),
        earliest_date=earliest_date,
        source_client=client,
        propagate_deletes=propagate_deletes,
        max_deletes_per_run=max_deletes_per_run,
        submit_enabled=submit_enabled,
        week_starts_on=WEEKDAYS.index(week_start),
        project_map=project_map,
        task_map=task_map,
        email=email,
    )


_MISSING: Any = object()
_TYPE_NAMES = {
    str: "text in quotes",
    int: "a whole number",
    bool: "true or false",
    list: "a list",
    dict: "a section",
    date: "a date such as 2026-11-01",
}


class _Section:
    """Typed access to one table of the config file. Problems are reported by their dotted name."""

    def __init__(self, data: dict[str, Any], prefix: str = "") -> None:
        self._data = data
        self._prefix = prefix
        self._used: set[str] = set()

    def where(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def has(self, key: str) -> bool:
        return key in self._data

    def get(self, key: str, kind: type | tuple[type, ...], default: Any = _MISSING) -> Any:
        self._used.add(key)
        if key not in self._data:
            if default is _MISSING:
                raise ConfigError(f"missing setting {self.where(key)}")
            return default
        value = self._data[key]
        kinds = kind if isinstance(kind, tuple) else (kind,)
        # bool is a subclass of int, so true/false must not pass where a number is wanted.
        if not isinstance(value, kinds) or (isinstance(value, bool) and bool not in kinds):
            expected = " or ".join(_TYPE_NAMES[k] for k in kinds)
            raise ConfigError(f"{self.where(key)} must be {expected}")
        return value

    def text(self, key: str, default: Any = _MISSING) -> str:
        value = self.get(key, str, default)
        if not value.strip():
            raise ConfigError(f"{self.where(key)} can't be blank")
        return value.strip()

    def number(self, key: str, default: Any = _MISSING, minimum: int = 0) -> int:
        value = self.get(key, int, default)
        if value < minimum:
            raise ConfigError(f"{self.where(key)} must be at least {minimum}")
        return value

    def names(self, key: str) -> tuple[str, ...]:
        """A name, or a non-empty list of names."""
        value = self.get(key, (str, list))
        items = [value] if isinstance(value, str) else value
        if not items or not all(isinstance(item, str) and item.strip() for item in items):
            raise ConfigError(f"{self.where(key)} must be a name or a list of names")
        return tuple(item.strip() for item in items)

    def section(self, key: str) -> _Section:
        return _Section(self.get(key, dict, {}), f"{self.where(key)}.")

    def tables(self, key: str) -> list[_Section]:
        self._used.add(key)
        items = self._data.get(key, [])
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items):
            raise ConfigError(f"{self.where(key)} must be written as [[{self.where(key)}]] blocks")
        return [_Section(item, f"{self.where(key)}[{n}].") for n, item in enumerate(items, start=1)]

    def finish(self) -> None:
        unknown = sorted(self.where(key) for key in self._data if key not in self._used)
        if unknown:
            raise ConfigError(f"unknown setting {', '.join(unknown)} (check the spelling against config.example.toml)")


def _project_rule(table: _Section) -> ProjectRule:
    rule = ProjectRule(sources=table.names("from"), dest=table.text("to"))
    table.finish()
    return rule


def _task_rule(table: _Section, location_prefix: bool) -> TaskRule:
    require_note = table.get("require_note", bool, True)
    if not require_note and table.has("default_note"):
        raise ConfigError(f"{table.where('default_note')} can't be used with require_note = false, which leaves blank notes blank")
    rule = TaskRule(
        sources=table.names("from"),
        dest=table.text("to"),
        default_note=table.text("default_note", DEFAULT_NOTE) if require_note else None,
        location_prefix=table.get("location_prefix", bool, location_prefix),
    )
    table.finish()
    return rule


def _email(section: _Section) -> EmailConfig:
    to = section.names("to")
    sender = section.text("from")
    for address in (*to, sender):
        if "@" not in address:
            raise ConfigError(f"{address!r} in [email] must be an email address")
    region = section.text("mailgun_region", MAILGUN_REGIONS[0]).lower()
    if region not in MAILGUN_REGIONS:
        raise ConfigError(f"{section.where('mailgun_region')} must be one of {', '.join(MAILGUN_REGIONS)}")
    email = EmailConfig(
        to=to,
        sender=sender,
        domain=section.text("mailgun_domain"),
        api_key=_token(MAILGUN_KEY_VAR),
        region=region,
        send_every_run=section.get("send_every_run", bool, True),
        send_on_problems=section.get("send_on_problems", bool, True),
    )
    section.finish()
    return email


def _no_repeats(table: str, names: Any) -> None:
    seen: set[str] = set()
    for name in names:
        if name.casefold() in seen:
            raise ConfigError(f"{name!r} appears in more than one [[{table}]] rule")
        seen.add(name.casefold())


def _date(section: _Section, key: str) -> date:
    value = section.get(key, (date, str))
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            raise ConfigError(f"{section.where(key)} must be a date such as 2026-11-01") from None
    if isinstance(value, datetime):  # a datetime is also a date, but a time makes no sense here
        raise ConfigError(f"{section.where(key)} must be a date without a time, such as 2026-11-01")
    return value


def _timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError(f"timezone {name!r} isn't a known time zone (use a name such as America/Los_Angeles)") from None


def _token(var: str) -> str:
    value = os.environ.get(var, "").strip()
    if not value:
        raise ConfigError(f"{var} isn't set; put it in the environment or in a .env file next to the config")
    return value
