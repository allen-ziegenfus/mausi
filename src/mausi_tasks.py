"""The task store: one folder per task, <root>/<PREFIX>-<n>/, holding TASK.md
and whatever files belong to the task. The Markdown overview (by default
~/.claude/tasks.md, so Claude can read it) is generated from it and is not
edited by hand. Root, prefix and overview path come from config.toml [tasks].

TASK.md is a YAML header and the task's notes:

    ---
    id: T-12
    title: PROJ-1234 — structured JSON logging for the batch image.
    summary: 'Stage 4: PR open, waiting on review.'
    section: In dev (stage 2–3)
    order: 3
    status: open            # or done
    added: '2026-10-21'
    done: null              # the date it was marked done
    pinned_from: null       # the section it came from while pinned to Now
    keywords: [cx, scaling] # optional: search words not in the text (tasks tag / tasks find)
    watch: [PROJ-123]       # optional: tickets/PRs the task waits on, checked at session start (tasks watch)
    repos: [~/src/foo]      # optional: other folders (repos) that belong to the task, linked into code/
    extra_hours: 1.5        # optional: time spent outside Claude (tasks spent)
    effort: {...}           # optional: the effort, frozen when the task is done
    ---
    - PROJ-1200 cleanup done …
    - Session: `cd ~/tasks/T-12 && claude --resume …`

The notes are tasks.md's indented sub-bullets, two spaces shallower. The folder
name is the id only and never changes (Claude sessions are tied to their
folder's path); by-name/ holds symlinks with readable names, rebuilt from the
titles, and each folder carries its title and section in the user.xdg.comment
extended attribute (Linux). The root is a git repository; every change is committed.

Used by mausi-dash, mausi_taskview.py and the `tasks` command.
"""
import calendar
import datetime
import json
import os
import re
import subprocess
import time

import yaml

import mausi_common as cfg

HOME = cfg.HOME
ROOT = cfg.path("tasks", "root")
OVERVIEW = cfg.path("tasks", "overview")
SECTIONS = os.path.join(ROOT, "sections.json")  # [{"heading", "note"}] in tasks.md order
DONE_ARCHIVE = os.path.join(ROOT, "done-archive.md")  # tasks.md's Done section from before the move
COUNTER = os.path.join(ROOT, ".next-id")
LAST_RENDER = os.path.join(ROOT, ".last-render")  # the overview as last generated, to spot hand edits
OVERWRITTEN = os.path.join(ROOT, "overwritten")  # hand edits to tasks.md, kept before regenerating
BY_NAME = os.path.join(ROOT, "by-name")
PREFIX = cfg.get("tasks", "prefix")  # pick one no Jira project uses, so ids never collide with a ticket
ID_RE = re.compile(rf"\b{re.escape(PREFIX)}-(\d+)\b")
DONE_DAYS = 30  # done tasks listed in the overview's Done section this long
SESSIONS_BEGIN, SESSIONS_END = "<!-- OPEN-CLAUDE-SESSIONS:BEGIN -->", "<!-- OPEN-CLAUDE-SESSIONS:END -->"
DEFAULT_SECTION = "Follow-ups"
NOW = "Now"


def locked():
    """The task store's lock (ROOT/.lock), held while writing: shared by every
    tasks, dashboard and hook process."""
    return cfg.file_lock(os.path.join(ROOT, ".lock"))


write_atomic = cfg.write_atomic


def task_path(tid):
    return os.path.join(ROOT, tid, "TASK.md")


BROKEN = {}  # task id -> why its TASK.md couldn't be read (load() skips it); `tasks` reports them
HEADER_RE = re.compile(r"\A\ufeff?---[ \t]*\n(.*?)^---[ \t]*$\n?(.*)", re.S | re.M)
TEXT_FIELDS = ("id", "title", "summary", "section", "status", "pinned_from")


def load(tid):
    try:
        text = open(task_path(tid), encoding="utf-8", errors="replace").read()
    except OSError:
        return None
    BROKEN.pop(tid, None)
    m = HEADER_RE.match(text)
    if not m:
        BROKEN[tid] = "no --- header"
        return None
    try:
        t = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        BROKEN[tid] = f"bad YAML header: {str(e).splitlines()[0]}"
        return None
    if not isinstance(t, dict):
        BROKEN[tid] = "the header isn't a list of fields"
        return None
    # Hand edits: an unquoted date is read as a date, a number as a number.
    for k, v in list(t.items()):
        if isinstance(v, (datetime.date, datetime.datetime)):
            t[k] = v.isoformat()
        elif k in TEXT_FIELDS and v is not None and not isinstance(v, str):
            t[k] = str(v)
        if k in ("title", "summary") and isinstance(t[k], str) and "\n" in t[k]:
            t[k] = " ".join(t[k].split())  # one line in the overview, or it reads as more tasks
    t["id"] = tid
    t.setdefault("title", "")
    t["notes"] = [l for l in m.group(2).splitlines()]
    t["path"] = task_path(tid)
    t["mtime"] = os.path.getmtime(t["path"])
    return t


