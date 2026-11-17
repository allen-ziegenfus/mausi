"""The task view of mausi-dash (v): tasks.md items with everything that
belongs to them — Jira tickets, PRs, Claude sessions, worktrees, mail, Slack.

Loaded by mausi-dash, which sets `dash` to itself; this file only reads
the dashboard's data (work_data from the board, mail, Slack, sessions)
and renders lines in the dashboard's (text, action) format, so the dashboard's
keys (Enter, Space, c, w, x, t, e, T) work on its rows unchanged.

Linking (phase 1, read-only):
- A task's keys are the Jira keys in its entry; the first one in its bold title
  (else the first anywhere) is its main key, the rest are related.
- An item (ticket, PR, session, mail, Slack message) carries keys and PR refs.
  It goes to the tasks whose entry names the PR, else whose main key it has,
  else whose related keys it has — unless a related key is in more than
  RELATED_MAX tasks, which makes it too generic to mean anything. If none of
  that finds a task, the same is tried with the keys' Jira parents, so a
  sub-task (and its PRs and mail) lands on the task of its parent ticket.
- A Claude session goes to the task whose entry has its `claude --resume <id>`
  line (a writes one, so tasks.md is where the link lives), or the one it was
  attached to with A (also kept in SESSION_LINKS by session id), or started for with c
  (kitty user var claude_task), else to a task quoted in its first few prompts,
  else the first key or PR link they name, else by its branch/folder ticket.
- Items that land nowhere and are mine to act on go to Unsorted; mail there
  leaves out bots and mail about a ticket or PR Unsorted already lists.
- Mail with the same subject is one row (GitHub sends one per comment).

Workflow (see workflow()): every ticket gets a stage — PLAN, DEV, REVIEW, FWD
(forwarded to the final reviewer; config [review], shown as their name), DONE — from its Jira status, moved forward by PR evidence; a task's stage is its
least advanced open ticket (the bottleneck) with done/total. When everything is
closed the task is in VERIFY — try it in a real use case — until a "Verified …:"
line under it (y writes one) makes it DONE. Attention marks:
! your move, • new since you last looked at the task, ◷ waiting on others.
Hygiene hints flag where the parent ticket and its sub-tasks disagree.
"""
import calendar
import json
import os
import re
import time

import mausi_common as cfg

dash = None  # set by mausi-dash

# The stage after peer review, when a PR is forwarded to a final reviewer's repo
# (config [review]); shown as their name in capitals.
FWD = (cfg.get("review", "forward_name") or "forward").upper()
FWD_NAME = cfg.get("review", "forward_name") or "the final reviewer"
FWD_OWNER = cfg.get("review", "forward_owner")

RELATED_MAX = 3
CACHE_SECS = 5
CLOSED_DAYS = 7  # recently closed PRs and tickets stay on their task this long (the board keeps 2)
MAIL_DAYS = 7
PERSONAL = ("Personal / side projects",)
DONE_DAYS = 30  # the Done group at the end of the list, like tasks.md's Done
PR_REF_RE = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
RESUME_RE = re.compile(r"--resume ([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})")
TITLE_RE = re.compile(r"\*\*(.+?)\*\*")

STAGES = ("PLAN", "DEV", "REVIEW", FWD, "VERIFY", "DONE")
VERIFIED_RE = re.compile(r"^Verified\b", re.I)  # a "Verified 2026-10-24: …" line under a task
STAGE_OF_STATUS = {
    "open": "PLAN", "selected for development": "PLAN", "ready for development": "PLAN", "to do": "PLAN",
    "backlog": "PLAN", "planning": "PLAN",
    "in progress": "DEV", "in development": "DEV", "reopened": "DEV",
    "in review": "REVIEW", "in peer review": "REVIEW", "code review": "REVIEW", "testing": "REVIEW",
    "done": "DONE", "closed": "DONE", "ready for release": "DONE", "resolved": "DONE", "released": "DONE",
}
MAGENTA = "\033[35m"
SEEN = os.path.join(cfg.CACHE_DIR, "task-seen.json")
# Claude session id -> {"task": title or None (unlinked on purpose), "at": epoch}, set with A.
SESSION_LINKS = os.path.join(cfg.CACHE_DIR, "session-tasks.json")
# What mausi-guard (the UserPromptSubmit hook) needs: tasks, Jira parents, session -> task.
TASK_INDEX = os.path.join(cfg.CACHE_DIR, "task-index.json")
FIRST_PROMPTS = 3  # prompts read from the start of a session to link it automatically  # task title -> when I last looked at it
SEEN_AFTER = 3  # seconds a task has to stay selected to count as looked at

_cache = {"at": 0.0, "data": None}
_prompts = {}  # session id -> (transcript mtime, text of its first prompts)
_selected = {"task": None, "since": 0.0}


def when(stamp):
    """Epoch seconds from "YYYY-MM-DD[ T]HH:MM" or "YYYY-MM-DD"; 0 if unknown."""
    for fmt, size in (("%Y-%m-%dT%H:%M", 16), ("%Y-%m-%d", 10)):
        try:
            return time.mktime(time.strptime((stamp or "").replace(" ", "T")[:size], fmt))
        except ValueError:
            continue
    return 0


def when_utc(stamp):
    """Epoch seconds from a UTC "YYYY-MM-DD HH:MM" (GitHub's mergedAt/closedAt,
    as the board passes them); 0 if unknown."""
    try:
        return calendar.timegm(time.strptime((stamp or "").replace(" ", "T")[:16], "%Y-%m-%dT%H:%M"))
    except ValueError:
        return when(stamp)


