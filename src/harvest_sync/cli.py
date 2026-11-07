"""The harvest-sync command line."""

from __future__ import annotations

import argparse
import logging
import sys
import traceback
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from harvest_sync import __version__, notify
from harvest_sync.config import WEEKDAYS, Config, ConfigError, EmailConfig, load_config
from harvest_sync.harvest import AuthError, HarvestClient, HarvestError
from harvest_sync.mapping import Mapper, MappingError
from harvest_sync.report import submit_report, sync_report
from harvest_sync.submit import Submitter, last_finished_week, week_containing
from harvest_sync.sync import SyncError, Syncer, sync_window

EXIT_OK = 0
EXIT_ATTENTION = 1  # it ran, but something needs a person to look at it
EXIT_FAILED = 2  # it couldn't run: config, token or network problem

TOKENS_URL = "https://id.getharvest.com/developers"
HEADER = f"harvest-sync {__version__}"
EMAIL_HELP = "email the result, as the [email] settings say (meant for scheduled runs)"

log = logging.getLogger("harvest_sync")


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):  # never crash on an unusual character in a Windows console
            stream.reconfigure(errors="backslashreplace")
    args = _parser().parse_args(argv)
    _setup_logging(args.verbose, args.log_file)
    print(f"\n{HEADER}\n", flush=True)  # flushed so a console shows it above anything logged to stderr
    cfg, code, report, failure = None, EXIT_OK, "", ""
    try:
        cfg = load_config(args.config, args.env_file)
        source, dest = make_clients(cfg)
        code, report = args.command(args, cfg, source, dest)
    except AuthError as e:
        failure = f"{e}\nCheck that side's token and account_id; tokens are managed at {TOKENS_URL}"
    except (ConfigError, MappingError, SyncError, HarvestError) as e:
        failure = str(e)
    except Exception:  # a bug rather than a known failure, but an unattended run must still report it
        if not args.email:
            raise
        failure = traceback.format_exc()
    if failure:
        log.error("%s", failure)
        code = EXIT_FAILED
    else:
        _emit(report, args.log_file)
    return _email_result(args, cfg, code, failure or report) if args.email else code


def _email_result(args: argparse.Namespace, cfg: Config | None, code: int, text: str) -> int:
    """Email a run's result if the config asks for that, and return the exit code to finish with."""
    if cfg is None:
        log.error("couldn't email this failure, because the email settings are in the config that failed to load")
        return code
    if cfg.email is None or not notify.should_send(cfg.email, problem=code != EXIT_OK):
        return code
    status = {EXIT_OK: "OK", EXIT_ATTENTION: "needs attention"}.get(code, "FAILED")
    try:
        notify.send(cfg.email, f"harvest-sync {args.name} {status}: {cfg.source_client}", f"{HEADER}\n\n{text}")
    except notify.EmailError as e:
        log.error("couldn't send the email: %s", e)
        return max(code, EXIT_ATTENTION)
    return code


def make_clients(cfg: Config) -> tuple[HarvestClient, HarvestClient]:
    source = HarvestClient(cfg.source_token, cfg.source_account_id, cfg.user_agent, name="your account")
    dest = HarvestClient(cfg.dest_token, cfg.dest_account_id, cfg.user_agent, name="their account")
    return source, dest


