# mausi design notes

For working on mausi: how the pieces fit, why they're built this way, and
the traps found so far. The README covers installing and using it.

## Files mausi reads and writes

| Where | What |
|---|---|
| `~/.config/mausi/config.toml` | the config; every key is optional (`config.example.toml`) |
| `~/tasks/<PREFIX>-<n>/TASK.md` | one task: YAML header (id, title, summary, section, order, status, added, done, keywords, watch, repos, effort) and `- ` notes. The task store is a git repo; each change is a commit |
| `~/tasks/<id>/code/` | links to the task's worktrees and `repos:`, kept up to date by the dashboard every minute (git ignores it) |
| `~/tasks/by-name/` | readable links to the task folders |
| `~/.claude/tasks.md` | generated overview for Claude; never edited by hand |
| `~/.claude/open-sessions.tsv`, `~/.claude/session-history/` | the session snapshot `mausi` restores from, and earlier ones |
| `~/.cache/mausi/` | dashboard state that can be lost: tasks seen, session ↔ task links, meeting reminders |
| `~/.local/state/mausi/` | state worth keeping: per-session activity, notification log (20 MB, then `.1`), pidfiles, declined setup items, `errors.log` |

Open sessions come from Claude Code's own `~/.claude/sessions/<pid>.json`;
transcripts from `~/.claude/projects/`.

## Decisions

- **No installer.** `mausi setup` checks four things (kitty include, plugin,
  PATH, status line) and asks before changing each. Background helpers are
  started and kept alive by the dashboard (pidfiles), not by systemd or
  launchd, so there's nothing to install per platform and nothing left behind.
- **uv runs everything.** Each script declares its Python version and
  dependencies inline (PEP 723), so nothing goes into the system Python, and
  macOS's old Python 3.9 doesn't matter. The first run takes about 0.3 s, later
  ones under 0.1 s.
- **The Claude Code side is a plugin** in the repo root, installed from the
  checkout through a local marketplace. It runs in place, so `git pull` is the
  update. It brings the hooks, the tasks skill, and `bin/` on Claude's PATH.
- **`bin/` holds only the commands** (`mausi`, `tasks`, `mausi-tui`); the
  modules, hooks and helpers are in `src/`, called by path, so they stay off
  PATH. Scripts in `src/` find each other through their own location.
  A plugin can't set the status line, which is why `mausi setup` does.
- **kitty's remote control, one socket per kitty** (`unix:/tmp/kitty-{kitty_pid}`).
  `geninclude kitty/mausi.py` rather than `include`, because a kitten mapping
  needs the absolute path of `picker.py`.
- **Extensions are commands or URLs** (`[board] url`, `[commands] calendar` and
  `mail`, `[commands] worktree`), each off until set, and sections whose source
  isn't there (Docker, Kubernetes, Slack, …) are hidden. Anything specific to
  one employer lives outside the repo.
- **The dashboard reloads `mausi_tasks.py` when it changes**, since it runs for
  days and re-links `code/` every minute: old code kept undoing new links.
- A port to Go (Bubble Tea) is an option for later: one binary, a Homebrew tap.

## Dashboard

Two views, switched with `v`, each with its own cursor:

- **System view**: left column You (break timer, active time today and this
  week, energy log), Today/Tomorrow (calendar), Sessions, Now (pinned tasks),
  Tasks, Docker, Kubernetes. Right column Slack, Notifications, Mail, Notes,
  PRs, Jira. The order is `VIEW_SECTIONS` in `mausi-dash`.
- **Task view** (`mausi_taskview.py`): the task list on the left (Unsorted,
  Now, Active, other tasks, Personal, a dimmed Done group), the selected task's
  notes, sessions, tickets, PRs, mail and Slack on the right. Its docstring has
  the linking rules and the workflow stages.

Rendering is plain ANSI on a raw tty (no curses): each section returns
`(text, action)` lines, and the keys act on the selected line's action. That
is why the task view can reuse every key.

- Keys work with or without Shift, except `D` (dismiss all) and `L` (log energy;
  `l` moves right). `?` shows the HELP text; keep it in step when keys change.
- Each list keeps its own `/` filter. While typing, ↑↓/PgUp/PgDn move through
  the matches; Enter keeps the filter; Esc clears it and stays on the entry.
- Mouse reporting is on (the wheel moves the cursor), so hold Shift to select
  text.

## Sessions and tasks

A session belongs to the task whose notes have its `claude --resume <id>` line
(`a` writes one), else the task it was attached to or started for (`c` sets the
kitty user var `claude_task`), else the task folder it runs in, else a task id
or ticket its first prompts name, else its worktree's ticket.

**Effort**: active time is the gaps of up to 15 minutes between your prompts,
merged across the task's sessions. `mausi_tasks.scan_sessions()` reads the
transcripts incrementally into `~/.local/state/mausi/session-activity.json`,
which outlives the transcripts (Claude Code deletes them after 30 days).
`tasks done` freezes the numbers into `TASK.md`; `tasks spent` adds time
outside Claude.

**Break timer**: with `mausi-activity` running, a 5-minute gap in keyboard and
mouse activity is a break (swayidle on Linux, `HIDIdleTime` on macOS);
otherwise gaps between your prompts. A call (any app recording from a real
microphone) or a calendar meeting counts as active, not as a break.

## Robustness

- Every write to the task store (and the session block in the overview) holds
  `<root>/.lock` (`mausi_common.file_lock`), so `tasks`, the dashboard and Claude
  sessions can write at once. Files are replaced through `write_atomic` (a
  unique temp file, then rename).
- A TASK.md that can't be read is skipped and `tasks` names it, rather than
  breaking every command. Unquoted dates and numbers in the header are read as
  text.
- The dashboard's background loops restart after an error, and an error from
  a key is shown on the status line; both go to `errors.log`.
- Text from outside (mail, calendar, PRs, Jira, notifications) has control
  characters removed on the way in (`mausi_common.clean`), so it can't send
  escape sequences to the terminal. Notifications built from tool calls have
  tokens and passwords masked.

## Gotchas

- **Never rename a task folder.** Claude Code ties sessions to the folder's
  path; `by-name/` gives readable names instead.
- The snapshot is written every minute, so a tab closed just before a restart
  can come back.
- kitty reads `listen_on` only at startup: after adding the include, kitty has
  to be restarted, not just reloaded.
- Slack shows only what Slack sends as desktop notifications, so Slack's own
  notification settings decide what appears. Closing a notification from the
  dashboard (`d`) needs a desktop that lets one app close another's (COSMIC
  does).
- On Wayland, links open in a browser tab behind the terminal: raising
  another app's window needs a focus token, which kitty doesn't pass on.
  On COSMIC, `mausi-open` raises it through COSMIC's toplevel protocols (it
  watches for the browser window whose title changes) and is set as kitty's
  `open_url_with`. Other Wayland desktops would need their own way.
- Claude Code also sends its own terminal notification; turn it off under
  `/config` → Notifications or you get two.
- A task id prefix must not be a Jira project key: task ids link like tickets
  but are never treated as Jira tickets.
