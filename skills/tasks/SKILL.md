---
name: tasks
description: The user's task store (mausi), managed with the `tasks` command. Use when the user names a task (an id like T-12 or a title), mentions a new follow-up, commitment or "remind me to…", when a task gets resolved, when the session runs inside a task folder, or when looking for earlier work on a topic.
---

# Task tracking (mausi)

Each task is a folder `<root>/<PREFIX>-<n>/` (by default `~/tasks/T-<n>/`; `tasks path <id>` prints it) with a `TASK.md`: a YAML header (id, title, summary, section, order, status, added, done, and optional keywords and repos), then the notes as `- ` bullets. Any files that belong to the task live in its folder. The root is a git repository; every change is committed.

The Markdown overview (by default `~/.claude/tasks.md`) is **generated** from those folders. Read it for the overview, but never edit it; changes there are overwritten.

Change tasks with the `tasks` command:
- `tasks` lists open ones
- `tasks new "Title" [-s SECTION] [-n NOTE]`, `tasks note <id> "…"`, `tasks done <id>`, `tasks undone <id>`
- `tasks show <id>`, `tasks path <id>`, `tasks find <words> [-o]`, `tasks tag <id> <words> [-r]`
- `tasks effort [<id>]` (time spent, from the Claude sessions in its folder), `tasks spent <id> 1.5 "note"` (time outside Claude)
- `tasks code <id>` opens the task in VS Code with its worktrees and repos
- or edit a `TASK.md` and run `tasks sync`

Tasks can be named by id or by the start of their title.

**When a session belongs to a task** (it runs inside a task folder, or the user names the task), keep the task's intermediate files (downloads, query output, drafts) in its `scratch/` folder, which git ignores. Never rename a task folder: Claude sessions are tied to their folder's path. Don't put files in `code/`; it holds links to the task's worktrees and repos.

1. When the user mentions a new follow-up, commitment, or "remind me to…", offer to add it (`tasks new`).
2. When a task is resolved during a conversation, mark it done (`tasks done`) before the conversation ends.
3. When creating a task, and again when it's done, tag it (`tasks tag`) with search words that may not appear in its text. To find earlier work on a topic, run `tasks find` first.
