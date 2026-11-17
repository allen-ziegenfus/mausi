"""Open Claude Code sessions and the snapshot mausi restores them from.

Running sessions come from Claude Code's own records, ~/.claude/sessions/
<pid>.json (pid, sessionId, cwd), so this needs neither /proc nor pgrep and
works the same on Linux and macOS. Claude Code removes a record when its
session ends, so after a reboot only the snapshot knows what was open.

The dashboard writes the snapshot every minute; `mausi snapshot` does it once.
"""
import datetime
import glob
import json
import os
import re
import subprocess
import time

import mausi_common as cfg

CLAUDE = os.path.join(cfg.HOME, ".claude")
SNAP = cfg.path("sessions", "snapshot")
HIST = cfg.path("sessions", "history")
HIST_KEEP = 100
BEGIN, END = "<!-- OPEN-CLAUDE-SESSIONS:BEGIN -->", "<!-- OPEN-CLAUDE-SESSIONS:END -->"


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A recycled pid: make sure it is still Claude (the native binary is
    # "claude"; an npm install runs under node).
    out = subprocess.run(["ps", "-o", "comm=", "-p", str(pid)], capture_output=True, text=True).stdout
    return any(name in out for name in ("claude", "node"))


def live_sessions():
    """[(cwd, session id)] of every running interactive Claude Code session."""
    return [(cwd, sid) for _, cwd, sid in live_records()]


def host_app(pid):
    """The app a process runs under ("Terminal", "Code", "kitty"), from the
    first ancestor inside an .app bundle (or else its terminal-ish name), or ""."""
    for _ in range(12):
        out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        if not out:
            return ""
        ppid, _, comm = out.partition(" ")
        m = re.search(r"([^/]+)\.app/", comm)
        if m:
            return m.group(1)
        name = os.path.basename(comm.strip())
        if name in ("tmux", "screen", "SCREEN", "sshd", "kitty", "gnome-terminal-server", "konsole", "alacritty", "wezterm-gui"):
            return name
        if not ppid.strip().isdigit() or int(ppid) <= 1:
            return ""
        pid = int(ppid)
    return ""


def live_records():
    """[(pid, cwd, session id)] of every running interactive Claude Code session."""
    seen, out = set(), []
    for f in sorted(glob.glob(os.path.join(CLAUDE, "sessions", "*.json"))):
        try:
            rec = json.load(open(f))
        except (OSError, ValueError):
            continue
        pid, sid, cwd = rec.get("pid"), rec.get("sessionId"), rec.get("cwd")
        if not (pid and sid and cwd) or rec.get("kind", "interactive") != "interactive":
            continue
        if sid in seen or not alive(pid):
            continue
        seen.add(sid)
        out.append((pid, cwd, sid))
    return out


def read_snapshot(path):
    rows = []
    try:
        for line in open(path):
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                rows.append((parts[0], parts[1]))
    except OSError:
        pass
    return rows


def update_overview_block(rows):
    """The human-readable copy of the snapshot in the task overview, between
    the OPEN-CLAUDE-SESSIONS markers (only when the overview has them)."""
    overview = cfg.path("tasks", "overview")
    root = cfg.path("tasks", "root")
    if not os.path.isdir(root):
        return
    with cfg.file_lock(os.path.join(root, ".lock")):  # the task store regenerates the same file
        _update_overview_block(overview, rows)


def _update_overview_block(overview, rows):
    try:
        text = open(overview).read()
    except OSError:
        return
    if BEGIN not in text:
        return
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    snap = SNAP.replace(cfg.HOME, "~")
    lines = [BEGIN, f"## Open Claude sessions (backup of {snap} — snapshot {ts})", "",
             "Human-readable fallback for the session restore. Resume one with "
             "`cd <dir> && claude --resume <id>`, or all of them with `mausi`. "
             "Earlier snapshots: `mausi snapshot --history`.", "",
             "| Directory | Session ID |", "|---|---|"]
    lines += [f"| `{d}` | {s} |" for d, s in rows]
    lines.append(END)
    new = re.sub(re.escape(BEGIN) + ".*?" + re.escape(END), lambda m: "\n".join(lines), text, count=1, flags=re.S)
    if new != text:
        cfg.write_atomic(overview, new)


def write_snapshot():
    """Record the open sessions in the snapshot (and in the history when the
    set changed). Returns how many; 0 leaves the old snapshot alone."""
    rows = live_sessions()
    # Never replace a good snapshot with an empty one (the dashboard writing it
    # after the last session closed).
    if not rows:
        return 0
    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    cfg.write_atomic(SNAP, "# cwd\tsession_id\tsnapshot_time\n" + "".join(f"{cwd}\t{sid}\t{now}\n" for cwd, sid in rows))

    # History: a copy whenever the set of sessions changes, so a fuller list is
    # never lost to a later, smaller one.
    os.makedirs(HIST, exist_ok=True)
    files = sorted(glob.glob(os.path.join(HIST, "*.tsv")), key=os.path.getmtime, reverse=True)
    if not files or sorted(read_snapshot(files[0])) != sorted(rows):
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        with open(SNAP) as src, open(os.path.join(HIST, f"{stamp}.tsv"), "w") as dst:
            dst.write(src.read())
        for old in files[HIST_KEEP - 1:]:
            os.remove(old)
        update_overview_block(rows)
    return len(rows)


AGENT_ACTIVE = 120  # a background agent whose transcript moved this recently is still working


def background_tasks():
    """{session id: number of background jobs still running}: shell commands
    Claude started in the background (a live process still writes the task's
    output file) and background agents (their output is a link to the agent's
    transcript, which keeps moving while it works). Claude Code keeps both
    under <tmp>/claude-<uid>/<project>/<session>/tasks/. While a turn runs,
    its foreground command shows up here too: use this for idle sessions.
    Shell commands are found through /proc, so on macOS only agents count."""
    import tempfile
    roots = {tempfile.gettempdir(), "/tmp"}
    counts, outputs = {}, {}
    now = time.time()
    for root in roots:
        for tasks in glob.glob(os.path.join(root, f"claude-{os.getuid()}", "*", "*", "tasks")):
            sid = os.path.basename(os.path.dirname(tasks))
            try:
                names = os.listdir(tasks)
            except OSError:
                continue
            for name in names:
                path = os.path.join(tasks, name)
                if os.path.islink(path):
                    try:
                        if now - os.path.getmtime(path) < AGENT_ACTIVE:
                            counts[sid] = counts.get(sid, 0) + 1
                    except OSError:
                        pass  # the link's target is gone
                else:
                    outputs[os.path.realpath(path)] = sid
    if outputs and os.path.isdir("/proc/self/fd"):
        running = set()
        for fd_dir in glob.glob("/proc/[0-9]*/fd"):
            try:
                fds = os.listdir(fd_dir)
            except OSError:
                continue
            for fd in fds:
                try:
                    target = os.readlink(os.path.join(fd_dir, fd))
                except OSError:
                    continue
                if target in outputs:
                    running.add(target)
        for target in running:
            counts[outputs[target]] = counts.get(outputs[target], 0) + 1
    return counts
