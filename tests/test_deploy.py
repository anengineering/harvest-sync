"""Checks on the Docker files that don't need a Docker engine to run."""

import re
from pathlib import Path
from zoneinfo import ZoneInfo

from harvest_sync import cli

ROOT = Path(__file__).resolve().parents[1]


def lines(name):
    return [line.strip() for line in (ROOT / name).read_text(encoding="utf-8").splitlines() if line.strip()]


def allowed():
    return [line[1:].rstrip("/") for line in lines(".dockerignore") if line.startswith("!")]


def test_the_image_can_never_pick_up_your_tokens():
    entries = [line for line in lines(".dockerignore") if not line.startswith("#")]
    assert entries[0] == "*"
    assert not any(".env" in name for name in allowed())


def test_everything_the_dockerfile_copies_is_allowed_into_the_build():
    copied = []
    for line in lines("Dockerfile"):
        if line.startswith("COPY ") and "--from" not in line:
            copied += line.split()[1:-1]
    assert copied
    assert set(copied) <= set(allowed())


def test_the_scheduled_commands_are_valid():
    jobs = [line.lstrip("# ") for line in lines("deploy/crontab") if re.match(r"#?\s*[\d*]", line)]
    assert len(jobs) == 2  # the sync, and the submit that's commented out
    for job in jobs:
        program, *arguments = job.split()[5:]
        assert program == "harvest-sync"
        assert cli._parser().parse_args(arguments)


def test_the_schedule_names_a_real_time_zone():
    (setting,) = [line for line in lines("deploy/crontab") if line.startswith("CRON_TZ=")]
    assert ZoneInfo(setting.split("=")[1])