def keys_in(text):
    seen = []
    for k in dash.TICKET_ANY_RE.findall(text or ""):
        if k not in seen:
            seen.append(k)
    return seen


def refs_in(text):
    refs = {f"{o}/{r}#{n}" for o, r, n in dash.PR_URL_RE.findall(text or "")}
    refs |= {f"{repo}#{n}" for repo, n in PR_REF_RE.findall(text or "")}
    return refs


def load_tasks():
    tasks = []
    for sec in dash.task_sections_md():
        for it in sec["items"]:
            if any(h in it["head"] for h in dash.TASK_HIDE):
                continue
            text = "\n".join([it["head"]] + it["lines"])
            bold = TITLE_RE.match(it["head"])
            title = (bold.group(1) if bold else dash.plain(it["head"])).strip()
            keys = keys_in(text)
            # The task's own id (<PREFIX>-<n>, from its "ID:" line) links like a key but is
            # never a Jira ticket, so it can't be the main ticket. Other AZ ids the
            # notes mention stay related keys.
            own = re.search(r"^ID: (\S+)", "\n".join(it["lines"]), re.M)
            tid = own.group(1) if own and dash.store().ID_RE.fullmatch(own.group(1)) else None
            jira = [k for k in keys if not dash.store().ID_RE.fullmatch(k)]
            # The main ticket comes from the title, the summary or a "Jira:" note;
            # tickets only mentioned in other notes are related keys.
            stated = it["head"] + "\n" + "\n".join(l for l in it["lines"] if l.startswith("Jira"))
            main = ([k for k in keys_in(title) if k in jira] or [k for k in keys_in(stated) if k in jira] or [None])[0]
            added = re.search(r"Added: (\d{4}-\d\d-\d\d)", text)
            tasks.append({
                "id": len(tasks), "section": sec["title"], "title": title, "head": it["head"],
                "lines": it["lines"], "line": it["line"], "keys": keys, "main": main, "tid": tid,
                "refs": refs_in(text), "added": added.group(1) if added else None,
                "resume": set(RESUME_RE.findall(text)),
                "jira": [], "prs": [], "sessions": [], "mail": [], "slack": [], "latest": 0,
            })
    return tasks


