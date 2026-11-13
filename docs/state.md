# State: backup, restore and moving a home

All of Spartito's state is in its home, `~/.orchestrator/` (or `$ORCH_HOME`): the database
`orch.db`, the worktrees, the logs and your own roles ([files](configuration.md#files)).
`spartito state` backs up the part of it that is not in git, lists the backups and restores
one. Moving a home to another directory or machine is a procedure, described at the end.

- [What a backup holds](#what-a-backup-holds)
- [Backup](#backup)
- [List](#list)
- [Restore](#restore)
- [Backups and versions](#backups-and-versions)
- [Moving a home](#moving-a-home)

## What a backup holds

| in a backup | not in a backup |
|---|---|
| the database: projects, agents, turns, events, usage, settings, board, decisions, notices | the worktrees, and the uncommitted files in them (committed work is on the agents' branches, in your project repositories) |
| `roles/` and `loadout.toml` of the home, when they exist | the project repositories, and their own `.orch/` roles and loadout |
| `logs/`, only with `--logs` | the provider CLIs' logins and sessions (`~/.claude`, `$CLAUDE_CONFIG_DIR`, `~/.codex`, `$CODEX_HOME`) |
| | `workspace/`, `run/`, `hooks-off/`, the background service's definition and the launcher |

Nothing outside the home is read or written: symbolic links in the home are left out of a
backup, and a restore leaves them as they are (the commands list them). A backup holds prompts, answers and tool output, which can include secrets; the command
says so every time. Keep backups private.

## Backup

```sh
spartito state backup [--to DIR] [--logs]
```

The database is copied with SQLite's online backup API, never as a file: the copy is one
consistent snapshot, taken while agents run and other processes write, and it holds whole
transactions only. The copy is then checked with `PRAGMA integrity_check`; a copy that fails
it makes the command fail. The copy is a single file in rollback-journal mode (no `-wal` beside
it), so it can be moved or opened anywhere.

A backup is a new directory, by default `backups/<UTC time>/` in the home (for example
`backups/20261113T142530Z/`; a second one in the same second gets `-2`, and so on), or `DIR`
with `--to`; an existing directory is never written into, and neither is a place inside
`roles/` (or `logs/` with `--logs`), which the backup copies. It is built in a hidden
directory next to it, `.<name>.incomplete`, and renamed into place once it is complete: a
backup that failed leaves nothing behind, and one interrupted by a crash is neither listed nor
restored by name. The directory and its subdirectories are created with mode 0700, its files
with 0600, and `backups/` itself with 0700. Inside are `orch.db`, `roles/` and `loadout.toml` (when the home
has them) and, with `--logs`, `logs/`. The command prints where the backup is, what it holds,
its size, and the warning about secrets.

## List

```sh
spartito state list [--json]
```

Lists the backups in the home's `backups/`, newest first: the name, the time (UTC) and size,
and `logs` when the logs are included. Database files put there by hand (`*.db`, for example
`orch-20260101-120000.db`) are listed too; their time is when the file was last written.
`--json` gives the same as a list of objects (`name`, `path`, `kind`, `time`, `size`, `logs`).
Backups made with `--to` elsewhere are not listed; restore them by their path.

## Restore

```sh
spartito state restore BACKUP [--yes|-y] [--logs]
```

`BACKUP` is a name that `spartito state list` shows, or the path of a backup directory or of a
single database file. In this order, the command:

1. checks the backup: it must open as SQLite, pass `PRAGMA integrity_check` and have the
   tables of a state database. Anything else is refused and nothing changes;
2. refuses while any turn is running or queued, and names them (`fix-login-3f2a run 12
   running`). Wait for them, or `spartito stop` the agents; a turn whose runner process is
   gone does not count;
3. without `--yes`, prints what it would do, changes nothing and exits with 1;
4. keeps the current state as a new backup, `backups/<UTC time>-before-restore/`. Nothing is
   ever deleted: `spartito state restore <that name> --yes` brings the replaced state back.
   First the home's `roles/` and `loadout.toml` (with `--logs` also `logs/`) are copied into
   it, still under its hidden name, with its permissions set. Then, holding the database's
   write lock, the command checks again that no turn is running or queued (else it refuses,
   removes the unfinished kept backup and changes nothing), copies the current database into
   the kept backup with the backup API and checks the copy, renames the kept backup into its
   name and prints where it is, and only then replaces the content (step 5) and commits. So
   nothing another process commits meanwhile is lost (what a dashboard committed before is in
   the kept backup; a write that comes during it waits and lands in the restored state), and
   whatever stops the command, an error or a crash, the live state is either unchanged or
   restored with the kept backup complete, listed and printed. A kept backup that was named
   stays even when the replace then fails: it is a valid backup of the unchanged state;
5. replaces the rows of every table of the live database with the backup's, in one
   transaction: no file is swapped, so processes that have the database open (a dashboard) see
   the restored content with their next read, and a backup with another page size restores
   too. Turns that were queued or running in the backup end as `stopped` (their runners are
   gone), the restore wakes no parent, and new turns get numbers above every earlier one, so
   no turn reuses the log directory of another;
6. copies the backup's `roles/` and `loadout.toml` over the home's, and with `--logs` its
   `logs/`, if it has them. Files the backup lacks stay. A single database file restores the
   database only. It never writes through a symbolic link: an entry of the home that is one,
   or lies below one (`loadout.toml` linked to a file elsewhere, a linked `roles/`, one linked
   role file), is left as it is, and the command lists it as not restored. Such links are not
   in the kept backup either (it lists them too).

Afterwards, restart a running dashboard: it works on the restored database, but what it shows
may still be from before. For `spartito web` stop and start it; for the background service run
`spartito service install` again (with the `--host` and `--port` you used), which restarts it;
quit `spartito app` and open it again. Leave an open `spartito chat` and continue it with
`spartito chat --continue`: its conversation remembers the state from before. The command says
this too.

Worktrees are not restored. An agent of the restored state whose worktree directory is gone
gets one again at its next turn, from its branch (if the branch is gone too: a new branch from
its base), unless git still has the old worktree registered (`git worktree prune` in the
project clears registrations of missing worktrees). Worktrees of agents the restored state does
not know stay where they are.

## Backups and versions

- **An older version's backup** is restored as it is: the live database keeps this version's
  tables and columns, a table the backup lacks ends up empty and a column it lacks gets its
  default.
- **A newer version's backup** is restored too, without its tables and columns this version
  does not have: the command names them, and they stay in the backup, for the version that
  knows them. Everything else is restored.
- **An older install** ignores the home's `backups/` directory and has no `spartito state`.
  After a restore it opens the database as it opens any newer one.

## Moving a home

There is no command that moves a home: this procedure does it with the commands above, a copy
of the directories and, where needed, `git worktree repair`.

### What depends on paths

| what | where it is stored | after a move |
|---|---|---|
| the home itself | `ORCH_HOME` (default `~/.orchestrator`) | set `ORCH_HOME` on the new machine if the home is not `~/.orchestrator` |
| agents' working directories | the database (`agents.cwd`): `<home>/worktrees/<project>/<agent>/`, `<home>/workspace/` for the orchestrator, the project's path for agents in the checkout | must exist at the same absolute paths |
| projects | the database (`projects.path`), absolute | must exist at the same absolute paths |
| worktrees in git | each project's `.git/worktrees/<name>/gitdir` and each worktree's `.git` file point at each other with absolute paths | if `git worktree list` in a project marks them `prunable`, run `git worktree repair <worktree>…` there |
| Claude Code sessions | `~/.claude/projects/` (or `$CLAUDE_CONFIG_DIR/projects/`), in a directory named after the agent's working directory | copy them; an agent resumes its session (`claude --resume`) only from the same working directory |
| Codex sessions | `$CODEX_HOME/sessions/<year>/<month>/<day>/rollout-…-<session>.jsonl`, found by the session's id | copy them; `codex exec resume` finds them by id |
| background service, launcher | the service definition and the launcher name the install's Python and keep your environment | remove them before, install them again after |

Spartito does not rewrite the paths it has stored. An agent whose working directory does not
exist cannot continue: a worktree agent's next turn creates its worktree again at the stored
path from its branch (with another home path, that is outside the new home), and an agent in a
missing checkout fails.

### What the procedure promises

When the home and every project keep their absolute paths (the same directories, or a machine
with the same user name and layout): every project, agent, turn, setting, board item and
decision; the worktrees with their uncommitted files; and, with the provider sessions copied
and the CLIs logged in, agents that continue their conversations.

It does not promise anything for other paths. Then the history can still be read
(`spartito ps`, `spartito status`, the dashboard), but existing agents cannot continue; to work
on a project at its new path, register it under another name
(`spartito project add PATH --name NAME`) and start new agents there. Logins are never moved:
log in to `claude` and `codex` on the new machine.

### Steps

On the old machine:

1. Let the agents finish: `spartito ps` shows none `running` or `waiting` (or `spartito stop`
   them). Quit `spartito app` and `spartito menubar`, and leave `spartito chat`.
2. Stop everything that writes to the home: `spartito service uninstall` and
   `spartito app uninstall`, if you have installed them.
3. `spartito state backup --logs`: a checked copy to fall back on.
4. Copy the whole home directory (with `worktrees/`, the only place of uncommitted work, and
   `orch.db` together with any `orch.db-wal` beside it) and every project repository, each to
   the same absolute path on the new machine, keeping permissions. For agents to continue
   their conversations, copy the provider sessions too (`~/.claude/projects/`,
   `$CODEX_HOME/sessions/`).

On the new machine:

5. Install Spartito and log in to `claude` and `codex`. Set `ORCH_HOME` if the home is not
   `~/.orchestrator`.
6. In each project, `git worktree list`; if it marks the agents' worktrees `prunable`, run
   `git worktree repair <worktree>…` in the project.
7. Check with `spartito project list` and `spartito ps`. If the copied database does not open,
   restore the backup of step 3: `spartito state restore <path of the backup> --yes`.
8. `spartito service install` and `spartito app install`, as on the old machine.

To move only the state, without worktrees: on the new machine, with the projects at the same
paths and the agents' branches in them, `spartito state restore <path of the backup> --yes`.
Each agent's worktree is created again from its branch at its next turn; uncommitted files
are not there.
