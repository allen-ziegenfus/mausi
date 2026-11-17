# The context file

`~/.cache/mausi/context.json` (mode 0600) holds everything mausi last fetched.
One collector writes it (`src/mausi_context.py`): the dashboard runs one
unless `mausi context --watch` or another dashboard already does (whoever
holds the `flock` on `context.json.lock`). The dashboard, `mausi-tui` and the
triage step read it; nothing else fetches.

```
mausi context              # each source's age, size and error
mausi context --json       # the whole file
mausi context --refresh    # ask the collector to fetch everything now
mausi context --refresh mail
```

## Format (version 1)

```json
{
  "version": 1,
  "written": 1795000000.0,          // epoch seconds
  "collector": 12345,               // pid of the writer
  "sources": {
    "<name>": {
      "at": 1795000000.0,           // when this source was last fetched
      "error": null,                // or a short message; data is then the last good data (or null)
      "stale": true,                // only on data carried over from an earlier collector, until refetched
      "data": ...
    }
  }
}
```

A source missing from `sources` is not configured. Refresh intervals are in
`SOURCES` in `mausi_context.py`.

| source | every | `data` |
|---|---|---|
| `board` | 60 s | claude-merge-board's `/api/dash.json` (PRs: `mine`, `forwards`, `involved`, `recent`, `to_close`; Jira; `tasks`), with PRs closed by a reviewer's "Merged…" comment marked `merged`, `forwarded` or `superseded` |
| `mail` | 2 min | the `[commands] mail` output: `{"unread": [{id, from, subject, date, url, ...}], ...}` |
| `calendar` | 2 min | `{"today": <[commands] calendar output: {events, synced, running}>, "tomorrow": {events}}` |
| `notifications` | when the log changes | `{"items": [{ts, id, app, label, url, summary, body}], "dismissed": {"before": epoch, "ids": ["<ts>\|<label>\|<summary>"]}}`, newest first, the last 3 days. `label` is the web app for Chromium ones (Slack, Mail, Jira, Meet, GitHub…). Slack messages are the items labelled `Slack` |
| `infra` | 15 s | `{"docker": [docker ps rows] \| null, "k3d": [clusters], "kube": {ctx, pods, nodes, hosts: {"<ns>/<service>": host}} \| null}` |
| `sessions` | 15 s | `[{cwd, session}]`, the running Claude Code sessions |

Readers should treat unknown keys as optional: sources and fields are added
without bumping `version`; a change that breaks readers bumps it.