def build(rows):
    """Tasks with their linked items, and Unsorted. Cached for CACHE_SECS."""
    if _cache["data"] is not None and time.time() - _cache["at"] < CACHE_SECS:
        return _cache["data"]
    with dash.lock:
        work, mail = dash.work_data, dash.mail
    work = work if work and not work.get("error") else {}
    tasks = load_tasks()
    by_main, by_key, by_ref = {}, {}, {}
    for t in tasks:
        for k in t["keys"]:
            by_key.setdefault(k, []).append(t["id"])
        for k in (t["main"], t["tid"]):
            if k:
                by_main.setdefault(k, []).append(t["id"])
        for ref in t["refs"]:
            by_ref.setdefault(ref, []).append(t["id"])

    parent_of = {i["key"]: i["parent"] for i in work.get("jira", []) + (work.get("recent") or {}).get("jira", [])
                 if i.get("parent")}

    def owners(keys, refs=(), parents=True):
        ids = {i for ref in refs for i in by_ref.get(ref, [])}
        if not ids:
            ids = {i for k in keys for i in by_main.get(k, [])}
        if not ids:
            ids = {i for k in keys if len(by_key.get(k, [])) <= RELATED_MAX for i in by_key.get(k, [])}
        if not ids and parents:
            up = [parent_of[k] for k in keys if k in parent_of]
            return owners(up, parents=False) if up else []
        return [tasks[i] for i in sorted(ids)]

    unsorted = {"jira": [], "prs": [], "mail": [], "slack": []}

    def place(kind, item, keys, refs=(), ts=0, mine=False):
        homes = owners(keys, refs)
        for t in homes:
            t[kind].append(item)
            t["latest"] = max(t["latest"], ts)
        if not homes and mine:
            unsorted[kind].append(item)

    now = time.time()
    for i in work.get("jira", []):
        place("jira", i, [i["key"]], ts=when(i.get("updated")), mine=i.get("mine", True))
    for i in (work.get("recent") or {}).get("jira", []):
        if now - when(i.get("closed")) < CLOSED_DAYS * 86400:
            place("jira", {**i, "done": True}, [i["key"]], ts=when(i.get("closed")))

    prs = [(p, "mine") for p in work.get("mine", [])] + [(p, "forward") for p in work.get("forwards", [])]
    prs += [(p, "involved") for p in work.get("involved", [])]
    prs += [(p, "closed") for p in (work.get("recent") or {}).get("prs", [])
            if now - when_utc(p.get("closed")) < CLOSED_DAYS * 86400]
    seen = set()
    for p, kind in prs:
        if p["ref"] in seen:
            continue
        seen.add(p["ref"])
        keys = keys_in(p["title"]) + [w for w in p.get("why", []) if dash.TICKET_ANY_RE.fullmatch(w)]
        place("prs", {**p, "kind": kind}, keys, {p["ref"]},
              ts=when_utc(p["closed"]) if p.get("closed") else when(p.get("updated")), mine=kind != "closed")

    links = load_json(SESSION_LINKS)
    by_title = {t["title"]: t for t in tasks}
    # Session lines in tasks.md come first: they are the durable record of A.
    for t in tasks:
        for sid in t["resume"]:
            links[sid] = {"task": t["title"]}
    session_task = {}
    for r in rows:
        ts = now if r.get("wait") else 0
        link = links.get(r.get("sid_full") or "") or ({"task": r["task_var"]} if r.get("task_var") else None)
        if link:
            home = by_title.get(link.get("task"))
            r["link"] = "attached" if home else "unlinked"
            if home:
                home["sessions"].append(r)
                home["latest"] = max(home["latest"], ts)
                session_task[r["sid_full"]] = home["title"]
            continue
        text = first_prompts(r)
        # A task quoted in the opening prompts (c on a task pastes it), then the
        # first key or PR they name, then the session's folder/branch.
        quoted = [t for t in tasks if len(t["title"]) >= 8 and t["title"] in text]
        home = [max(quoted, key=lambda t: len(t["title"]))] if quoted else None
        home = home or next((owners([k]) for k in keys_in(text) if owners([k])), None) or \
            next((owners([], {ref}) for ref in sorted(refs_in(text)) if owners([], {ref})), None) or \
            owners([r["ticket"]] if r.get("ticket") else keys_in(r.get("cwd", "")))
        if home:
            r["link"] = "auto"
            home[0]["sessions"].append(r)
            home[0]["latest"] = max(home[0]["latest"], ts)
            session_task[r["sid_full"]] = home[0]["title"]
    write_index(tasks, parent_of, session_task)
    try:
        ids = {t["title"]: t["tid"] for t in tasks if t["tid"]}
        dash.store().remember_sessions({sid: ids[title] for sid, title in session_task.items() if title in ids})
    except Exception:  # noqa: BLE001 — effort bookkeeping must not break the view
        pass

    for m in (mail or {}).get("messages", []):
        if now - when(m["date"]) > MAIL_DAYS * 86400:
            continue
        refs = refs_in(m.get("body", "")) if re.match(r"(Re: )?\[[\w.-]+/[\w.-]+\]", m["subject"]) else set()
        keys = keys_in(m["subject"])
        if keys or refs:
            place("mail", m, keys, refs, ts=when(m["date"]), mine=not m.get("read"))

    for s in dash.slack_messages(dash.SLACK_ZOOM_WINDOW):
        keys, refs = keys_in(s["text"]), refs_in(s["text"])
        if keys or refs:
            place("slack", s, keys, refs, ts=s["ts"], mine=not s["dismissed"] and (s["dm"] or s["mention"]))

    # Unsorted mail: not from bots, and not about a ticket or PR already listed there.
    listed = {i["key"] for i in unsorted["jira"]} | {p["ref"] for p in unsorted["prs"]}
    unsorted["mail"] = [m for m in unsorted["mail"] if "[bot]" not in m["from"]
                        and not (set(keys_in(m["subject"])) | refs_in(m.get("body", ""))) & listed]
    for t in tasks + [unsorted]:
        t["mail"] = fold_mail(t["mail"])

    seen = load_seen()
    to_close = {i.get("jira") for i in work.get("to_close", []) if i.get("jira")}
    to_close |= {i.get("id") for i in work.get("to_close", []) if i.get("id")}  # merged-by-the-final-reviewer PR refs too
    for t in tasks:
        workflow(t, to_close, seen)

    data = {"tasks": tasks, "unsorted": unsorted}
    _cache.update(at=time.time(), data=data)
    return data


def load_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def first_prompts(r):
    """The text of a session's first FIRST_PROMPTS prompts (what it was started
    for), read from its transcript; cached until the transcript changes while
    it has fewer than that."""
    sid, cwd = r.get("sid_full"), r.get("cwd_full")
    if not sid or not cwd:
        return ""
    path = os.path.join(dash.PROJECTS, re.sub(r"[^A-Za-z0-9]", "-", cwd), sid + ".jsonl")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _prompts.get(sid)
    if hit and (hit[0] == mtime or hit[2] >= FIRST_PROMPTS):
        return hit[1]
    texts = []
    try:
        with open(path, errors="ignore") as f:
            for line in f:
                if '"type":"user"' not in line and '"type": "user"' not in line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                content = (ev.get("message") or {}).get("content")
                if ev.get("isMeta") or not content:
                    continue
                if isinstance(content, list):
                    if any(isinstance(c, dict) and c.get("type") == "tool_result" for c in content):
                        continue
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                if content.lstrip().startswith(("<command-", "<local-command", "<system-reminder")):
                    continue
                texts.append(content[:4000])
                if len(texts) >= FIRST_PROMPTS:
                    break
    except OSError:
        return ""
    _prompts[sid] = (mtime, "\n".join(texts), len(texts))
    return _prompts[sid][1]


def write_index(tasks, parent_of, session_task):
    index = {"tasks": [{"title": t["title"], "main": t["main"], "id": t["tid"], "keys": t["keys"],
                        "refs": sorted(t["refs"])}
                       for t in tasks],
             "parents": parent_of, "sessions": session_task}
    text = json.dumps(index, sort_keys=True)
    try:
        with open(TASK_INDEX) as f:
            if f.read() == text:
                return
    except OSError:
        pass
    os.makedirs(os.path.dirname(TASK_INDEX), exist_ok=True)
    dash.write_atomic(TASK_INDEX, text)