def load_all():
    """Every task, open and done, in section then order."""
    try:
        ids = [d for d in os.listdir(ROOT) if ID_RE.fullmatch(d)]
    except OSError:
        return []
    tasks = [t for t in (load(d) for d in ids) if t]
    rank = {s["heading"]: i for i, s in enumerate(sections())}
    return sorted(tasks, key=lambda t: (rank.get(t.get("section"), len(rank)), t.get("order", 0)))


def sections():
    try:
        with open(SECTIONS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return [{"heading": NOW, "note": None}, {"heading": DEFAULT_SECTION, "note": None}]


def save(t, message):
    """Write the task's TASK.md, regenerate the overview, commit."""
    with locked():
        write_atomic(task_path(t["id"]), _text(t))
        render(message)


def find(ref):
    """A task by id (T-12) or by title (exact, else unique prefix)."""
    ref = (ref or "").strip()
    if ID_RE.fullmatch(ref):
        return load(ref)
    tasks = load_all()
    exact = [t for t in tasks if t.get("title", "").strip() == ref]
    if exact:
        return exact[0]
    prefix = [t for t in tasks if t.get("title", "").startswith(ref)] if ref else []
    return prefix[0] if len(prefix) == 1 else None


def next_id():
    """One past both the counter and the highest id on disk; ids are never reused.
    The task's folder is created here, exclusively, so two sessions creating a
    task at the same moment get different ids."""
    with locked():
        try:
            last = int(open(COUNTER).read().strip() or 0)
        except (OSError, ValueError):
            last = 0
        try:
            used = [int(m.group(1)) for d in os.listdir(ROOT) for m in [ID_RE.fullmatch(d)] if m]
        except OSError:
            used = []
        os.makedirs(ROOT, exist_ok=True)
        n = max([last] + used) + 1
        while True:
            try:
                os.mkdir(os.path.join(ROOT, f"{PREFIX}-{n}"))
                break
            except FileExistsError:
                n += 1
        write_atomic(COUNTER, str(n))
        return f"{PREFIX}-{n}"


def top_order(section):
    orders = [t.get("order", 0) for t in load_all() if t.get("section") == section and t.get("status") == "open"]
    return min(orders, default=1) - 1


def create(title, summary="", section=DEFAULT_SECTION, notes=None, top=True):
    tid = next_id()
    if section not in [s["heading"] for s in sections()]:
        write_atomic(SECTIONS, json.dumps(sections() + [{"heading": section, "note": None}], ensure_ascii=False,
                                          indent=1))
    t = {"id": tid, "title": title.strip(), "summary": summary.strip(), "section": section,
         "order": top_order(section) if top else time.time(), "status": "open",
         "added": time.strftime("%Y-%m-%d"), "done": None, "pinned_from": None, "notes": list(notes or [])}
    save(t, f"new {tid}: {title[:60]}")
    return tid


def add_notes(t, lines):
    """Append note lines (each "- …", or indented continuation), skipping ones
    already there. Returns how many were added."""
    new = [l if l.startswith(("- ", "  ")) else f"- {l}" for l in lines]
    with locked():
        fresh = load(t["id"]) or t  # another process may have added notes since t was read
        new = [l for l in new if l not in fresh["notes"]]
        if new:
            fresh["notes"] = fresh["notes"] + new
            save(fresh, f"{t['id']}: note")
            t.update(fresh)
    return len(new)


def set_done(t, done=True):
    t["status"], t["done"] = ("done", time.strftime("%Y-%m-%d")) if done else ("open", None)
    t["effort"] = None
    if done:  # keep the numbers: the transcripts they come from are deleted after 30 days
        e = efforts([t]).get(t["id"])
        if e and e["secs"]:
            t["effort"] = {"hours": round(e["secs"] / 3600, 1), "sessions": e["sessions"],
                           "first": day(e["first"]), "last": day(e["last"])}
    save(t, f"{t['id']}: {'done' if done else 'reopened'}")


def toggle_now(t):
    """Pin to the top of Now, or send a pinned task back to where it came from."""
    if t.get("section") == NOW:
        t["section"], t["pinned_from"] = t.get("pinned_from") or DEFAULT_SECTION, None
        message = f"unpinned to {t['section'].split(' (')[0]}"
    else:
        t["pinned_from"], t["section"] = t.get("section"), NOW
        message = "pinned to Now"
    t["order"] = top_order(t["section"])
    save(t, f"{t['id']}: {message}")
    return message


def nudge(t, step):
    """Swap with the neighbour above (step -1) or below (+1) in its section."""
    peers = [p for p in load_all() if p.get("section") == t.get("section") and p.get("status") == "open"]
    i = next(i for i, p in enumerate(peers) if p["id"] == t["id"])
    if not 0 <= i + step < len(peers):
        return "already at the " + ("top" if step < 0 else "bottom") + " of its section"
    peers[i], peers[i + step] = peers[i + step], peers[i]
    for n, p in enumerate(peers):  # renumber the section, so equal orders can't stick
        if p.get("order") != n:
            p["order"] = n
            write_atomic(task_path(p["id"]), _text(p))
    render(f"{t['id']}: moved {'up' if step < 0 else 'down'}")
    return f"moved {'up' if step < 0 else 'down'}"


def _text(t):
    header = {k: t.get(k) for k in ("id", "title", "summary", "section", "order", "status", "added", "done",
                                     "pinned_from")}
    header.update({k: t[k] for k in ("keywords", "watch", "repos", "extra_hours", "effort") if t.get(k)})
    body = "\n".join(t.get("notes", [])).rstrip("\n")
    return "---\n" + yaml.safe_dump(header, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + \
        (body + "\n" if body else "")


def set_keywords(t, words, remove=False):
    """Add (or remove) search keywords, lowercased, kept sorted and unique."""
    current = set(t.get("keywords") or [])
    words = {w.strip().lower() for w in words if w.strip()}
    t["keywords"] = sorted(current - words if remove else current | words)
    save(t, f"{t['id']}: keywords {' '.join(t['keywords']) or '(none)'}")
    return t["keywords"]


def set_watch(t, refs, remove=False):
    """Add (or remove) what the task waits on: ticket keys or PR URLs, in the order given, no duplicates."""
    current = list(t.get("watch") or [])
    refs = [r.strip() for r in refs if r.strip()]
    t["watch"] = [r for r in current if r not in refs] if remove else current + [r for r in refs if r not in current]
    save(t, f"{t['id']}: watch {' '.join(t['watch']) or '(none)'}")
    return t["watch"]


SEARCH_SKIP = {"scratch", "code"}  # git-ignored scratch files, links to worktrees
SEARCH_MAX_BYTES = 1_000_000


def _file_lines(t):
    """(relative path, line) for the task's text files, TASK.md excluded."""
    folder = os.path.dirname(t["path"])
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = sorted(d for d in dirnames if not d.startswith(".") and d not in SEARCH_SKIP)
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            if path == t["path"] or name.startswith("."):
                continue
            try:
                if os.path.getsize(path) > SEARCH_MAX_BYTES:
                    continue
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            yield os.path.relpath(path, folder), line.strip()
            except (OSError, UnicodeDecodeError):
                continue


SEARCH_TIERS = ("keywords", "title", "notes", "files")


def search(terms, include_done=True, max_hits=3):
    """Tasks where every term (case-insensitive substring) appears somewhere,
    best first: ranked by the first place a term matched (keywords, then
    title/summary, notes, the task's files), then open before done, then most
    recently changed. Returns (task, tier, [(where, line)])."""
    terms = [x.lower() for x in terms if x.strip()]
    if not terms:
        return []
    out = []
    for t in load_all():
        if not include_done and t.get("status") != "open":
            continue
        sources = {
            "keywords": [("keywords", ", ".join(t.get("keywords") or []))],
            "title": [("title", t.get("title") or ""), ("summary", t.get("summary") or "")],
            "notes": [("notes", l.strip()) for l in t.get("notes", []) if l.strip()],
            "files": _file_lines(t),
        }
        found, hits, best = set(), [], None
        for tier in SEARCH_TIERS:
            for where, line in sources[tier]:
                matched = {x for x in terms if x in line.lower()}
                if not matched:
                    continue
                best = best or tier
                found |= matched
                if len(hits) < max_hits:
                    hits.append((where, line))
                if found == set(terms) and len(hits) >= max_hits:
                    break
            if found == set(terms) and len(hits) >= max_hits:
                break
        if found == set(terms):
            out.append((t, best, hits))
    out.sort(key=lambda r: (SEARCH_TIERS.index(r[1]), r[0].get("status") != "open", -(r[0].get("mtime") or 0)))
    return out


def files(t, limit=None):
    """The task's own files (not TASK.md), newest first, as (path, mtime)."""
    folder = os.path.dirname(t["path"])
    out = []
    for dirpath, dirnames, filenames in os.walk(folder):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if path == t["path"] or name.startswith("."):
                continue
            try:
                out.append((path, os.path.getmtime(path)))
            except OSError:
                pass
    out.sort(key=lambda f: -f[1])
    return out[:limit] if limit else out


CODE = "code"  # <task>/code/: links to the task's worktrees (git ignores it)
WORKTREES = cfg.path("tasks", "worktrees")  # <repo>-<TICKET> worktrees; "" = no code/ links
KEY_RE = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b")


def main_ticket(t):
    """The Jira key a task is about: from its title, else its summary, else a
    "Jira:" note (the task view's rule; tickets other notes mention don't count)."""
    notes = [n[2:] if n.startswith("- ") else n for n in t.get("notes", [])]
    for text in (t.get("title", ""), t.get("summary") or "", "\n".join(n for n in notes if n.startswith("Jira"))):
        keys = [k for k in KEY_RE.findall(text) if not ID_RE.fullmatch(k)]
        if keys:
            return keys[0]
    return None


def task_tickets(t):
    """The main ticket plus every key on a "Jira:" note line: the tickets whose
    worktrees belong to the task."""
    keys = []
    main = main_ticket(t)
    if main:
        keys.append(main)
    for n in t.get("notes", []):
        n = n[2:] if n.startswith("- ") else n
        if n.startswith("Jira"):
            keys += [k for k in KEY_RE.findall(n) if not ID_RE.fullmatch(k) and k not in keys]
    return keys


def _is_ticket_worktree(name, key):
    """<repo>-<KEY> or <repo>-<KEY>-<suffix> (e.g. api-PROJ-7-retry)."""
    return name.endswith("-" + key) or f"-{key}-" in name


def task_repos(t):
    """{link name: path} for the folders in the task's `repos:` field that exist."""
    out = {}
    for r in t.get("repos") or []:
        path = os.path.realpath(os.path.expanduser(str(r)))
        if os.path.isdir(path):
            out[os.path.basename(path)] = path
    return out


def link_worktrees(only=None):
    """Keep <task>/code/ in step with the worktrees: a link to every
    <worktrees>/<repo>-<TICKET>[-<suffix>] directory whose ticket is one of an
    open task's tickets, and to each folder in its `repos:` field; no links to
    worktrees that are gone. Also keeps the task's VS Code workspace and the
    Project Manager list in step. `only` limits it to one task id. Returns the
    links added."""
    try:
        dirs = [d for d in os.listdir(WORKTREES) if os.path.isdir(os.path.join(WORKTREES, d))]
    except OSError:
        dirs = []
    added = []
    for t in load_all():
        if only and t["id"] != only:
            continue
        folder = os.path.join(os.path.dirname(t["path"]), CODE)
        keys = task_tickets(t) if t.get("status") == "open" else []
        want = {d: os.path.join(WORKTREES, d) for d in dirs if any(_is_ticket_worktree(d, k) for k in keys)}
        if t.get("status") == "open":
            want.update(task_repos(t))
        have = os.listdir(folder) if os.path.isdir(folder) else []
        changed = False
        for name in have:
            link = os.path.join(folder, name)
            if os.path.islink(link) and (name not in want or not os.path.exists(link)
                                         or os.readlink(link) != want[name]):
                os.remove(link)
                changed = True
        for name, target in want.items():
            link = os.path.join(folder, name)
            if not os.path.lexists(link):
                os.makedirs(folder, exist_ok=True)
                os.symlink(target, link)
                added.append(link)
                changed = True
        if os.path.isdir(folder) and not os.listdir(folder):
            os.rmdir(folder)
        if t.get("status") == "open" and want and (changed or not os.path.exists(workspace_path(t))):
            write_workspace(t)
    write_project_list()
    return added


# VS Code: <task>/<id>.code-workspace (git-ignored) holds the task folder plus,
# for each linked worktree, the module folders its branch changes, and each
# folder in `repos:` whole. The Project
# Manager extension's projects.json lists every open task, so any task opens
# from VS Code (Alt+Shift+P) without having been opened before.
WORKSPACE_MAX_ROOTS = 8  # per worktree; more than this and the whole worktree is one root
MODULE_MARKERS = ("bnd.bnd", "build.gradle", "Chart.yaml", "package.json", "pom.xml", "main.tf")
_VSCODE_USER = [os.path.join(HOME, "Library", "Application Support", app, "User") for app in ("Code",)] \
    if cfg.IS_MAC else [os.path.join(HOME, ".config", app, "User") for app in ("Code - OSS", "Code")]  # Arch: Code - OSS
PROJECT_MANAGER = next((os.path.join(u, "globalStorage", "alefragnani.project-manager", "projects.json")
                        for u in _VSCODE_USER if os.path.isdir(u)),
                       os.path.join(_VSCODE_USER[0], "globalStorage", "alefragnani.project-manager", "projects.json"))
PROJECT_TAG = "task"  # entries with this tag are ours; the rest of projects.json is left alone


def workspace_path(t):
    return os.path.join(os.path.dirname(t["path"]), f"{t['id']}.code-workspace")


def _git(cwd, *args):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=60)
    return r.stdout if r.returncode == 0 else None


def _changed_files(worktree):
    """Files the worktree's branch changes against master, plus uncommitted ones."""
    out = set()
    for ref in ("upstream/master", "origin/master", "master", "origin/main", "main"):
        base = (_git(worktree, "merge-base", "HEAD", ref) or "").strip()
        if base:
            out.update((_git(worktree, "diff", "--name-only", base, "HEAD") or "").split())
            break
    out.update((_git(worktree, "diff", "--name-only", "HEAD") or "").split())
    return sorted(out)


def _module_roots(worktree, files):
    """The nearest ancestor of each file that looks like a module (bnd.bnd,
    build.gradle, Chart.yaml, ...), else its top-level folder; files at the top
    level are skipped and nested roots folded into their parent."""
    roots = set()
    for f in files:
        if "/" not in f:
            continue
        d = os.path.dirname(f)
        root = f.split("/")[0]
        while d:
            if any(os.path.exists(os.path.join(worktree, d, m)) for m in MODULE_MARKERS):
                root = d
                break
            d = os.path.dirname(d)
        roots.add(root)
    roots = sorted(roots)
    return [r for r in roots if not any(r.startswith(o + "/") for o in roots if o != r)]


def _short_name(name):
    """A worktree link's label: "api-PROJ-12-fix" -> "PROJ-12-fix (api)"."""
    m = re.match(r"^(.+?)-([A-Z][A-Z0-9]+-\d+.*)$", name)
    return f"{m.group(2)} ({m.group(1)})" if m else name


def write_workspace(t):
    """Write <task>/<id>.code-workspace. Returns its path."""
    folders = [{"name": f"{t['id']} notes", "path": "."}]
    repos = set(task_repos(t).values())
    for name, target in code_links(t):
        try:
            roots = [] if target in repos else _module_roots(target, _changed_files(target))
        except (OSError, subprocess.SubprocessError):
            roots = []
        if not roots or len(roots) > WORKSPACE_MAX_ROOTS:
            roots = [""]
        for r in roots:
            short = _short_name(name)
            folders.append({"name": f"{short} · {r}" if r else short,
                            "path": os.path.join(target, r) if r else target})
    ws = {
        "folders": folders,
        "settings": {
            "files.exclude": {"code": True, "**/.gradle": True, "**/build": True, "**/node_modules": True},
            "search.exclude": {"scratch": True},
            "window.title": f"{t['id']} · {t.get('title', '')[:60]}${{separator}}${{activeEditorShort}}",
        },
    }
    path = workspace_path(t)
    text = json.dumps(ws, indent=2, ensure_ascii=False) + "\n"
    try:
        if open(path).read() == text:
            return path
    except OSError:
        pass
    write_atomic(path, text)
    return path


def write_project_list():
    """Put every open task in Project Manager's projects.json (tagged "task"),
    opening its workspace if it has one, else its folder. Leaves other entries
    alone. Does nothing until the extension is installed."""
    if not os.path.isdir(os.path.dirname(os.path.dirname(PROJECT_MANAGER))):
        return
    try:
        current = json.load(open(PROJECT_MANAGER))
    except FileNotFoundError:
        current = []
    except (OSError, ValueError):
        return  # unreadable or hand-edited mid-way: leave the user's projects alone
    if not isinstance(current, list):
        return
    others = [p for p in current if PROJECT_TAG not in (p.get("tags") or [])]
    ours = []
    # "Now" tasks first (in their overview order), then the rest by number
    for t in sorted(load_all(), key=lambda t: (t.get("section") != NOW,
                                               t.get("order") or 0 if t.get("section") == NOW else 0,
                                               int(t["id"].rsplit("-", 1)[1]))):
        if t.get("status") != "open":
            continue
        ws = workspace_path(t)
        ours.append({
            "name": f"{t['id']} {t.get('title', '')}",
            "rootPath": ws if os.path.exists(ws) else os.path.dirname(t["path"]),
            "paths": [],
            "tags": [PROJECT_TAG, t.get("section") or DEFAULT_SECTION],
            "enabled": True,
        })
    text = json.dumps(others + ours, indent=4, ensure_ascii=False) + "\n"
    try:
        if open(PROJECT_MANAGER).read() == text:
            return
    except OSError:
        pass
    write_atomic(PROJECT_MANAGER, text)


def code_links(t):
    """(name, target) for the task's worktree links."""
    folder = os.path.join(os.path.dirname(t["path"]), CODE)
    try:
        return sorted((n, os.readlink(os.path.join(folder, n))) for n in os.listdir(folder)
                      if os.path.islink(os.path.join(folder, n)))
    except OSError:
        return []


# Effort: how long I worked on a task, from my messages to Claude in the
# sessions that belong to it. Gaps of up to ACTIVE_GAP between my messages count
# as active (as the dashboard's week view counts it); the
# spans of all its sessions are merged, so parallel sessions don't count twice.
# Each session's spans are kept in ACTIVITY, read incrementally, and outlive the
# transcripts, which Claude Code deletes after 30 days.
PROJECTS = os.path.join(HOME, ".claude", "projects")
ACTIVITY = os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.join(HOME, ".local", "state"),
                        "mausi", "session-activity.json")
