# harvest-sync

One-way sync of time entries from your own Harvest account to a client's Harvest account. It's for anyone who keeps all their time in their own Harvest but has a client who wants it in theirs too. You enter time once, and harvest-sync copies that client's share across, using the Harvest login the client gave you. It can also submit your finished weeks for approval.

It uses only Harvest API v2, and it never changes anything in your own account.

## How it works

Each run covers the current month so far, and each new month starts fresh. A run does this:

1. Read your entries for the client from your Harvest.
2. Read your entries in their Harvest for the same days.
3. Create, update or delete entries on their side so they match yours.

**harvest-sync manages every entry of yours in their Harvest that sits in a project `[[project_map]]` maps to.** So don't type time into those projects directly in their Harvest: harvest-sync would treat it as a stray copy and overwrite or delete it. Your entries in their other projects are left alone and flagged.

It recognises its copies by content, not by a hidden marker. (Harvest only keeps a link back to your entry if it includes a URL, and that turns the note into a hyperlink.) Each run matches your entries to the copies one day at a time:

1. Exact matches first: same date, project, task, hours and note.
2. Each entry you changed is paired with the most similar copy left that day, and that copy is updated.
3. Anything left over is created (your side) or deleted (their side).

This brings two guarantees:

- **Safe to re-run.** Running it twice changes nothing the second time.
- **Any machine.** It can run from more than one machine without creating duplicates.

Moving an entry to another date in the same month creates a copy on the new date and deletes the old one. To change a finished month, for example after the 1st, sync that month explicitly: `sync --to 2026-11-30` covers all of November. Moving an entry from one month to another needs a run for each month.

**Your side always wins.** Edits, deletions and moves on your side are copied over, and any change made on their side to a synced entry is overwritten. The exception is an entry their Harvest has locked, for example because it was approved or invoiced; harvest-sync reports those instead of trying to change them.

### What a copied entry looks like

| Your entry | Their entry |
|---|---|
| Project | Mapped by `[[project_map]]` |
| Task | Mapped by `[[task_map]]`; several of your tasks can map to one of theirs |
| Date and hours | The same (exact hours, not rounded). If you track start and end times, only the hours are copied |
| Note | Your note, filled in if it's blank, with a location in front if you use them |

`check` shows what a blank note becomes for each task, so you can spot problems before syncing.

#### Blank notes

Notes are required by default, because some clients want one on every entry.

- **`default_note`** sets what a blank note becomes in that rule. If a rule doesn't set one, it's `TBD`.
- **`require_note = false`** leaves blank notes blank. It can't be combined with `default_note`.

| Your task | The rule has | Your note | Their note |
|---|---|---|---|
| Development | `default_note = "Engineering work"` | *(blank)* | `Engineering work` |
| Development | no `default_note` | *(blank)* | `TBD` |
| Development | `require_note = false` | *(blank)* | *(blank)* |
| Development | | Reviewed schematics | `Reviewed schematics` |

#### Location prefixes (optional)

These are off by default. Their purpose is tracking taxable income in each location: your own tasks keep the time separate by where the work was done, such as CA-DEV and OR-DEV, and each note sent to the client says where that time was worked. If you don't need that, leave them off, and task names are just names.

To use them, set `location_prefix = true` under `[sync]`. Then:

- **A task code with a `-`** is read as `<location>-<task>`. Everything before the last `-` is the location, so CA-OAK-DEV gives CA-OAK, and the note starts with that location.
- **A task code with no `-`** has no location, so its note is copied as written.
- **A task code with a `-` that isn't a location**, such as `Non-Billable`, needs `location_prefix = false` in its own `[[task_map]]` rule. Otherwise `Non` would be read as the location.

| Your task | The rule has | Your note | Their note |
|---|---|---|---|
| CA-DEV | `default_note = "EE"` | *(blank)* | `CA-EE` |
| CA-OAK-DEV | `default_note = "EE"` | *(blank)* | `CA-OAK-EE` |
| CA-DEV | no `default_note` | *(blank)* | `CA-TBD` |
| CA-DEV | `require_note = false` | *(blank)* | *(blank)*, with no location either |
| CA-DEV | | Reviewed schematics | `CA-Reviewed schematics` |
| CA-DEV | | ca - Reviewed schematics | `CA-Reviewed schematics` (your tag, tidied) |
| CA-OAK-DEV | | CA-Reviewed schematics | `CA-Reviewed schematics`, flagged because the tag doesn't match the task |
| ADMIN | | Paperwork | `Paperwork` |
| Non-Billable | `location_prefix = false` | Paperwork | `Paperwork` |

### Safety rules

- **Nothing before `earliest_date`.** Entries dated before it are never synced. Set it to the day the client started using Harvest, or the day you want syncing to begin.
- **Preview by default.** `sync` and `submit` only show what they would do until you add `--apply`.
- **Deletes are limited.**
  - A copy is deleted only when none of your entries matches it any more, because you deleted, moved or changed the entry.
  - On a day where one of your entries is still running or has no mapping rule, no copies are deleted, since one of them may belong to that entry.
  - If one run would delete more than `max_deletes_per_run` entries, it deletes none and flags the run instead.
