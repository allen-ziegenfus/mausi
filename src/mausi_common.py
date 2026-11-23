"""Config and platform helpers shared by the mausi scripts.

Config lives in ~/.config/mausi/config.toml (or $MAUSI_CONFIG); every key is
optional, see config.example.toml in the repo for the full list. Values are
read once per process.

Platform helpers hide the Linux/macOS differences: the clipboard, opening a
URL, and looking at another process (its cwd and command line).
"""
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time

HOME = os.path.expanduser("~")
IS_MAC = sys.platform == "darwin"


def _leave_uv_env():
    """uv runs each script in its own virtualenv and puts that first on PATH.
    Commands mausi starts (the calendar and mail commands, helpers, shells)
    must get the user's PATH, or their `#!/usr/bin/env python3` finds this
    env's Python without their packages."""
    venv = os.environ.get("VIRTUAL_ENV")
    if not venv or os.path.realpath(venv) != os.path.realpath(sys.prefix):
        return
    venv_bin = os.path.join(venv, "bin")
    os.environ["PATH"] = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if p and p != venv_bin)
    for name in ("VIRTUAL_ENV", "VIRTUAL_ENV_PROMPT", "PYTHONHOME"):
        os.environ.pop(name, None)


_leave_uv_env()

CONFIG_PATH = os.environ.get("MAUSI_CONFIG") or os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.join(HOME, ".config"), "mausi", "config.toml")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME") or os.path.join(HOME, ".cache"), "mausi")
STATE_DIR = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"), "mausi")

DEFAULTS = {
    "tasks": {
        "root": "~/tasks",                  # one folder per task: <root>/<PREFIX>-<n>/TASK.md
        "prefix": "T",                      # task ids are <prefix>-<n>; pick one no Jira project uses
        "overview": "~/.claude/tasks.md",   # generated Markdown overview (Claude reads it)
        "worktrees": "",                    # dir of <repo>-<TICKET> git worktrees, linked into <task>/code/
        "git_name": "mausi",                # author of the commits in the task store's git repo
        "git_email": "mausi@localhost",
    },
    "dash": {
        "list_sections": ["Follow-ups"],    # overview sections listed item by item; the rest are counted
        "hide_tasks": [],                   # tasks whose title contains one of these aren't shown
        "late_hour": 20,                    # active time after this hour is flagged
        "github_user": "",                  # for the PR lists; empty = ask gh
        "section_order": {},                # per view ("Now", "Work"): section keys listed first, in this order
    },
    "breaks": {
        "gap_minutes": 5,                   # a pause this long between your prompts is a break
        "warn_minutes": 50,
        "due_minutes": 60,
    },
    "energy": {
        "log": "",                          # Markdown energy log (L key); empty = feature off
    },
    "notes": {
        "dir": "~/notes",                   # snippets.md and notes.md
    },
    "notifications": {
        "capture": False,                   # the dashboard runs mausi-notif-capture (Linux, D-Bus)
        "log": "",                          # JSONL of desktop notifications (Linux capture); empty = state dir
        "my_name": "",                      # an @mention of this name in Slack highlights the conversation
    },
    "sessions": {
        "snapshot": "~/.claude/open-sessions.tsv",
        "history": "~/.claude/session-history",
    },
    "autosave": {
        "enabled": True,                    # SessionEnd: resume the closed session once to save notes and memories
        "min_prompts": 3,                   # sessions with fewer of your prompts aren't saved
        "model": "",                        # e.g. "sonnet" for cheaper saves; empty = your default
        "skip_reasons": ["clear"],          # SessionEnd reasons that don't save (/clear)
    },
    # Optional extensions: each is off until configured.
    "board": {
        "url": "",                          # a local work board serving /api/dash.json (PRs, Jira)
        "command": "",                      # or a command printing the same JSON (e.g. mausi-github); see board_get
    },
    "jira": {
        "site": "",                         # e.g. https://example.atlassian.net
    },
    "standup": {
        "enabled": False,                   # draft a statement before each standup a board names (needs the
                                            # board's "boards" with "standup", and the calendar); sends Claude the
                                            # board's digest, task notes and PRs
        "lead_minutes": 30,                 # how long before the meeting
        "model": "sonnet",
    },
    "triage": {
        "enabled": False,                   # every 30 min (when something new came in) and on i: Claude reads
                                            # the context and lists what needs you; sends mail and Slack to Claude
        "model": "sonnet",                  # the claude -p model for it
    },
    "review": {
        "forward_owner": "",                # GitHub owner whose repo PRs are forwarded to for the final review
        "forward_name": "",                 # who that is, for labels ("waiting on <name>")
    },
    "projects": {},                         # Projects section: group name = [folders or globs], e.g.
                                            # personal = ["~/src/app", "~/src/tools-*"]
    "mail": {
        "sync": "",                         # run every 5 min by the collector (and after a delete): keeps
                                            # the local copy of the mail mausi-mail --local reads
        "viewer": False,                    # Enter on a mail: a box over the dashboard; d deletes (the mail
                                            # command must take --raw ID and --delete ID, as mausi-mail does)
        "render": "",                       # HTML to PNG, for the rendered view (h): <cmd> IN.html OUT.png
                                            # --width CSS_PX --max-pixels N [--network]; "" = text only
        "save_dir": "~/Downloads",          # s in the mail box saves the mail here (rendered: PNG)
    },
    "commands": {
        "calendar": "",                     # prints today's meetings as JSON (optional arg: YYYY-MM-DD)
        "weather": "",                      # prints {"alerts": [{"text"}]} for big changes (e.g. bin/mausi-weather)
        "mail": "",                         # prints unread mail as JSON
        "mail_open": "",                    # opens one mail; {id} is replaced with its Message-ID
        "reminders": "",                    # prints open reminders/to-dos as JSON ({"items": [...]}); --complete ID marks
                                            # one done. A list of commands: their items merged, R adds with the first
        "worktree": "",                     # makes/opens a ticket worktree: <cmd> <KEY>
    },
}