def run_check(args: argparse.Namespace, cfg: Config, source: HarvestClient, dest: HarvestClient) -> tuple[int, str]:
    out = _Checklist()
    out.heading("Your account")
    source_ok = _check_account(source, out)
    if source_ok:
        company = source.company()
        if company is not None:  # either mode works on your side
            if company.get("wants_timestamp_timers"):
                out.ok("time tracking: start and end times; their copies get just the hours")
            else:
                out.ok("time tracking: durations")
    out.heading("Their account")
    dest_ok = _check_account(dest, out)
    if dest_ok:
        company = dest.company()
        if company is None:
            out.note("time tracking: your role can't read their company settings; assuming durations")
        elif company.get("wants_timestamp_timers"):
            out.flag("their account tracks start and end times; harvest-sync can only copy to an account that tracks durations")
        else:
            out.ok("time tracking: durations")
        week_start = (company or {}).get("week_start_day")
        if week_start and week_start.lower() != WEEKDAYS[cfg.week_starts_on]:
            out.flag(f"their week starts on {week_start}, but submit.week_starts_on is {WEEKDAYS[cfg.week_starts_on]}")
        try:
            approvals = dest.approvals_enabled()
        except HarvestError:
            approvals = None
        out.note({True: "timesheet approval: on", False: "timesheet approval: off"}.get(approvals, "timesheet approval: your role can't tell"))

    out.heading("Mapping")
    if not (source_ok and dest_ok):
        out.note("skipped until both tokens work")
    else:
        try:
            mapper = Mapper.build(cfg, source.project_assignments(), dest.project_assignments())
        except MappingError as e:
            out.flag(str(e))
        else:
            out.ok(f'client "{mapper.client_name}" (ID {mapper.client_id})')
            for rule in cfg.project_map:
                out.ok(f"your {', '.join(rule.sources)} -> their {rule.dest}")
            for rule in cfg.task_map:
                blank = dict.fromkeys(mapper.notes.format(None, rule.location(name), rule.default_note)[0] for name in rule.sources)
                outcome = f"blank notes become {', '.join(blank)}" if rule.default_note else "blank notes stay blank"
                out.ok(f"your {', '.join(rule.sources)} -> their {rule.dest} ({outcome})")
            for warning in mapper.warnings:
                out.flag(warning)

    out.heading("Email")
    out.note(_email_summary(cfg.email))

    out.heading("")
    out.heading("Ready to sync." if not out.flagged else f"{out.flagged} item(s) marked !! need attention.")
    return (EXIT_ATTENTION if out.flagged else EXIT_OK), out.text()


def run_list(args: argparse.Namespace, cfg: Config, source: HarvestClient, dest: HarvestClient) -> tuple[int, str]:
    lines = []
    for title, client in (("Your account", source), ("Their account", dest)):
        name = _account_name(client)
        lines.append(f"{title}: " + (f'"{name}" (account {client.account_id})' if name else f"account {client.account_id}"))
        by_client: dict[tuple[str, int], list[dict]] = {}
        for a in client.project_assignments():
            by_client.setdefault((a["client"]["name"], a["client"]["id"]), []).append(a)
        for (client_name, client_id), assignments in sorted(by_client.items()):
            lines.append(f"  {client_name} (client ID {client_id})")
            for a in sorted(assignments, key=lambda a: a["project"]["name"].casefold()):
                code = a["project"].get("code")
                lines.append(f"    {a['project']['name']}" + (f" [code {code}]" if code else ""))
                tasks = sorted(ta["task"]["name"] for ta in a["task_assignments"] if ta.get("is_active", True))
                lines.append(f"      tasks: {', '.join(tasks) or '(none)'}")
        lines.append("")
    return EXIT_OK, "\n".join(lines).rstrip()


def run_test_email(args: argparse.Namespace, cfg: Config, source: HarvestClient, dest: HarvestClient) -> tuple[int, str]:
    if cfg.email is None:
        raise ConfigError("there's no [email] section in the config file")
    try:
        notify.send(cfg.email, "harvest-sync test email", f"{HEADER}\n\nIf you can read this, the [email] settings work.")
    except notify.EmailError as e:
        raise ConfigError(f"couldn't send the test email: {e}") from None
    return EXIT_OK, f"Sent a test email to {', '.join(cfg.email.to)}."


def _email_summary(email: EmailConfig | None) -> str:
    if email is None:
        return "not set up; scheduled runs won't email anything"
    if email.send_every_run:
        when = "after every run with --email"
    elif email.send_on_problems:
        when = "only when a run with --email has a problem"
    else:
        return "switched off in the [email] section"
    return f"{when}, to {', '.join(email.to)} through Mailgun ({email.domain})"


def today(cfg: Config) -> date:
    """Today's date where you are, not where the server is."""
    return datetime.now(cfg.timezone).date()


def run_sync(args: argparse.Namespace, cfg: Config, source: HarvestClient, dest: HarvestClient) -> tuple[int, str]:
    start, end, notes = sync_window(cfg, today(cfg), args.start, args.end)
    if start > end:
        return EXIT_OK, f"Nothing to sync from {start} to {end}; nothing before earliest_date ({cfg.earliest_date}) is ever synced."
    result = Syncer(cfg, source, dest).run(start, end, apply=args.apply)
    result.notes[:0] = notes
    return (EXIT_ATTENTION if result.needs_attention else EXIT_OK), sync_report(result)