def link_session(sid, title):
    """a: attach a Claude session to a task (title), or None to keep it unlinked."""
    links = load_json(SESSION_LINKS)
    links[sid] = {"task": title, "at": time.time()}
    os.makedirs(os.path.dirname(SESSION_LINKS), exist_ok=True)
    dash.write_atomic(SESSION_LINKS, json.dumps(links))
    _cache["at"] = 0


def session_task_title(sid, rows=None):
    """The task a session belongs to (for the system view's Sessions section);
    builds the links if the task view hasn't since the dashboard started."""
    if not sid:
        return None
    data = build(rows) if rows is not None else _cache["data"]
    if not data:
        return None
    for t in data["tasks"]:
        if any(r.get("sid_full") == sid for r in t["sessions"]):
            return f"{t['tid']} {t['title']}" if t.get("tid") else t["title"]
    return None


def load_seen():
    """Task title -> when I last looked at it; "_since" is when tracking began,
    the baseline for tasks never looked at (so nothing is "new" on day one)."""
    try:
        with open(SEEN) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    if "_since" not in seen:
        seen["_since"] = time.time()
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        dash.write_atomic(SEEN, json.dumps(seen))
    return seen


def note_selected(task):
    """Called on every draw with the selected task's title: one that stays
    selected for SEEN_AFTER seconds counts as looked at (no longer • new)."""
    now = time.time()
    if task != _selected["task"]:
        _selected.update(task=task, since=now)
        return
    if not task or now - _selected["since"] < SEEN_AFTER:
        return
    seen = load_seen()
    if now - seen.get(task, 0) > 60:
        seen[task] = now
        os.makedirs(os.path.dirname(SEEN), exist_ok=True)
        dash.write_atomic(SEEN, json.dumps(seen))
        _cache["at"] = 0  # recompute the marks


def pr_keys(p):
    return set(keys_in(p["title"])) | {w for w in p.get("why", []) if dash.TICKET_ANY_RE.fullmatch(w)}


def pr_stage(prs):
    """Stage from PR evidence: the furthest open PR (draft DEV, open REVIEW,
    forward FWD); with none open, a merged one means DONE."""
    open_ = [p for p in prs if p["kind"] != "closed"]
    if open_:
        return max(((FWD if p["kind"] == "forward" else "DEV" if p.get("draft") else "REVIEW") for p in open_),
                   key=STAGES.index)
    if any(p.get("merged") for p in prs):
        return "DONE"
    return None


def workflow(t, to_close, seen):
    """Set t's stage, progress, attention (! your move, • new, ◷ waiting),
    reasons and hygiene hints."""
    tickets = {i["key"]: i for i in t["jira"]}
    family = [i for i in t["jira"] if i["key"] == t["main"] or i.get("parent") == t["main"]] or t["jira"]
    prs_of = {}
    for p in t["prs"]:
        if p["kind"] == "involved":
            continue  # others' PRs are not this task's progress
        for k in pr_keys(p):
            prs_of.setdefault(k, []).append(p)

    def stage(i):
        jira = STAGE_OF_STATUS.get(i["status"].lower(), "DEV")
        if jira == "DONE" or i.get("done") or i["key"] in to_close:
            return "DONE"
        evidence = pr_stage(prs_of.get(i["key"], []) or prs_of.get(i.get("parent"), []))
        return max(jira, evidence or "PLAN", key=STAGES.index)

    t["stages"] = {i["key"]: stage(i) for i in t["jira"]}
    pieces = [t["stages"][i["key"]] for i in family]
    open_pieces = [s for s in pieces if s != "DONE"]
    if pieces:
        t["stage"] = min(open_pieces, key=STAGES.index) if open_pieces else "DONE"
        t["progress"] = f"{len(pieces) - len(open_pieces)}/{len(pieces)}" if len(pieces) > 1 else ""
    else:
        t["stage"] = pr_stage([p for p in t["prs"] if p["kind"] != "involved"]) or (
            "PLAN" if t["section"].startswith("Planning") else "DEV" if t["section"] == "In dev" else None)
        t["progress"] = ""
    if t["stage"] == "PLAN" and t["main"] and (dash.worktree_for(t["main"]) or t["sessions"]):
        t["stage"] = "DEV"

    reasons = []
    for p in t["prs"]:
        if p["kind"] in ("mine", "forward") and p.get("ci") in ("FAILURE", "ERROR"):
            reasons.append(f"CI failing on {p['ref']}")
        if p["kind"] == "mine" and p.get("review") == "CHANGES_REQUESTED":
            reasons.append(f"changes requested on {p['ref']}")
        if p["kind"] == "mine" and p.get("last_comment") not in (None, dash.GITHUB_ME) \
                and not p["last_comment"].endswith(("[bot]", "bot")):
            reasons.append(f"{p['last_comment']} commented last on {p['ref']}")
        if p["kind"] == "involved" and "review requested" in p.get("why", []):
            reasons.append(f"your review is requested on {p['ref']}")
    waiting = [r for r in t["sessions"] if r.get("wait")]
    if waiting:
        reasons.append(f"Claude session in tab {waiting[0]['tab']} is waiting on you"
                       + (f" (+{len(waiting) - 1} more)" if len(waiting) > 1 else ""))
    # The final reviewer may "merge" by closing with a comment (the board lists
    # those); a forward closed otherwise was turned down — usually a rebase or a fix.
    reforwarded = {k for p in t["prs"] if p["kind"] == "forward" for k in pr_keys(p)}
    for p in t["prs"]:
        if (p["kind"] == "closed" and FWD_OWNER and p["ref"].startswith(FWD_OWNER + "/") and not p.get("merged")
                and p["ref"] not in to_close and not pr_keys(p) & reforwarded):
            said = f": “{p['last_comment_text'][:120]}”" if p.get("last_comment_text") else ""
            reasons.append(f"{FWD_NAME} closed {p['ref']} without merging{said}")
    for k in sorted(set(tickets) & to_close):
        if not tickets[k].get("done") and STAGE_OF_STATUS.get(tickets[k]["status"].lower()) != "DONE":
            reasons.append(f"merged by {FWD_NAME} — close {k}")
    for s in t["slack"]:
        if not s["dismissed"] and (s["dm"] or s["mention"]):
            reasons.append(f"Slack {'DM' if s['dm'] else 'mention'} from {s['sender'] or s['conversation']}")
            break

    hints = []
    main = tickets.get(t["main"])
    subs = [i for i in t["jira"] if t["main"] and i.get("parent") == t["main"]]
    if main and STAGE_OF_STATUS.get(main["status"].lower()) == "PLAN" and any(
            STAGE_OF_STATUS.get(i["status"].lower()) in ("DEV", "REVIEW") for i in subs):
        hints.append(f"a sub-task is under way but {main['key']} is still {main['status']} — move it to In Development")
    if main and subs and t["stages"][main["key"]] != "DONE" and all(t["stages"][i["key"]] == "DONE" for i in subs):
        hints.append(f"every sub-task is done — close {main['key']}?")
    for i in t["jira"]:
        own = STAGE_OF_STATUS.get(i["status"].lower(), "DEV")
        evidence = pr_stage(prs_of.get(i["key"], []) or prs_of.get(i.get("parent"), []))
        if own != "DONE" and not i.get("done") and (evidence == "DONE" or i["key"] in to_close):
            hints.append(f"its PR is merged — close {i['key']} ({i['status']})")
        if i in subs and own == "REVIEW" and not (prs_of.get(i["key"]) or prs_of.get(i.get("parent"))):
            hints.append(f"{i['key']} is {i['status']} but no PR was found for it or {t['main']}")
    # Closed and merged isn't the end: I still try it in a real use case.
    if t["stage"] == "DONE":
        if any(VERIFIED_RE.match(dash.plain(l)) for l in t["lines"]):
            hints.append("verified — x marks the task done in tasks.md")
        else:
            t["stage"] = "VERIFY"
            reasons.append("everything is closed — try it in a real use case, then y to record how it went")
    t["reasons"], t["hints"] = reasons, hints

    if reasons:
        t["mark"] = "!"
    elif t["latest"] > seen.get(t["title"], seen["_since"]) and any(t[k] for k in ("jira", "prs", "mail", "slack")):
        t["mark"] = "•"
    elif any(p["kind"] in ("mine", "forward") for p in t["prs"]) or t["stage"] in ("REVIEW", FWD):
        t["mark"] = "◷"
    else:
        t["mark"] = " "