ACTIVE_GAP = 15 * 60
FIRST_PROMPTS = 3
RESUME_RE = re.compile(r"--resume ([0-9a-f]{8}-[0-9a-f-]{27})")


def project_dir(path):
    """The ~/.claude/projects folder name Claude Code uses for a cwd."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def load_activity():
    try:
        with open(ACTIVITY) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def scan_sessions():
    """Read what's new in every transcript into ACTIVITY: per session id its
    project folder, my message spans [[start, end]], first/last message and count."""
    state = load_activity()
    changed = False
    try:
        dirs = os.listdir(PROJECTS)
    except OSError:
        return state
    for d in dirs:
        try:
            names = [n for n in os.listdir(os.path.join(PROJECTS, d)) if n.endswith(".jsonl")]
        except OSError:
            continue
        for name in names:
            path = os.path.join(PROJECTS, d, name)
            sid = name[:-6]
            e = state.get(sid)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if e and e.get("offset") == size:
                continue
            if not e or size < e.get("offset", 0):  # new, or rewritten: read from the start
                e = state[sid] = {"dir": d, "offset": 0, "spans": [], "first": None, "last": None, "msgs": 0}
            _read_transcript(path, e)
            changed = True
    if changed:
        write_atomic(ACTIVITY, json.dumps(state, separators=(",", ":")))
    return state


def _read_transcript(path, e):
    with open(path, "rb") as f:
        f.seek(e["offset"])
        data = f.read()
    end = data.rfind(b"\n") + 1  # a half-written last line waits for the next scan
    for line in data[:end].decode(errors="ignore").splitlines():
        if '"type":"user"' not in line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if ev.get("type") != "user" or ev.get("isMeta") or ev.get("isSidechain"):
            continue
        content = (ev.get("message") or {}).get("content")
        if isinstance(content, list) and any(isinstance(c, dict) and c.get("type") == "tool_result"
                                             for c in content):
            continue  # tool results arrive while I'm away
        try:
            ts = calendar.timegm(time.strptime(ev["timestamp"][:19], "%Y-%m-%dT%H:%M:%S"))
        except (KeyError, ValueError):
            continue
        if e["msgs"] < FIRST_PROMPTS:  # the tickets a session was started for
            text = content if isinstance(content, str) else " ".join(
                c.get("text", "") for c in content or [] if isinstance(c, dict))
            e.setdefault("keys", [])
            e["keys"] += [k for k in KEY_RE.findall(text) if k not in e["keys"]]
        last = e["last"]
        if last is not None and 0 < ts - last <= ACTIVE_GAP:
            if e["spans"] and e["spans"][-1][1] == last:
                e["spans"][-1][1] = ts
            else:
                e["spans"].append([last, ts])
        e["first"] = ts if e["first"] is None else min(e["first"], ts)
        e["last"] = ts if last is None else max(last, ts)
        e["msgs"] += 1
    e["offset"] += end


def remember_sessions(links):
    """{session id: task id} as the dashboard linked them (attached with a, or
    found from the opening prompts), kept so effort still counts them later."""
    state = load_activity()
    new = {sid: tid for sid, tid in links.items() if sid in state and state[sid].get("task") != tid}
    for sid, tid in new.items():
        state[sid]["task"] = tid
    if new:
        write_atomic(ACTIVITY, json.dumps(state, separators=(",", ":")))


def efforts(tasks=None, state=None):
    """{task id: {"secs", "sessions", "first", "last", "extra"}} — "first"/"last"
    are epochs of my first and last message. A session belongs to the task whose
    notes name it (claude --resume), else whose folder it ran in, else the task
    the dashboard linked it to, else the task whose id or main ticket its first
    prompts name, else whose main ticket its worktree is for. Only time from the
    day the task was added counts: one long session can serve several tasks."""
    tasks = load_all() if tasks is None else tasks
    state = scan_sessions() if state is None else state
    every = load_all() if len(tasks) < 2 else tasks
    resumed = {sid: t["id"] for t in every for sid in RESUME_RE.findall("\n".join(t.get("notes", [])))}
    root = project_dir(ROOT) + "-"
    keys = {}
    for t in every:
        if t.get("status") == "open" or t in tasks:
            key = main_ticket(t)
            if key:
                keys.setdefault(key, t["id"])
    owner = {}
    for sid, e in state.items():
        d = e["dir"]
        folder = ID_RE.match(d[len(root):]) if d.startswith(root) else None
        tid = resumed.get(sid) or (folder.group(0) if folder else None) or e.get("task")
        if not tid:
            named = [k for k in e.get("keys", []) if ID_RE.fullmatch(k) or k in keys]
            tid = keys.get(named[0], named[0]) if named else None
        if not tid:
            tid = next((t for k, t in keys.items() if re.search(rf"-{re.escape(k)}(-|$)", d)), None)
        if tid:
            owner.setdefault(tid, []).append(e)
    out = {}
    for t in tasks:
        since = time.mktime(time.strptime(t["added"], "%Y-%m-%d")) if t.get("added") else 0
        mine = [e for e in owner.get(t["id"], []) if (e.get("last") or 0) >= since]
        spans, merged = sorted((max(a, since), b) for e in mine for a, b in e["spans"] if b > since), []
        for a, b in spans:
            if merged and a <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        stamps = [max(e[k], since) for e in mine for k in ("first", "last") if e.get(k)]
        out[t["id"]] = {"secs": sum(b - a for a, b in merged), "sessions": sum(1 for e in mine if e["msgs"]),
                        "first": min(stamps) if stamps else None, "last": max(stamps) if stamps else None,
                        "extra": float(t.get("extra_hours") or 0)}
    return out


def day(epoch):
    return time.strftime("%Y-%m-%d", time.localtime(epoch)) if epoch else None


def hours(secs):
    return f"{int(secs // 3600)}h{int(secs % 3600 // 60):02d}m" if secs >= 3600 else f"{int(secs // 60)}m"


def effort_text(t, e=None):
    """One line: "≈ 4h30m in 3 sessions (+1.5h outside Claude) · 2026-10-21 → 10-26, 6 days".
    A done task shows what was frozen when it was marked done."""
    frozen = t.get("effort") if t.get("status") == "done" else None
    if frozen:
        secs, n, first, last = frozen["hours"] * 3600, frozen["sessions"], frozen["first"], frozen["last"]
    else:
        e = e or efforts([t]).get(t["id"])
        secs, n, first, last = e["secs"], e["sessions"], day(e["first"]), day(e["last"])
    extra = float(t.get("extra_hours") or 0)
    parts = []
    if secs or n:
        parts.append(f"≈ {hours(secs)} in {n} session{'s' if n != 1 else ''}")
    if extra:
        parts.append(f"+{extra:g}h outside Claude" if parts else f"{extra:g}h outside Claude")
    start, end = t.get("added") or first, t.get("done") or (time.strftime("%Y-%m-%d") if t.get("status") == "open"
                                                             else last)
    if start and end:
        try:
            days = (time.mktime(time.strptime(end, "%Y-%m-%d")) - time.mktime(time.strptime(start, "%Y-%m-%d")))
            days = round(days / 86400) + 1
            span = f"{start} → {end[5:] if end[:4] == start[:4] else end}, {days} day{'s' if days != 1 else ''}"
        except ValueError:
            span = ""
        if span:
            parts.append(span + (" so far" if t.get("status") == "open" else ""))
    if first and last:
        parts.append(f"worked {first[5:]}" + (f" → {last[5:]}" if last != first else ""))
    return " · ".join(parts) if parts else "no Claude sessions found"


def spend(t, h, note=""):
    """tasks spent: add time spent outside Claude (meetings, reviews, reading)."""
    t["extra_hours"] = round(float(t.get("extra_hours") or 0) + h, 2)
    t["notes"] = t["notes"] + [f"- Spent {h:g}h {time.strftime('%Y-%m-%d')}" + (f": {note}" if note else "")]
    save(t, f"{t['id']}: spent {h:g}h")


def slug(title):
    words = re.findall(r"[a-z0-9]+", title.lower())
    return "-".join(words)[:60].rstrip("-") or "task"


def render(message=None):
    """Regenerate the overview, by-name/ and the folder comments, then
    commit ~/tasks. The session-snapshot block in tasks.md is kept as it is."""
    with locked():
        _render(message)


def _render(message=None):
    tasks = load_all()
    try:
        old = open(OVERVIEW).read()
    except OSError:
        old = ""
    block = re.search(re.escape(SESSIONS_BEGIN) + r".*?" + re.escape(SESSIONS_END), old, re.S)
    out = ["# Tasks", "",
           "Global todo list. Items in `Open` are summarized at the start of every Claude Code session.", "",
           f"**Generated — don't edit.** Each task is `{ROOT.replace(HOME, '~')}/{PREFIX}-<n>/TASK.md`; change them with the `tasks` "
           "command (tasks new/note/done/show), the mausi dashboard, or by editing a TASK.md.", ""]
    out += [block.group(0) if block else f"{SESSIONS_BEGIN}\n{SESSIONS_END}", "", "## Open"]
    open_tasks = [t for t in tasks if t.get("status") == "open"]
    for sec in sections():
        members = [t for t in open_tasks if t.get("section") == sec["heading"]]
        if not members and not sec.get("note"):
            continue
        # The heading "" holds tasks that sat above the first heading in tasks.md.
        out += ["", f"### {sec['heading']}", ""] if sec["heading"] else [""]
        if sec.get("note") and not sec.get("note_after"):
            out += [sec["note"], ""]
        for t in members:
            out += [f"- [ ] **{t['title']}**" + (f" {t['summary']}" if t.get("summary") else ""),
                    f"  - ID: {t['id']}"] + \
                   ([f"  - Watch: {', '.join(t['watch'])}"] if t.get("watch") else []) + ["  " + l if l.strip() else "" for l in t.get("notes", [])] + [""]
        if sec.get("note") and sec.get("note_after"):
            out += ["", sec["note"]]
    stray = [t for t in open_tasks if t.get("section") not in {s["heading"] for s in sections()}]
    if stray:
        out += ["", f"### {DEFAULT_SECTION} (unfiled)", ""]
        for t in stray:
            out += [f"- [ ] **{t['title']}**", f"  - ID: {t['id']}", ""]
    out += ["", "## Done", ""]
    cutoff = time.strftime("%Y-%m-%d", time.localtime(time.time() - DONE_DAYS * 86400))
    for t in sorted((t for t in tasks if t.get("status") == "done" and (t.get("done") or "") >= cutoff),
                    key=lambda t: t.get("done") or "", reverse=True):
        out += [f"- **{t['title']}** — Done: {t['done']} ({t['id']})"]
    try:
        out += ["", open(DONE_ARCHIVE).read().rstrip("\n")]
    except OSError:
        pass
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out)) + "\n"
    keep_hand_edits(old)
    if text != old:
        write_atomic(OVERVIEW, text)
    write_atomic(LAST_RENDER, text)
    names(tasks)
    commit(message or "sync")


def keep_hand_edits(current):
    """tasks.md is generated, but an old dashboard or a Claude session may still
    edit it. If it differs from what was last generated (the session-snapshot
    block aside), keep a copy in overwritten/ before it is regenerated."""
    try:
        last = open(LAST_RENDER).read()
    except OSError:
        return
    def strip(text):
        return re.sub(re.escape(SESSIONS_BEGIN) + r".*?" + re.escape(SESSIONS_END), "", text, flags=re.S)
    if current and strip(current) != strip(last):
        write_atomic(os.path.join(OVERWRITTEN, time.strftime("%Y%m%d-%H%M%S") + "-tasks.md"), current)


def names(tasks):
    """by-name/<slug> -> ../<PREFIX>-n for open tasks, and the folder comments."""
    os.makedirs(BY_NAME, exist_ok=True)
    want = {}
    for t in tasks:
        if t.get("status") != "open":
            continue
        name = slug(t["title"])
        want[name if name not in want else f"{name}-{t['id'].lower()}"] = t["id"]
        comment = f"{t['title']} [{t.get('section', '').split(' (')[0]}]".encode()
        folder = os.path.dirname(t["path"])
        if hasattr(os, "setxattr"):  # Linux only; file managers show it as the folder's comment
            try:
                if os.getxattr(folder, "user.xdg.comment") != comment:
                    raise OSError
            except OSError:
                try:
                    os.setxattr(folder, "user.xdg.comment", comment)
                except OSError:
                    pass
    for name in os.listdir(BY_NAME):
        link = os.path.join(BY_NAME, name)
        if want.get(name) and os.readlink(link) == f"../{want[name]}":
            del want[name]
        elif os.path.islink(link):
            os.remove(link)
    for name, tid in want.items():
        try:
            os.symlink(f"../{tid}", os.path.join(BY_NAME, name))
        except FileExistsError:
            pass


GITIGNORE = "by-name/\n*.tmp*\n.last-render\n.lock\nscratch/\ncode/\n*.code-workspace\n"


def _ensure_repo():
    """The root folder, its .gitignore and git repo, created on first use.
    Returns what it did."""
    done = []
    os.makedirs(ROOT, exist_ok=True)
    ignore = os.path.join(ROOT, ".gitignore")
    if not os.path.exists(ignore):
        write_atomic(ignore, GITIGNORE)
        done.append("wrote .gitignore")
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        if subprocess.run(["git", "init", "-q", ROOT]).returncode == 0:
            done.append("git init")
    return done


def init():
    """Create an empty store. Leaves an existing store alone. Returns what it
    did, for the CLI. (The first change to a new store does the same.)"""
    done = _ensure_repo()
    sync()
    commit("init")
    return done


GIT_PROBLEM = None  # the last failed commit's git error; `tasks` reports it


def commit(message):
    """Commit the store if anything changed (by-name/ and counters are ignored).
    A store without a git repo gets one."""
    _ensure_repo()
    if not os.path.isdir(os.path.join(ROOT, ".git")):
        return
    global GIT_PROBLEM
    git = ["git", "-C", ROOT]
    steps = [git + ["status", "--porcelain"], git + ["add", "-A"],
             git + ["-c", "commit.gpgsign=false", "-c", f"user.name={cfg.get('tasks', 'git_name')}",
                    "-c", f"user.email={cfg.get('tasks', 'git_email')}", "commit", "-q", "-m", message]]
    for i, cmd in enumerate(steps):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            last = (r.stderr or r.stdout).strip().splitlines()[-1:] or [f"exit {r.returncode}"]
            GIT_PROBLEM = f"git {('status', 'add', 'commit')[i]}: {last[0]}"
            cfg.log_error(f"task store commit ({message})", (r.stderr or r.stdout).strip())
            return
        if i == 0 and not r.stdout.strip():
            return  # nothing changed


def sync():
    """Pick up TASK.md files edited by hand (or by a Claude session): regenerate
    the overview when any is newer than it, or when a task folder was added or
    removed (the overview's ID lines no longer match the folders)."""
    try:
        listed = set(re.findall(r"^  - ID: (\S+)$", open(OVERVIEW).read(), re.M))
        # When the overview was last generated: its own mtime also moves when
        # the session block in it is rewritten, which would hide edits.
        overview = os.path.getmtime(LAST_RENDER if os.path.exists(LAST_RENDER) else OVERVIEW)
    except OSError:
        overview, listed = 0, None
    tasks = load_all()
    shown = {t["id"] for t in tasks if t.get("status") == "open"}
    if any(t["mtime"] > overview for t in tasks) or listed is None or not shown <= listed or \
            listed - {t["id"] for t in tasks}:
        render("sync edits")
        return True
    return False