- **Checked after applying.** After writing, it reads their side again and compares once more. If anything is still left to change, for example because Harvest stored something other than what was sent, that's flagged. A create whose result is unclear (e.g. a network failure) is never retried blindly; the next run finds the entry if it was created.
- **Problems are flagged, not skipped silently.** Each of these is marked `!!` in the output:
  - entries in projects or tasks with no mapping rule
  - copies not deleted because of a running timer or an unmapped entry that day
  - locked entries
  - your entries in their Harvest outside the mapped projects
  - days where the two sides' totals differ

Running timers are skipped and picked up once stopped.

## Setup

You need:

- Python 3.11 or later and [uv](https://docs.astral.sh/uv/) to run it on your own computer, and [Docker](#running-it-on-a-server-with-docker) to run it on a server.
- A Harvest login on each side. Your own account can track durations or start and end times, but the client's must track durations.

Then:

1. **Create two personal access tokens** at <https://id.getharvest.com/developers>: one signed in with your own Harvest login, one signed in with the login the client gave you. The page shows each token's account IDs.
2. **Install:** run `git clone https://github.com/anengineering/harvest-sync.git`, then `uv sync` in that folder.
3. **Configure:**
   - Copy `config.example.toml` to `config.toml` and fill it in.
   - Copy `.env.example` to `.env` and paste in the two tokens.
   - Git ignores both files. Tokens never go in `config.toml`.
4. **Check:** `uv run harvest-sync check` confirms both tokens and accounts, your role on each side, and every name in the mapping. `uv run harvest-sync list` prints the exact project and task names on both sides, so you can fill in the mapping.
5. **Preview, then apply,** starting with one day you've logged time for the client:
   ```
   uv run harvest-sync sync --from YYYY-MM-DD --to YYYY-MM-DD
   uv run harvest-sync sync --from YYYY-MM-DD --to YYYY-MM-DD --apply
   ```
   Read the first preview closely: any time you already have in the mapped projects in their Harvest counts as copies, so it may be changed or deleted. After applying, look at the result in their Harvest. Then sync the rest the same way: preview first, then add `--apply`.

## Commands

All commands take `--config PATH` (default `./config.toml`), `--env-file PATH` (default: `.env` next to the config), `--log-file PATH` and `-v` (log every API request). Put these before the command name.

Every command's output starts with `harvest-sync` and its version. `uv run harvest-sync --version` prints just that.

| Command | What it does |
|---|---|
| `check` | Checks the tokens, accounts, time-tracking mode, approval setting and mapping. Read-only. |
| `list` | Lists the projects and tasks you can log time to on both sides. Read-only. |
| `sync [--from DATE] [--to DATE] [--apply] [--email]` | Syncs the current month up to today. With only `--to`, it syncs that date's month up to it, so `--to 2026-11-30` is all of November. Never before `earliest_date`. |
| `submit [--week-of DATE] [--apply] [--email]` | Syncs a finished week, then submits it for approval. Defaults to last week. |
| `test-email` | Sends a test email through Mailgun using the `[email]` settings. |

`sync` ends with each day's hours on both sides and a total for the whole period, which is handy to check against an invoice. Days with no hours on either side aren't listed.

Exit codes: `0` means both sides match, or in a preview that they will once applied. `1` means something needs attention (the lines marked `!!`). `2` means it couldn't run (config, token or network problem).

## Running it on a server with Docker

The container stays up and runs harvest-sync on a schedule, using [supercronic](https://github.com/aptible/supercronic) inside it, so the server needs nothing but Docker. It's the only server deployment this project provides.

First create `config.toml` and `.env` as in [Setup](#setup). Then run these in this folder, either on the server or on your own computer with a Docker context pointed at the server:

```
docker compose up -d --build
docker compose exec harvest-sync harvest-sync check
```

The image is built with your `config.toml` and `deploy/crontab` inside it, so don't push it to a public registry. Your tokens aren't in it: Compose reads `.env` from this folder and passes them to the container when it starts.

- **Schedule:** `deploy/crontab` syncs every evening at 23:30 Pacific. Edit it for the time and time zone you want.
- **Changing `config.toml` or `deploy/crontab`:** run `docker compose up -d --build`. For `.env`, `docker compose up -d` is enough.
- **Running it by hand:** `docker compose exec harvest-sync harvest-sync sync`, then the same command with `--apply`.
- **Logs and failures:** `docker compose logs harvest-sync` shows each run's report. A run that needs attention exits with `1`, and that's logged too. To hear about problems without reading the logs, set up [email reports](#email-reports).
- **Missed runs aren't made up.** If the server is down at 23:30, that night's sync doesn't happen. The next run covers the month so far, so this only matters on a month's last day. Before you invoice, run `sync --to` that last day by hand.
- **Updating:** get the new code in this folder, then run `docker compose up -d --build`.
- **Disk space:** logs are capped at 30 MB (three files of 10 MB, set in `docker-compose.yml`). Each rebuild leaves the previous version's images behind, about 150 MB a time, so after a rebuild remove them with `docker image prune -f --filter label=org.opencontainers.image.title=harvest-sync`. That only removes unused harvest-sync images; images from anything else on the server are left alone. The next rebuild then takes a minute longer, because it starts without the old images to reuse.

Schedule it on one machine only. You can still run it by hand on any machine; matching by content prevents duplicates. But two runs at the same moment could race.

## Email reports

Scheduled runs can email you their result through [Mailgun](https://www.mailgun.com/). This is optional: with no `[email]` section in `config.toml`, nothing is ever emailed. Mailgun's free plan is plenty for this.

To set it up:

1. In Mailgun, add a sending domain and create the DNS records it asks for. A subdomain such as `mail.example.com` keeps this apart from your normal email.
2. Create an API key. Use a Domain Sending Key if your plan offers one, since it can only send mail for that domain. Put the key in `.env` as `HARVEST_MAILGUN_API_KEY`.
3. Add the `[email]` section to `config.toml`; `config.example.toml` shows it. Set `mailgun_region = "eu"` if the domain was created in Mailgun's EU region.
4. Try it with `harvest-sync test-email`.

Two settings decide when mail goes out. Both are on by default:

- **`send_every_run`** emails every run's report. It's meant for a test period: set it to `false` once you trust the sync.
- **`send_on_problems`** emails any run that failed or has a line marked `!!`, such as a locked entry or a write that failed, even with `send_every_run` off.

Only runs with `--email` send anything. The schedule in `deploy/crontab` uses it, so runs by hand stay quiet.

Things to know:

- **Silence can mean two things.** With `send_every_run` off, no email means either all is well or the container isn't running, so check the logs before you rely on a month.
- **A run that can't load `config.toml` can't email,** because the email settings are in that file. That failure only shows in the logs.
- **Reports include your notes and hours,** and they pass through Mailgun.
- **If sending fails,** the error is logged and the run exits with `1`. The sync itself isn't affected.

## Running it on Windows

Run it by hand from this folder: `uv run harvest-sync sync`, then the same command with `--apply`.

To schedule it on Windows instead of Docker, `deploy\register-windows-task.ps1` registers a daily Task Scheduler job. It appends output to `logs\harvest-sync.log`.

## Submitting timesheets

`submit` previews by default. `submit --apply` refuses to run until you set `enabled = true` under `[submit]`. Before submitting, it:

1. Picks the last finished week, using `week_starts_on`; set it to match their Harvest. The first week starts at `earliest_date`.
2. Stops if that week is already submitted. It flags entries added after the submission rather than resubmitting.
3. Syncs the week, and holds if anything needs attention, a timer is running, or the week has no time.
4. Submits, then confirms that the week's entries show as submitted. If the answer is unclear it says so, and it never resubmits blindly.

Things to know about Harvest's submit endpoint:

- **It's beta.** Harvest marks this part of its API as beta.
- **It only works if the client has timesheet approval switched on.**
- **It notifies their approver.** Submitting a second time sends a second notification, which is why harvest-sync never resubmits blindly.
- **It includes expenses.** Any expenses you've entered in their Harvest for those days are submitted too.
- **There's no undo through the API.** Changes after submitting go through their approver.

To submit automatically in Docker, enable it in the config, uncomment the `submit` line in `deploy/crontab`, and run `docker compose up -d --build`. That line is set to Mondays at 07:15 Pacific; change it to suit.

## Troubleshooting

- **`401` or "token rejected":** the token was mistyped or revoked, or `account_id` is for another account. `check` lists the accounts each token can reach.
- **`"…" isn't one of the projects…`:** a name in the mapping doesn't match Harvest. Copy it from `list`.
- **"tracks start and end times":** their Harvest records start and end times instead of durations, and harvest-sync can only copy to an account that tracks durations. Your own Harvest can track either way.
- **"isn't saved as UTF-8":** save `config.toml` or `.env` as UTF-8 and try again.
- **"Mailgun answered 401" or "404":** the API key, `mailgun_domain` or `mailgun_region` is wrong.
- **`docker compose` build fails on `config.toml`, or Compose can't find `.env`:** create them first, as in [Setup](#setup).
- **A conflict on a locked entry:** the client approved or invoiced it before your change. Ask them to unlock it, or leave it as it is.

## Development

```
uv run pytest
```

The tests run against an in-memory fake of Harvest (`tests/fakes.py`). `tests/test_harvest.py` covers the HTTP layer with mocked responses.

### Releasing a version

The version is set only in `pyproject.toml`; the header and `--version` read it from the installed package. To release one, bump it, commit, and tag that commit:

```
uv version --bump minor        # or patch for a fix, major for a breaking change
git commit -am "Release 0.2.0"
git tag v0.2.0
git push origin main v0.2.0
```

`uv run` picks up a new version by itself. The Docker container shows it after `docker compose up -d --build`.

## License

MIT; see [LICENSE](LICENSE). It's provided as is, with no warranty, and it can change or delete entries in a client's Harvest, so preview before you apply.