def stage_label(t):
    """The fixed-width stage column, coloured by stage."""
    S = dash
    if not t.get("stage"):
        return " " * 10
    color = {"PLAN": S.DIM, "DEV": S.CYAN, "REVIEW": S.YELLOW, FWD: MAGENTA, "VERIFY": f"{S.BOLD}\033[34m",
             "DONE": S.GREEN}[t["stage"]]
    return f"{color}{t['stage']:<6}{S.RESET}{S.DIM}{t.get('progress', ''):>4}{S.RESET}"


def session_label(t):
    """The fixed-width sessions column: ▶ and how many Claude sessions are open for
    the task, yellow ⏳ if one is waiting on me."""
    S = dash
    n = len(t.get("sessions", []))
    if not n:
        return "   "
    if any(r.get("wait") for r in t["sessions"]):
        return f"{S.YELLOW}⏳{n}{S.RESET}" if n < 10 else f"{S.YELLOW}⏳+{S.RESET}"
    return f"{S.GREEN}▶{n:<2}{S.RESET}" if n < 10 else f"{S.GREEN}▶+ {S.RESET}"


def mark_label(t):
    S = dash
    return {"!": f"{S.RED}{S.BOLD}!{S.RESET}", "•": f"{S.CYAN}•{S.RESET}", "◷": f"{S.YELLOW}◷{S.RESET}"}.get(
        t.get("mark"), " ")


def fold_mail(messages):
    """One row per subject (newest first), with how many there were and
    whether any is unread."""
    rows = {}
    for m in messages:
        subject = re.sub(r"^(Re|Fwd?): ", "", m["subject"])
        if subject in rows:
            rows[subject]["count"] += 1
            rows[subject]["read"] = rows[subject]["read"] and m.get("read")
        else:
            rows[subject] = {**m, "count": 1, "read": m.get("read")}
    return list(rows.values())


