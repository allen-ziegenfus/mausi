"""mausi context: everything mausi knows right now, fetched once, in one file.

One collector fetches each source on its own schedule (the board for PRs, Jira
and tasks; mail; the calendar; docker and Kubernetes; the notification log,
which carries Slack too; the running Claude sessions) and writes the result
to CONTEXT (~/.cache/mausi/context.json, mode 0600: it holds mail and Slack).
The dashboard draws from it, the triage step reads it, and nothing else
fetches. docs/context.md describes the format.

Whoever holds the lock on CONTEXT + ".lock" is the collector:

    ctx = mausi_context.attach(on_update)   # the dashboard: collect in-process
                                            # if nobody else does, else follow
                                            # the file
    ctx.get("board")                        # {"at", "error", "data"} or None
    ctx.refresh("board", hard=True)         # fetch now (hard: the board drops
                                            # its caches too); None: all but triage

`mausi context --watch` runs a collector on its own (a service, or for the Go
port); a dashboard started next to it only reads.
"""
import fcntl
import html
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request

import mausi_common as cfg

CONTEXT = os.path.join(cfg.CACHE_DIR, "context.json")
LOCK = CONTEXT + ".lock"
REFRESH_REQ = CONTEXT + ".refresh"  # a follower asks the collector: "board hard\nmail\n"
VERSION = 1

BOARD_URL = cfg.get("board", "url").rstrip("/")
JIRA_SITE = cfg.get("jira", "site").rstrip("/")
CALENDAR_CMD = shlex.split(os.path.expanduser(cfg.get("commands", "calendar")))
MAIL_CMD = shlex.split(os.path.expanduser(cfg.get("commands", "mail")))
REMINDERS_CMD = shlex.split(os.path.expanduser(cfg.get("commands", "reminders")))
NOTIFICATIONS = cfg.data_path("notifications", "log", "notifications.jsonl")
DISMISSED = os.path.join(cfg.CACHE_DIR, "dismissed.json")
NOTIFY_KEEP = 3 * 86400  # how far back the notifications source goes (Slack's zoomed window)
NOTIFY_SKIP = {"kitty", "COSMIC Screenshot", " ", ""}  # never interesting; "Claude Code" stays for triage

MERGED_COMMENT_RE = re.compile(r"^\s*merged\b", re.I)
FORWARDED_COMMENT_RE = re.compile(r"\bforwarded to\b|^\s*see [\w.-]+#\d+\W*$", re.I)
SUPERSEDED_COMMENT_RE = re.compile(r"^\s*superseded\b", re.I)


def run(cmd, timeout=5):
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


# --- sources: each returns (data, error) ----------------------------------

def fetch_board(hard=False):
    """The board's dash.json ([board] url or command): PRs, Jira, tasks."""
    try:
        return mark_review_merges(cfg.clean(cfg.board_get("dash.json", hard))), None
    except (OSError, ValueError) as e:
        return None, (f"board unreachable ({e})" if BOARD_URL else f"board command failed ({e})")


def mark_review_merges(data):
    """Reviewers often merge by pushing the commits themselves and closing the
    PR with "Merged. Thank you.": GitHub calls that closed, not merged. Such a
    PR is marked merged when the board lists it to close in Jira or the last
    comment, by someone other than its author, starts with "merged". A PR
    closed because it went on to another repo ("forwarded to …", "See
    owner#123") is marked forwarded; one closed as "Superseded…", superseded."""
    if not isinstance(data, dict):
        return data
    to_close = {i.get("id") for i in data.get("to_close") or []}
    lists = [data.get(k) for k in ("mine", "forwards", "involved")]
    lists.append((data.get("recent") or {}).get("prs") if isinstance(data.get("recent"), dict) else None)
    for prs in lists:
        for pr in prs or []:
            if not isinstance(pr, dict) or pr.get("merged") or not pr.get("closed"):
                continue
            said = pr.get("last_comment_text") or ""
            by_other = pr.get("last_comment") and pr.get("last_comment") != pr.get("author")
            if pr.get("ref") in to_close or (by_other and MERGED_COMMENT_RE.search(said)):
                pr["merged"] = True
            elif FORWARDED_COMMENT_RE.search(said):
                pr["forwarded"] = True
            elif SUPERSEDED_COMMENT_RE.search(said):
                pr["superseded"] = True
    return data


