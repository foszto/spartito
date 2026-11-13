# Changelog

The changes of each release of Spartito that matter to the people who use it. The format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); how the version number is
chosen is in the
[versioning rule](https://github.com/foszto/spartito/blob/master/docs/releasing.md#the-version).

## [Unreleased]

## [0.2.0] - 2026-11-13

### Added

- A read-only code view in the dashboard: **Code** in the header of an agent with a worktree
  shows the files changed against its base branch, committed and uncommitted, with each
  file's patch, and the worktree's files with their content. Only files git tracks or would
  track are shown; ignored files and `.git` never are, and nothing is edited.
- `spartito state backup` backs up the state, also while agents run: the database (checked
  after the copy), the home's `roles/` and `loadout.toml`, and with `--logs` the logs.
  `spartito state list` lists the backups.
- `spartito state restore` restores a backup into the home. It refuses while a turn is
  running or queued, and without `--yes` only says what it would do; the state it replaces is
  kept as a new backup first.
- `spartito cost` reports the spend of the recorded turns with a total, grouped with `--by`
  by project (the default), day (local time), agent, model or role. `--since` and `--until`
  limit it to a range of local days, both included; `-p` keeps one project, and `--json`
  prints JSON. Codex reports no cost: its tokens are counted and its cost shows as `n/a`.
- A cost report refuses a range of more than 3,660 days (about ten years) and a day at the
  very edge of the calendar (such as `--until 9999-12-31`) with an error. All time runs from
  the first recorded day to today, so it meets the limit too once the records reach back
  further than that.
- Budgets with `spartito budget list`, `spartito budget set` and `spartito budget remove`: one
  global budget and one per project, per day, month or in total. A desktop notification warns
  once per period at a threshold (80 % by default) and once when the amount is reached.
- An opt-in hard stop for a budget (`spartito budget set --hard-stop`, off by default): once
  the amount is reached, new message turns of the agents the budget counts are refused before
  they start. A running turn is never stopped, and notification and compaction turns still run.
- A **Cost** view in the dashboard: each budget with what is spent in its current period,
  then the spend over a range of local days: the last 7, 30 or 90 days with today, all time,
  or any from–to, the last 30 days by default. A chart stacks each day's spend by group (the
  top eight, the rest as one `other`) and shows cost or tokens; a day with turns that
  reported no cost (Codex) gets a hollow mark and is never drawn as $0. A legend names the
  groups and the mark. A table gives each group's totals over the range. It groups by
  project, model, role or agent, keeps one project or all, and is read-only: budgets are set
  with `spartito budget set`.
- `GET /api/cost` gives the report (`by`, `since`, `until`, `project`), with every day of the
  range and the spend per day and group, and the budgets with their state.
- `spartito --version` prints the version; the dashboard's settings show it too.
- `spartito version` prints the version and the kind of install. `spartito version --check`
  asks PyPI whether a newer release exists, only when typed: nothing checks for updates on its
  own.
- `spartito upgrade` prepares an upgrade: it refuses while a turn is running or queued, backs
  the state up, and prints the command that upgrades this kind of install and what to restart
  afterwards. It installs nothing itself and runs no git. `--dry-run` prints the plan without
  a backup.
- The database gets the table `budgets` and the index `run_usage_created`, added when it is
  opened. The change is additive: older versions keep opening the database.

### Fixed

- Stopping an agent at the moment a turn had just been queued for it (such as the notice that
  one of its agents finished) could miss that turn: it started after the stop and ran. It now
  ends as stopped.

## [0.1.0] - 2026-11-13

### Added

- The first public release, on PyPI as `spartito`.

[Unreleased]: https://github.com/foszto/spartito/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/foszto/spartito/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/foszto/spartito/releases/tag/v0.1.0