def badges(t):
    """Short status after a task's title: main ticket, PRs, sessions, mail, Slack."""
    S = dash
    out = []
    main = next((i for i in t["jira"] if i["key"] == t["main"] and not i.get("done")), None)
    if main:
        color = S.CYAN if main["status"] in ("In Progress", "In Development") else S.DIM
        out.append(f"{color}{main['status']}{S.RESET}")
    if any(i.get("sprint") and not i.get("done") for i in t["jira"]):
        out.append(f"{S.MAGENTA}◆ sprint{S.RESET}")
    open_prs = [p for p in t["prs"] if p["kind"] != "closed"]
    if open_prs:
        ci = {p.get("ci") for p in open_prs}
        color = S.RED if ci & {"FAILURE", "ERROR"} else S.YELLOW if ci & {"PENDING", "EXPECTED"} else \
            S.GREEN if ci == {"SUCCESS"} else S.DIM
        out.append(f"{color}PR×{len(open_prs)}{S.RESET}")
    if any("review requested" in p.get("why", []) for p in open_prs):
        out.append(f"{S.YELLOW}review{S.RESET}")
    if any(p["kind"] == "closed" for p in t["prs"]):
        out.append(f"{S.DIM}merged/closed{S.RESET}")
    unread = sum(m["count"] for m in t["mail"] if not m.get("read"))
    if unread:
        out.append(f"{S.BOLD}✉{unread}{S.RESET}")
    slack = [s for s in t["slack"] if not s["dismissed"]]
    if slack:
        out.append(f"{S.DIM}slack {len(slack)}{S.RESET}")
    return " ".join(out)


def task_action(t):
    S = dash
    with S.lock:
        work = S.work_data or {}
    action = S.ticket_action(work, t["main"]) if t["main"] else {}
    action.update(task_id=t["id"], task=t["title"], task_ref=t["tid"], tid=t["tid"], edit=(S.TASKS_MD, t["line"]),
                  detail=(t["section"], "\n".join([S.plain(t["head"])] + [S.plain(l) for l in t["lines"]])))
    if t["main"]:
        action["issue"] = t["main"]
    return action


def list_lines(rows):
    """The left column: Unsorted, then NOW, tasks with linked activity (newest
    first), the rest in tasks.md order, personal ones last."""
    S = dash
    data = build(rows)
    tasks, unsorted = data["tasks"], data["unsorted"]
    lines = [(f"{S.BOLD}Tasks{S.RESET} {S.DIM}({len(tasks)} open · {S.RESET}{S.RED}!{S.RESET}{S.DIM} your move · "
              f"{S.RESET}{S.CYAN}•{S.RESET}{S.DIM} new · {S.RESET}{S.YELLOW}◷{S.RESET}{S.DIM} waiting · "
              f"{S.RESET}{S.YELLOW}⚑{S.RESET}{S.DIM} Jira hint · {S.RESET}{S.MAGENTA}◆{S.RESET}{S.DIM} sprint · "
              f"{S.RESET}{S.GREEN}▶{S.RESET}{S.DIM} open sessions){S.RESET}",
              None)]
    loose = sum(len(v) for v in unsorted.values())
    if loose:
        lines.append(("", None))
        lines.append((f"  {S.YELLOW}▸ Unsorted{S.RESET} {S.DIM}{loose} item{'s' if loose != 1 else ''} not linked to a task"
                      f"{S.RESET}", {"task_id": "unsorted"}))

    def linked(t):
        return any(t[k] for k in ("jira", "prs", "sessions", "mail", "slack"))

    groups = [
        ("NOW", [t for t in tasks if t["section"] == "Now"]),
        ("Active", sorted((t for t in tasks if t["section"] != "Now" and t["section"] not in PERSONAL and linked(t)),
                          key=lambda t: ("!•◷ ".index(t.get("mark", " ")), -t["latest"]))),
        ("Other tasks", [t for t in tasks if t["section"] != "Now" and t["section"] not in PERSONAL and not linked(t)]),
        ("Personal", [t for t in tasks if t["section"] in PERSONAL]),
    ]
    for name, group in groups:
        if not group:
            continue
        lines.append(("", None))
        head = f"{S.PIN_HEAD} NOW {S.RESET}" if name == "NOW" else f"{S.BOLD}{name}{S.RESET}"
        lines.append((f"{head} {S.DIM}({len(group)}){S.RESET}", None))
        for t in group:
            pin = f"{S.YELLOW}▌{S.RESET}" if name == "NOW" else " "
            title = f"{S.BOLD}{t['title']}{S.RESET}" if name in ("NOW", "Active") else t["title"]
            section = f"  {S.DIM}{t['section']}{S.RESET}" if name == "Other tasks" else ""
            hint = f" {S.YELLOW}⚑{S.RESET}" if t.get("hints") else ""
            tid = f"{S.DIM}{(t['tid'] or ''):<7}{S.RESET}"
            lines.append((f" {pin}{mark_label(t)} {stage_label(t)} {session_label(t)} {tid}{title}  {badges(t)}{hint}{section}",
                          task_action(t)))
    done = done_tasks()
    if done:
        lines.append(("", None))
        lines.append((f"{S.BOLD}Done{S.RESET} {S.DIM}(last {DONE_DAYS} days: {len(done)} · x reopens){S.RESET}", None))
        for t in done:
            lines.append((f"  {S.GREEN}✓{S.RESET} {S.DIM}{t['done'][5:]}{S.RESET}         {S.DIM}{t['id']:<7}{t['title']}"
                          f"  {t.get('section', '')}{S.RESET}", done_action(t)))
    return lines


_done = {"at": 0, "tasks": []}


def done_tasks():
    """Tasks marked done in the last DONE_DAYS days, newest first. They come
    from the store (~/tasks): the list above reads only tasks.md's open part."""
    if time.time() - _done["at"] > 5:
        since = time.strftime("%Y-%m-%d", time.localtime(time.time() - DONE_DAYS * 86400))
        found = [t for t in dash.store().load_all() if t.get("status") == "done" and str(t.get("done") or "") >= since]
        _done.update(at=time.time(), tasks=sorted(found, key=lambda t: str(t["done"]), reverse=True))
    return _done["tasks"]