def fetch_mail(hard=False):
    """The mail command's output: {"unread": [...], ...}."""
    try:
        return cfg.clean(json.loads(run(MAIL_CMD, 60) or "")), None
    except (TypeError, json.JSONDecodeError):
        return None, f"{MAIL_CMD[0]} failed"


def fetch_reminders(hard=False):
    """The reminders command's output: {"items": [...]}, items without an id or
    title left out."""
    try:
        data = cfg.clean(json.loads(run(REMINDERS_CMD, 60) or ""))
    except (TypeError, json.JSONDecodeError):
        return None, f"{REMINDERS_CMD[0]} failed"
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None, f"{REMINDERS_CMD[0]}: no items list"
    keep = [i for i in items if isinstance(i, dict) and i.get("id") and i.get("title")]
    fields = ("id", "title", "group", "due", "priority", "note", "url")
    out = {"items": [{k: str(i.get(k) or "") for k in fields} for i in keep]}
    return out, None


def fetch_calendar(hard=False):
    """{"today": <calendar command output>, "tomorrow": {...}}."""
    tomorrow = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    try:
        later = cfg.clean(json.loads(run(CALENDAR_CMD + [tomorrow], 60) or ""))
    except (TypeError, json.JSONDecodeError):
        later = {"events": []}
    try:
        today = cfg.clean(json.loads(run(CALENDAR_CMD, 60) or ""))
        today.setdefault("running", True)  # the command can say its source isn't running
    except (TypeError, json.JSONDecodeError):
        return {"today": {"events": [], "synced": None}, "tomorrow": later}, f"{CALENDAR_CMD[0]} failed"
    return {"today": today, "tomorrow": later}, None


def kubectl_json(*args):
    out = run(["kubectl", *args, "-o", "json", "--request-timeout=3s"], 6)
    try:
        return json.loads(out) if out else None
    except json.JSONDecodeError:
        return None


def fetch_infra(hard=False):
    """{"docker": [...] | None, "k3d": [...], "kube": {ctx, pods, nodes,
    hosts: {"<namespace>/<service>": host}} | None}."""
    data = {"docker": None, "k3d": [], "kube": None}
    out = run(["docker", "ps", "--format", "{{json .}}"])
    if out is not None:
        data["docker"] = [json.loads(l) for l in out.splitlines() if l.strip()]
    out = run(["k3d", "cluster", "list", "-o", "json"])
    data["k3d"] = json.loads(out) if out else []

    ctx = (run(["kubectl", "config", "current-context"]) or "").strip()
    if ctx:
        pods = kubectl_json("get", "pods", "-A")
        kube = {"ctx": ctx, "pods": None, "nodes": [], "hosts": {}}
        if pods is not None:
            kube["pods"] = pods["items"]
            kube["nodes"] = (kubectl_json("get", "nodes") or {"items": []})["items"]
            # Ingress hosts by backend service, so a pod row can open its app.
            for ing in (kubectl_json("get", "ingress", "-A") or {"items": []})["items"]:
                ns = ing["metadata"]["namespace"]
                for rule in ing["spec"].get("rules", []):
                    for path in rule.get("http", {}).get("paths", []):
                        svc = path.get("backend", {}).get("service", {}).get("name")
                        if svc and rule.get("host"):
                            kube["hosts"].setdefault(f"{ns}/{svc}", rule["host"])
        data["kube"] = kube
    return data, None


# Chromium web apps put their site first in the body:
#   <a href="https://app.slack.com/">app.slack.com</a>\n\nAlex Example: Hey team!
ORIGIN_RE = re.compile(r'^\s*<a href="(https?://[^"]+)">[^<]*</a>\s*')
SITES = (("app.slack.com", "Slack"), ("mail.google.com", "Mail"), ("calendar.google.com", "Calendar"),
         ("meet.google.com", "Meet"), ("atlassian.net", "Jira"), ("github.com", "GitHub"))


