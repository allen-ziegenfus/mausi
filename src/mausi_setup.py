"""The setup mausi needs outside its own folder, checked when `mausi` starts.

Four things, each offered with a yes/no prompt and never changed silently:

- kitty: a `geninclude <checkout>/kitty/mausi.py` line in kitty.conf
- Claude Code: the mausi plugin, installed from this checkout (it runs in
  place, so `git pull` updates it)
- PATH: <checkout>/bin in the shell profile, so `mausi` and `tasks` work
  without the full path
- status line: Claude Code's `statusLine` set to mausi-statusline (model,
  folder, task, context bar); only offered when no status line is set

A "no" is remembered, so plain `mausi` doesn't ask again; `mausi setup` asks
about everything that is still missing. Everything else needs no setup: the
config file is optional and the task store is created on first use.
"""
import json
import os
import shutil
import subprocess
import sys

import mausi_common as cfg

REPO = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
KITTY_PY = os.path.join(REPO, "kitty", "mausi.py")
BIN = os.path.join(REPO, "bin")
SRC = os.path.join(REPO, "src")
SKIPPED = os.path.join(cfg.STATE_DIR, "setup-skipped")
PLUGIN = "mausi@mausi"


def _tilde(path):
    return "~" + path[len(cfg.HOME):] if path.startswith(cfg.HOME + os.sep) else path


# --- kitty ----------------------------------------------------------------

def kitty_conf():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(cfg.HOME, ".config"),
                        "kitty", "kitty.conf")


def kitty_ok():
    """kitty.conf loads mausi's settings (geninclude of mausi.py, or a plain
    include of mausi.conf)."""
    try:
        lines = open(kitty_conf()).read().splitlines()
    except OSError:
        return False
    for line in lines:
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0] in ("geninclude", "include"):
            path = os.path.realpath(os.path.join(os.path.dirname(kitty_conf()), os.path.expanduser(parts[1])))
            if path in (KITTY_PY, os.path.join(REPO, "kitty", "mausi.conf")):
                return True
    return False


def kitty_fix():
    path = kitty_conf()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(f"\n# mausi\ngeninclude {_tilde(KITTY_PY)}\n")
    return "added; restart kitty to load it (remote control and the Alt keys)"


# --- Claude Code plugin ---------------------------------------------------

def plugin_state():
    """True (installed and enabled), False (missing), or None (Claude Code
    isn't installed, or installed but disabled: the user's call)."""
    if not shutil.which("claude"):
        return None
    try:
        out = subprocess.run(["claude", "plugin", "list", "--json"], capture_output=True, text=True,
                             timeout=30).stdout
        plugins = json.loads(out or "[]")
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    for p in plugins:
        if p.get("id") == PLUGIN:
            return True if p.get("enabled") else None
    return False


def plugin_fix():
    for cmd in (["claude", "plugin", "marketplace", "add", REPO], ["claude", "plugin", "install", PLUGIN]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and "already" not in (r.stdout + r.stderr).lower():
            return f"failed: {' '.join(cmd)}\n{(r.stderr or r.stdout).strip()}"
    return "installed; new Claude sessions load it (/reload-plugins in running ones)"


# --- PATH -----------------------------------------------------------------

def path_ok():
    """mausi's bin/ is on PATH, or the shell profile already adds it (and this
    shell was started before that)."""
    found = shutil.which("mausi")
    if found and os.path.realpath(found) == os.path.join(BIN, "mausi"):
        return True
    profile, line = shell_profile()
    try:
        return bool(profile) and line in open(profile).read()
    except OSError:
        return False


def shell_profile():
    """(profile file, line to add) for the login shell, or (None, line)."""
    shell = os.path.basename(os.environ.get("SHELL", ""))
    bin_ = _tilde(BIN).replace("~", "$HOME", 1)
    if shell == "fish":
        return os.path.join(cfg.HOME, ".config", "fish", "config.fish"), f"fish_add_path {bin_}"
    line = f'export PATH="{bin_}:$PATH"'
    if shell == "zsh":
        return os.path.join(cfg.HOME, ".zshrc"), line
    if shell == "bash":
        return os.path.join(cfg.HOME, ".bash_profile" if cfg.IS_MAC else ".bashrc"), line
    return None, line


def path_fix():
    profile, line = shell_profile()
    if not profile:
        return f"don't know your shell's profile; add this line yourself:\n    {line}"
    os.makedirs(os.path.dirname(profile), exist_ok=True)
    with open(profile, "a") as f:
        f.write(f"\n# mausi\n{line}\n")
    return f"added to {_tilde(profile)}; new shells pick it up"


def path_question():
    profile, line = shell_profile()
    where = f"to {_tilde(profile)}" if profile else "to your shell profile"
    return f"`mausi` and `tasks` aren't on your PATH. Add {_tilde(BIN)} {where}?"


# --- Claude Code status line -----------------------------------------

def claude_settings():
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(cfg.HOME, ".claude"),
                        "settings.json")