_config = None


def _merge(base, extra):
    out = dict(base)
    for k, v in extra.items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def config():
    """The merged config: DEFAULTS overlaid with config.toml."""
    global _config
    if _config is None:
        data = {}
        try:
            import tomllib
            with open(CONFIG_PATH, "rb") as f:
                data = tomllib.load(f)
        except FileNotFoundError:
            pass
        except ImportError:
            print("mausi: Python 3.11+ is needed to read config.toml; using defaults", file=sys.stderr)
        except Exception as e:  # a broken config shouldn't take everything down
            print(f"mausi: can't read {CONFIG_PATH}: {e}; using defaults", file=sys.stderr)
        _config = _merge(DEFAULTS, data)
    return _config


def get(section, key):
    return config()[section][key]


def path(section, key):
    """A path setting with ~ expanded; "" stays ""."""
    value = get(section, key)
    return os.path.expanduser(value) if value else ""


def data_path(section, key, default_name):
    """A path setting that falls back to a file in the state dir."""
    return path(section, key) or os.path.join(STATE_DIR, default_name)


# --- platform -------------------------------------------------------------

def open_url(url):
    """Open a URL or file with the desktop's default handler, in the background.
    On COSMIC through mausi-open, which also brings the browser to the front."""
    cmd = ["open", url] if IS_MAC else ["xdg-open", url]
    if not IS_MAC and "COSMIC" in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() and url.lower().startswith(("http://", "https://")):
        cmd = [os.path.join(os.path.dirname(os.path.realpath(__file__)), "mausi-open"), url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def _clip_cmds():
    if IS_MAC:
        return ["pbcopy"], ["pbpaste"]
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        return ["wl-copy"], ["wl-paste", "--no-newline"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"], ["xclip", "-selection", "clipboard", "-o"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"], ["xsel", "--clipboard", "--output"]
    return None, None


def copy_to_clipboard(text):
    cmd = _clip_cmds()[0]
    if not cmd:
        return False
    try:
        subprocess.run(cmd, input=text, text=True, timeout=5, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def paste_from_clipboard():
    cmd = _clip_cmds()[1]
    if not cmd:
        return None
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def process_cwd(pid):
    """A process's working directory, or None."""
    if not IS_MAC:
        try:
            return os.readlink(f"/proc/{pid}/cwd")
        except OSError:
            return None
    try:
        out = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return None


def write_atomic(path, text):
    """Replace path with text in one step (a unique temp file next to it, then
    rename), keeping the file's mode."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    try:
        mode = os.stat(path).st_mode & 0o777
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


_locks, _locks_guard, _held = {}, threading.Lock(), threading.local()


@contextlib.contextmanager
def file_lock(path):
    """Hold an exclusive flock on path, shared by every mausi process; re-entrant
    within one thread, and also excludes the process's other threads."""
    import fcntl
    with _locks_guard:
        rlock = _locks.setdefault(path, threading.RLock())
    with rlock:
        depth = getattr(_held, "depth", {})
        _held.depth = depth
        f = None
        if not depth.get(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            f = open(path, "a")
            fcntl.flock(f, fcntl.LOCK_EX)
        depth[path] = depth.get(path, 0) + 1
        try:
            yield
        finally:
            depth[path] -= 1
            if f:
                f.close()


_CONTROLS = None


def clean(obj):
    """obj (a string, or JSON-like lists and dicts of them) without control
    characters other than newline and tab: text from outside (mail, calendar,
    PRs, notifications) must not reach the terminal as escape sequences."""
    global _CONTROLS
    if _CONTROLS is None:
        import re
        _CONTROLS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
    if isinstance(obj, str):
        return _CONTROLS.sub("", obj)
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items()}
    return obj


ERROR_LOG = os.path.join(STATE_DIR, "errors.log")


def log_error(where, text=None):
    """Append the current exception's traceback (or text) to ERROR_LOG (kept
    under 1 MB), for errors a long-running loop or a key press survives."""
    import traceback
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(ERROR_LOG) and os.path.getsize(ERROR_LOG) > 1 << 20:
            with open(ERROR_LOG, "rb") as f:
                f.seek(-(1 << 19), os.SEEK_END)
                tail = f.read()
            with open(ERROR_LOG, "wb") as f:
                f.write(tail)
        with open(ERROR_LOG, "a") as f:
            f.write(f"--- {time.strftime('%Y-%m-%d %H:%M:%S')} {where}\n{text or traceback.format_exc()}\n")
    except OSError:
        pass


def ensure_background(name, argv):
    """Start argv detached (its own session, so it outlives the caller) unless
    the process recorded in STATE_DIR/<name>.pid still runs. For the helpers
    the dashboard keeps alive: mausi-activity, mausi-notif-capture."""
    pidfile = os.path.join(STATE_DIR, f"{name}.pid")
    try:
        pid = int(open(pidfile).read().strip())
        if any(name in a for a in process_args(pid)):
            return
    except (OSError, ValueError):
        pass
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(pidfile, "w") as f:
            f.write(f"{proc.pid}\n")
    except OSError:
        pass


def process_args(pid):
    """A process's argv as a list (on macOS split on spaces, which is enough
    for claude's own flags), or []."""
    if not IS_MAC:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                return [a.decode(errors="replace") for a in f.read().split(b"\0") if a]
        except OSError:
            return []
    try:
        out = subprocess.run(["ps", "-o", "args=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        return out.stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []


def reminders_commands():
    """[commands] reminders as argv lists: one command (a string) or several
    (a list; their items are merged, R adds with the first)."""
    value = get("commands", "reminders") or []
    return [shlex.split(os.path.expanduser(c)) for c in ([value] if isinstance(value, str) else value) if c.strip()]


def board_commands():
    """[board] command as argv lists: one command (a string) or several (a list)."""
    value = get("board", "command") or []
    return [shlex.split(os.path.expanduser(c)) for c in ([value] if isinstance(value, str) else value) if c.strip()]


def _run_board(cmd, args, timeout):
    try:
        r = subprocess.run(cmd + args, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise OSError(f"{os.path.basename(cmd[0])} timed out")
    except OSError as e:
        raise OSError(f"{os.path.basename(cmd[0])}: {e.strerror}")
    if not r.stdout.strip():
        raise OSError((r.stderr.strip() or f"{os.path.basename(cmd[0])} exited {r.returncode}").splitlines()[-1][:200])
    return json.loads(r.stdout)


def merge_boards(parts):
    """Several boards' dash.json as one: lists joined, dicts merged, the first
    value kept for anything else."""
    out = {}
    for part in parts:
        for k, v in part.items():
            if k not in out:
                out[k] = v
            elif isinstance(out[k], list) and isinstance(v, list):
                out[k] = out[k] + v
            elif isinstance(out[k], dict) and isinstance(v, dict):
                out[k] = merge_boards([out[k], v])
    return out


def board_get(path="dash.json", hard=False, timeout=60):
    """The board's JSON for path ("dash.json", "pr/<owner>/<repo>/<n>",
    "issue/<KEY>"): from [board] url, or else from [board] command(s), each run
    as `<command> [--refresh] [<path>]` (no path for dash.json). Several
    commands' dash.json are merged (merge_boards); one that fails adds its
    error to "board_errors". A pop-up comes from the first command that has it.
    Raises OSError or ValueError when there's no board or it fails."""
    url = get("board", "url").rstrip("/")
    if url:
        import urllib.request
        q = "?refresh=1" if hard and path == "dash.json" else ""
        with urllib.request.urlopen(f"{url}/api/{path}{q}", timeout=timeout) as r:
            return json.load(r)
    cmds = board_commands()
    if not cmds:
        raise OSError("no board configured")
    args = (["--refresh"] if hard else []) + ([] if path == "dash.json" else [path])
    if path != "dash.json":
        last = None
        for cmd in cmds:
            try:
                data = _run_board(cmd, args, timeout)
            except (OSError, ValueError) as e:
                last = {"error": str(e)}
                continue
            if not (isinstance(data, dict) and data.get("error")):
                return data
            last = data
        return last
    parts, errors = [], []
    for cmd in cmds:  # one after another: there are only a few, and gh is the slow one
        try:
            data = _run_board(cmd, args, timeout)
            if isinstance(data, dict):
                parts.append(data)
        except (OSError, ValueError) as e:
            errors.append(f"{os.path.basename(cmd[0])}: {e}")
    if not parts:
        raise OSError("; ".join(errors) or "no board output")
    out = merge_boards(parts)
    if errors:
        out["board_errors"] = errors
    return out


BOARD = bool(get("board", "url") or get("board", "command"))  # PR/Jira sections on
