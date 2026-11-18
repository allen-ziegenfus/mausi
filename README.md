# mausi

A [kitty](https://sw.kovidgoyal.net/kitty/) add-on for working with many
[Claude Code](https://claude.com/claude-code) sessions at once, one per task.

- **Tasks as folders.** Each task is a folder (`~/tasks/T-12/`) with a `TASK.md`
  and whatever files belong to it, in one git repository. Start Claude in the
  folder and the session belongs to the task. The `tasks` command manages them,
  and a generated Markdown overview lets Claude read the whole list.
- **Sessions in kitty tabs.** `mausi` records which Claude sessions are open
  and puts them back into kitty tabs after a restart or reboot.
- **A dashboard in tab 1.** Sessions, with ⏳ on the ones waiting for you;
  pinned and open tasks; a break timer and your active time; Docker and
  Kubernetes; notes and snippets. Optionally also PRs, Jira, calendar, mail and reminders.
  Press `v` for the task view: each task with its sessions, tickets and PRs.
- **Notifications that take you there.** When Claude needs you, a desktop
  notification names the tab, and clicking it focuses that tab.
- **A task guard.** If a prompt in one task's session names another task's
  ticket or PR, Claude is asked to point out the mix-up before doing the work.
- **Autosave on close.** When you close a session, mausi resumes it once in the
  background (`claude -p`) to do the save you'd otherwise ask for: notes, tags
  and done on its task, and memories. It skips short sessions, `/clear`, and
  sessions whose last prompt already asked to save. Results go to
  `~/.local/state/mausi/autosave.log`; turn it off with `[autosave] enabled = false`.

Works on Linux and macOS. It's a personal tool that grew over a few months of
daily use, so expect rough edges.

## Install

You need kitty, [uv](https://docs.astral.sh/uv/), `jq`, `git`, `python3` and
Claude Code. uv runs the Python scripts and fetches Python 3.11+ and PyYAML for
them on the first run; nothing is installed into your system Python. The task
guard hook is the exception: it runs on every prompt, so it uses the system
`python3` (any 3.8+, standard library only) to start fast.

Optional, each used when it's there:

| Tool | For |
|---|---|
| `swayidle` (Linux, Wayland) | keyboard/mouse activity for the break timer; without it, gaps between your prompts count |
| `pactl` (Linux) | spotting calls (an app recording from the microphone), so they don't count as breaks |
| `dbus-monitor`, `stdbuf` (Linux) | `[notifications] capture`: the Notifications and Slack sections |
| `wl-copy`/`wl-paste` or `xclip` (Linux) | copying (`c`) and saving the clipboard as a snippet (`b`) |
| `docker`, `kubectl`, `k3d` | the Docker and Kubernetes sections |
| `curl` | ticket titles from Jira (`JIRA_API_USER`, `JIRA_API_TOKEN`) |
| `code` | `tasks code`: a task in VS Code with its worktrees |

```sh
# macOS
brew install uv jq git && brew install --cask kitty
# Arch Linux
sudo pacman -S uv jq git kitty

git clone https://github.com/allen-ziegenfus/mausi ~/mausi
~/mausi/bin/mausi setup
```

There's no installer. `mausi setup` (and plain `mausi`, the first time) offers
what's missing, one yes/no question each:

- **kitty**: one line in `kitty.conf`, `geninclude ~/mausi/kitty/mausi.py`
- **Claude Code**: the mausi plugin, installed from the checkout: the hooks
  (notifications, ⏳ on tabs, the task guard, autosave), a skill that tells Claude how to
  use the task store, and `mausi`/`tasks` on Claude's PATH
- **PATH**: `~/mausi/bin` in your shell profile
- **Status line** (only if Claude Code has none): model, folder, the session's
  task, and how full the context is

Then restart kitty and run `mausi` in it. `git pull` updates everything; in
Claude, `/reload-plugins` or a new session picks the changes up.

Nothing else needs setting up. The config file is optional, `tasks new`
creates the task store, and the dashboard keeps its background helpers running
(the session snapshot and the activity tracker for the break timer). To
remove mausi, delete the lines it added to `kitty.conf` and your shell profile
(each under a `# mausi` comment), remove `statusLine` from
`~/.claude/settings.json` if it points at mausi, and run
`claude plugin uninstall mausi@mausi`.

To use mausi's status line in place of one you already have, set it yourself:
`"statusLine": {"type": "command", "command": "~/mausi/src/mausi-statusline"}`.

Claude Code sends its own terminal notification as well, so you'll get two.
Turn Claude Code's off under `/config` → Notifications.

## Use

```sh
mausi                    # restore the last open sessions into tabs, then show the dashboard
mausi dash               # just the dashboard
mausi-tui                # the same dashboard on Textual (try-out)
mausi snapshot --history # earlier snapshots; mausi --from <file> restores one
mausi setup              # offer the kitty / Claude Code / PATH / status line setup that's missing

tasks new "Try mausi on the Mac" -n "first note"
tasks                    # open tasks
cd "$(tasks path T-1)" && claude
tasks note T-1 "works"
tasks find mac
tasks done T-1
```

In the dashboard, `?` lists the keys. The main ones:

| Key | Does |
|---|---|
| Tab, ←/→ | switch columns |
| j/k, ↑/↓, Enter | move, open (jump to a session's tab, open a link) |
| z | zoom a section to full screen; `/` filters it |
| Space | show the full text |
| v | task view |
| c | new Claude session in a new tab, primed with the row (task, PR, ticket) |
| t, d, p | new task, done (task, reminder; dismiss a notification), pin to Now |
| n, b | quick note, save the clipboard as a snippet |
| L | log how the work feels (with `[energy] log` set) |
| 1–9, g | go to a tab, tab picker |

In any kitty window, Alt-1…Alt-0 switch tabs and Alt-g opens the tab picker
(on macOS the left Option key works as Alt).

## Configure

Everything is in `~/.config/mausi/config.toml`; see
[`config.example.toml`](config.example.toml) for all keys. Common ones:

```toml
[tasks]
prefix = "T"             # task ids; pick a prefix no Jira project uses
worktrees = "~/src"      # link <repo>-<TICKET> git worktrees into each task's code/

[dash]
list_sections = ["Now", "In progress"]
hide_tasks = ["private"]

[energy]
log = "~/notes/energy.md"
```

### Optional extensions

Each of these is off until you configure it:

- **`[board] url`**: a local HTTP service serving `/api/dash.json` with your
  PRs and Jira issues. It adds the PR and Jira sections. mausi doesn't include
  a board; the shape it expects is in `mausi-dash` (`pr_lines`, `jira_lines`,
  `project_lines`). Sections whose key the board doesn't send stay hidden.
- **`[board] command`**: instead of a URL, a command that prints the same JSON
  (`<command> [--refresh] [pr/<owner>/<repo>/<n>]`), run every 3 minutes. mausi
  ships one for GitHub, no server needed: `bin/mausi-github --repo me/app
  --project me/1` lists the open PRs in those repos and the moving items on
  the GitHub Projects (all statuses but the first and last, or `--status`), via `gh`.
  `bin/mausi-todos FILE...` shows TODO files the same way: each file's open
  items under their headings (Enter opens the file at the item). `command` can
  be a list; the commands' output is merged.
- **`[commands] calendar`**: a command that prints today's meetings as JSON:
  `{"events": [{"title", "start": "YYYY-MM-DDTHH:MM", "end", "allday", "location", "meet"}], "synced": "…"}`.
  It gets an optional `YYYY-MM-DD` argument for another day. Adds the Today section and meeting reminders.
- **`[commands] mail`**: a command that prints unread mail as JSON
  (`{"unread": [...]}`; see `mail_lines` in `mausi-dash`). `mail_open` opens one.
  `bin/mausi-mail --host imap.example.com --user me@example.com` reads the
  Inbox over IMAP (read-only), with the password in the macOS Keychain.
- **`[commands] reminders`**: a command that prints open reminders or to-dos
  from any app as JSON, `{"items": [{"id", "title", "group", "due", "priority",
  "note", "url"}]}` (`due` is `YYYY-MM-DD` or `YYYY-MM-DDTHH:MM`, local, or
  empty; `priority` is `high`, `medium`, `low` or empty), and marks one done
  with `--complete ID`. Adds the Reminders section to Now (overdue and due
  today; z shows all of them, d marks one done, R adds one with
  `--add TITLE [--group G] [--due YYYY-MM-DD]`). Triage sees none of it unless
  the command also prints `"digest": ["line", ...]` (and a `"digest_title"`):
  those lines go to Claude as they are, so the command picks what it reads.
- **`[commands] worktree`**: creates or opens a ticket's worktree (`w` key).
- **`[notifications] capture = true`** (Linux): the dashboard runs
  `mausi-notif-capture`, which logs every desktop notification from D-Bus, for
  the Notifications and Slack sections.

## How it fits together

| Piece | What it does |
|---|---|
| `bin/` | the commands: `mausi`, `tasks`, `mausi-tui`; everything else is in `src/` |
| `src/mausi_tasks.py` | the task store, behind `tasks` |
| `src/mausi_sessions.py` | session snapshots: `mausi` restores from them |
| `src/mausi-dash`, `src/mausi_taskview.py` | the dashboard and its task view; every minute it also writes the session snapshot and keeps the helpers below running |
| `bin/mausi-tui` | the dashboard drawn with [Textual](https://textual.textualize.io/); it runs `mausi-dash`'s code for data and keys |
| `src/mausi_common.py`, `src/mausi_setup.py` | config and paths; `mausi setup` |
| `src/mausi-hook`, `src/mausi-mark` | Claude Code hooks: notification and ⏳ on the tab |
| `src/mausi-guard` | Claude Code hook: the task guard |
| `src/mausi-activity` | keyboard/mouse activity for the break timer (swayidle on Linux, HIDIdleTime on macOS) |
| `src/mausi-notif-capture` | logs desktop notifications from D-Bus (Linux, `[notifications] capture`) |
| `src/mausi-break` | the full-screen break and meeting reminder |
| `src/mausi-statusline` | Claude Code status line: model, folder, task, context bar |
| `src/mausi-open`, `protocols/` | opens links; on COSMIC it brings the browser to the front (Wayland protocol files, MIT-style licensed, notices inside) |

Design notes, the files mausi keeps, and gotchas are in
[`docs/NOTES.md`](docs/NOTES.md).

The dashboard starts itself again when its code (`src/`, `bin/`) or `config.toml`
changes, in the same view, once the files have settled and compile.

Open sessions come from Claude Code's own records in `~/.claude/sessions/`.
Sessions running outside kitty (Terminal, VS Code) are listed too, marked
"in Terminal" and without a tab number. Enter on one moves it into kitty: after
you confirm (and optionally name a task for it), mausi quits it there and resumes
it in a new tab, only once the old process has exited.
Notifications use kitty's notification protocol, so they work the same on
both platforms.

## License

MIT