def done_action(t):
    """A done task's row. "task" is its id (find takes ids), so e, f and n work
    on it; done_task makes x offer to reopen it."""
    return {"task_id": "done:" + t["id"], "task": t["id"], "done_task": t["id"],
            "detail": (f"{t['id']} · done {t['done']}", "\n".join(
                [t["title"]] + ([t["summary"]] if t.get("summary") else []) + t.get("notes", [])))}


def jira_row(i, depth=0):
    S = dash
    with S.lock:
        work = S.work_data or {}
    done = i.get("done")
    color = S.DIM if done else S.CYAN if i["status"] in ("In Progress", "In Development") else ""
    action = {**S.ticket_action(work, i["key"]), "issue": i["key"],
              "detail": (f"{i['key']} · {i['status']}", i["summary"])}
    tail = f"{S.DIM}closed {i.get('closed', '')[:10]}{S.RESET}" if done else S.age(i)
    return (f"  {S.ticket_label(i['key'], depth)} {tail} {color}{i['status'][:14]:<14}{S.RESET} {i['summary']}", action)


def files_lines(task):
    """The task's worktrees (code/ links) and its folder's files, newest
    first, for the details column."""
    S = dash
    found = S.store().files(task)
    folder = os.path.dirname(task["path"]).replace(S.HOME, "~", 1)
    lines = []
    links = S.store().code_links(task)
    if links:
        lines += [("", None), (f"{S.BOLD}Code{S.RESET} {S.DIM}{folder}/code · Enter opens a shell tab there{S.RESET}", None)]
        for name, target in links:
            lines.append((f"  {name}  {S.DIM}→ {target.replace(S.HOME, '~', 1)}{S.RESET}", {"dir": target}))
    lines += [("", None),
             (f"{S.BOLD}Files{S.RESET} {S.DIM}{folder} ({len(found)}) · s shell there · Enter file manager{S.RESET}",
              {"open": os.path.dirname(task["path"]), "detail": (folder, "\n".join(
                  os.path.relpath(p, os.path.dirname(task["path"])) for p, _ in found) or "(empty)")})]
    for path, mtime in found[:5]:
        lines.append((f"  {S.DIM}{time.strftime('%m-%d %H:%M', time.localtime(mtime))}{S.RESET} "
                      f"{os.path.relpath(path, os.path.dirname(task['path']))}", {"open": path, "file": path}))
    if len(found) > 5:
        lines += S.sub(f"… {len(found) - 5} more", indent=4)
    return lines


_effort = {}  # task id -> (TASK.md mtime, when computed, text)


def effort_lines(task, width):
    """⏱ time spent and timespan (tasks effort; tasks spent adds time outside Claude),
    recomputed at most once a minute: the activity loop rescans the transcripts that often."""
    hit = _effort.get(task["id"])
    if not hit or hit[0] != task["mtime"] or time.time() - hit[1] > 60:
        try:
            S = dash.store()
            text = S.effort_text(task, S.efforts([task], S.load_activity())[task["id"]])
        except Exception:  # noqa: BLE001
            return []
        hit = _effort[task["id"]] = (task["mtime"], time.time(), text)
    return dash.sub("⏱ " + hit[2], width, indent=2)


