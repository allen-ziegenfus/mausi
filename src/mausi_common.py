"""Config and platform helpers shared by the mausi scripts.

Config lives in ~/.config/mausi/config.toml (or $MAUSI_CONFIG); every key is
optional, see config.example.toml in the repo for the full list. Values are
read once per process.

Platform helpers hide the Linux/macOS differences: the clipboard, opening a
URL, and looking at another process (its cwd and command line).
"""
import contextlib
import os
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
    # Optional extensions: each is off until configured.
    "board": {
        "url": "",                          # a local work board serving /api/dash.json (PRs, Jira)
    },
    "jira": {
        "site": "",                         # e.g. https://example.atlassian.net
    },
    "review": {
        "forward_owner": "",                # GitHub owner whose repo PRs are forwarded to for the final review
        "forward_name": "",                 # who that is, for labels ("waiting on <name>")
    },
    "commands": {
        "calendar": "",                     # prints today's meetings as JSON (optional arg: YYYY-MM-DD)
        "mail": "",                         # prints unread mail as JSON
        "mail_open": "",                    # opens one mail; {id} is replaced with its Message-ID
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
