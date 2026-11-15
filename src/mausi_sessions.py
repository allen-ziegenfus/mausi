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
        out.append((cwd, sid))
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