def detail_lines(rows, task_id, width):
    """The right column: everything linked to the selected task (or Unsorted)."""
    S = dash
    data = build(rows)
    if str(task_id).startswith("done:"):
        task = S.store().find(task_id[5:])
        if task is None:
            return [(f"{S.DIM}select a task on the left{S.RESET}", None)]
        lines = [(f"{S.GREEN}✓{S.RESET} {S.BOLD}{task['title']}{S.RESET}", done_action(task))]
        facts = [task["id"], task.get("section", ""), f"added {task['added']}" if task.get("added") else "",
                 f"done {task['done']}", "x reopens it"]
        lines += S.sub(" · ".join(f for f in facts if f), width, indent=2)
        lines += effort_lines(task, width)
        if task.get("summary"):
            lines += S.sub(task["summary"], width, indent=4)
        notes = [l[2:] if l.startswith("- ") else l for l in task.get("notes", []) if l.strip()]
        for l in notes[:12]:
            lines += S.sub(S.plain(l), width, indent=4)
        if len(notes) > 12:
            lines += S.sub(f"… {len(notes) - 12} more lines (Space or e)", indent=4)
        return lines + files_lines(task)
    if task_id == "unsorted":
        t = {"title": "Unsorted", "section": "", "lines": [], "main": None, "sessions": [], **data["unsorted"]}
        lines = [(f"{S.BOLD}Unsorted{S.RESET} {S.DIM}— mine to act on, not linked to any task. "
                  f"t makes a task from the selected one.{S.RESET}", None)]
    else:
        t = next((t for t in data["tasks"] if t["id"] == task_id), None)
        if t is None:
            return [(f"{S.DIM}select a task on the left{S.RESET}", None)]
        lines = [(f"{S.BOLD}{t['title']}{S.RESET}", task_action(t))]
        facts = ([t["tid"]] if t["tid"] else []) + [t["section"]] + ([f"added {t['added']}"] if t["added"] else [])
        if t["main"]:
            facts.append(f"main ticket {t['main']}")
            tree = S.worktree_for(t["main"])
            facts.append(f"worktree {tree.replace(S.HOME, '~', 1)}" if tree else "no worktree (w creates one)")
        related = [k for k in t["keys"] if k not in (t["main"], t["tid"])]
        if related:
            facts.append("also mentions " + ", ".join(related[:6]) + (" …" if len(related) > 6 else ""))
        lines += S.sub(" · ".join(facts), width, indent=2)
        task = S.store().find(t["tid"]) if t["tid"] else None
        if task:
            lines += effort_lines(task, width)
        if t.get("stage"):
            lines.append((f"  {mark_label(t)} {stage_label(t)} {S.DIM}{' ▸ '.join(STAGES)}{S.RESET}", None))
        for r in t.get("reasons", []):
            lines.append((f"    {S.RED}! {r}{S.RESET}", None))
        for h in t.get("hints", []):
            lines.append((f"    {S.YELLOW}⚑ {h}{S.RESET}", None))
        notes = [l for l in t["lines"] if not l.startswith("ID: ")]
        for l in notes[:6]:
            lines += S.sub(S.plain(l), width, indent=4)
        if len(notes) > 6:
            lines += S.sub(f"… {len(notes) - 6} more lines (Space on the title, e to edit its TASK.md)", indent=4)
        if task:
            lines += files_lines(task)

    def group(name, items):
        if items:
            lines.append(("", None))
            lines.append((f"{S.BOLD}{name}{S.RESET} {S.DIM}({len(items)}){S.RESET}", None))
        return items

    for r in group("Sessions", t["sessions"]):
        wait = f"{S.YELLOW}⏳ waiting{S.RESET} " if r.get("wait") else ""
        how = {"attached": "attached with A", "auto": "linked from its first prompts"}.get(r.get("link"), "")
        place = f"in {r['where']}" if r.get("where") else f"tab {r['tab']}"
        lines.append((f"  {place}  {wait}{r.get('title') or r['cwd']}  {S.DIM}{r['cwd']} {r.get('branch', '')}"
                      f" · {how}{S.RESET}",
                      {"tab": r["tab_id"], "sid": r.get("sid_full"), "cwd": r.get("cwd_full"),
                       "session": r.get("title") or f"tab {r['tab']}"}))
    tickets = sorted(t["jira"], key=lambda i: (bool(i.get("done")), i["key"] != t["main"]))
    for i, depth in S.nest_tickets(group("Jira", tickets)):
        text, action = jira_row(i, depth)
        stage = t.get("stages", {}).get(i["key"])
        sprint = f"  {S.MAGENTA}◆ {S.sprint_text(i)}{S.RESET}" if i.get("sprint") and not i.get("done") else ""
        lines.append((text + (f"  {S.DIM}[{stage}]{S.RESET}" if stage else "") + sprint, action))
    # Open ones first, then merged/closed ones (newest first).
    for p in group("PRs", sorted(t["prs"], key=lambda p: (p["kind"] == "closed", -when_utc(p.get("closed")) if p.get("closed") else 0))):
        text, action = S.pr_line(p)
        tag = {"forward": f"{S.CYAN}waiting on {FWD_NAME}{S.RESET}", "involved": S.why_tag(p),
               "closed": f"{S.DIM}{'merged' if p.get('merged') else 'closed'} "
                         f"{time.strftime('%m-%d %H:%M', time.localtime(when_utc(p.get('closed'))))}{S.RESET}"}
        lines.append((text + "  " + tag.get(p["kind"], ""), action))
    today = time.strftime("%Y-%m-%d")
    for m in group("Mail", t["mail"]):
        stamp = m["date"][11:16] if m["date"].startswith(today) else m["date"][5:10]
        dot = f"{S.CYAN}●{S.RESET}" if not m.get("read") else " "
        color = S.DIM if m.get("read") else ""
        action = {"open": m["url"], "message_id": m.get("message_id"), "task_seed": S.mail_task_seed(m),
                  "detail": (f"{m['from']} · {m['date'].replace('T', ' ')}",
                             m["subject"] + ("\n\n" + m["body"] if m.get("body") else ""))}
        count = f" {S.DIM}×{m['count']}{S.RESET}" if m.get("count", 1) > 1 else ""
        lines.append((f"  {S.DIM}{stamp:>5}{S.RESET} {dot} {color}{S.fit(m['from'], 20)} {color}{m['subject']}{S.RESET}{count}",
                      action))
    for s in group("Slack", t["slack"]):
        stamp = time.strftime("%a %H:%M", time.localtime(s["ts"]))
        where = ("DM · " if s["dm"] else "") + s["conversation"]
        color = S.DIM if s["dismissed"] else ""
        text = (f"{s['sender']}: " if s["sender"] and not s["dm"] else "") + s["text"]
        lines.append((f"  {S.DIM}{stamp}{S.RESET} {color}{S.fit(where, 22)} {text}{S.RESET}",
                      {"open": "https://app.slack.com/client", "notification": [s["id"]], "close": s["ids"],
                       "detail": (f"Slack · {where} · {stamp}", text)}))
    if not any(t[k] for k in ("jira", "prs", "sessions", "mail", "slack")):
        lines.append(("", None))
        lines.append((f"  {S.DIM}nothing linked yet — no ticket, PR, session, mail or Slack message names this task"
                      f"{S.RESET}", None))
    return lines