def run_submit(args: argparse.Namespace, cfg: Config, source: HarvestClient, dest: HarvestClient) -> tuple[int, str]:
    if args.apply and not cfg.submit_enabled:
        raise ConfigError("submitting is switched off; set enabled = true under [submit] in config.toml first")
    now = today(cfg)
    if args.week_of:
        week_start, week_end = week_containing(args.week_of, cfg.week_starts_on)
    else:
        week_start, week_end = last_finished_week(now, cfg.week_starts_on)
    result = Submitter(cfg, Syncer(cfg, source, dest)).run(week_start, week_end, now, apply=args.apply)
    return (EXIT_ATTENTION if result.needs_attention else EXIT_OK), submit_report(result)


class _Checklist:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.flagged = 0

    def heading(self, text: str) -> None:
        self.lines.append(text)

    def ok(self, text: str) -> None:
        self.lines.append(f"  OK  {text}")

    def note(self, text: str) -> None:
        self.lines.append(f"  --  {text}")

    def flag(self, text: str) -> None:
        self.flagged += 1
        self.lines.append(f"  !!  {text}")

    def text(self) -> str:
        return "\n".join(self.lines)


def _check_account(client: HarvestClient, out: _Checklist) -> bool:
    try:
        user = client.me()
    except HarvestError as e:
        out.flag(str(e))
        try:
            reachable = [f'{a["name"]} ({a["id"]})' for a in client.accounts() if a.get("product") == "harvest"]
        except HarvestError:
            reachable = []
        if reachable:
            out.note(f"this token can reach: {', '.join(reachable)}; is account_id right?")
        return False
    person = f'{user.get("first_name", "")} {user.get("last_name", "")}'.strip() or f'user {user.get("id")}'
    roles = ", ".join(user.get("access_roles") or []) or "role unknown"
    name = _account_name(client)
    where = f'"{name}" (account {client.account_id})' if name else f"account {client.account_id}"
    out.ok(f"token works: {person} ({roles}) in {where}")
    return True


def _account_name(client: HarvestClient) -> str | None:
    try:
        for account in client.accounts():
            if account.get("id") == client.account_id:
                return account.get("name")
    except HarvestError:
        pass
    return None


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harvest-sync",
        description="One-way sync of time entries from your Harvest account to a client's Harvest account.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=Path("config.toml"), help="config file (default: ./config.toml)")
    parser.add_argument("--env-file", type=Path, help="file holding the tokens (default: .env next to the config file)")
    parser.add_argument("--log-file", type=Path, help="also append all output to this file")
    parser.add_argument("-v", "--verbose", action="store_true", help="log every API request")
    parser.set_defaults(email=False)
    commands = parser.add_subparsers(title="commands", required=True, metavar="COMMAND")
    _command(commands, "check", run_check, "check the tokens, accounts and mapping (read-only)")
    _command(commands, "list", run_list, "list the projects and tasks you can log time to on both sides (read-only)")
    sync = _command(commands, "sync", run_sync, "copy your time entries to their Harvest (only a preview unless --apply)")
    sync.add_argument("--from", dest="start", type=_date_arg, metavar="YYYY-MM-DD", help="first day (default: the 1st of the month --to is in)")
    sync.add_argument("--to", dest="end", type=_date_arg, metavar="YYYY-MM-DD", help="last day (default: today)")
    sync.add_argument("--apply", action="store_true", help="make the changes; without it, only show them")
    sync.add_argument("--email", action="store_true", help=EMAIL_HELP)
    submit = _command(
        commands, "submit", run_submit, "sync a finished week, then submit it for approval (only a preview unless --apply)"
    )
    submit.add_argument("--week-of", type=_date_arg, metavar="YYYY-MM-DD", help="any day in the week (default: last finished week)")
    submit.add_argument("--apply", action="store_true", help="sync and submit; needs [submit] enabled = true")
    submit.add_argument("--email", action="store_true", help=EMAIL_HELP)
    _command(commands, "test-email", run_test_email, "send a test email using the [email] settings")
    return parser


def _date_arg(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} isn't a date; use YYYY-MM-DD") from None


def _command(commands: argparse._SubParsersAction, name: str, handler: Callable, help_text: str) -> argparse.ArgumentParser:
    command = commands.add_parser(name, help=help_text, description=help_text)
    command.set_defaults(command=handler, name=name)
    return command


def _setup_logging(verbose: bool, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _emit(report: str, log_file: Path | None) -> None:
    print(report)
    if log_file:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now().isoformat(timespec='seconds')}  {HEADER}\n{report}\n")