def _load_settings():
    try:
        with open(claude_settings()) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def statusline_missing():
    """True when Claude Code has no status line yet (one of the user's own is
    left alone; settings that don't parse too)."""
    if not shutil.which("claude"):
        return False
    try:
        return "statusLine" not in _load_settings()
    except (OSError, ValueError):
        return False


def statusline_fix():
    settings = _load_settings()
    settings["statusLine"] = {"type": "command", "command": os.path.join(SRC, "mausi-statusline")}
    path = os.path.realpath(claude_settings())  # a symlink (dotfiles) stays one: write its target
    backup = ""
    if os.path.exists(path):
        backup = path + ".mausi-backup"
        shutil.copy2(path, backup)
    cfg.write_atomic(path, json.dumps(settings, indent=2) + "\n")
    return (f"set in {_tilde(path)}; Claude Code picks it up on the next message"
            + (f" (the old file is in {_tilde(backup)})" if backup else ""))


# --- the prompt -----------------------------------------------------------

def checks():
    """[(name, question, fix)] for what is missing."""
    out = []
    if not kitty_ok():
        out.append(("kitty", f"kitty doesn't load mausi's settings. Add `geninclude {_tilde(KITTY_PY)}` "
                             f"to {_tilde(kitty_conf())}?", kitty_fix))
    if plugin_state() is False:
        out.append(("plugin", "The mausi Claude Code plugin (notifications, ⏳ on tabs, the task guard, "
                              "the tasks skill) isn't installed. Install it from this checkout?", plugin_fix))
    if not path_ok():
        out.append(("path", path_question(), path_fix))
    if statusline_missing():
        out.append(("statusline", "Claude Code has no status line. Use mausi's (model, folder, task, "
                                  "context bar)?", statusline_fix))
    return out


def _skipped():
    try:
        return set(open(SKIPPED).read().split())
    except OSError:
        return set()


def _skip(names):
    os.makedirs(os.path.dirname(SKIPPED), exist_ok=True)
    with open(SKIPPED, "w") as f:
        f.write("\n".join(sorted(names)) + "\n")


def run(ask_again=False):
    """Offer what's missing. ask_again: also what was declined before (`mausi
    setup`). Returns the names of the fixes made."""
    todo = checks()
    skipped = _skipped()
    if not ask_again:
        todo = [c for c in todo if c[0] not in skipped]
    if not todo:
        if ask_again:
            print("mausi: all set up")
        return []
    if not sys.stdin.isatty():
        print("mausi: setup incomplete (" + ", ".join(c[0] for c in todo) + "); run `mausi setup`",
              file=sys.stderr)
        return []
    done = []
    for name, question, fix in todo:
        try:
            answer = input(f"{question} [Y/n] ").strip().lower()
        except EOFError:
            answer = "n"
        if answer in ("", "y", "yes"):
            print("  " + fix())
            done.append(name)
            skipped.discard(name)
        else:
            print("  skipped (`mausi setup` asks again)")
            skipped.add(name)
    _skip(skipped)
    return done