def notification_source(n):
    """(label, url to open, message text) for a captured notification."""
    app, summary = n.get("app", ""), n.get("summary", "")
    body = n.get("body", "") or ""
    origin = ORIGIN_RE.match(body)
    if origin:
        body = body[origin.end():]
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", body))).strip()
    if text == "default":
        text = ""  # logged before mausi-notif-capture parsed multi-line bodies
    link = re.search(r"https?://[^\s<>\"]+", text)

    label, home = app, None
    if origin:
        site = origin.group(1)
        label = next((name for host, name in SITES if host in site), re.sub(r"^https?://|/.*$", "", site))
        home = site
    elif app == "Chromium":
        # Older entries without a body: tell the web apps apart by title.
        if summary.endswith("(Jira)"):
            label, home = "Jira", (f"{JIRA_SITE}/jira/your-work" if JIRA_SITE else None)
        elif re.search(r"\(.+ - [A-Z][a-z]{2} \d{1,2}\)$", summary):
            label, home = "Meet", "https://meet.google.com"
        else:
            # Mostly Slack, though Gmail ones (sender as title) look the same.
            label, home = "Slack", "https://app.slack.com/client"
    return label, (link.group(0) if link else home), text


def tail(path, size=65536):
    """The last `size` bytes of path, as lines."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            start = max(0, f.tell() - size)
            f.seek(start)
            lines = f.read().decode(errors="ignore").splitlines()
            return lines[1:] if start else lines  # mid-file, the first line is cut
    except OSError:
        return []


def parse_notification(line):
    """A notification log line as {ts, id, app, label, url, summary, body}, or None."""
    try:
        n = cfg.clean(json.loads(line))
        ts = time.mktime(time.strptime(n["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
    except (ValueError, KeyError, TypeError):
        return None
    label, url, body = notification_source(n)
    return {"ts": ts, "id": n.get("id"), "app": n.get("app", ""), "label": label, "url": url,
            "summary": n.get("summary", ""), "body": body}


def read_notifications(window=NOTIFY_KEEP, size=4 << 20):
    """Captured notifications from the last `window` seconds, newest first."""
    now, out = time.time(), []
    for line in reversed(tail(NOTIFICATIONS, size)):
        n = parse_notification(line)
        if n is None:
            continue
        if now - n["ts"] > window:
            break
        if n["app"] not in NOTIFY_SKIP:
            out.append(n)
    return out


def load_dismissed():
    try:
        with open(DISMISSED) as f:
            d = json.load(f)
        return {"before": d.get("before", 0), "ids": list(d.get("ids", []))}
    except (OSError, ValueError):
        return {"before": 0, "ids": []}


def fetch_notifications(hard=False):
    """{"items": [...], "dismissed": {"before": epoch, "ids": [...]}}: the
    desktop notifications of the last 3 days (Slack's among them) and what
    d/D dismissed."""
    return {"items": read_notifications(), "dismissed": load_dismissed()}, None


def fetch_sessions(hard=False):
    """The running Claude Code sessions: [{cwd, session}]."""
    import mausi_sessions
    return [{"cwd": cwd, "session": sid} for cwd, sid in mausi_sessions.live_sessions()], None


TRIAGE = os.path.join(cfg.CACHE_DIR, "triage.json")
TRIAGE_EVERY = 30 * 60  # at most this often on its own (and only when the input changed); i runs it now


def fetch_triage(hard=False):
    """What mausi-triage (Claude over this context) last said needs me:
    {at, summary, items: [{title, why, urgency, source, ref, url}], error}.
    Runs it when asked (hard) or when its answer is older than TRIAGE_EVERY."""
    try:
        with open(TRIAGE) as f:
            last = json.load(f)
    except (OSError, ValueError):
        last = {}
    if hard or time.time() - last.get("at", 0) > TRIAGE_EVERY:
        here = os.path.dirname(os.path.realpath(__file__))
        run([os.path.join(here, "mausi-triage"), "--quiet"] + (["--force"] if hard else []), 300)
        try:
            with open(TRIAGE) as f:
                last = json.load(f)
        except (OSError, ValueError):
            pass
    return (last or None), (last or {}).get("error")


def notifications_changed():
    """A stamp that changes when the notification log or the dismissed list
    does, so the notifications source can be re-read only then."""
    stamp = []
    for path in (NOTIFICATIONS, DISMISSED):
        try:
            st = os.stat(path)
            stamp.append((st.st_mtime_ns, st.st_size))
        except OSError:
            stamp.append(None)
    return tuple(stamp)


# name: (fetch, seconds between fetches, enabled). The order is the file's.
SOURCES = {
    "board": (fetch_board, 60 if BOARD_URL else 180, cfg.BOARD),  # a command (e.g. gh) is slower: every 3 min
    "mail": (fetch_mail, 120, bool(MAIL_CMD)),
    "calendar": (fetch_calendar, 120, bool(CALENDAR_CMD)),
    "reminders": (fetch_reminders, 120, bool(REMINDERS_CMD)),
    "notifications": (fetch_notifications, 2, True),  # cheap: re-read only when the log changes
    "infra": (fetch_infra, 15, True),
    "sessions": (fetch_sessions, 15, True),
    "triage": (fetch_triage, 60, bool(cfg.get("triage", "enabled"))),
}


# --- the file ---------------------------------------------------------------

def read(path=CONTEXT):
    """The context file as a dict, or None."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def write(state):
    """Write state ({name: entry}) to CONTEXT, readable only by me."""
    doc = {"version": VERSION, "written": time.time(), "collector": os.getpid(),
           "sources": {name: state[name] for name in SOURCES if name in state}}
    if not os.path.exists(CONTEXT):
        os.makedirs(os.path.dirname(CONTEXT), exist_ok=True)
        os.close(os.open(CONTEXT, os.O_WRONLY | os.O_CREAT, 0o600))
    cfg.write_atomic(CONTEXT, json.dumps(doc, separators=(",", ":")))


class Context:
    """The collector (when it holds the lock) or a follower of the file. Either
    way get() answers from memory and on_update(name, entry) runs whenever a
    source has new data."""

    def __init__(self, on_update=None):
        self.on_update = on_update or (lambda name, entry: None)
        self.state = {}
        self.lock = threading.Lock()
        self.wakes = {name: threading.Event() for name in SOURCES}
        self.hard = set()
        self.lock_fd = None
        self.collecting = False
        self._dirty = threading.Event()

    # -- shared --
    def get(self, name):
        with self.lock:
            return self.state.get(name)

    def data(self, name, default=None):
        entry = self.get(name)
        return default if entry is None or entry.get("data") is None else entry["data"]

    def _set(self, name, entry):
        with self.lock:
            self.state[name] = entry
        try:
            self.on_update(name, entry)
        except Exception:  # noqa: BLE001 — a bad view must not stop collecting
            cfg.log_error(f"context on_update {name}")

    def refresh(self, name=None, hard=False):
        """Fetch a source now; None: all of them except triage (a paid model
        call, run only when asked for by name)."""
        names = [n for n in SOURCES if n != "triage"] if name is None else [name]
        if self.collecting:
            self._wake(names, hard)
            return
        # A follower: leave the request for the collector, which polls for it.
        try:
            with open(REFRESH_REQ, "a") as f:
                f.write("".join(f"{n}{' hard' if hard else ''}\n" for n in names))
        except OSError:
            pass

    def _wake(self, names, hard):
        for n in names:
            if n in self.wakes:
                if hard:
                    with self.lock:
                        self.hard.add(n)
                self.wakes[n].set()

    # -- starting --
    def start(self):
        """Collect if nobody else does, else follow the file (and take over
        when that collector stops)."""
        if self._take_lock():
            self._start_collecting()
        else:
            threading.Thread(target=self._follow, daemon=True).start()
        return self

    def _take_lock(self):
        os.makedirs(os.path.dirname(LOCK), exist_ok=True)
        fd = os.open(LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self.lock_fd = fd
        return True

    def _start_collecting(self):
        self.collecting = True
        old = read()  # start from what the last collector saw, so views aren't empty
        if old and old.get("version") == VERSION:
            for name, entry in (old.get("sources") or {}).items():
                if name in SOURCES and SOURCES[name][2]:
                    self._set(name, dict(entry, stale=True))
        for name, (fetch, every, enabled) in SOURCES.items():
            if enabled:
                threading.Thread(target=self._loop, args=(name, fetch, every), daemon=True).start()
        threading.Thread(target=self._writer, daemon=True).start()
        threading.Thread(target=self._poll_requests, daemon=True).start()

    # -- collecting --
    def _fresh(self):
        """Whether every other source has fetched since this collector started."""
        with self.lock:
            return all(n in self.state and not self.state[n].get("stale")
                       for n, (_, _, on) in SOURCES.items() if on and n != "triage")

    def _put(self, name, data, error):
        """Store a fetch; write the file only when something changed (or a
        source's time is 5 minutes behind), not on every unchanged poll."""
        prev = self.get(name) or {}
        if data is None and prev.get("data") is not None:
            data = prev["data"]  # keep the last good data, with the error
        changed = prev.get("data") != data or prev.get("error") != error or prev.get("stale")
        now = time.time()
        if changed or now - prev.get("at", 0) > 300:
            self._set(name, {"at": now, "error": error, "data": data})
            self._dirty.set()

    def _loop(self, name, fetch, every):
        if name == "triage":
            # Not over the last collector's leftovers: wait for fresh data.
            for _ in range(120):
                if self._fresh():
                    break
                time.sleep(1)
        last_stamp = None
        while True:
            with self.lock:
                hard = name in self.hard
                self.hard.discard(name)
            try:
                if name == "notifications":
                    stamp = notifications_changed()
                    if stamp != last_stamp or hard:
                        last_stamp = stamp
                        self._put(name, *fetch(hard))
                else:
                    self._put(name, *fetch(hard))
            except Exception as e:  # noqa: BLE001
                cfg.log_error(f"context {name}")
                self._put(name, None, f"{type(e).__name__}: {e}")
            self.wakes[name].wait(every)
            self.wakes[name].clear()

    def _writer(self):
        while True:
            self._dirty.wait()
            time.sleep(0.5)  # gather sources that finish together into one write
            self._dirty.clear()
            with self.lock:
                state = dict(self.state)
            try:
                write(state)
            except OSError:
                cfg.log_error("context write")

    def _read_requests(self):
        # Move the file aside first: a request appended while we read goes
        # into a new file instead of being deleted unread.
        taken = f"{REFRESH_REQ}.{os.getpid()}"
        try:
            os.rename(REFRESH_REQ, taken)
            with open(taken) as f:
                lines = f.read().split("\n")
            os.unlink(taken)
        except OSError:
            return
        for line in lines:
            name, _, hard = line.partition(" ")
            self._wake([name], bool(hard))

    def _poll_requests(self):
        while True:
            time.sleep(1)
            if os.path.exists(REFRESH_REQ):
                self._read_requests()

    # -- following --
    def _follow(self):
        last = None
        while True:
            try:
                mtime = os.stat(CONTEXT).st_mtime_ns
            except OSError:
                mtime = None
            if mtime != last:
                last = mtime
                doc = read()
                if doc and doc.get("version") == VERSION:
                    for name, entry in (doc.get("sources") or {}).items():
                        prev = self.get(name) or {}
                        if name in SOURCES and (entry.get("data"), entry.get("error")) != (prev.get("data"), prev.get("error")):
                            self._set(name, entry)
            if self._take_lock():  # the collector stopped: collect here
                self._start_collecting()
                return
            time.sleep(1)


def attach(on_update=None):
    return Context(on_update).start()


# --- the command -------------------------------------------------------------

def age(seconds):
    seconds = int(seconds)
    return f"{seconds}s" if seconds < 120 else f"{seconds // 60}m" if seconds < 7200 else f"{seconds // 3600}h"


def main(args):
    """mausi context [--watch | --json | --refresh [SOURCE]]"""
    if "--watch" in args:
        ctx = Context()
        if not ctx._take_lock():
            doc = read() or {}
            print(f"a collector already runs (pid {doc.get('collector', '?')})")
            return 1
        ctx._start_collecting()
        print(f"collecting into {CONTEXT} (Ctrl-C stops)")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return 0
    if "--refresh" in args:
        rest = [a for a in args if a != "--refresh"]
        Context().refresh(rest[0] if rest else None, hard=True)  # triage only by name
        return 0
    doc = read()
    if doc is None:
        print(f"no context yet at {CONTEXT}: start the dashboard (mausi dash) or `mausi context --watch`")
        return 1
    if "--json" in args:
        print(json.dumps(doc, indent=2))
        return 0
    now = time.time()
    alive = False
    try:
        fd = os.open(LOCK, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            alive = True
        os.close(fd)
    except OSError:
        pass
    print(f"{CONTEXT}  written {age(now - doc['written'])} ago by pid {doc.get('collector')}"
          + ("" if alive else "  (no collector running)"))
    for name, entry in doc.get("sources", {}).items():
        data = entry.get("data")
        size = (len(data) if isinstance(data, list)
                else len(data.get("items", [])) if isinstance(data, dict) and "items" in data
                else len(data.get("unread", [])) if isinstance(data, dict) and "unread" in data
                else "")
        print(f"  {name:<14} {age(now - entry['at']):>4} ago  {size!s:>4}  {entry.get('error') or ''}")
    return 0
