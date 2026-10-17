#!/usr/bin/env python3
"""agent-board: a tiny self-hosted task board for AI agent workflows. Standard library only.

  board.py serve                          run the server (needs BOARD_TOKEN, or --no-auth)
  board.py task T2 --title ... --status testing --note "..."
  board.py agent <id> --task T2 --status running|done|failed
  board.py archive T2
  board.py brief T2 --file review.md --option 'merge|Approve and merge|ships it' --primary merge [--cwd DIR]
  board.py wait [--cwd DIR]               block until the developer decides on the board, print it, exit 0
  board.py done <id>                      acknowledge a board decision
  board.py hook                           Claude Code hook: JSON on stdin, always exits 0
"""
import argparse
import contextlib
import hmac
import html
import json
import os
import re
import select
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATUSES = ["planned", "analysing", "building", "review", "testing", "done", "blocked"]
AGENT_STATUSES = ["running", "done", "failed"]
AGENT_TOOLS = ("Agent", "Task")  # the subagent tool was called Task before it was renamed Agent
STALE_SECS = 2 * 3600
KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")
WAITING = ["me", "agent", "qc", "po", "none"]  # who the next step waits on; '' = unset
TASK_RE = re.compile(r"^Task\s+(\d+)\b[\s:.-]*")  # the dev's rule: every agent description starts with "Task <N> ..."
MR_RE = re.compile(r"/merge_requests/(\d+)")

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
  project TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',
  mr_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]', created_at INTEGER, updated_at INTEGER,
  last_event_at INTEGER, archived INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0,
  next TEXT NOT NULL DEFAULT '', waiting TEXT NOT NULL DEFAULT '', done_at INTEGER);
CREATE TABLE IF NOT EXISTS agents(
  id TEXT PRIMARY KEY, task_key TEXT, name TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL DEFAULT '', tool_use_id TEXT,
  status TEXT NOT NULL DEFAULT 'running', started_at INTEGER, ended_at INTEGER, last_event_at INTEGER,
  tool_uses INTEGER NOT NULL DEFAULT 0, tokens INTEGER, rev INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, session_id TEXT, cwd TEXT, project TEXT,
  task_key TEXT, agent_id TEXT, kind TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '', data TEXT NOT NULL DEFAULT '{}',
  rev INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS decisions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, task_key TEXT NOT NULL, option_id TEXT NOT NULL DEFAULT '', label TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', created_at INTEGER, status TEXT NOT NULL DEFAULT 'pending',
  delivered_at INTEGER, delivered_to TEXT, done_at INTEGER, rev INTEGER NOT NULL DEFAULT 0);
CREATE INDEX IF NOT EXISTS decisions_status ON decisions(status);
CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS events_rev ON events(rev);
CREATE INDEX IF NOT EXISTS events_task ON events(task_key, id);
CREATE INDEX IF NOT EXISTS tasks_rev ON tasks(rev);
CREATE INDEX IF NOT EXISTS agents_rev ON agents(rev);
CREATE INDEX IF NOT EXISTS agents_tu ON agents(tool_use_id);
"""

# ---------------------------------------------------------------- redaction
_NAMES = r"(?:token|password|passwd|secret|api[_-]?key|access[_-]?key|private[_-]?key|pwd)"
_VAL = r"(\"[^\"]*\"|'[^']*'|[^\s\"']+)"
REDACTIONS = [
    (re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"']+"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\bbearer\s+[\w.~+/=-]{8,}"), "Bearer [REDACTED]"),
    # no leading [\w-]*: it made this pattern quadratic on a long run of word characters (3 MB of "x" froze the server for 8 s); the prefix stays outside the match
    (re.compile(r"(?i)(" + _NAMES + r"[\w-]{0,40}[\"']?\s*[=:]\s*)" + _VAL), r"\1[REDACTED]"),  # bounded: linear even on "passwordpassword..."
    (re.compile(r"(?i)(--[\w-]{0,40}" + _NAMES + r"[\w-]{0,40}\s+)" + _VAL), r"\1[REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|glpat-[\w-]{10,}|xox[baprs]-[\w-]{10,})"), "[REDACTED]"),
    (re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]*"), "[REDACTED]"),
    (re.compile(r"(?i)\b([a-z][a-z0-9+.-]{0,20}://[^\s:/@]+:)[^\s@/]+@"), r"\1[REDACTED]@"),
]


def redact(s):
    s = str(s)
    for rx, rep in REDACTIONS:
        s = rx.sub(rep, s)
    return s


def clip(s, n=200):
    """Redact on the full text first, then cut, so a secret is never half-kept."""
    s = redact(s).replace("\n", " ")
    return s if len(s) <= n else s[: n - 1] + "..."


OPT_ID_RE = re.compile(r"^[a-z0-9-]{1,32}$")


def under(path, base):
    """path equals base or is inside it."""
    base = base.rstrip("/")
    return path == base or path.startswith(base + "/")


def decision_block(d):
    """the text a Claude session sees for one decision (same for `wait` and the hook)."""
    return "\n".join(["BOARD DECISION #%d %s: %s" % (d["id"], d["task_key"], d["label"])] + ([d["text"]] if d["text"] else []) +
                     ["(ack: board.py done %d)" % d["id"]])


def parse_options(opts, primary=None):
    """CLI --option 'id|Label|hint' (hint optional) -> the brief_options list."""
    out = []
    for o in opts or []:
        parts = (o.split("|", 2) + ["", ""])[:3]
        out.append({"id": parts[0].strip(), "label": parts[1].strip(), "hint": parts[2].strip(), "primary": parts[0].strip() == primary})
    if primary and not any(o["primary"] for o in out):
        raise ValueError("--primary %s is not one of the options" % primary)
    return out


BODY_MAX, HOOK_MAX, WAIT_MAX = 1 << 20, 8 << 20, 55  # request body caps (a failed Write can carry a whole file) and the longest long-poll
# The router: (method, path regex, handler Handler.r_<name>, what it does). /architecture is generated from this table.
ROUTES = [
    ("GET", r"/", "page", "the board page, one HTML document"),
    ("GET", r"/architecture", "arch", "this page"),
    ("GET", r"/api/state", "state", "tasks, agents and events changed since ?since=rev; the page polls it every 3 s"),
    ("GET", r"/api/standup", "standup", "plain-text standup (Copy standup)"),
    ("GET", r"/api/task/(?P<key>[^/]+)/events", "events", "one task's timeline events"),
    ("GET", r"/api/decisions/wait", "wait", "long-poll (up to 55 s) for decisions under ?cwd=; claims each exactly once"),
    ("POST", r"/api/hook", "hook", "the raw Claude Code hook payload; empty reply, or the hook JSON when decisions wait"),
    ("POST", r"/api/task", "task", "create or update a task"),
    ("POST", r"/api/task/(?P<key>[^/]+)/brief", "brief", "post a review card (markdown + option buttons)"),
    ("POST", r"/api/task/(?P<key>[^/]+)/decision", "decision", "the developer's answer: option, reply or approve"),
    ("POST", r"/api/decision/(?P<id>\d+)/done", "done", "Claude acknowledges a decision"),
    ("POST", r"/api/agent", "agent", "create or update an agent row"),
    ("POST", r"/api/archive", "archive", "hide a task from the board"),
    ("POST", r"/login", "login", "token form, sets the cookie"),
]
HOOK_EVENTS = {  # the hook events DB.ingest handles; a test keeps this in step with the code
    "SessionStart": "logged", "SessionEnd": "logged", "Stop": "turn finished, logged", "Notification": "logged",
    "UserPromptSubmit": "logged; also hands over waiting decisions for the prompt's folder",
    "SubagentStart": "agent starts, or is resumed (status running)", "SubagentStop": "agent done",
    "PreToolUse": "Agent spawns an agent row; SendMessage re-links a resumed agent; other tools ignored",
    "PostToolUse": "agent finished, or an ordinary tool counted and logged (BOARD_TOOL_EVENTS=0 skips it)",
    "PostToolUseFailure": "failed command: command, last 40 output lines (redacted), exit code, duration",
}
ARCH_DIAGRAM = """<div class=sv><svg viewBox="0 0 976 440" role=img aria-label="Claude Code sessions post hook events to the board server. The orchestrator CLI posts tasks, agents and review cards. The browser page polls state every 3 seconds and posts decisions. The server stores everything in SQLite and returns decisions to the Claude session, by long-poll or in the reply to the next prompt hook.">
<title>agent-board architecture</title>
<defs><marker id=a viewBox="0 0 10 10" refX=9 refY=5 markerWidth=7 markerHeight=7 orient=auto-start-reverse><path d="M0 0L10 5L0 10z"/></marker></defs>
<g class=nd><rect x=14 y=44 width=212 height=84 rx=10 /><text class=t x=26 y=72>Claude Code sessions</text><text class=s x=26 y=98>hooks on every event</text>
<rect x=14 y=214 width=212 height=94 rx=10 /><text class=t x=26 y=242>Orchestrator CLI</text><text class=s x=26 y=268>board.py task/brief/done</text><text class=s x=26 y=288>and board.sh</text>
<rect x=808 y=110 width=156 height=140 rx=10 /><text class=t x=820 y=138>Browser page</text><text class=s x=820 y=164>the board you use</text><text class=s x=820 y=190>Copy standup:</text><text class=s x=820 y=210>/api/standup</text>
<rect x=420 y=345 width=200 height=70 rx=10 /><text class=t x=432 y=373>SQLite</text><text class=s x=432 y=397>board.db</text></g>
<g class="nd hub"><rect x=420 y=110 width=200 height=140 rx=10 /><text class=t x=432 y=138>Board server</text><text class=s x=432 y=164>board.py, Python</text><text class=s x=432 y=184>ThreadingHTTPServer</text><text class=s x=432 y=204>port 8787</text></g>
<g class=ed><path d="M226 100L420 140" /><path d="M226 262L420 220" /><path d="M808 220L620 220" /><path class=rw d="M520 250V345" /></g>
<g class="ed ds"><path d="M808 150L620 150" /><path d="M520 110V26H118V42" /></g>
<g class=lb><text x=234 y=66>curl POST /api/hook</text><text x=234 y=85>(7 ms)</text>
<text x=234 y=294>/api/task, /api/agent</text><text x=234 y=313>/api/archive</text><text x=234 y=332>/api/task/&lt;k&gt;/brief</text><text x=234 y=351>/api/decision/&lt;id&gt;/done</text>
<text x=628 y=119>poll /api/state</text><text x=628 y=138>every 3 s</text><text x=628 y=189>click:</text><text x=628 y=208>/api/task/&lt;k&gt;/decision</text>
<text x=532 y=302>read / write</text>
<text x=128 y=18>decision: board.py wait (long-poll /api/decisions/wait) or the next UserPromptSubmit reply</text></g></svg></div>"""


THEME_JS = "<script>(function(){try{var t=localStorage.getItem('theme');if(t==='dark'||t==='light')document.documentElement.dataset.theme=t}catch(e){}})()</script>"  # the stored System/Light/Dark choice, applied before first paint


def hook_json(decisions):
    """the exact JSON Claude Code reads from a UserPromptSubmit hook (curl prints the reply body verbatim)."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                              "additionalContext": "Board decisions waiting for you:\n\n" + "\n\n".join(map(decision_block, decisions))}})


def arch_page(db):
    """GET /architecture: a hand-written diagram and prose, plus parts generated from ROUTES, the live tables and HOOK_EVENTS."""
    e = html.escape
    base = PAGE[PAGE.index("<style>") + 8:PAGE.index(".ic{")]  # the board's own tokens and base type
    show = lambda rx: re.sub(r"\(\?P<(\w+)>[^)]*\)", r"<\1>", rx)
    routes = "".join("<tr><td>%s</td><td><code>%s</code></td><td>%s</td></tr>" % (m, e(show(rx)), e(doc)) for m, rx, _n, doc in ROUTES)
    with db.lock:
        tabs = [r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = "".join("<tr><td><code>%s</code></td><td>%s</td></tr>" % (t, e(", ".join(c["name"] for c in db.q("PRAGMA table_info(%s)" % t)))) for t in tabs)
    events = "".join("<tr><td><code>%s</code></td><td>%s</td></tr>" % (k, e(v)) for k, v in HOOK_EVENTS.items())
    poll = (re.search(r"setTimeout\(loop,(\d+)\)", PAGE) or [0, 0])[1]
    nums = ["board page %.1f KB of the 80 KB budget" % (len(PAGE.encode()) / 1000), "page polls /api/state every %g s" % (int(poll) / 1000),
            "hook: curl gives up after 2 s (board.py hook: 1 s); the reply is empty unless decisions wait",
            "request bodies: %d MB on /api/hook, %d MB elsewhere" % (HOOK_MAX >> 20, BODY_MAX >> 20), "long-poll: at most %d s per call" % WAIT_MAX,
            "an agent silent for %d h shows as stale; events are pruned after 30 days" % (STALE_SECS // 3600)]
    return ("<!doctype html><html lang=en><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>agent-board architecture</title>" + THEME_JS + "<style>" + base +
            "main{max-width:860px;margin:0 auto;padding:24px 16px 48px}h1{font-size:20px;line-height:26px}h2{margin:28px 0 10px;font-size:13px;font-weight:500;letter-spacing:.4px;color:var(--mu)}p,li{margin:4px 0;color:var(--t2)}"
            "table{width:100%;border-collapse:collapse;font-size:13px}td{border-bottom:1px solid var(--bd);padding:6px 10px;text-align:left;vertical-align:top;overflow-wrap:anywhere}tr:last-child td{border:0}"
            ".w{overflow-x:auto;background:var(--surf);border:1px solid var(--bd);border-radius:12px;padding:4px 8px}code{font:12px var(--mono);background:var(--sunk);padding:0 4px;border-radius:4px;overflow-wrap:anywhere}"
            ".sv{overflow-x:auto;margin:12px 0}.sv svg{display:block;width:100%;min-width:820px;height:auto}svg text{fill:var(--tx);font-size:14.5px}svg .t{font-size:16px;font-weight:600}svg .s{fill:var(--t2)}svg .lb text{fill:var(--t2)}"
            ".nd rect{fill:var(--surf);stroke:var(--bd2)}.hub rect{fill:var(--sunk);stroke:var(--ac);stroke-width:1.5}.ed path{fill:none;stroke:var(--t2);stroke-width:1.5;marker-end:url(#a)}.ds path{stroke-dasharray:6 4}.ed .rw{marker-start:url(#a)}marker path{fill:var(--t2)}</style>"
            "<main><p><a href=/>Back to the board</a></p><h1>How agent-board works</h1>" + ARCH_DIAGRAM +
            "<p>Hooks and the orchestrator write into one small server; the page and Claude read from it. The tables below are generated from the code, so they cannot go stale.</p>"
            "<h2>How a decision travels</h2><ol>"
            "<li>The orchestrator posts a review card: <code>board.py brief KEY --file review.md --option ...</code> (waiting becomes you).</li>"
            "<li>You open it on the board. The page already has it, from its 3 s poll of <code>/api/state</code>.</li>"
            "<li>You click an option or reply: the server writes a pending row in <code>decisions</code> and a timeline note, and hands the task back to the agent.</li>"
            "<li>Exactly one of <code>board.py wait</code> or the next <code>UserPromptSubmit</code> hook in that folder claims it (status delivered) and Claude sees the text.</li>"
            "<li>Claude acts and runs <code>board.py done ID</code>; the page shows Done.</li></ol>"
            "<h2>Parents and sub-tasks</h2><p>A big task is a parent and its pieces are sub-tasks: <code>board.py task T32 --parent T31</code> sets <code>tasks.parent</code> (one level only; the parent must exist; <code>--parent \"\"</code> clears it). "
            "A parent has no status of its own: it is derived from its sub-tasks (all done = done, any blocked = blocked, any review or testing = review, any building or analysing = building, else planned) and needs you when a sub-task does, but only the sub-tasks appear in Needs you.</p>"
            "<h2>Moving parts</h2><ul><li><code>board.py</code>: server, CLI, hook fallback and the page, one file; <code>test_board.py</code>: its tests</li>"
            "<li><code>.token</code>: the access token (BOARD_TOKEN); <code>~/.local/share/agent-board/board.db</code>: the SQLite data</li>"
            "<li><code>~/.claude/settings.json</code>: the hook entries (curl to <code>/api/hook</code>)</li>"
            "<li>your orchestrator's wrapper script (optional): it calls the CLI</li></ul>"
            "<h2>Numbers</h2><ul>" + "".join("<li>%s</li>" % e(n) for n in nums) + "</ul>"
            "<h2>Routes (generated)</h2><div class=w><table>" + routes + "</table></div>"
            "<h2>Tables (generated)</h2><div class=w><table>" + tables + "</table></div>"
            "<h2>Hook events (generated)</h2><div class=w><table>" + events + "</table></div>"
            "<p>When the architecture changes, update the diagram and prose above; a test fails if a route, table or hook event is missing here.</p></main>")


def _int(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def fail_data(ti, p):
    """What the failed-command drawer shows: redacted command, last 40 output lines (<= 8 KB, each redacted), exit code, duration."""
    tr = p.get("tool_response")
    parts = [p.get("error")] + ([tr.get(k) for k in ("stderr", "stdout", "output")] if isinstance(tr, dict) else [tr])
    text = "\n".join(str(x) for x in parts if x)[-65536:]
    out = "\n".join(redact(ln) for ln in text.splitlines()[-40:])[-8192:]
    m = re.match(r"\s*Exit code (\d+)", str(p.get("error") or ""))
    ex = _int(p.get("exit_code")) if p.get("exit_code") is not None else _int(tr.get("exit_code")) if isinstance(tr, dict) else None
    return {"cmd": redact(str(ti.get("command") or ""))[:2048] or None, "out": out or None,
            "exit": ex if ex is not None else int(m.group(1)) if m else None, "dur_ms": _int(p.get("duration_ms"))}


# ---------------------------------------------------------------- database
class DB:
    # ponytail: one connection + one global lock; fine for a single-user board, add a pool if it ever matters
    def __init__(self, path):
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SCHEMA)
        have = {r["name"] for r in self.q("PRAGMA table_info(tasks)")}
        for col, typ in (("next", "TEXT NOT NULL DEFAULT ''"), ("waiting", "TEXT NOT NULL DEFAULT ''"), ("done_at", "INTEGER"),
                         ("brief", "TEXT NOT NULL DEFAULT ''"), ("brief_options", "TEXT NOT NULL DEFAULT '[]'"), ("brief_at", "INTEGER"),
                         ("brief_cwd", "TEXT NOT NULL DEFAULT ''"), ("parent", "TEXT")):
            if col not in have:  # old DB: add the column, backfill done_at from the last update
                self.c.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (col, typ))
                if col == "done_at":
                    self.c.execute("UPDATE tasks SET done_at=updated_at WHERE status='done'")
        self.c.execute("CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent)")  # after the loop: an old DB gets the column first
        self.lock = threading.RLock()
        self.wait_seen = {}  # cwd -> last time a `board.py wait` long-poll for it was alive
        self.rev =max(self.q1("SELECT COALESCE(MAX(rev),0) m FROM " + t)["m"] for t in ("tasks", "agents", "events"))

    def close(self):
        with self.lock:
            self.c.close()

    def q(self, sql, a=()):
        return [dict(r) for r in self.c.execute(sql, a)]

    def q1(self, sql, a=()):
        r = self.q(sql, a)
        return r[0] if r else None

    @contextlib.contextmanager
    def tx(self):
        with self.lock:
            self.c.execute("BEGIN")
            try:
                yield
            except BaseException:
                self.c.execute("ROLLBACK")
                raise
            self.c.execute("COMMIT")

    def bump(self):
        self.rev += 1
        return self.rev

    # -- helpers
    def event(self, kind, summary, task=None, sid="", cwd="", project="", agent=None, data=None, ts=None):
        ts = ts or int(time.time())
        r = self.bump()
        self.c.execute(
            "INSERT INTO events(ts,session_id,cwd,project,task_key,agent_id,kind,summary,data,rev) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (ts, sid, cwd, project, task, agent, kind, summary, json.dumps(data or {}, separators=(",", ":")), r))
        if task:
            self.c.execute("UPDATE tasks SET last_event_at=?, rev=? WHERE key=?", (ts, r, task))

    def link(self, cwd, agent_id=None):
        """task key for an event: the agent's task, else the longest worktree prefix of cwd, else a unique project name in cwd."""
        if agent_id:
            a = self.q1("SELECT task_key FROM agents WHERE id=?", (agent_id,))
            if a and a["task_key"]:
                return a["task_key"]
        if not cwd:
            return None
        best = None
        for t in self.q("SELECT key,worktree FROM tasks WHERE archived=0"):
            w = t["worktree"].rstrip("/")
            if w and (cwd == w or cwd.startswith(w + "/")) and (best is None or len(w) > len(best[0])):
                best = (w, t["key"])
        return best[1] if best else None  # no guessing by project name: it linked every agent to the wrong task

    def desc_task(self, desc, now):
        """task key from an agent description "Task <N> title"; creates a minimal task row if it is new."""
        m = TASK_RE.match(desc or "")
        if not m:
            return None
        key = "T" + m.group(1)
        if not self.q1("SELECT 1 x FROM tasks WHERE key=?", (key,)):
            self.c.execute("INSERT INTO tasks(key,title,status,notes,created_at,updated_at,rev) VALUES(?,?,?,?,?,?,?)",
                           (key, clip(desc[m.end():].strip(), 200) or key, "building", "[]", now, now, self.bump()))
        return key

    def project_of(self, task, cwd):
        t = self.q1("SELECT project FROM tasks WHERE key=?", (task,)) if task else None
        return (t["project"] if t and t["project"] else os.path.basename(cwd.rstrip("/"))) if (t or cwd) else ""

    # -- tasks
    def task_out(self, t):
        t["notes"] = json.loads(t["notes"] or "[]")
        t["brief_options"] = json.loads(t.get("brief_options") or "[]")
        with self.lock:  # the developer's latest answer, so the page can say pending / delivered / done
            t["decision"] = self.q1("SELECT id,option_id,label,text,status,created_at,delivered_at,done_at FROM decisions WHERE task_key=?"
                                    " ORDER BY id DESC LIMIT 1", (t["key"],))
        # A done task never needs you, whatever "waiting" was left at.
        t["end_at"] = (t["done_at"] or t["updated_at"]) if t["status"] == "done" else None  # total = end_at - created_at; null while running
        t["needs_you"] = t["status"] != "done" and (
            t["waiting"] == "me" or (not t["waiting"] and t["status"] in ("review", "testing")))
        with self.lock:  # sub-task derivations: a child knows its parent, a parent rolls its children up
            pt = self.q1("SELECT title FROM tasks WHERE key=?", (t["parent"],)) if t.get("parent") else None
            kids = self.q("SELECT key,status,waiting,created_at,done_at,updated_at FROM tasks WHERE parent=? AND archived=0 ORDER BY created_at,key", (t["key"],))
        t["parent_title"] = pt["title"] if pt else ("" if t.get("parent") else None)
        t["children"], t["progress"] = [k["key"] for k in kids], None
        if kids:
            self.rollup(t, kids)
        return t

    def rollup(self, t, kids):
        """a parent has no status of its own: it is derived from its sub-tasks (all done -> done; any blocked -> blocked; any
        review/testing -> review; any building/analysing -> building; else planned). It needs you when any child does."""
        st = [k["status"] for k in kids]
        by = {s: st.count(s) for s in STATUSES if s in st}
        t["status"] = ("done" if by.get("done") == len(st) else "blocked" if "blocked" in by else "review" if "review" in by or "testing" in by
                       else "building" if "building" in by or "analysing" in by else "planned")
        t["progress"] = {"total": len(st), "by_status": by, "done": by.get("done", 0)}
        t["needs_you"] = any(k["status"] != "done" and (k["waiting"] == "me" or (not k["waiting"] and k["status"] in ("review", "testing"))) for k in kids)
        t["created_at"] = min([k["created_at"] for k in kids if k["created_at"]] or [t["created_at"]])  # started = the earliest child
        t["end_at"] = max(k["done_at"] or k["updated_at"] or 0 for k in kids) if t["status"] == "done" else None  # total runs until the last child is done

    def get_task(self, key):
        t = self.q1("SELECT * FROM tasks WHERE key=?", (key,))
        return self.task_out(t) if t else None

    def task_upsert(self, d):
        key = str(d.get("key") or "")
        if not KEY_RE.match(key):
            raise ValueError("bad task key (use letters, digits, _ . -)")
        f = {}
        for k in ("title", "project", "branch", "worktree", "mr_url", "next"):
            if d.get(k) is not None:
                f[k] = str(d[k])[:500]
        if "next" in f:
            f["next"] = redact(f["next"])
        if d.get("waiting") is not None:
            if d["waiting"] not in WAITING + [""]:
                raise ValueError("waiting must be one of " + ", ".join(WAITING))
            f["waiting"] = d["waiting"]
        if f.get("worktree") and not f["worktree"].startswith("/"):
            raise ValueError("worktree must be an absolute path")
        if f.get("mr_url") and not re.match(r"^https?://", f["mr_url"]):
            raise ValueError("mr_url must be http(s)")
        status = d.get("status")
        status = "building" if status == "running" else status  # alias
        if status is not None and status not in STATUSES:
            raise ValueError("status must be one of " + ", ".join(STATUSES))
        note = d.get("note")
        now = int(time.time())
        with self.tx():
            row = self.get_task(key)
            newp = oldp = None
            if d.get("parent") is not None:  # "" clears it
                newp = str(d["parent"]).strip()
                oldp = row["parent"] if row else None
                if newp:
                    prow = self.q1("SELECT parent FROM tasks WHERE key=? AND archived=0", (newp,))
                    if newp == key:
                        raise ValueError("a task cannot be its own parent")
                    if not prow:
                        raise ValueError("no such parent task " + newp)
                    if prow["parent"]:
                        raise ValueError("one level only: %s is itself a sub-task" % newp)
                    if self.q1("SELECT 1 x FROM tasks WHERE parent=? AND archived=0 LIMIT 1", (key,)):
                        raise ValueError("a task with sub-tasks cannot become a sub-task")
                f["parent"] = newp or None
            r = self.bump()
            if row is None:
                f.setdefault("title", key)
                f.update(status=status or "planned", done_at=now if status == "done" else None, notes="[]", created_at=now, updated_at=now, archived=0, rev=r, key=key)
                self.c.execute("INSERT INTO tasks(%s) VALUES(%s)" % (",".join(f), ",".join("?" * len(f))), list(f.values()))
                self.event("status", "%s created (%s): %s" % (key, f["status"], f["title"]), task=key, ts=now)
            else:
                if status and status != row["status"]:
                    f.update(status=status, done_at=now if status == "done" else None)
                    self.event("status", "%s: %s -> %s" % (key, row["status"], status), task=key, ts=now)
                if f:
                    f.update(updated_at=now, rev=r)
                    self.c.execute("UPDATE tasks SET %s WHERE key=?" % ",".join(k + "=?" for k in f), list(f.values()) + [key])
            for pk in {newp, oldp} - {None, ""}:  # both parents re-send their roll-up
                self.c.execute("UPDATE tasks SET rev=? WHERE key=?", (self.bump(), pk))
            if note:
                self.add_note(key, str(note), now)
        return self.get_task(key)

    def add_note(self, key, text, now):
        text = redact(text)[:2000]
        notes = self.get_task(key)["notes"] + [{"ts": now, "text": text}]
        self.c.execute("UPDATE tasks SET notes=?, updated_at=?, rev=? WHERE key=?", (json.dumps(notes), now, self.bump(), key))
        self.event("note", clip(text), task=key, ts=now)

    def set_brief(self, key, d):
        """a review card: markdown + up to 5 option buttons. Replaces the previous one and hands the task to the developer."""
        brief = redact(str(d.get("brief") or "")).strip()[:20000]
        if not brief:
            raise ValueError("brief required")
        opts = d.get("options") or []
        if not isinstance(opts, list) or len(opts) > 5:
            raise ValueError("options must be a list of at most 5")
        clean = []
        for o in opts:
            if not isinstance(o, dict) or not OPT_ID_RE.match(str(o.get("id") or "")) or not str(o.get("label") or "").strip():
                raise ValueError("each option needs an id ([a-z0-9-], max 32) and a label")
            if any(c["id"] == o["id"] for c in clean):
                raise ValueError("duplicate option id " + o["id"])
            clean.append({"id": o["id"], "label": redact(str(o["label"]).strip())[:80], "hint": redact(str(o.get("hint") or "").strip())[:200],
                          "primary": bool(o.get("primary"))})
        cwd = str(d.get("cwd") or "")[:500]
        if cwd and not cwd.startswith("/"):
            raise ValueError("cwd must be an absolute path")
        now = int(time.time())
        with self.tx():
            # a review card means the work is waiting for review: the column and the next step follow (a done task keeps both)
            self.c.execute("UPDATE tasks SET brief=?, brief_options=?, brief_at=?, brief_cwd=?, waiting='me', updated_at=?, rev=?,"
                           " status=CASE WHEN status='done' THEN status ELSE 'review' END,"
                           " next=CASE WHEN status='done' THEN next ELSE 'Review card ready' END WHERE key=?",
                           (brief, json.dumps(clean), now, cwd, now, self.bump(), key))
            self.event("brief", "review ready: " + clip(brief.lstrip("# ").split("\n")[0], 100), task=key, ts=now, data={"options": [c["id"] for c in clean]})
        return self.get_task(key)

    def decide(self, key, d):
        """the developer's answer to a needs-you task: a timeline note, the ball goes back to the agent, and (when the task has a
        review brief with a cwd) a pending decision row for the Claude session that owns it. Runs nothing."""
        act, text = d.get("action"), redact(str(d.get("text") or "").strip())[:2000]
        if act not in ("approve", "reply", "option"):
            raise ValueError("action must be approve, reply or option")
        if act == "reply" and not text:
            raise ValueError("reply needs text")
        now = int(time.time())
        with self.tx():
            t = self.get_task(key)
            oid = label = ""
            if act == "option":
                o = next((o for o in t["brief_options"] if o["id"] == d.get("option")), None)
                if not o:
                    raise ValueError("unknown option")
                oid, label, note = o["id"], o["label"], "Decision: " + o["label"]
            elif act == "reply":
                label, note = "Reply", "Reply: " + text
            else:
                note = "Decision: approve and merge"
            what = label if act == "option" else "your reply" if act == "reply" else "approve and merge"
            # the ball goes back to the agent, so the column follows the work (a done task keeps its status and next)
            self.c.execute("UPDATE tasks SET notes=?, waiting='agent', updated_at=?, rev=?,"
                           " status=CASE WHEN status='done' THEN status ELSE 'building' END,"
                           " next=CASE WHEN status='done' THEN next ELSE ? END WHERE key=?",
                           (json.dumps(t["notes"] + [{"ts": now, "text": note}]), now, self.bump(), "Acting on: " + what, key))
            if act != "approve" and t["brief_cwd"]:
                self.c.execute("INSERT INTO decisions(task_key,option_id,label,text,cwd,created_at,rev) VALUES(?,?,?,?,?,?,?)",
                               (key, oid, label, text if act == "reply" else "", t["brief_cwd"], now, self.bump()))
            self.event("decision", clip(note), task=key, ts=now, data=dict(action=act, **({"option": oid} if oid else {})))
        t = self.get_task(key)
        t["waiter"] = self.waiter_for(t["brief_cwd"])
        return t

    def waiter_for(self, cwd):
        """is a `board.py wait` for this folder alive (a long-poll runs up to 55 s; 70 s of silence = gone)?"""
        return bool(cwd) and any(time.time() - ts < 70 and under(cwd, w) for w, ts in list(self.wait_seen.items()))

    def claim(self, cwd, to, hook=False):
        """mark pending decisions for this folder delivered, each exactly once (one transaction, status guard in the UPDATE)."""
        now, out = int(time.time()), []
        with self.tx():
            for d in self.q("SELECT * FROM decisions WHERE status='pending' ORDER BY id"):
                if not (under(cwd, d["cwd"]) if hook else under(d["cwd"], cwd)):  # hook: prompt folder is the project or inside it; waiter: decision is inside the waiter's folder
                    continue
                if self.c.execute("UPDATE decisions SET status='delivered', delivered_at=?, delivered_to=?, rev=? WHERE id=? AND status='pending'",
                                  (now, to, self.bump(), d["id"])).rowcount:
                    self.c.execute("UPDATE tasks SET rev=? WHERE key=?", (self.bump(), d["task_key"]))
                    out.append(dict(d, status="delivered", delivered_at=now, delivered_to=to))
        return out

    def finish_decision(self, did):
        with self.tx():
            d = self.q1("SELECT task_key FROM decisions WHERE id=?", (did,))
            if not d:
                return None
            self.c.execute("UPDATE decisions SET status='done', done_at=?, rev=? WHERE id=?", (int(time.time()), self.bump(), did))
            self.c.execute("UPDATE tasks SET rev=? WHERE key=?", (self.bump(), d["task_key"]))
        return self.q1("SELECT * FROM decisions WHERE id=?", (did,))

    def archive(self, key):
        with self.tx():
            if not self.get_task(key):
                raise ValueError("no such task")
            self.c.execute("UPDATE tasks SET archived=1, updated_at=?, rev=? WHERE key=?", (int(time.time()), self.bump(), key))
            self.event("status", "%s archived" % key, task=key)
        return self.get_task(key)

    # -- agents
    def agent_upsert(self, d):
        aid = str(d.get("id") or "")[:100]
        if not aid:
            raise ValueError("agent id required")
        st = d.get("status")
        if st is not None and st not in AGENT_STATUSES:
            raise ValueError("status must be one of " + ", ".join(AGENT_STATUSES))
        task = d.get("task") or d.get("task_key")
        if task and not self.get_task(task):
            raise ValueError("no such task " + str(task))
        now = int(time.time())
        with self.tx():
            self.agent_set(aid, now, task_key=task, name=d.get("name"), model=d.get("model"), status=st, tokens=d.get("tokens"))
            a = self.q1("SELECT * FROM agents WHERE id=?", (aid,))
            self.event("status", "agent %s %s%s" % (aid[:12], a["status"], " (%s)" % TASK_RE.sub("", a["name"]) if a["name"] else ""),
                       task=a["task_key"], agent=aid, ts=now)
        return a

    def agent_set(self, aid, now, **f):
        """create-or-update an agent row; only non-None fields are written."""
        f = {k: v for k, v in f.items() if v is not None}
        for k in ("name", "model", "type"):
            if k in f:
                f[k] = str(f[k])[:200]
        if f.get("status") in ("done", "failed"):
            f["ended_at"] = now
        elif f.get("status") == "running":
            f["ended_at"] = None
        f.update(last_event_at=now, rev=self.bump())
        if self.q1("SELECT 1 x FROM agents WHERE id=?", (aid,)):
            self.c.execute("UPDATE agents SET %s WHERE id=?" % ",".join(k + "=?" for k in f), list(f.values()) + [aid])
        else:
            f.update(id=aid, started_at=now)
            f.setdefault("status", "running")
            self.c.execute("INSERT INTO agents(%s) VALUES(%s)" % (",".join(f), ",".join("?" * len(f))), list(f.values()))

    # -- hook ingest
    def ingest(self, p):
        ev = str(p.get("hook_event_name") or "")
        if not ev:
            raise ValueError("hook_event_name missing")
        ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        tr = p.get("tool_response") if isinstance(p.get("tool_response"), dict) else {}
        tool = str(p.get("tool_name") or "")
        is_agent_tool = tool in AGENT_TOOLS
        if tool and not is_agent_tool:
            if ev == "PreToolUse":
                if tool == "SendMessage":
                    self.resume_relink(ti, int(time.time()))
                return  # ordinary tools are recorded once, on PostToolUse
            if ev == "PostToolUse" and os.environ.get("BOARD_TOOL_EVENTS", "1") == "0":
                return
        sid = str(p.get("session_id") or "")[:100]
        cwd = str(p.get("cwd") or "")[:500]
        aid = p.get("agent_id") if isinstance(p.get("agent_id"), str) else None
        now = int(time.time())
        data = {}
        with self.tx():
            task = self.link(cwd, aid)
            if is_agent_tool:
                task = self.desc_task(ti.get("description"), now) or task
            summary = ev
            if ev == "SessionStart":
                summary = "session start (%s)" % (p.get("source") or "?")
                data = {"source": p.get("source"), "model": p.get("model")}
            elif ev == "SessionEnd":
                summary = "session end (%s)" % (p.get("reason") or "?")
            elif ev == "UserPromptSubmit":
                summary = "prompt: " + clip(p.get("prompt") or "", 120)
            elif ev == "Stop":
                summary = "turn finished"
            elif ev == "Notification":
                summary = "notification: " + clip(p.get("message") or "")
                data = {"type": p.get("notification_type")}
            elif ev == "SubagentStart":
                summary = "subagent start" + (" (%s)" % p["agent_type"] if p.get("agent_type") else "")
                self.subagent_start(aid, str(p.get("agent_type") or ""), sid, task, now)
                task = self.link(cwd, aid) or task  # the adopted "Task N" row wins over the worktree guess
            elif ev == "SubagentStop":
                summary = "subagent stop" + (" (%s)" % p["agent_type"] if p.get("agent_type") else "")
                if aid:
                    self.agent_set(aid, now, status="done", type=p.get("agent_type"), session_id=sid or None, task_key=task)
            elif is_agent_tool:
                summary = self.agent_tool_event(ev, ti, tr, p, sid, task, now)
            elif tool:  # ordinary PostToolUse / PostToolUseFailure
                what = clip(ti.get("command") or ti.get("file_path") or ti.get("pattern") or ti.get("path") or "")
                summary = "%s: %s" % (tool, what) if what else tool
                data = {"tool": tool}
                if ev == "PostToolUseFailure":
                    summary = "%s failed: %s (%s)" % (tool, what, clip(str(p.get("error") or "").split("\n")[0], 100))
                    data.update(fail_data(ti, p))
                elif aid:
                    self.c.execute("UPDATE agents SET tool_uses=tool_uses+1, last_event_at=?, rev=? WHERE id=?", (now, self.bump(), aid))
            if aid and not is_agent_tool and ev not in ("SubagentStart", "SubagentStop"):
                self.c.execute("UPDATE agents SET last_event_at=?, rev=? WHERE id=?", (now, self.bump(), aid))
            self.event(ev, summary, task=task, sid=sid, cwd=cwd, project=self.project_of(task, cwd), agent=aid,
                       data={k: v for k, v in data.items() if v is not None}, ts=now)
        # fallback delivery: a prompt typed in the owning folder picks up decisions no waiter took (only from the project folder or a subfolder/worktree of it, never a parent or sibling)
        return self.claim(cwd, "hook:" + sid, hook=True) if ev == "UserPromptSubmit" and cwd.startswith("/") else []

    def subagent_start(self, aid, atype, sid, task, now):
        if not aid:
            return
        # best guess: adopt the oldest running placeholder row made by PreToolUse(Agent) in this session
        ph = self.q1("SELECT id FROM agents WHERE status='running' AND id LIKE 'tu:%' AND session_id=? AND (type=? OR type='')"
                     " ORDER BY started_at LIMIT 1", (sid, atype))
        if ph and not self.q1("SELECT 1 x FROM agents WHERE id=?", (aid,)):
            self.c.execute("UPDATE agents SET id=?, rev=? WHERE id=?", (aid, self.bump(), ph["id"]))
        row = self.q1("SELECT task_key FROM agents WHERE id=?", (aid,))
        # status=running also revives a finished agent that was resumed with SendMessage (SubagentStop sets it back to done)
        self.agent_set(aid, now, type=atype, session_id=sid, task_key=None if row and row["task_key"] else task, status="running")

    def resume_relink(self, ti, now):
        """SendMessage {to, message}: a message that starts with "Task N" moves the addressed agent (by id or name) to task TN; no prefix, no move."""
        to = str(ti.get("to") or "")
        if not to or not TASK_RE.match(str(ti.get("message") or "")):
            return
        with self.tx():
            a = self.q1("SELECT id FROM agents WHERE id=? OR name=? ORDER BY COALESCE(last_event_at,0) DESC LIMIT 1", (to, to))
            key = self.desc_task(str(ti["message"]), now) if a else None
            if key:
                self.c.execute("UPDATE agents SET task_key=?, rev=? WHERE id=?", (key, self.bump(), a["id"]))

    def agent_tool_event(self, ev, ti, tr, p, sid, task, now):
        desc = clip(ti.get("description") or "", 120)
        kind = ti.get("model") or ti.get("subagent_type")
        what = TASK_RE.sub("", desc) + (" (%s)" % kind if kind else "")  # feed text: no "Task N" prefix, no "(?)"
        tuid = str(p.get("tool_use_id") or "")
        row = self.q1("SELECT id FROM agents WHERE tool_use_id=?", (tuid,)) if tuid else None
        if ev == "PreToolUse":
            if not row:
                aid = "tu:" + (tuid or "%s:%d" % (sid, now))
                self.agent_set(aid, now, name=desc, model=ti.get("model"), type=ti.get("subagent_type"), session_id=sid,
                               task_key=task, status="running")
                if tuid:
                    self.c.execute("UPDATE agents SET tool_use_id=? WHERE id=?", (tuid, aid))
            return "agent start: " + what
        failed = ev == "PostToolUseFailure"
        if ev in ("PostToolUse", "PostToolUseFailure"):
            if not row:  # no Pre seen (hook installed mid-flight): create it already finished
                aid = str(tr.get("agentId") or "tu:" + (tuid or "%s:%d" % (sid, now)))
                self.agent_set(aid, now, name=desc, model=ti.get("model"), type=ti.get("subagent_type"), session_id=sid, task_key=task)
                row = {"id": aid}
            if tr.get("status") == "async_launched":
                return "agent launched in background: " + what
            self.agent_set(row["id"], now, status="failed" if failed else "done", tokens=tr.get("totalTokens"),
                           model=tr.get("resolvedModel") or ti.get("model") or None)
            if tr.get("totalToolUseCount") is not None:
                self.c.execute("UPDATE agents SET tool_uses=? WHERE id=?", (int(tr["totalToolUseCount"]), row["id"]))
            return "agent %s: %s" % ("failed" if failed else "done", TASK_RE.sub("", desc))
        return "agent: " + what

    # -- reads
    def state(self, since):
        with self.lock:
            cur = self.rev
            if since <= 0:
                tasks = self.q("SELECT * FROM tasks WHERE archived=0")
                agents = self.q("SELECT a.* FROM agents a LEFT JOIN tasks t ON t.key=a.task_key WHERE t.archived=0"
                                " OR (a.task_key IS NULL AND a.last_event_at>?)", (int(time.time()) - 2 * 86400,))
                events = self.q("SELECT * FROM events ORDER BY id DESC LIMIT 100")
            else:
                tasks = self.q("SELECT * FROM tasks WHERE rev>?", (since,))
                have, par, more = {t["key"] for t in tasks}, {t["parent"] for t in tasks if t["parent"]}, []
                if have:  # a changed child changes its parent's roll-up; a changed parent changes its children's chip
                    more += self.q("SELECT * FROM tasks WHERE parent IN (%s)" % ",".join("?" * len(have)), list(have))
                if par:
                    more += self.q("SELECT * FROM tasks WHERE key IN (%s)" % ",".join("?" * len(par)), list(par))
                tasks += [t for t in more if t["key"] not in have and not have.add(t["key"])]
                agents = self.q("SELECT * FROM agents WHERE rev>?", (since,))
                events = self.q("SELECT * FROM events WHERE rev>? ORDER BY id DESC LIMIT 200", (since,))
        for a in agents:  # read-time only: running with no event for 2 h is shown as stale
            if a["status"] == "running" and (a["last_event_at"] or a["started_at"] or 0) < time.time() - STALE_SECS:
                a["status"] = "stale"
        # failed commands (ordinary tools only) in the last 24 h per task; "" = unlinked. Sent whole every poll: tiny, and the window slides without any rev bump.
        failed = {r["k"]: r["n"] for r in self.q("SELECT COALESCE(task_key,'') k, COUNT(*) n FROM events WHERE kind='PostToolUseFailure'"
                                                 " AND ts>? AND data LIKE '%\"tool\"%' GROUP BY k", (int(time.time()) - 86400,))}
        return {"cursor": cur, "tasks": [self.task_out(t) for t in tasks], "agents": agents, "events": events, "failed": failed}

    def standup(self, now=None, off=None):
        """plain-text standup; off = seconds east of UTC for the day boundaries (default: server local time)."""
        now = int(now or time.time())
        off = time.localtime(now).tm_gmtoff if off is None else off
        d = (now + off) // 86400
        p = d - 1
        while (p + 3) % 7 >= 5:  # epoch day 0 was a Thursday; skip Sat/Sun
            p -= 1
        span = lambda a, b: "SELECT * FROM tasks WHERE status='done' AND done_at>=%d AND done_at<%d ORDER BY done_at" % (a * 86400 - off, b * 86400 - off)
        ref = lambda t: t["key"] + " " + t["title"] + "".join(" (!%s)" % m.group(1) for m in [MR_RE.search(t["mr_url"])] if m)
        live = {r["task_key"] for r in self.q("SELECT task_key FROM agents WHERE status='running' AND COALESCE(last_event_at,started_at)>=?", (now - STALE_SECS,))}
        today = [ref(t) for t in self.q(span(d, d + 1))]
        for t in self.q("SELECT * FROM tasks WHERE archived=0 AND status!='done'"):
            if t["status"] in ("analysing", "building", "review", "testing") or t["key"] in live:
                today.append(ref(t))
        blocked = [ref(t) for t in self.q("SELECT * FROM tasks WHERE archived=0 AND status='blocked'")]
        line = lambda name, xs: "%s: %s" % (name, "; ".join(xs) or "None")
        return "\n".join([line("Yesterday", [ref(t) for t in self.q(span(p, p + 1))]), line("Today", today), line("Blockers", blocked)])

    def task_events(self, key, limit=200):
        with self.lock:
            return self.q("SELECT * FROM events WHERE task_key=? AND kind NOT IN ('note','decision') ORDER BY id DESC LIMIT ?", (key, limit))

    def prune(self, days=30, cap=200000):
        with self.tx():
            self.c.execute("DELETE FROM events WHERE ts<?", (int(time.time()) - days * 86400,))
            mx = self.q1("SELECT MAX(id) m FROM events")["m"]
            if mx:
                self.c.execute("DELETE FROM events WHERE id<=?", (mx - cap,))


# ---------------------------------------------------------------- http server
def _eq(a, b):
    return hmac.compare_digest(a.encode(), b.encode())


LOGIN = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>agent-board</title>
<link rel=icon href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3.5' fill='%230f1011'/%3E%3Crect x='3' y='3.5' width='2.6' height='9' rx='1' fill='%23fff'/%3E%3Crect x='6.7' y='3.5' width='2.6' height='5.5' rx='1' fill='%235e6ad2'/%3E%3Crect x='10.4' y='3.5' width='2.6' height='7.5' rx='1' fill='%23fff'/%3E%3C/svg%3E">
<script>(function(){try{var t=localStorage.getItem('theme');if(t==='dark'||t==='light')document.documentElement.dataset.theme=t}catch(e){}})()</script>
<style>:root{color-scheme:light;--bg:#fcfcfd;--surf:#ffffff;--bd:#e6e6e8;--bd2:#d6d7da;--tx:#0f1011;--mu:#6b6f76;--ac:#5e6ad2;--ach:#4f58c2;--wh:#ffffff;--er:#b42318;--fo:#5e6ad2;--sh:0 8px 32px rgba(15,16,17,.12)}
[data-theme=dark]{color-scheme:dark;--bg:#010102;--surf:#0f1011;--bd:#23252a;--bd2:#34343a;--tx:#f7f8f8;--mu:#8a8f98;--ac:#5e6ad2;--ach:#4f58c2;--wh:#ffffff;--er:#ff9a94;--fo:#828fff;--sh:none}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#010102;--surf:#0f1011;--bd:#23252a;--bd2:#34343a;--tx:#f7f8f8;--mu:#8a8f98;--ac:#5e6ad2;--ach:#4f58c2;--wh:#ffffff;--er:#ff9a94;--fo:#828fff;--sh:none}}
*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px;background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"SF Pro Display",system-ui,"Segoe UI",Roboto,sans-serif}
form{width:100%;max-width:380px;background:var(--surf);border:1px solid var(--bd);border-radius:16px;padding:28px;display:flex;flex-direction:column;gap:16px;box-shadow:var(--sh)}
.b{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:600;letter-spacing:-.2px}.b b{color:var(--mu);font-weight:inherit}.b .a{fill:var(--ac)}
.f{display:flex;flex-direction:column;gap:6px}label{font-size:13px;font-weight:500}
input,button{font:inherit;height:40px}input{padding:0 12px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);color:inherit;font:13px ui-monospace,"SF Mono",Menlo,Consolas,monospace}input:has(+.err){border-color:var(--er)}
button{border:0;border-radius:8px;background:var(--ac);color:var(--wh);font-weight:500;cursor:pointer}button:hover{background:var(--ach)}:focus-visible{outline:2px solid var(--fo);outline-offset:2px}
.err{color:var(--er);margin:0;font-size:12px;line-height:16px}.hint{color:var(--mu);font-size:12px;line-height:16px;margin:0}</style>
<form method=post action=/login><div class=b><svg width=24 height=24 viewBox="0 0 24 24" aria-hidden=true><rect x=3 y=4 width=4.5 height=16 rx=1.5 fill=currentColor /><rect class=a x=9.75 y=4 width=4.5 height=10 rx=1.5 /><rect x=16.5 y=4 width=4.5 height=13 rx=1.5 fill=currentColor /></svg><span>agent<b>-board</b></span></div>
<div class=f><label for=t>Access token</label>
<input id=t name=token type=password autocomplete=current-password autofocus required>@ERR@
<p class=hint>The token is in BOARD_TOKEN on the server (locally: the .token file next to the script)</p></div>
<button>Sign in</button></form>"""


def _cookie(tok):
    return "board_token=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000" % urllib.parse.quote(tok, safe="")


class Handler(BaseHTTPRequestHandler):
    timeout = 10
    server_version = "agent-board"

    def log_message(self, *a):
        pass

    def authed(self):
        tok = self.server.token
        if tok is None:
            return True
        h = self.headers.get("Authorization", "")
        if h[:7].lower() == "bearer " and _eq(h[7:].strip(), tok):
            return True
        for part in self.headers.get("Cookie", "").split(";"):
            k, _, v = part.strip().partition("=")
            if k == "board_token" and _eq(urllib.parse.unquote(v), tok):
                return True
        return False

    def send(self, code, body, ctype="application/json", extra=()):
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in extra:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(u.query)
        tok = self.server.token
        if u.path == "/" and tok is not None and "token" in qs and _eq(qs["token"][0], tok):
            return self.send(302, b"", "text/plain", [("Location", "/"), ("Set-Cookie", _cookie(tok))])
        if not self.authed():
            if u.path == "/" and "text/html" in self.headers.get("Accept", ""):
                return self.send(200, LOGIN.replace("@ERR@", "").encode(), "text/html")
            return self.send(401, {"error": "unauthorized"} if u.path != "/" else b"unauthorized: open /?token=<BOARD_TOKEN> once",
                             "application/json" if u.path != "/" else "text/plain")
        fn, g = self.find("GET", u.path)
        if fn is None:
            return self.send(404, {"error": "not found"})
        fn(qs, **g)

    def find(self, method, path):
        """the ROUTES table is the only router: (handler, named groups) or (None, None)."""
        for m, rx, name, _doc in ROUTES:
            mt = m == method and re.fullmatch(rx, path)
            if mt:
                return getattr(self, "r_" + name), {k: urllib.parse.unquote(v) for k, v in mt.groupdict().items()}
        return None, None

    # -- GET handlers: (self, query string dict, **path groups); each sends its own reply
    def r_page(self, qs):
        self.send(200, PAGE.encode(), "text/html")

    def r_arch(self, qs):
        self.send(200, arch_page(self.server.db).encode(), "text/html")

    def r_standup(self, qs):
        o = qs.get("off", [""])[0]
        self.send(200, {"text": self.server.db.standup(off=int(o) if o.lstrip("-").isdigit() else None)})

    def r_state(self, qs):
        try:
            since = int(qs.get("since", ["0"])[0])
        except ValueError:
            since = 0
        self.send(200, self.server.db.state(since))

    def r_events(self, qs, key):
        self.send(200, self.server.db.task_events(key))

    def r_wait(self, qs):  # long-poll; this is a thread per request, so other requests are not blocked
        db, cwd = self.server.db, qs.get("cwd", [""])[0]
        if not cwd.startswith("/"):
            return self.send(400, {"error": "cwd must be an absolute path"})
        try:
            end = time.time() + max(0, min(WAIT_MAX, int(qs.get("timeout", ["25"])[0])))
        except ValueError:
            end = time.time() + 25
        while True:
            db.wait_seen[cwd] = time.time()
            if select.select([self.connection], [], [], 0)[0]:
                return  # the waiter hung up: claiming now would lose the decision
            got = db.claim(cwd, "waiter")
            if got or time.time() >= end:
                break
            time.sleep(0.5)
        self.send(200, got)  # ponytail: a hang-up between claim and send still loses it; the board shows "Claude is on it", `done` never comes

    def login(self):
        fails = self.server.fails  # ponytail: in-memory, per remote address, resets on restart
        ip, now = self.client_address[0], time.time()
        fails[ip] = [t for t in fails.get(ip, []) if now - t < 60]
        if len(fails[ip]) >= 5:
            return self.send(429, b"too many attempts, wait a minute", "text/plain", [("Retry-After", "60")])
        n = min(int(self.headers.get("Content-Length") or 0), 4096)
        tok = urllib.parse.parse_qs(self.rfile.read(n).decode("utf-8", "replace")).get("token", [""])[0]
        if self.server.token is not None and _eq(tok, self.server.token):
            return self.send(303, b"", "text/plain", [("Location", "/"), ("Set-Cookie", _cookie(self.server.token))])
        fails[ip].append(now)
        self.send(401, LOGIN.replace("@ERR@", "<p class=err>Wrong token</p>").encode(), "text/html")

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        fn, g = self.find("POST", path)
        if fn == self.r_login if fn else False:
            return self.login()
        if not self.authed():
            return self.send(401, {"error": "unauthorized"})
        if fn is None:
            return self.send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            cap = HOOK_MAX if fn == self.r_hook else BODY_MAX
            if n > cap:
                left = min(n, 2 * HOOK_MAX)  # drain what the client is sending so it can read the 413 instead of a reset
                while left > 0:
                    chunk = self.rfile.read(min(left, 1 << 16))
                    if not chunk:
                        break
                    left -= len(chunk)
                return self.send(413, {"error": "too large"})
            d = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(d, dict):
                raise ValueError("JSON object expected")
            out = fn(d, **g)
            if out is not None:
                self.send(200, out)
        except (ValueError, KeyError) as e:  # json.JSONDecodeError is a ValueError
            self.send(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self.send(500, {"error": "%s: %s" % (type(e).__name__, e)})

    # -- POST handlers: (self, JSON body dict, **path groups); return the JSON reply, or None after sending it themselves
    def r_hook(self, d):
        """the raw Claude Code hook payload (curl posts it as is). Reply: empty body, unless decisions are waiting for this prompt."""
        got = self.server.db.ingest(reduce_hook(d))
        self.send(200, hook_json(got).encode() if got else b"")

    def r_task(self, d):
        return self.server.db.task_upsert(d)

    def r_agent(self, d):
        return self.server.db.agent_upsert(d)

    def r_archive(self, d):
        return self.server.db.archive(str(d.get("key") or ""))

    def r_decision(self, d, key):
        if not self.server.db.get_task(key):
            return self.send(404, {"error": "no such task"})
        return self.server.db.decide(key, d)

    def r_brief(self, d, key):
        if not self.server.db.get_task(key):
            return self.send(404, {"error": "no such task"})
        return self.server.db.set_brief(key, d)

    def r_done(self, d, id):
        out = self.server.db.finish_decision(int(id))
        return out if out else self.send(404, {"error": "no such decision"})

    def r_login(self, d=None):
        return self.login()


class Server(ThreadingHTTPServer):
    def server_close(self):  # closing the server closes its database
        super().server_close()
        if getattr(self, "db", None):
            self.db.close()


def make_server(db_path, host="127.0.0.1", port=8787, token=None):
    srv = Server((host, port), Handler)
    srv.db = DB(db_path)
    srv.token = token
    srv.fails = {}
    return srv


def cmd_serve(a):
    token = os.environ.get("BOARD_TOKEN") or None
    if token is None and not a.no_auth:
        sys.exit("BOARD_TOKEN is required (or pass --no-auth for local dev)")
    srv = make_server(a.db, a.host, a.port, None if a.no_auth else token)
    srv.db.prune()
    stop = threading.Event()

    def hourly():
        while not stop.wait(3600):
            try:
                srv.db.prune()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=hourly, daemon=True).start()
    print("agent-board on http://%s:%d (db %s, auth %s)" % (a.host, srv.server_address[1], a.db, "off" if a.no_auth else "token"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- client side (CLI + hook)
def call(path, payload, timeout=10):
    url = os.environ.get("BOARD_URL", "http://127.0.0.1:8787").rstrip("/") + path
    h = {"Content-Type": "application/json"}
    if os.environ.get("BOARD_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["BOARD_TOKEN"]
    req = urllib.request.Request(url, json.dumps(payload).encode(), h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read()).get("error")
        except Exception:  # noqa: BLE001
            msg = None
        sys.exit("error %d: %s" % (e.code, msg or e.reason))
    except OSError as e:
        sys.exit("cannot reach %s: %s" % (url, e))


def reduce_hook(p):
    """Shrink a hook payload client-side: no tool output, no transcript, only the fields the server uses."""
    keep = ("session_id", "cwd", "hook_event_name", "tool_name", "tool_use_id", "agent_id", "agent_type", "source", "model",
            "reason", "message", "notification_type", "duration_ms", "exit_code")
    out = {k: p[k] for k in keep if k in p}
    if "prompt" in p:
        out["prompt"] = str(p["prompt"])[:600]  # server redacts, then keeps 120
    if "error" in p:
        e = str(p["error"])  # keep the first line (summary, "Exit code N") plus the tail; the server keeps the last 40 lines (redacted, <= 8 KB)
        out["error"] = e if len(e) <= 20000 else e.split("\n", 1)[0][:300] + "\n" + e[-20000:]
    ti = p.get("tool_input")
    if isinstance(ti, dict):
        out["tool_input"] = {k: (str(v)[:2000] if k in ("command", "message", "to") else v) for k, v in ti.items()
                             if k in ("command", "file_path", "description", "subagent_type", "model", "pattern", "path", "to", "message")}
    tr = p.get("tool_response")
    if isinstance(tr, dict) and p.get("tool_name") in AGENT_TOOLS:
        out["tool_response"] = {k: tr[k] for k in ("status", "agentId", "totalTokens", "totalToolUseCount", "resolvedModel") if k in tr}
    elif isinstance(tr, dict) and p.get("hook_event_name") == "PostToolUseFailure":  # only a failure ships its output
        out["tool_response"] = {k: str(tr[k])[-20000:] for k in ("stderr", "stdout", "output", "exit_code") if k in tr}
    return out


def cmd_hook():
    """Never raises, never blocks Claude for more than ~1 s. Prints (valid hook JSON) only when the server hands back board decisions
    for this folder on a UserPromptSubmit; ponytail: if the print fails after the server claimed them, they show as delivered but are lost."""
    try:
        p = reduce_hook(json.loads(sys.stdin.read(4_000_000)))  # the server skips what it ignores (ordinary PreToolUse, BOARD_TOOL_EVENTS=0)
        url = os.environ.get("BOARD_URL", "http://127.0.0.1:8787").rstrip("/") + "/api/hook"
        h = {"Content-Type": "application/json"}
        if os.environ.get("BOARD_TOKEN"):
            h["Authorization"] = "Bearer " + os.environ["BOARD_TOKEN"]
        resp = urllib.request.urlopen(urllib.request.Request(url, json.dumps(p).encode(), h), timeout=1).read()
        if resp:  # the server's reply is empty, or exactly the hook JSON
            sys.stdout.write(resp.decode("utf-8", "replace"))
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001
        pass


def fetch(path, timeout):
    h = {"Authorization": "Bearer " + os.environ["BOARD_TOKEN"]} if os.environ.get("BOARD_TOKEN") else {}
    url = os.environ.get("BOARD_URL", "http://127.0.0.1:8787").rstrip("/") + path
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        return json.loads(r.read())


def cmd_wait(cwd):
    """Long-poll for this folder's decisions; print them and exit 0 on the first answer. Errors only slow it down (backoff up to 30 s)."""
    delay = 1
    while True:
        try:
            got = fetch("/api/decisions/wait?" + urllib.parse.urlencode({"cwd": cwd, "timeout": 50}), 65)
            delay = 1
        except BaseException as e:  # noqa: BLE001
            if isinstance(e, KeyboardInterrupt):
                raise
            time.sleep(delay)
            delay = min(30, delay * 2)
            continue
        if got:
            print("\n\n".join(decision_block(d) for d in got), flush=True)
            return
        time.sleep(0.2)


def main():
    if sys.argv[1:2] == ["hook"]:
        cmd_hook()
        sys.exit(0)
    ap = argparse.ArgumentParser(description="agent-board: tiny task board for AI agent workflows",
                                 epilog="Env: BOARD_TOKEN, BOARD_URL (client, default http://127.0.0.1:8787), BOARD_DB, BOARD_TOOL_EVENTS=0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--db", default=os.environ.get("BOARD_DB", "./board.db"))
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-auth", action="store_true", help="no token (local dev only)")
    t = sub.add_parser("task", help="create or update a task")
    t.add_argument("key")
    t.add_argument("--title")
    t.add_argument("--status", choices=STATUSES + ["running"], help="running = building")
    for k in ("project", "branch", "worktree", "mr", "note", "next"):
        t.add_argument("--" + k)
    t.add_argument("--parent", help='make this a sub-task of a big task (one level); --parent "" clears it')
    t.add_argument("--waiting", choices=WAITING, help="who the next step waits on")
    g = sub.add_parser("agent", help="create or update an agent row")
    g.add_argument("id")
    g.add_argument("--task")
    g.add_argument("--name")
    g.add_argument("--model")
    g.add_argument("--status", choices=AGENT_STATUSES)
    r = sub.add_parser("archive", help="archive a task")
    r.add_argument("key")
    sub.add_parser("hook", help="Claude Code hook: JSON on stdin, always exits 0")
    b = sub.add_parser("brief", help="post a review card (markdown + option buttons) to a task")
    b.add_argument("key")
    b.add_argument("--file", required=True, help="markdown file, or - for stdin")
    b.add_argument("--option", action="append", default=[], metavar="'id|Label|hint'", help="repeat up to 5 times")
    b.add_argument("--primary", help="id of the option drawn as the main button")
    b.add_argument("--cwd", default=os.environ.get("PWD") or os.getcwd(), help="folder of the session that owns the task (default $PWD)")
    w = sub.add_parser("wait", help="block until the developer decides on the board, print the decision(s), exit 0")
    w.add_argument("--cwd", default=os.environ.get("PWD") or os.getcwd())
    dn = sub.add_parser("done", help="acknowledge a board decision")
    dn.add_argument("id", type=int)
    a = ap.parse_args()
    if a.cmd == "serve":
        return cmd_serve(a)
    if a.cmd == "wait":
        return cmd_wait(a.cwd)
    if a.cmd == "brief":
        try:
            opts = parse_options(a.option, a.primary)
        except ValueError as e:
            sys.exit(str(e))
        md = sys.stdin.read() if a.file == "-" else open(a.file, encoding="utf-8").read()
        print(json.dumps(call("/api/task/%s/brief" % urllib.parse.quote(a.key, safe=""), {"brief": md, "options": opts, "cwd": a.cwd}), indent=2))
        return
    if a.cmd == "done":
        print(json.dumps(call("/api/decision/%d/done" % a.id, {}), indent=2))
        return
    if a.cmd == "task":
        out = call("/api/task", {"key": a.key, "title": a.title, "status": a.status, "project": a.project, "branch": a.branch,
                                 "worktree": a.worktree, "mr_url": a.mr, "note": a.note,
                                 "next": a.next, "waiting": a.waiting, "parent": a.parent})
    elif a.cmd == "agent":
        out = call("/api/agent", {"id": a.id, "task": a.task, "name": a.name, "model": a.model, "status": a.status})
    else:
        out = call("/api/archive", {"key": a.key})
    print(json.dumps(out, indent=2))


# ---------------------------------------------------------------- the page (light theme, no deps)
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>agent-board</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'%3E%3Crect width='16' height='16' rx='3.5' fill='%230f1011'/%3E%3Crect x='3' y='3.5' width='2.6' height='9' rx='1' fill='%23fff'/%3E%3Crect x='6.7' y='3.5' width='2.6' height='5.5' rx='1' fill='%235e6ad2'/%3E%3Crect x='10.4' y='3.5' width='2.6' height='7.5' rx='1' fill='%23fff'/%3E%3C/svg%3E">
<script>(function(){try{var t=localStorage.getItem('theme');if(t==='dark'||t==='light')document.documentElement.dataset.theme=t}catch(e){}})()</script>
<style>
:root{color-scheme:light;--bg:#fcfcfd;--surf:#ffffff;--sunk:#f5f6f6;--s3:#eeeff1;--bd:#e6e6e8;--bd2:#d6d7da;--tx:#0f1011;--t2:#3c4149;--mu:#6b6f76;
--ac:#5e6ad2;--ach:#4f58c2;--acl:#5e6ad2;--wh:#ffffff;--dng:#b42318;--er:#b42318;--ov:rgba(15,16,17,.35);--fo:#5e6ad2;--sh:0 8px 32px rgba(15,16,17,.12);
--i-pl:#8a8f98;--i-an:#7c5cd6;--i-bu:#a87400;--i-rv:#1f8a52;--i-te:#0e8aa0;--i-bk:#d13d3d;--i-dn:#5e6ad2;--i-ok:#27a644}
[data-theme=dark]{color-scheme:dark;--bg:#010102;--surf:#0f1011;--sunk:#141516;--s3:#18191a;--bd:#23252a;--bd2:#34343a;--tx:#f7f8f8;--t2:#d0d6e0;--mu:#8a8f98;
--ac:#5e6ad2;--ach:#4f58c2;--acl:#828fff;--wh:#ffffff;--dng:#b42318;--er:#ff9a94;--ov:rgba(0,0,0,.6);--fo:#828fff;--sh:none;
--i-pl:#8a8f98;--i-an:#a78bfa;--i-bu:#f2c94c;--i-rv:#4cb782;--i-te:#26b5ce;--i-bk:#eb5757;--i-dn:#5e6ad2;--i-ok:#27a644}
@media(prefers-color-scheme:dark){:root:not([data-theme=light]){color-scheme:dark;--bg:#010102;--surf:#0f1011;--sunk:#141516;--s3:#18191a;--bd:#23252a;--bd2:#34343a;--tx:#f7f8f8;--t2:#d0d6e0;--mu:#8a8f98;
--ac:#5e6ad2;--ach:#4f58c2;--acl:#828fff;--wh:#ffffff;--dng:#b42318;--er:#ff9a94;--ov:rgba(0,0,0,.6);--fo:#828fff;--sh:none;
--i-pl:#8a8f98;--i-an:#a78bfa;--i-bu:#f2c94c;--i-rv:#4cb782;--i-te:#26b5ce;--i-bk:#eb5757;--i-dn:#5e6ad2;--i-ok:#27a644}}
html{--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;--pg:24px;--sw:360px}
.s-planned{--sv:var(--i-pl)}.s-analysing{--sv:var(--i-an)}.s-building{--sv:var(--i-bu)}.s-review{--sv:var(--i-rv)}.s-testing{--sv:var(--i-te)}.s-blocked{--sv:var(--i-bk)}.s-done{--sv:var(--i-dn)}
*{box-sizing:border-box}
html,body{margin:0;background:var(--bg);color:var(--tx);font:14px/1.5 -apple-system,"SF Pro Display",system-ui,"Segoe UI",Roboto,sans-serif;-webkit-font-smoothing:antialiased;overflow-x:hidden}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer}
:focus-visible{outline:2px solid var(--fo);outline-offset:2px}
a{color:var(--acl);text-decoration:none}a:hover{text-decoration:underline}
h2,h3{margin:0;font-weight:600;letter-spacing:-.2px}
[hidden]{display:none!important}
.sp{flex:1}.mu{color:var(--mu)}.sm{font-size:12px;color:var(--mu);font-weight:400}
.k{font:500 12px var(--mono);color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
kbd{font:11px/16px var(--mono);padding:0 6px;border:1px solid var(--bd2);border-radius:4px;color:var(--mu)}
.ic{fill:none;stroke:currentColor;stroke-width:2.25;stroke-linecap:round;stroke-linejoin:round;flex:none}
.g{display:inline-block;flex:none;width:8px;height:8px;background:var(--c,var(--sv))}
.g-ring{background:transparent;border:1.5px solid var(--c,var(--sv));border-radius:50%}
.g-half{background:linear-gradient(90deg,var(--c,var(--sv)) 50%,transparent 50%);border:1.5px solid var(--c,var(--sv));border-radius:50%}
.g-dot{border-radius:50%}.g-dot.p{animation:pulse 1.6s ease-in-out infinite}
.g-diamond{width:7px;height:7px;border-radius:1px;transform:rotate(45deg);margin:0 1px}
.g-tri{width:10px;height:9px;clip-path:polygon(50% 0,100% 100%,0 100%)}
.g-square{border-radius:1px}
.g-check{width:10px;height:10px;clip-path:polygon(12% 48%,0 62%,40% 100%,100% 24%,86% 10%,40% 72%)}
.g-x{width:9px;height:9px;clip-path:polygon(20% 0,50% 30%,80% 0,100% 20%,70% 50%,100% 80%,80% 100%,50% 70%,20% 100%,0 80%,30% 50%,0 20%)}
.c-ok{--c:var(--i-ok)}.c-bad{--c:var(--i-bk)}.c-warn{--c:var(--i-bu)}.c-info{--c:var(--i-an)}.c-ac{--c:var(--acl)}.c-w{--c:var(--t2)}
@keyframes pulse{0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.75)}}
@keyframes sheet{from{transform:translateY(32px);opacity:0}to{transform:none;opacity:1}}
@keyframes fade{from{opacity:0}to{opacity:1}}
@keyframes fresh{from{background:var(--sunk)}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
.bt{height:36px;padding:0 14px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);color:var(--tx);font-size:14px;font-weight:500;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.bt:hover{background:var(--sunk)}.bt.p{background:var(--ac);border-color:var(--ac);color:var(--wh)}.bt.p:hover{background:var(--ach)}
.bt:disabled{background:var(--sunk);border-color:var(--bd);color:var(--mu);cursor:default}.bt.d{background:var(--dng);border-color:var(--dng);color:var(--wh)}
.bt.ab:hover{border-color:var(--dng);color:var(--er)}.bt.g0{border-color:transparent;background:none;font-weight:400;color:var(--t2)}
a.bt,a.bt:hover{text-decoration:none;color:var(--tx)}a.bt.p,a.bt.p:hover{color:var(--wh)}
::placeholder{color:var(--mu);opacity:1}
.lk{border:0;background:none;color:var(--acl);font-size:12px;padding:0 8px;height:28px;border-radius:6px}.lk:hover{background:var(--sunk)}
.ib{width:32px;height:32px;flex:none;padding:0;display:grid;place-items:center;border:1px solid var(--bd2);border-radius:8px;background:transparent;color:var(--t2)}.ib:hover{background:var(--sunk);color:var(--tx)}
.sg{display:flex;border:1px solid var(--bd2);border-radius:8px;overflow:hidden}
.sg button{height:28px;padding:0 12px;border:0;background:var(--surf);color:var(--t2);font-size:12px;white-space:nowrap}
.sg [aria-pressed=true]{background:var(--sunk);color:var(--tx);font-weight:500}
header{position:sticky;top:0;z-index:20;background:var(--bg);border-bottom:1px solid var(--bd)}
.hr{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:10px var(--pg);min-height:56px}
.brand{display:flex;align-items:center;gap:8px;font-size:16px;font-weight:600;letter-spacing:-.2px;white-space:nowrap}.brand b{color:var(--mu);font-weight:inherit}.brand .a{fill:var(--ac)}
#attn{display:flex;gap:6px;overflow-x:auto;min-width:0}
.bg{--bt:var(--sunk);--bi:var(--t2);--bb:var(--bt);display:inline-flex;align-items:center;justify-content:center;gap:6px;height:22px;padding:0 8px;border:1px solid var(--bb);border-radius:4px;background:var(--bt);color:var(--bi);font-size:12px;line-height:1;font-weight:500;white-space:nowrap;font-variant-numeric:tabular-nums;max-width:100%;text-decoration:none}
.bg.warn{--bb:var(--i-bu)}.bg.hot{--bt:var(--surf);--bi:var(--acl);--bb:var(--ac)}.bg.bad{--bb:var(--i-bk)}.bg.info{--bb:var(--i-an)}.bg.ok{--bb:var(--i-ok)}.bg.st{--bb:var(--bd)}
.bg.cnt{min-width:22px;padding:0 6px;border-radius:999px}.bg.kc{font-family:var(--mono);border-radius:6px}
button.bg,a.bg{cursor:pointer}a.bg:hover,button.bg:not(:disabled):hover{--bt:var(--s3);text-decoration:none}
.bg:disabled{--bt:var(--surf);--bi:var(--mu);--bb:var(--bd);cursor:default}
.bg[aria-pressed=true]{--bt:var(--ac);--bi:var(--wh);--bb:var(--ac)}
.bg[aria-pressed=true] .g.g{--c:var(--wh)}
#attn .bg{flex:none;--bt:var(--surf);--bi:var(--tx);--bb:var(--bd2)}#attn .bg:disabled{--bi:var(--mu);--bb:var(--bd)}#attn .bg[aria-pressed=true]{--bt:var(--ac);--bi:var(--wh);--bb:var(--ac)}
.lv{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--mu);font-variant-numeric:tabular-nums;white-space:nowrap}
.srch{position:relative}
.srch label{display:flex;align-items:center;gap:8px;height:36px;padding:0 12px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);width:260px;color:var(--mu)}
.srch label:focus-within{outline:2px solid var(--fo);outline-offset:2px}
.srch input{flex:1;min-width:0;border:0;outline:0;background:transparent;font-size:13px;color:var(--tx)}
#res{position:absolute;top:42px;right:0;width:340px;max-width:calc(100vw - 32px);z-index:30;background:var(--surf);border:1px solid var(--bd2);border-radius:12px;padding:6px;max-height:360px;overflow:auto}
.ro{display:flex;align-items:center;gap:8px;padding:8px 10px;border-radius:8px;cursor:pointer;font-size:13px}.ro[aria-selected=true]{background:var(--sunk)}
.ro .k{width:34px;flex:none}.ro .rt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ro .mu{font-size:12px;flex:none}
.ne{padding:10px;font-size:13px;color:var(--mu)}
.hb{display:flex;align-items:center;gap:6px}
.hb .bt[aria-pressed=true]{background:var(--sunk);border-color:var(--ac)}
#hp{padding:0;justify-content:center;font-weight:600}#hp,.hb .ib{width:36px;height:36px}#sbtn{display:none}
#bn{display:flex;flex-wrap:wrap;align-items:center;gap:8px 12px;padding:8px var(--pg);font-size:13px;background:var(--sunk);border-top:1px solid var(--bd);border-left:2px solid var(--i-bu)}
#bn.bad{border-left-color:var(--i-bk)}
#bn span{flex:1;min-width:200px}#bn button{height:28px;padding:0 12px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);color:var(--tx);font-size:12px;font-weight:500}
.page{position:relative;display:grid;grid-template-columns:minmax(0,1fr) var(--sw);gap:16px;padding:20px var(--pg);align-items:start;max-width:1680px;margin:0 auto;width:100%}
#sbt{position:absolute;z-index:5;top:20px;right:calc(var(--pg) + var(--sw) - 8px);padding:0;background:var(--bg)}
body.sbc #sbt svg{transform:rotate(180deg)}
.rtl,.ptb{display:flex;gap:4px;border-bottom:1px solid var(--bd)}.rtl{margin-left:32px}.ptb{padding:0 24px}
.rtl button,.ptb button{height:40px;padding:0 10px;border:0;border-bottom:2px solid transparent;background:none;color:var(--mu);font-size:13px;font-weight:500;letter-spacing:.4px;display:inline-flex;align-items:center;gap:6px}
.rtl [aria-selected=true],.ptb [aria-selected=true]{color:var(--tx);border-bottom-color:var(--ac)}
@media(min-width:1100px){.rail[data-rt=ov] #rg-ac,.rail[data-rt=ac] #rg-ov{display:none}
body.sbc aside.rail{display:none}body.sbc .page{grid-template-columns:minmax(0,1fr);padding-right:calc(var(--pg) + 32px)}body.sbc #sbt{right:var(--pg)}}
main,aside.rail,.grp{display:flex;flex-direction:column;gap:16px;min-width:0}
.sc{background:var(--surf);border:1px solid var(--bd);border-radius:12px;min-width:0}
.sh{display:flex;align-items:center;gap:8px;padding:0 16px;height:48px;border-bottom:1px solid var(--bd)}.sh.e{border-color:transparent}
.sh h2,.bh h2,.fh h2{font-size:13px;font-weight:500;letter-spacing:.4px;color:var(--mu)}.n{font-size:13px;color:var(--mu);font-variant-numeric:tabular-nums}
.mt0{padding:12px 16px;font-size:13px;color:var(--mu)}
.rw{display:grid;align-items:center;gap:6px 12px;padding:10px 16px;cursor:pointer}.rw+.rw{border-top:1px solid var(--bd)}.rw:hover{background:var(--sunk)}
.rw.nd{grid-template-columns:auto minmax(0,1fr) auto auto;gap:6px 14px;padding:14px 16px}
.nd .tt{font-weight:500;font-size:14px}.nd .tt,.nd .sm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.nd .sm .pc{margin-right:8px;vertical-align:middle}
#s-needs{border-left:2px solid var(--ac)}#s-needs .el{color:var(--acl)}
.e0{display:flex;align-items:center;gap:8px;padding:16px;font-size:13px}.e0 span{display:grid;color:var(--i-ok)}
.rw.ag,.rw.dn{grid-template-columns:12px 52px minmax(0,1fr) auto}.rw.fx{grid-template-columns:38px 38px minmax(0,1fr);align-items:start}
.rw.ag .bg{justify-self:start;max-width:52px;overflow:hidden;text-overflow:ellipsis;display:block;line-height:22px;padding:0 6px}
.rw.fx .cm{font:12px/16px var(--mono);color:var(--tx);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dg .rw{border-top:1px solid var(--bd)}
.rw.stale{color:var(--mu)}.rw .t1{font-size:13px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.col2{min-width:0;display:flex;flex-direction:column}
.cc{display:flex;flex-wrap:wrap;align-items:center;gap:6px}
.el{font-size:12px;font-variant-numeric:tabular-nums;white-space:nowrap}.rw .el{text-align:right}.rw.stale .el{color:var(--t2)}.rw.fa .el{color:var(--er)}
.ee{font:11px/16px var(--mono);color:var(--er);overflow-wrap:anywhere}.rw .ee{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.bh{display:flex;align-items:center;gap:8px;min-height:32px;flex-wrap:wrap;margin-bottom:10px}
.ft{padding-right:3px}
.ft button{width:18px;height:18px;display:grid;place-items:center;border:0;background:transparent;color:inherit;border-radius:50%}
.fr0{background:var(--surf);border:1px dashed var(--bd2);border-radius:12px;padding:24px;display:flex;flex-direction:column;gap:10px;max-width:640px}.fr0 p{margin:0;color:var(--t2);font-size:13px}
code{display:block;padding:10px 12px;background:var(--sunk);border:1px solid var(--bd);border-radius:8px;font:12px var(--mono);overflow-wrap:anywhere}
#bw{overflow-x:auto;min-width:0}
.cols{display:grid;grid-template-columns:var(--gc);gap:12px;align-items:start;--mc:150px}
.col{display:flex;flex-direction:column;min-width:0}
.rl{display:flex;flex-direction:column;align-items:center;gap:10px;width:48px;padding:14px 0;border:1px solid var(--bd);background:var(--sunk);border-radius:12px;min-height:120px}.rl:hover{background:var(--s3)}
.emp{padding:12px;border:1px dashed var(--bd2);border-radius:8px;font-size:13px;color:var(--mu)}
.chd{display:flex;align-items:center;gap:6px;height:32px;padding:0 4px}.chd h3{font-size:13px;font-weight:500;color:var(--tx)}.chd .bg{--bt:transparent;--bi:var(--mu);--bb:transparent}
.cl{display:flex;flex-direction:column;gap:8px;max-height:600px;overflow-y:auto;overscroll-behavior:contain;padding:2px;margin:-2px}
.cl.mk{padding-bottom:30px;-webkit-mask-image:linear-gradient(to bottom,black calc(100% - 28px),transparent);mask-image:linear-gradient(to bottom,black calc(100% - 28px),transparent)}
.cmp .cl{gap:6px}
.cd{display:flex;flex-direction:column;gap:6px;padding:12px 14px;background:var(--surf);border:1px solid var(--bd);border-radius:12px;cursor:pointer;min-width:0;transition:opacity .2s cubic-bezier(.2,0,0,1),border-color .12s}
.cd:hover{background:var(--sunk);border-color:var(--bd2)}.cd.nd{border-color:var(--ac)}.cd.sel{background:var(--sunk);border-color:var(--acl)}.cd.dim{opacity:.55}
.cd .top{display:flex;align-items:center;gap:6px;min-width:0}.cd .top .k{flex:none}
.ttl{font-size:14px;line-height:19px;font-weight:500;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.mt{font:11px/16px var(--mono);color:var(--mu);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.nx{font-size:12px;line-height:17px;color:var(--t2);display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;overflow-wrap:anywhere}
.tl{font-size:11px;line-height:16px;color:var(--mu);font-variant-numeric:tabular-nums}
.pbar{display:flex;gap:2px;height:6px;border-radius:3px;overflow:hidden;background:var(--s3)}.pbar i{background:var(--sv);min-width:4px}
.kr{display:flex;align-items:center;gap:6px;font-size:12px;min-width:0}.kr .kt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--t2)}
.bg.pc{--bt:var(--surf);--bi:var(--t2);--bb:var(--bd2);min-width:0}.bg.pc .pct{max-width:16ch;overflow:hidden;text-overflow:ellipsis}
.stk{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:6px 12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--bd)}.stf{padding:12px 0 4px}.tt2{font-size:14px;font-weight:500}
.cmp .cd{padding:8px 10px}.cmp .ttl{font-size:13px;line-height:18px}.cmp .nx,.cmp .tl{display:none}
.eb{display:flex;align-items:center;justify-content:center;gap:6px;height:30px;border:1px dashed var(--bd2);border-radius:8px;background:transparent;color:var(--t2);font-size:12px;flex:none}.eb:hover{background:var(--surf)}
.hp{display:flex;gap:6px;padding:10px 16px 6px}
.hp button{height:28px;padding:0 12px;border-radius:8px;border:1px solid var(--bd);background:var(--surf);color:var(--t2);font-size:12px;font-weight:500;white-space:nowrap;font-variant-numeric:tabular-nums}
.hp [aria-pressed=true]{background:var(--sunk);border-color:var(--bd2);color:var(--tx)}
.dl{display:flex;justify-content:space-between;padding:8px 16px 2px;font-size:12px;font-weight:500;color:var(--t2)}.dl span+span{font-weight:400;color:var(--mu)}
.chg{display:flex;flex-wrap:wrap;align-items:center;gap:4px 8px;padding:10px 16px;border-top:1px solid var(--bd);font-size:12px;color:var(--mu)}
.chg button,.kl{border:0;padding:0 2px;background:transparent;color:var(--acl);font:500 12px var(--mono)}
.kl{margin-right:6px;padding:0}
.fh{display:flex;flex-wrap:wrap;align-items:center;gap:8px;padding:8px 12px 8px 16px;border-bottom:1px solid var(--bd)}
.fh select{height:32px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);font-size:12px;padding:0 10px;max-width:120px}
.fh label{display:flex;align-items:center;gap:4px;font-size:12px;color:var(--t2);white-space:nowrap}.fh input{margin:0;accent-color:var(--ac)}
#fd{max-height:420px;overflow-y:auto}
.fr,.tr{display:grid;grid-template-columns:40px 64px minmax(0,1fr);align-items:start;gap:8px;padding:6px 16px}.fr+.fr{border-top:1px solid var(--bd)}.fr.fresh{animation:fresh 2.4s ease-out}
.tr{padding:4px 0;font-size:13px}
.tm{font-size:12px;color:var(--mu);font-variant-numeric:tabular-nums;line-height:20px}
.kb{justify-self:start;margin-top:1px;display:inline-flex;align-items:center;height:18px;padding:0 6px;border-radius:4px;background:var(--sunk);color:var(--t2);font-size:11px;font-weight:500;white-space:nowrap}
.k-fail{color:var(--er)}.k-decision,.k-brief{color:var(--acl)}
.fs{min-width:0;display:flex;flex-direction:column;gap:2px;font-size:13px;overflow-wrap:anywhere}.fs .ee{background:var(--sunk);padding:2px 8px;border-radius:6px}
.ff{padding:8px 16px;border-top:1px solid var(--bd);font-size:12px;color:var(--mu)}
#bd,#dbd{position:fixed;inset:0;background:var(--ov);animation:fade .16s ease-out}#bd{z-index:40}#dbd{z-index:50}
#panel,#xd{position:fixed;z-index:41;left:50%;top:50%;transform:translate(-50%,-50%);width:min(820px,calc(100% - 32px));max-height:92vh;background:var(--surf);border:1px solid var(--bd2);border-radius:16px;display:flex;flex-direction:column;box-shadow:var(--sh);animation:fade .16s ease-out;overflow:hidden}
#panel{height:min(92vh,920px)}#xd{width:min(680px,calc(100% - 32px))}body.dlg-open{overflow:hidden}
.ph{display:flex;align-items:flex-start;gap:10px;padding:20px 20px 16px 28px;border-bottom:1px solid var(--bd)}
.pt{flex:1;min-width:0;display:flex;flex-direction:column;gap:8px}.pr{display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.pqn{display:flex;align-items:center;gap:6px;margin-left:auto}
.xp{margin:0;padding:10px 14px;background:var(--sunk);border:1px solid var(--bd);border-radius:8px;font:12px/18px var(--mono);color:var(--tx)}
.xp.c{white-space:pre-wrap;overflow-wrap:anywhere;max-height:160px;overflow:auto}.xp.o{white-space:pre;overflow:auto;max-height:50vh}
.pv{max-width:760px;margin:0 auto}.rvm{margin:16px 0 4px;font-size:13px;color:var(--mu)}
.md{overflow-wrap:anywhere}.md p{margin:0 0 10px}.md h2{margin:18px 0 6px;font-size:14px}.md h4{margin:14px 0 6px;font-size:13px}.md h2:first-child,.md h4:first-child{margin-top:8px}
.md ul,.md ol{margin:0 0 10px;padding-left:24px}.md li{margin:4px 0}.md li.cb{list-style:none;margin-left:-24px}.md pre{margin:0 0 10px}
.md code.ci{display:inline;padding:0 4px;border:0;border-radius:4px;background:var(--sunk);font:12px var(--mono);overflow-wrap:anywhere}
.md input{width:16px;height:16px;margin:0 8px 0 0;vertical-align:-3px;accent-color:var(--ac)}.md label{cursor:pointer}.ckn{margin-left:10px;font-size:12px;font-weight:400;color:var(--mu)}
.tw{overflow-x:auto;margin:0 0 10px}.md table{width:100%;min-width:480px;border-collapse:collapse;overflow-wrap:normal;word-break:normal}.md th,.md td{border-bottom:1px solid var(--bd);padding:6px 10px;text-align:left;vertical-align:top}.md th{border-bottom-color:var(--bd2);font-weight:500}
.pdb{padding:12px 28px calc(12px + env(safe-area-inset-bottom));border-top:1px solid var(--bd);background:var(--surf)}
#rv-opts{display:flex;gap:10px;max-width:760px;margin:0 auto}
.ob{flex:1 1 0;min-width:0;max-width:none;height:auto;min-height:44px;border-radius:8px;padding:8px 12px 8px 10px;gap:10px;font-size:13px;text-align:left}#rv-rb{flex:none}.ob .ol{display:flex;flex-direction:column;gap:1px;min-width:0;white-space:normal}
.ob small{font-size:12px;font-weight:400;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
@media(max-width:899px){#rv-opts{flex-wrap:wrap}.ob{flex:1 1 200px}#rv-rb{flex:none}}
.kh{display:inline-grid;place-items:center;flex:none;min-width:20px;height:20px;border:1px solid currentColor;border-radius:4px;font-size:11px;font-weight:500}
#rv-reply{display:flex;flex-direction:column;gap:8px;max-width:760px;margin:0 auto 10px}#rv-reply:empty{display:none}#rv-reply textarea{width:100%;min-height:72px;padding:8px 12px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);font-size:14px;resize:vertical}
.rvs{max-width:760px;margin:0 auto 10px;font-size:13px;font-weight:500}.rvs:empty{display:none}
#p-title{font-size:20px;line-height:26px;font-weight:600;letter-spacing:-.2px;overflow-wrap:anywhere}
.br{display:block;overflow:hidden;text-overflow:ellipsis;line-height:22px;font-family:var(--mono)}
.pb{flex:1;overflow-y:auto;overscroll-behavior:contain;padding:4px 28px 28px}.pb section{padding:14px 0;border-bottom:1px solid var(--bd)}.pb section:last-child{border:0}
.pb h3{margin:0 0 10px;font-size:13px;font-weight:500;letter-spacing:.4px;color:var(--mu)}.pb h3 .sm{font-weight:400;letter-spacing:0}
.kv{display:grid;grid-template-columns:84px minmax(0,1fr);gap:8px 12px;font-size:13px}.kv>:nth-child(odd){color:var(--mu)}.kv>div{display:flex;flex-direction:column;gap:6px;min-width:0;overflow-wrap:anywhere}
.mo{font:12px var(--mono)}
.pa1{display:grid;grid-template-columns:12px minmax(0,1fr) auto;align-items:center;gap:8px;padding:6px 0}.pa1.stale{color:var(--mu)}.pa1.stale .el{color:var(--t2)}.pa1.fa .el{color:var(--er)}.pa1 .el{color:var(--mu)}
.ed{display:flex;flex-direction:column;gap:10px}.ed h3{margin:0}
.fm{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.fl{display:flex;flex-direction:column;gap:4px;font-size:12px;font-weight:500;color:var(--t2)}
.fl select,.fl input,.fl textarea{height:36px;border:1px solid var(--bd2);border-radius:8px;background:var(--surf);font-size:14px;font-weight:400;padding:0 12px;color:var(--tx)}.fl textarea{height:auto;padding:8px 12px;resize:vertical}
.ar{display:flex;align-items:center;gap:10px}
.th{display:flex;align-items:center;margin-bottom:6px}.th h3{flex:1;margin:0}
.dh{font-size:12px;font-weight:500;color:var(--t2);padding:8px 0 4px}
#dlg{position:fixed;left:50%;top:50%;transform:translate(-50%,-50%);width:calc(100% - 24px);max-width:var(--w,400px);z-index:51;background:var(--surf);border:1px solid var(--bd2);border-radius:16px;box-shadow:var(--sh);padding:24px;display:flex;flex-direction:column;gap:12px;max-height:calc(100vh - 32px);overflow:auto}
#dlg h2{font-size:20px;line-height:26px}#dlg p{margin:0;font-size:13px;color:var(--t2)}
#dlg textarea{width:100%;border:1px solid var(--bd2);border-radius:8px;background:var(--sunk);padding:8px 12px;font:12px/18px var(--mono);resize:vertical}
.kg{display:grid;grid-template-columns:auto 1fr;gap:8px 16px;font-size:13px;align-items:center}.kg span:nth-child(odd){display:flex;gap:4px}.kg span:nth-child(even){color:var(--t2)}
.kg kbd{min-width:22px;text-align:center;font-size:12px;padding:1px 6px;border-color:var(--bd2);background:var(--surf);color:var(--tx)}
.da{display:flex;justify-content:flex-end;gap:8px;margin-top:4px}
#ts{position:fixed;z-index:60;right:20px;bottom:20px;display:flex;flex-direction:column;gap:8px;align-items:flex-end;pointer-events:none}
.to{pointer-events:auto;display:flex;align-items:center;gap:10px;min-height:40px;padding:8px 8px 8px 14px;border-radius:8px;background:var(--s3);border:1px solid var(--bd2);color:var(--tx);font-size:13px;max-width:420px;user-select:text;overflow-wrap:anywhere;animation:sheet .2s cubic-bezier(.2,0,0,1)}.to.bad{border-color:var(--i-bk)}
.tc{flex:none;width:24px;height:24px;padding:0;display:grid;place-items:center;border:0;border-radius:6px;background:transparent;color:var(--mu)}.tc:hover{background:var(--sunk);color:var(--tx)}
#tabs{display:none}
@media(max-width:1359px){html{--sw:312px}}
@media(max-width:1099px){
html{--pg:16px}
.rtl,#sbt{display:none}
.page{display:flex;flex-direction:column;align-items:stretch;padding:16px var(--pg)}
main,aside.rail{display:contents}
#attn{order:5;flex-basis:100%}
#s-needs{order:1}.g1{order:2}#s-board{order:3}.g2{order:4}
.g1,.g2{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;align-items:start}
#bw{overflow-x:auto;padding-bottom:8px}.cols{--mc:210px}
.srch label{width:200px}#fd{max-height:480px}
}
@media(max-width:699px){
html{--pg:12px}
.page{gap:12px;padding:12px var(--pg) 76px}
.g1,.g2{display:contents}#s-run{order:2}#s-fail{order:3}#s-board{order:4}#s-today{order:5}#s-feed{order:6}
[data-t]{display:none}
body[data-tab=now] [data-t=now],body[data-tab=board] [data-t=board],body[data-tab=today] [data-t=today],body[data-tab=feed] [data-t=feed]{display:block}
.cols{display:flex;flex-direction:column;align-items:stretch;gap:16px}.rl,.hint,#rt,#hp,#al span{display:none}#al{width:32px;height:32px;padding:0;justify-content:center}.hb .ib{width:32px;height:32px}
.cl{max-height:none;overflow:visible}.cl.mk{-webkit-mask-image:none;mask-image:none;padding-bottom:2px}
.srch{order:6;flex-basis:100%;display:none}body.so .srch{display:block}.srch label{width:100%}#res{left:0;right:0;width:auto;max-width:none}
#sbtn{display:grid}.hr{gap:8px 12px}
#fd{max-height:none}
#tabs{display:grid;grid-template-columns:repeat(4,1fr);position:fixed;left:0;right:0;bottom:0;z-index:25;background:var(--surf);border-top:1px solid var(--bd);padding-bottom:env(safe-area-inset-bottom)}
#tabs button{height:56px;border:0;background:transparent;display:flex;align-items:center;justify-content:center;gap:4px;color:var(--mu);font-size:12px;font-weight:500;position:relative}
#tabs [aria-current]{color:var(--tx);font-weight:600}#tabs [aria-current]::before{content:"";position:absolute;top:0;left:30%;right:30%;height:2px;background:var(--ac)}
#tabs i{font-style:normal;min-width:18px;height:18px;padding:0 5px;border-radius:999px;color:var(--wh);font-size:11px;line-height:18px;font-weight:500}
#ts{left:12px;right:12px;bottom:72px;align-items:stretch}
.rw.nd{grid-template-columns:auto minmax(0,1fr)}.rw.nd .bt{grid-column:1/-1}.rw.nd .el{grid-column:2;text-align:left}
#panel,#xd{left:0;top:0;transform:none;width:100%;height:100%;max-height:none;border:0;border-radius:0}.ph{padding:14px 8px 12px 16px}.ptb{padding:0 12px}.pb{padding:4px 16px 24px}.pdb{padding:10px 16px calc(10px + env(safe-area-inset-bottom));max-height:45vh;overflow-y:auto}.ob,#rv-rb{flex:1 1 100%}
.fm{grid-template-columns:1fr}
}
</style></head><body data-tab="now">
<div id="app">
<header>
<div class="hr">
<div class="brand"><svg width="24" height="24" viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="4" width="4.5" height="16" rx="1.5" fill="currentColor"/><rect class="a" x="9.75" y="4" width="4.5" height="10" rx="1.5"/><rect x="16.5" y="4" width="4.5" height="13" rx="1.5" fill="currentColor"/></svg><span>agent<b>-board</b></span></div>
<nav id="attn" aria-label="Attention"></nav>
<div class="sp"></div>
<div class="srch"><label><i data-i="search"></i><input id="q" type="search" role="combobox" aria-controls="res" aria-autocomplete="list" aria-label="Search tasks" placeholder="Search tasks" autocomplete="off"><kbd>/</kbd></label><div id="res" role="listbox" hidden></div></div>
<div class="lv" role="status" aria-live="polite" title="Refreshes every 3 s"><i class="g g-ring c-warn" id="dot"></i><span id="upd">connecting</span><button type="button" class="bt" id="rt" hidden>Retry</button></div>
<div class="hb"><button type="button" class="ib" id="sbtn" aria-label="Search"><i data-i="search"></i></button><button type="button" class="bt" id="al" hidden aria-pressed="false"><i data-i="bell"></i><span></span></button><button type="button" class="ib" id="thm"></button><button type="button" class="bt" id="hp" aria-label="Keyboard shortcuts" title="Keyboard shortcuts (?)">?</button></div>
</div>
<div id="bn" role="alert" hidden><i data-i="warn"></i><span id="bt"></span><button type="button" id="ba"></button></div>
</header>
<div class="page">
<button type="button" id="sbt" class="ib" aria-expanded="true" aria-controls="rail" aria-label="Collapse sidebar" title="Collapse sidebar (])"><i data-i="chr" data-z="14"></i></button>
<main>
<section class="sc" id="s-needs" data-t="now" aria-labelledby="h-needs"><div class="sh" id="nh"><i class="g c-warn g-dot"></i><h2 id="h-needs">Needs you</h2><span class="bg cnt" id="nc">0</span><span class="sp"></span><span class="sm hint">Press <kbd>n</kbd> to jump here</span></div><div id="b-need"></div></section>
<section id="s-board" data-t="board" aria-labelledby="h-board">
<div class="bh"><h2 id="h-board">Board</h2><span class="bg cnt" id="bc">0</span><span id="bf"></span><span class="sp"></span><div class="sg" role="group" aria-label="Density"><button type="button" data-d="comfortable">Comfortable</button><button type="button" data-d="compact">Compact</button></div></div>
<div class="fr0" id="fr0" hidden><h3 style="font-size:16px">No tasks yet</h3><p>Tasks appear when the orchestrator reports them. To add one by hand:</p><code>python3 board.py task T1 --title "First task" --status planned</code></div>
<div id="bw"><div class="cols" id="cols"></div></div>
</section>
</main>
<aside class="rail" id="rail" data-rt="ov">
<div class="rtl" role="tablist" aria-label="Sidebar"><button type="button" role="tab" id="tab-ov" data-rt="ov" aria-selected="true" aria-controls="rg-ov">Overview</button><button type="button" role="tab" id="tab-ac" data-rt="ac" aria-selected="false" tabindex="-1" aria-controls="rg-ac">Activity<span class="bg cnt bad" id="rtn" hidden>0</span></button></div>
<div class="grp g1" id="rg-ov" role="tabpanel" aria-labelledby="tab-ov">
<section class="sc" id="s-run" data-t="now" aria-labelledby="h-run"><div class="sh"><h2 id="h-run">Running now</h2><span class="bg cnt" id="rc">0</span></div><div id="b-run"></div></section>
<section class="sc" id="s-today" data-t="today" aria-labelledby="h-today"><div class="sh" style="padding-right:8px"><h2 id="h-today">Finished</h2><span class="bg cnt" id="dc">0</span><span class="sp"></span><button type="button" class="bt" id="sb"><i data-i="copy" data-z="14"></i>Copy standup</button></div><div class="hp" role="group" aria-label="Finished range" id="hpt"></div><div id="b-today"></div></section>
</div>
<div class="grp g2" id="rg-ac" role="tabpanel" aria-labelledby="tab-ac">
<section class="sc" id="s-fail" data-t="now" aria-labelledby="h-fail"><div class="sh"><h2 id="h-fail">Failed today</h2><span class="bg cnt" id="fc">0</span><span class="sp"></span><button type="button" class="lk" id="fsf">Show in feed</button></div><div id="b-fail"></div></section>
<section class="sc" id="s-feed" data-t="feed" aria-labelledby="h-feed"><div class="fh"><h2 id="h-feed">Live feed</h2><span class="sp"></span><div class="sg" role="group" aria-label="Feed filter" id="fts"></div><select id="flt" aria-label="Filter by task"></select><label><input type="checkbox" id="fall">All kinds</label></div><div id="fd"></div><div class="ff" id="ff"></div></section>
</div>
</aside>
</div>
<nav id="tabs" aria-label="Sections"></nav>
</div>
<div id="bd" hidden></div>
<div id="panel" role="dialog" aria-modal="true" aria-labelledby="p-title" hidden>
<div class="ph"><div class="pt"><div class="pr"><span class="bg kc" id="pk"></span><span class="pr" id="pp"></span><div class="pqn" id="pqw" hidden><span class="sm" id="pq"></span><button type="button" class="ib" id="pqp" aria-label="Previous review (K)" title="Previous (K)"><i data-i="chl"></i></button><button type="button" class="ib" id="pqx" aria-label="Next review (J)" title="Next (J)"><i data-i="chr"></i></button></div></div><h2 id="p-title"></h2><div class="pr" id="pl"></div></div><button type="button" class="ib" id="pc" aria-label="Close" title="Close (Esc)"><i data-i="x"></i></button></div>
<div class="ptb" id="ptb" role="tablist" aria-label="Task view" hidden><button type="button" role="tab" id="ptab-rv" data-pt="rv" aria-selected="true">Review</button><button type="button" role="tab" id="ptab-st" data-pt="st" aria-selected="false" tabindex="-1" hidden>Sub-tasks</button><button type="button" role="tab" id="ptab-dt" data-pt="dt" aria-selected="false" tabindex="-1">Details</button></div>
<div class="pb" id="pbody">
<div class="pv" id="pv-rev"><div class="rvm" id="rv-at"></div><div class="md" id="rv-md"></div></div>
<div class="pv" id="pv-st" hidden><div id="stl"></div></div>
<div class="pv" id="pv-det" hidden>
<section><h3>Summary</h3><div class="kv" id="pm"></div></section>
<section><h3>Agents <span class="sm" id="pas"></span></h3><div id="pa"></div></section>
<section class="ed"><h3>Edit</h3>
<div class="fm"><label class="fl">Status<select id="pst"></select></label><label class="fl">Waiting on<select id="pw"></select></label></div>
<label class="fl">Next step<input id="pnx" placeholder="One line. The very next step."></label>
<div class="ar"><button type="button" class="bt" id="psv" disabled>Save</button><button type="button" class="bt g0" id="pdc" hidden>Discard</button><span class="sm" id="phn"></span></div>
<label class="fl" style="margin-top:4px">Add a note<textarea id="pn" rows="2" placeholder="Visible in the timeline. Ctrl+Enter to add."></textarea></label>
<div><button type="button" class="bt" id="pnb" disabled>Add note</button></div></section>
<section><div class="th"><h3>Timeline</h3><button type="button" class="lk" id="tlb" hidden></button></div><div id="ptl"></div></section>
<section class="ar"><button type="button" class="bt ab" id="par" style="height:32px;font-weight:400"><i data-i="archive"></i>Archive task</button><span class="sm">Hides it from the board. History is kept.</span></section>
</div></div>
<div class="pdb" id="pdb" hidden><div class="rvs" id="rv-st" role="status"></div><div id="rv-reply"></div><div id="rv-opts"></div></div></div>
<div id="xd" role="dialog" aria-modal="true" aria-labelledby="xh" hidden>
<div class="ph"><div class="pt"><div class="pr"><span class="k" id="xk"></span><span class="tm" id="xt"></span></div><h2 id="xh">Failed command</h2></div><button type="button" class="ib" id="xc" aria-label="Close details" title="Close (Esc)"><i data-i="x"></i></button></div>
<div class="pb">
<section><h3>Command</h3><pre class="xp c" id="xcm" tabindex="0"></pre></section>
<section><div class="kv" id="xkv"></div></section>
<section><div class="th"><h3 id="xoh">Output</h3><button type="button" class="bt" id="xcp"><i data-i="copy" data-z="14"></i>Copy</button></div><pre class="xp o" id="xo" tabindex="0"></pre></section>
<section class="ar"><button type="button" class="bt" id="xop" hidden>Open task</button></section>
</div></div>
<div id="dbd" hidden></div><div id="dlg" role="dialog" aria-modal="true" aria-labelledby="d-title" hidden></div>
<div id="ts" aria-live="polite"></div>
<script>
const oe=m=>{document.body.dataset.err=(document.body.dataset.err||'')+m+';'};onerror=(m)=>oe(m);onunhandledrejection=e=>oe(e.reason);
const S=['planned','analysing','building','review','testing','blocked','done'];
const ST={planned:['Planned','ring'],analysing:['Analysing','half'],building:['Building','dot'],review:['Review','diamond'],testing:['Testing','tri'],blocked:['Blocked','square'],done:['Done','check']};
const WT={me:'Me',agent:'Agent',qc:'QC (tester)',po:'PO (product owner)',none:'Nobody','':'Unset'};
const I={search:'<circle cx="11" cy="11" r="6.5"/><path d="M20 20l-4.2-4.2"/>',bell:'<path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
ext:'<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
mr:'<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="18" r="2"/><path d="M6 8v8"/><path d="M18 16V9a3 3 0 0 0-3-3h-4"/><path d="M13 4l-2 2 2 2"/>',
copy:'<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/>',x:'<path d="M6 6l12 12M18 6L6 18"/>',
warn:'<path d="M12 4l9 16H3z"/><path d="M12 10v4"/><path d="M12 17.5v.01"/>',check:'<path d="M5 12.5l4.5 4.5L19 7"/>',
archive:'<rect x="3.5" y="4" width="17" height="4.5" rx="1"/><path d="M5 8.5V19a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8.5"/><path d="M10 12.5h4"/>',chev:'<path d="M6 9l6 6 6-6"/>',chr:'<path d="M9 6l6 6-6 6"/>',chl:'<path d="M15 6l-6 6 6 6"/>',sun:'<circle cx="12" cy="12" r="4"/><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4"/>',moon:'<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',sys:'<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'};
const ic=(n,z)=>'<svg class="ic" width="'+(z||16)+'" height="'+(z||16)+'" viewBox="0 0 24 24" aria-hidden="true">'+I[n]+'</svg>';
let F={};
let cur=0,tasks={},agents={},events=[],sel=null,pev=[],timer=null,lastOk=0,ok=true,auth=false,filter=null,feedMode='all',feedTask='',allK=false,hist='today',earlier=false,
  dens=null,tlAll=false,prevNeed=null,prevFail=null,first=true,opener='',dr=null,fresh={},maxId=0,qv='',qf=false,qi=0,qres=[],saving=false,toasts=[],tid=0,fj='',dopen=false,dback='',cache={},
  dec=new Set(),xid=null,fback='',ex={},rt='ov',sbc=false,pr={open:0},queue=[],ptab='rv',pushed=false,ck0=[],curCk='',pf='',
  deep=(()=>{try{return decodeURIComponent(location.hash.slice(1))}catch(e){return''}})();
const $=id=>document.getElementById(id);
const esc=s=>String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const ls=(k,v)=>{try{if(v===undefined)return localStorage.getItem(k);localStorage.setItem(k,v)}catch(e){}};
const pad=n=>String(n).padStart(2,'0');
const now=()=>Math.floor(Date.now()/1000);
const hm=ts=>{const d=new Date(ts*1000);return pad(d.getHours())+':'+pad(d.getMinutes())};
const hms=ts=>hm(ts)+':'+pad(new Date(ts*1000).getSeconds());
const day0=()=>{const d=new Date();d.setHours(0,0,0,0);return d/1000};
const dayIdx=ts=>{const d=new Date(ts*1000);d.setHours(0,0,0,0);return Math.round((d/1000-day0())/86400)};
const DN=['Sun','Mon','Tue','Wed','Thu','Fri','Sat'],MN=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const when=ts=>{const i=dayIdx(ts),d=new Date(ts*1000);return i>=0?hm(ts):i>-7?DN[d.getDay()]+' '+hm(ts):d.getDate()+' '+MN[d.getMonth()]};
const dayLab=ts=>{const i=dayIdx(ts),d=new Date(ts*1000);return i>=0?'Today':i===-1?'Yesterday':DN[d.getDay()]+' '+d.getDate()+' '+MN[d.getMonth()]};
function dur(s,x){s=Math.max(0,Math.floor(s));const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  if(d)return d+'d '+h+'h';if(h)return h+'h '+pad(m)+'m';if(x||s<60)return m?m+'m '+pad(s%60)+'s':s+'s';return m+'m'}
const kTok=n=>n>=1e6?(n/1e6).toFixed(1)+'M':Math.round(n/1e3)+'k';
const tm=(p,s,e,x)=>'<span class="el" data-p="'+p+'" data-s="'+s+'" data-e="'+(e||'')+'" data-x="'+(x?1:'')+'">'+p+dur((e||now())-s,x)+'</span>';
const safeUrl=u=>/^https?:\/\//.test(u||'')?u:'';
const act=t=>t.last_event_at||t.updated_at||0;
const urls=t=>{const u=safeUrl(t.mr_url)?[t.mr_url]:[];for(const x of((t.next||'').match(/https?:\/\/[^\s<>"')\]]+/g)||[]).map(x=>x.replace(/[,.;:]+$/,'')))if(!u.includes(x))u.push(x);return u};
const lab=u=>{const m=u.match(/merge_requests\/(\d+)/);if(m)return'!'+m[1];const s=u.replace(/^https?:\/\//,'');if(s.length<=30)return s;const p=s.split('/');return p[0]+'/…/'+p[p.length-1]};
const lc=u=>'<a class="bg" href="'+esc(u)+'" target="_blank" rel="noopener" title="'+esc(u)+'">'+esc(lab(u))+ic('ext',12)+'</a>';
const mrc=u=>'<a class="bg" href="'+esc(u)+'" target="_blank" rel="noopener" title="Merge request '+esc(lab(u))+'">'+ic('mr',12)+esc(lab(u))+'</a>';
const gl=k=>'<i class="g g-'+ST[k][1]+' s-'+k+'"></i>';
const G=(shape,c)=>'<i class="g g-'+shape+' '+c+'"></i>';
const pill=k=>'<span class="bg st s-'+k+'">'+gl(k)+ST[k][0]+'</span>';
const nwb=()=>'<span class="bg hot">'+G('dot','c-ac')+'Needs you</span>';
const pch=t=>t.parent?'<button type="button" class="bg pc" data-k="'+esc(t.parent)+'" title="Sub-task of '+esc(t.parent)+'"><span>'+esc(t.parent)+'</span><span class="pct">'+esc(t.parent_title||'')+'</span></button>':'';
const tm0=(p,s)=>'<span class="el" data-p="'+p+'" data-s="'+s+'" data-e="" data-x=""></span>';
const dj=e=>{try{return JSON.parse(e.data||'{}')}catch(x){return{}}};
// tiny safe markdown: every text is escaped first; links only from http(s); no raw HTML ever reaches innerHTML
const mil=s=>s.split(/(`[^`\n]+`)/).map((p,n)=>n%2?'<code class="ci">'+esc(p.slice(1,-1))+'</code>':esc(p).replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[^\s<]*[^\s<.,;:!?)])/g,(m,t,u,b)=>'<a href="'+(u||b)+'" target="_blank" rel="noopener">'+(u?t:b)+'</a>').replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')).join('');
function md(src,ck){const L=String(src||'').replace(/\r/g,'').split('\n'),o=[],LI=/^\s*(?:[-*]|\d+\.)\s+(.*)$/,TB=/^\s*\|.*\|\s*$/;let i=0,m,n=0,cn=0;
  while(i<L.length){const l=L[i];
    if(!l.trim()){i++;continue}
    if(/^\s*```/.test(l)){const c=[];i++;while(i<L.length&&!/^\s*```/.test(L[i]))c.push(L[i++]);i++;o.push('<pre class="xp o" tabindex="0">'+esc(c.join('\n'))+'</pre>');continue}
    if(m=l.match(/^(#{1,3})\s+(.*)$/)){const t=m[1].length>2?'h4':'h2';o.push('<'+t+'>'+mil(m[2])+(!cn&&/what to check/i.test(m[2])&&++cn?'<span class="ckn" id="ckn"></span>':'')+'</'+t+'>');i++;continue}
    if(LI.test(l)){const ord=/^\s*\d+\./.test(l),it=[];while(i<L.length&&(m=L[i].match(LI))){const c=m[1].match(/^\[([ xX])\]\s+(.*)$/);
      it.push(c?'<li class="cb"><label><input type="checkbox"'+((ck&&ck[n]!==undefined?ck[n]:c[1]!==' ')?' checked':'')+'>'+mil(c[2])+'</label></li>':'<li>'+mil(m[1])+'</li>');n+=c?1:0;i++}
      o.push((ord?'<ol>':'<ul>')+it.join('')+(ord?'</ol>':'</ul>'));continue}
    if(TB.test(l)){const r=[];while(i<L.length&&TB.test(L[i]))r.push(L[i++].trim().replace(/^\||\|$/g,'').split('|').map(c=>c.trim()));
      const hd=r.length>1&&r[1].every(c=>/^:?-+:?$/.test(c)),b=r.filter((x,n)=>!(hd&&n===1)&&(n||!hd));
      o.push('<div class="tw"><table>'+(hd?'<thead><tr>'+r[0].map(c=>'<th>'+mil(c)+'</th>').join('')+'</tr></thead>':'')+'<tbody>'+b.map(x=>'<tr>'+x.map(c=>'<td>'+mil(c)+'</td>').join('')+'</tr>').join('')+'</tbody></table></div>');continue}
    const p=[];while(i<L.length&&L[i].trim()&&!/^\s*(```|#{1,3}\s)/.test(L[i])&&!LI.test(L[i])&&!TB.test(L[i]))p.push(L[i++]);
    if(!p.length)p.push(L[i++]);o.push('<p>'+mil(p.join(' '))+'</p>')}
  return o.join('')}
const briefSum=b=>{const l=String(b||'').split('\n').map(x=>x.trim()).find(x=>x&&!/^(#{1,6}\s|```)/.test(x));return l?l.replace(/^(?:[-*]|\d+\.)\s+(?:\[[ xX]\]\s+)?/,'').replace(/\[([^\]]+)\]\(https?:[^)]*\)/g,'$1').replace(/[*`]/g,'').slice(0,160):''};
const dpend=t=>!!(t.decision&&t.decision.created_at>=(t.brief_at||0)&&t.decision.status==='pending');
const errLine=(d,n)=>{const l=(d.out||'').split('\n').find(x=>x.trim()&&!/^Exit code \d+$/.test(x.trim()));return l?l.trim():n.x};
const nm=a=>(a.name||a.id.slice(0,12)).replace(/^Task\s+\d+\b[\s:.-]*/,'');
const ast=a=>a.status==='running'&&(a.last_event_at||a.started_at||0)<now()-7200?'stale':a.status;
const put=(id,h)=>{if(cache[id]!==h){cache[id]=h;$(id).innerHTML=h}};
function kd(e){const k=e.kind,s=e.summary||'';
  if(k==='PostToolUseFailure')return'fail';
  if(k==='SubagentStart'||k==='PreToolUse')return'agent start';
  if(k==='SubagentStop')return'agent stop';
  if(k==='PostToolUse')return/^agent launched/.test(s)?'agent start':/^agent /.test(s)?'agent stop':'tool';
  return{Notification:'notify',SessionStart:'session',SessionEnd:'session',UserPromptSubmit:'prompt',Stop:'turn'}[k]||k}
const noisy=e=>e.kind==='UserPromptSubmit'||e.kind==='Stop'||kd(e)==='tool';
const cmdFail=e=>e.kind==='PostToolUseFailure'&&(e.data||'').indexOf('"tool"')>0;
function norm(e){let s=e.summary||'',x='';if(e.kind==='PostToolUseFailure'){const m=s.match(/^(.*?) failed: (.*) \((.*)\)$/);if(m){s=m[1]+' failed: '+m[2];x=m[3]}}
  return{id:e.id,ts:e.ts,k:kd(e),raw:e.kind,s,x,task:e.task_key}}
function evr(e,pn){return'<div class="'+(pn?'tr':'fr'+(fresh[e.id]&&Date.now()-fresh[e.id]<2500?' fresh':''))+'"><span class="tm">'+hm(e.ts)+'</span><span class="kb k-'+e.k.replace(' ','-')+'" title="'+esc(e.raw||e.k)+'">'+e.k+'</span><span class="fs"><span>'+
  (!pn&&e.task?'<button type="button" class="kl" id="fk-'+e.id+'" data-k="'+esc(e.task)+'">'+esc(e.task)+'</button>':'')+esc(e.s)+'</span>'+(e.x?'<span class="ee">'+esc(e.x)+'</span>':'')+'</span></div>'}
async function api(p,body){
  const r=await fetch(p,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json'}:{},body:body?JSON.stringify(body):undefined,credentials:'same-origin'});
  if(!r.ok){let m=r.status;try{m=(await r.json()).error||m}catch(e){}const x=new Error(m);x.status=r.status;throw x}
  return r.json();
}
async function poll(){
  try{
    const d=await api('/api/state?since='+cur);
    for(const t of d.tasks){if(t.archived){delete tasks[t.key];if(sel===t.key)closePanel()}else tasks[t.key]=t}
    for(const a of d.agents)agents[a.id]=a;
    F=d.failed||{};const j=JSON.stringify(F),fch=j!==fj;fj=j;
    if(d.events.length){const m={},pm=maxId;for(const e of events.concat(d.events)){m[e.id]=e;if(!first&&e.id>pm)fresh[e.id]=Date.now();maxId=Math.max(maxId,e.id)}events=Object.values(m).sort((a,b)=>b.id-a.id).slice(0,100)}
    const changed=d.cursor!==cur||fch;cur=d.cursor;lastOk=now();ok=true;auth=false;live();
    if(changed||first){const f0=first;first=false;render();if(f0&&deep&&tasks[deep])openPanel(deep,[],'',1);if(sel)await loadPanel()}
    tick();
  }catch(e){ok=false;auth=e.status===401;live()}
}
function live(){
  $('upd').textContent=ok?'Live · '+hms(lastOk):lastOk?'Offline · retrying':'connecting';
  $('dot').className='g c-'+(ok?'ok g-dot':'warn g-ring');$('rt').hidden=ok||auth;
  const b=$('bn');b.hidden=ok;if(!ok){b.className=auth?'bad':'warn';
    $('bt').textContent=auth?'Your session expired. Sign in again to keep updating.':'Board offline. '+(lastOk?'Showing data from '+hms(lastOk)+'. ':'')+'Retrying every 3 s.';$('ba').textContent=auth?'Sign in again':'Retry now'}
}
function loop(){clearTimeout(timer);if(document.hidden)return;poll().finally(()=>{clearTimeout(timer);if(!document.hidden)timer=setTimeout(loop,3000)})}
document.addEventListener('visibilitychange',loop);
$('rt').onclick=loop;$('ba').onclick=()=>auth?location.reload():loop();
function arow(a,w){const s=ast(a),k=a.task_key||'',e=a.ended_at||a.last_event_at||now();let mark,el,cl='ag',sub=[a.model,a.type].filter(Boolean).join(' · ');
  if(s==='running'){mark=G('dot','c-info p');el=tm('',a.started_at,0,1)}
  else if(s==='stale'){mark=G('ring','c-warn');el=tm('idle ',a.last_event_at||a.started_at,0,1);sub+=' · stale';cl+=' stale'}
  else if(s==='failed'){mark=G('x','c-bad');el='<span class="el">failed · '+dur(e-a.started_at,1)+'</span>';cl+=' fa'}
  else{mark=G('check','c-ok');el=tm('',a.started_at,e,1)}
  sub+=(a.tool_uses?' · '+a.tool_uses+' tools':'')+(a.tokens?' · '+kTok(a.tokens)+' tok':'');
  return w?'<div class="rw ag'+cl.slice(2)+'" id="a-'+esc(a.id)+'"'+(k?' role="button" tabindex="0" data-k="'+esc(k)+'"':'')+' title="'+(s==='stale'?'Silent since '+hm(a.last_event_at||a.started_at)+'.':esc(a.name||'')+' · started '+hm(a.started_at))+'">'+mark+''+(k?'<span class="bg kc" title="'+esc(k)+'">'+esc(k)+'</span>':'<span></span>')+'<span class="col2"><span class="t1">'+esc(nm(a))+'</span><span class="sm">'+esc(sub)+'</span></span>'+el+'</div>'
    :'<div class="pa1'+cl.slice(2)+'">'+mark+'<span class="col2"><span class="t1">'+esc(nm(a))+'</span><span class="sm">'+esc(sub)+'</span></span>'+el+'</div>'}
function render(){
  const sc={};document.querySelectorAll('.cl').forEach(e=>sc[e.id]=e.scrollTop);
  const fd=$('fd'),ft=fd.scrollTop,fh=fd.scrollHeight,ae=document.activeElement,fid=ae&&ae.id||'',ss=ae&&ae.selectionStart,se=ae&&ae.selectionEnd;
  const all=Object.values(tasks),A=Object.values(agents).filter(a=>!a.task_key||tasks[a.task_key]);
  if(pf&&!tasks[pf]){pf='';ls('pf','')}
  const vis=pf?all.filter(t=>t.key===pf||t.parent===pf):all;
  const need=all.filter(t=>t.needs_you&&!kidsOf(t)),run=A.filter(a=>ast(a)==='running').sort((a,b)=>b.started_at-a.started_at),stl=A.filter(a=>ast(a)==='stale').sort((a,b)=>b.started_at-a.started_at);
  const nf=Object.values(F).reduce((a,b)=>a+b,0),ags={};
  for(const a of A)if(a.task_key)(ags[a.task_key]||(ags[a.task_key]=[])).push(a);
  for(const k in ags)ags[k].sort((a,b)=>b.started_at-a.started_at);
  document.title=(need.length?'('+need.length+') ':'')+'agent-board';
  document.body.classList.toggle('cmp',dens==='compact');
  document.querySelectorAll('[data-d]').forEach(b=>b.setAttribute('aria-pressed',b.dataset.d===dens));
  const ac=(k,n,text,tone,tip,m)=>'<button type="button" class="bg'+(n?' '+tone:'')+'" id="at-'+k+'" data-at="'+k+'" aria-pressed="'+(filter===k)+'"'+(n?'':' disabled')+' title="'+tip+'">'+(n?m:'')+'<span>'+text+'</span></button>';
  put('attn',ac('needs',need.length,need.length?need.length+(need.length===1?' needs you':' need you'):'Nothing needs you','warn','Tasks waiting on you. Click to highlight.',G('dot','c-ac'))+
    ac('failing',nf,nf+' failed today','bad','Failed commands (24 h) and failed agents',G('x','c-bad'))+
    ac('stale',stl.length,stl.length+' stale','warn','Agents silent for 2 h or more',G('ring','c-warn'))+
    ac('running',run.length,run.length+' running','info','Agents running now',G('dot','c-info p')));
  $('nc').textContent=need.length;$('nh').classList.toggle('e',!need.length);
  put('b-need',need.sort((a,b)=>act(b)-act(a)).map(t=>{const k=esc(t.key),sm=(t.brief?briefSum(t.brief):t.next)||(t.brief?'Review ready':'No next step set');
    return'<div class="rw nd" id="n-'+k+'" data-k="'+k+'"><span class="bg kc">'+k+'</span><div class="col2"><span class="tt">'+esc(t.title)+'</span><span class="sm">'+pch(t)+esc(sm)+'</span></div>'+tm0('waiting ',t.brief_at||t.updated_at||t.created_at||0)+
      '<button type="button" class="bt p" id="rv-'+k+'" data-needs data-k="'+k+'">'+(t.brief?'Open review':'Open task')+'</button></div>'}).join('')||
    (all.length?'<div class="mt0 e0"><span>'+ic('check')+'</span>Nothing needs you. Agents will flag the next decision here.</div>':''));
  $('rc').textContent=run.length+(stl.length?' + '+stl.length+' stale':'');
  put('b-run',run.concat(stl.slice(0,5)).map(a=>arow(a,1)).join('')+(stl.length>5?'<div class="mt0">+'+(stl.length-5)+' more stale</div>':'')||'<div class="mt0">No agents running.</div>');
  const fl=events.filter(e=>cmdFail(e)&&e.ts>now()-86400);
  $('fc').textContent=nf;
  $('rtn').textContent=nf;$('rtn').hidden=!nf;
  put('b-fail',fl.slice(0,12).map(e=>{const n=norm(e),d=dj(e),cm=d.cmd||n.s.replace(/^\S+ failed: /,''),er=errLine(d,n);
    return'<div class="rw fx" id="f-'+e.id+'" role="button" tabindex="0" data-fd="'+e.id+'" aria-label="Failed command '+esc(cm)+'. Show details"><span class="tm">'+hm(e.ts)+'</span><span class="k">'+esc(e.task_key||'-')+'</span><span class="col2" style="gap:2px"><span class="cm" title="'+esc(cm)+'">'+esc(cm)+'</span>'+(er?'<span class="ee">'+esc(er)+'</span>':'')+'</span></div>'}).join('')+(fl.length>12?'<div class="mt0">+'+(fl.length-12)+' more in the feed</div>':'')||'<div class="mt0">No failures today.</div>');
  const match=t=>{if(!filter)return true;const g=ags[t.key]||[];return filter==='needs'?t.needs_you:filter==='failing'?(F[t.key]>0||g.some(a=>a.status==='failed')):filter==='stale'?g.some(a=>ast(a)==='stale'):g.some(a=>ast(a)==='running')};
  const pcard=(t,c,r)=>{const p=t.progress,n=t.needs_you,kids=t.children.map(k=>tasks[k]).filter(Boolean),kn=kids.filter(x=>x.needs_you).length,kf=kids.filter(x=>x.status!=='done'&&F[x.key]>0).length;
    return'<div class="cd pc s-'+t.status+(n?' nd':'')+(sel===t.key?' sel':'')+(match(t)?'':' dim')+'" id="t-'+esc(t.key)+'" role="button" tabindex="0" data-k="'+esc(t.key)+'" data-col="'+c+'" data-row="'+r+'" aria-label="'+esc(t.key+' '+t.title+', '+p.done+' of '+p.total+' done'+(n?', needs you':''))+'">'+
    '<div class="top"><span class="k">'+esc(t.key)+'</span><span class="sp"></span><span class="bg">'+p.done+' / '+p.total+' done</span></div><span class="ttl">'+esc(t.title)+'</span>'+
    '<div class="pbar" role="img" aria-label="'+p.done+' of '+p.total+' done">'+['done'].concat(S.filter(s=>s!=='done')).map(s=>p.by_status[s]?'<i class="s-'+s+'" style="flex:'+p.by_status[s]+'" title="'+p.by_status[s]+' '+ST[s][0]+'"></i>':'').join('')+'</div>'+
    ((kn||kf)?'<div class="cc" style="gap:4px">'+(kn?'<span class="bg hot">'+G('dot','c-ac')+kn+' need you</span>':'')+(kf?'<span class="bg bad">'+G('x','c-bad')+kf+' failing</span>':'')+'</div>':'')+
    kids.filter(x=>x.status!=='done').slice(0,3).map(x=>'<div class="kr">'+gl(x.status)+'<span class="k">'+esc(x.key)+'</span><span class="kt">'+esc(x.title)+'</span></div>').join('')+
    '<button type="button" class="lk" data-pf="'+esc(t.key)+'">'+(pf===t.key?'Show all tasks':'Show only these')+'</button>'+
    (t.created_at?'<span class="tl">'+(t.end_at?'Done '+when(t.end_at)+' · took '+dur(t.end_at-t.created_at):'Started '+when(t.created_at)+' · '+tm('running ',t.created_at))+'</span>':'')+'</div>'};
  const card=(t,c,r)=>{if(kidsOf(t))return pcard(t,c,r);const g=ags[t.key]||[],m=safeUrl(t.mr_url),ch=[],dn=g.filter(a=>a.status==='done').length,fa=g.filter(a=>a.status==='failed').length,n=t.needs_you;
    for(const a of g){const s=ast(a);
      if(s==='running')ch.push('<span class="bg info" title="'+esc(nm(a))+' running">'+G('dot','c-info p')+tm('',a.started_at,0,1)+'</span>');
      else if(s==='stale')ch.push('<span class="bg warn" title="'+esc(nm(a))+' stale: silent since '+hm(a.last_event_at||a.started_at)+'">'+G('ring','c-warn')+tm('idle ',a.last_event_at||a.started_at,0,1)+'</span>')}
    if(dn)ch.push('<span class="bg ok">'+G('check','c-ok')+dn+' done</span>');
    if(fa)ch.push('<span class="bg bad">'+G('x','c-bad')+fa+' agent failed</span>');
    if(F[t.key]&&t.status!=='done')ch.push('<span class="bg bad" title="failed commands in the last 24 h">'+G('x','c-bad')+F[t.key]+' failed</span>');
    if(n&&t.brief&&!dpend(t))ch.unshift('<span class="bg info">'+G('diamond','c-info')+'Review ready</span>');
    if(t.parent)ch.unshift(pch(t));
    const w=t.waiting&&t.waiting!=='none'?t.waiting==='qc'?'QC':t.waiting==='po'?'PO':t.waiting:'';
    return'<div class="cd s-'+t.status+(n?' nd':'')+(sel===t.key?' sel':'')+(match(t)?'':' dim')+'" id="t-'+esc(t.key)+'" role="button" tabindex="0" data-k="'+esc(t.key)+'" data-col="'+c+'" data-row="'+r+'" aria-label="'+esc(t.key+' '+t.title+', '+ST[t.status][0]+(n?', needs you':''))+'">'+
    '<div class="top"><span class="k">'+esc(t.key)+'</span><span class="sp"></span>'+(n?nwb():w&&t.status!=='done'?'<span class="bg">waiting: '+esc(w)+'</span>':'')+'</div>'+
    '<span class="ttl">'+esc(t.title)+'</span>'+((t.project||t.branch)?'<span class="mt">'+esc([t.project,t.branch].filter(Boolean).join(' · '))+'</span>':'')+
    (m||ch.length?'<div class="cc" style="gap:4px">'+(m?mrc(m):'')+ch.join('')+'</div>':'')+
    (t.next&&t.status!=='done'?'<span class="nx"><span class="mu">Next: </span>'+esc(t.next)+'</span>':'')+
    (t.created_at?'<span class="tl">'+(t.end_at?'Done '+when(t.end_at)+' · took '+dur(t.end_at-t.created_at):'Started '+when(t.created_at)+' · '+tm('running ',t.created_at))+'</span>':'')+'</div>'};
  let ci=0;const cols=S.map(s=>{let l=vis.filter(t=>t.status===s),er=[];
    if(s==='done'){l.sort((a,b)=>b.end_at-a.end_at);er=l.filter(t=>dayIdx(t.end_at)<-1);l=l.filter(t=>dayIdx(t.end_at)>=-1)}
    else l.sort((a,b)=>(b.needs_you-a.needs_you)||((a.created_at||0)-(b.created_at||0)));
    const n=l.length+er.length,nm0=ST[s][0];
    if(!n&&!ex[s])return{z:1,h:'<button type="button" class="rl s-'+s+'" id="rl-'+s+'" data-ex="'+s+'" aria-expanded="false" aria-label="'+nm0+', 0 tasks" title="'+nm0+'">'+gl(s)+'<span class="bg cnt" aria-hidden="true">0</span></button>'};
    const sh=earlier?l.concat(er):l,ix=ci++,mk=sh.length>5;
    return{h:'<div class="col" role="region" aria-label="'+nm0+', '+n+' tasks"><div class="chd s-'+s+'">'+gl(s)+'<h3>'+nm0+'</h3><span class="bg cnt">'+n+'</span><span class="sp"></span>'+(n?'':'<button type="button" class="lk" id="cx-'+s+'" data-ex="'+s+'" aria-expanded="true" aria-label="Collapse '+nm0+' column">Collapse</button>')+'</div>'+
      '<div class="cl'+(mk?' mk':'')+'" id="cl-'+s+'">'+(n?'':'<div class="emp">No tasks</div>')+sh.map((t,r)=>card(t,ix,r)).join('')+(er.length?'<button type="button" class="eb" id="er" aria-expanded="'+earlier+'">'+(earlier?'Hide earlier':'Earlier ('+er.length+')')+ic('chev',12)+'</button>':'')+'</div></div>'}});
  $('cols').style.setProperty('--gc',cols.map(c=>c.z?'48px':'minmax(var(--mc),1fr)').join(' '));
  $('fr0').hidden=all.length>0;$('bc').textContent=all.filter(t=>t.status!=='done').length+' active';
  put('cols',all.length?cols.map(c=>c.h).join(''):'');
  put('bf',(pf?'<span class="bg info ft">Showing '+esc(pf)+'<button type="button" id="pfx" aria-label="Show all tasks">'+ic('x',12)+'</button></span>':'')+(filter?'<span class="bg info ft">Highlighting: '+({needs:'Needs you',failing:'Failing',stale:'Stale agents',running:'Running'})[filter]+'<button type="button" id="cf" aria-label="Clear filter">'+ic('x',12)+'</button></span>':''));
  const done=all.filter(t=>t.status==='done').sort((a,b)=>b.end_at-a.end_at),R={today:t=>dayIdx(t.end_at)===0,yesterday:t=>dayIdx(t.end_at)===-1,week:t=>dayIdx(t.end_at)>=-6};
  const hs=done.filter(R[hist]);$('dc').textContent=done.filter(R.today).length;
  put('hpt',[['today','Today'],['yesterday','Yesterday'],['week','7 days']].map(([k,l])=>'<button type="button" id="h-'+k+'" data-h="'+k+'" aria-pressed="'+(hist===k)+'">'+l+' '+done.filter(R[k]).length+'</button>').join(''));
  const gr=[];for(const t of hs){const lb=dayLab(t.end_at);let g=gr.find(x=>x.l===lb);if(!g)gr.push(g={l:lb,i:[]});g.i.push(t)}
  const di=hist==='today'?0:hist==='yesterday'?-1:null,hk=new Set(hs.map(t=>t.key));
  const chg=di===null?[]:all.filter(t=>t.status!=='done'&&!hk.has(t.key)&&dayIdx(act(t))===di).map(t=>t.key).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
  put('b-today',(gr.map(g=>'<div class="dg">'+(hist==='week'?'<div class="dl"><span>'+g.l+'</span><span>'+g.i.length+' done</span></div>':'')+g.i.map(t=>{const m=safeUrl(t.mr_url);
    return'<div class="rw dn" id="d-'+esc(t.key)+'" role="button" tabindex="0" data-k="'+esc(t.key)+'">'+G('check','c-ok')+'<span class="k">'+esc(t.key)+'</span><span class="col2"><span class="t1">'+esc(t.title)+'</span><span class="sm" style="font-variant-numeric:tabular-nums">Done '+(hist==='week'?when(t.end_at):hm(t.end_at))+(t.created_at?' · took '+dur(t.end_at-t.created_at):'')+pch(t)+'</span></span>'+(m?mrc(m):'<span></span>')+'</div>'}).join('')+'</div>').join('')||
    '<div class="mt0">'+({today:'Nothing finished yet today.',yesterday:'Nothing finished yesterday.',week:'Nothing finished in the last 7 days.'})[hist]+'</div>')+
    (hist==='week'&&done.some(t=>dayIdx(t.end_at)<-6)?'<div class="chg">Older tasks: search by key or title.</div>':'')+
    (chg.length?'<div class="chg"><span>Also changed '+(hist==='today'?'today':'yesterday')+':</span>'+chg.map(k=>'<button type="button" id="ck-'+esc(k)+'" data-k="'+esc(k)+'">'+esc(k)+'</button>').join('')+'</div>':''));
  put('fts',[['all','All'],['failures','Failures'],['unlinked','Unlinked']].map(([k,l])=>'<button type="button" id="ft-'+k+'" data-f="'+k+'" aria-pressed="'+(feedMode===k)+'">'+l+'</button>').join(''));
  const keys=Object.keys(tasks).sort((a,b)=>a.localeCompare(b,undefined,{numeric:true})),fs=$('flt'),fo='<option value="">All tasks</option>'+keys.map(k=>'<option value="'+esc(k)+'">'+esc(k)+'</option>').join('');
  if(cache.flt!==fo){cache.flt=fo;fs.innerHTML=fo}fs.value=keys.includes(feedTask)?feedTask:'';feedTask=fs.value;
  let es=events.filter(e=>(feedMode==='all'||feedMode==='failures'&&e.kind==='PostToolUseFailure'||feedMode==='unlinked'&&!e.task_key)&&(!feedTask||e.task_key===feedTask));
  const hid=allK?0:es.filter(noisy).length;if(!allK)es=es.filter(e=>!noisy(e));
  put('fd',es.map(e=>evr(norm(e))).join('')||'<div class="mt0">'+(feedMode==='failures'?'No failures in this view.':events.length?'No events match this filter.':'No events yet. Hooks report here as soon as a session starts.')+'</div>');
  $('ff').textContent=hid?es.length+' shown · '+hid+' prompt/tool events hidden':es.length+' events · last 100 kept';
  if(ft>0)fd.scrollTop=ft+(fd.scrollHeight-fh);
  const tb=document.body.dataset.tab;
  put('tabs',[['now','Now',need.length,'var(--ac)'],['board','Board',0],['today','Today',0],['feed','Feed',nf,'var(--dng)']].map(([k,l,b,c])=>'<button type="button" id="tb-'+k+'" data-tb="'+k+'"'+(tb===k?' aria-current="page"':'')+'>'+l+(b?'<i style="background:'+c+'">'+b+'</i>':'')+'</button>').join(''));
  for(const id in sc)if($(id))$(id).scrollTop=sc[id];
  alerts(need,A);sres();
  if(fid&&$(fid)&&document.activeElement!==$(fid)){const f=$(fid);f.focus({preventScroll:true});if(typeof ss==='number'&&f.setSelectionRange)try{f.setSelectionRange(ss,se)}catch(x){}}
}
function tick(){document.querySelectorAll('.el[data-s]').forEach(e=>{const t=e.dataset.p+dur((e.dataset.e?+e.dataset.e:now())-(+e.dataset.s),e.dataset.x);if(e.textContent!==t)e.textContent=t})}
setInterval(()=>{if(!document.hidden)tick()},1000);
const alOn=()=>{try{return ls('alerts')==='1'&&Notification.permission==='granted'}catch(e){return false}};
function alBtn(){const o=alOn(),b=$('al');b.setAttribute('aria-pressed',o);b.querySelector('span').textContent='Alerts '+(o?'on':'off');b.title=o?'Desktop alerts for new needs-you tasks and failures':'Turn on desktop alerts'}
function alerts(need,A){
  const n=new Set(need.map(t=>t.key)),f=new Set(A.filter(a=>a.status==='failed').map(a=>a.id));
  if(alOn()){if(prevNeed)for(const t of need)if(!prevNeed.has(t.key))try{new Notification(t.key+' needs you',{body:t.title+(t.next?' - '+t.next:'')})}catch(e){}
    if(prevFail)for(const a of A)if(f.has(a.id)&&!prevFail.has(a.id))try{new Notification((a.task_key||'Agent')+' agent failed',{body:nm(a)})}catch(e){}}
  prevNeed=n;prevFail=f;
}
if(typeof Notification!=='undefined'){$('al').hidden=false;alBtn()}
$('al').onclick=async()=>{try{if(ls('alerts')==='1'){ls('alerts','0');toast('Alerts off')}else if(Notification.permission==='granted'||await Notification.requestPermission()==='granted'){ls('alerts','1');toast('Alerts on.')}else toast('Alerts are blocked in this browser.',1)}catch(e){}alBtn()};
function toast(t,bad,ms){const id=++tid;toasts=toasts.concat({id,t,bad}).slice(-3);paintT();setTimeout(()=>{toasts=toasts.filter(x=>x.id!==id);paintT()},ms||3500)}
const paintT=()=>{$('ts').innerHTML=toasts.map(x=>'<div class="to'+(x.bad?' bad':'')+'" role="status">'+(x.bad?G('x','c-bad'):G('check','c-ok'))+'<span>'+esc(x.t)+'</span><button type="button" class="tc" data-tx="'+x.id+'" aria-label="Dismiss">'+ic('x',12)+'</button></div>').join('')};
$('ts').onclick=e=>{const b=e.target.closest('[data-tx]');if(b){toasts=toasts.filter(x=>String(x.id)!==b.dataset.tx);paintT()}};
async function clipText(text){try{await navigator.clipboard.writeText(text);return true}catch(x){const p=document.activeElement;try{const a=document.createElement('textarea');a.value=text;a.style.cssText='position:fixed;opacity:0';document.body.append(a);a.select();const r=document.execCommand('copy');a.remove();if(p&&p.focus)p.focus({preventScroll:true});return!!r}catch(y){return false}}}
async function copy(text,msg){if(await clipText(text))toast(msg);else toast('Could not copy. Select the text and press Ctrl+C.',1)}
async function decide(k,body,label){
  if(dec.has(k))return;dec.add(k);
  const line='Task '+k.replace(/^T(?=\d+$)/,'')+': '+(body.action==='approve'?'merge':body.action==='reply'?body.text.replace(/\s*\n\s*/g,' '):label),cp=clipText(line);
  let o;try{o=tasks[k]=await api('/api/task/'+encodeURIComponent(k)+'/decision',body)}
  catch(e){dec.delete(k);toast('Could not record the decision: '+e.message,1);return}
  dec.delete(k);if(body.action==='reply')pr={open:0};
  const ok=await cp;
  if(o.waiter)toast('Sent to Claude');
  else if(o.brief_cwd&&ok)toast('Recorded. Claude picks it up with your next message; copied the line as a backup',0,6000);
  else if(ok)toast('Copied - paste it in the terminal');
  else toast('Copy failed. Select and copy: '+line,1,20000);
  render();if(sel===k){paint();if(queue.includes(k))setTimeout(()=>{if(sel!==k)return;const q=queue,n=q.slice(q.indexOf(k)+1).find(x=>tasks[x]&&tasks[x].brief&&!decided(tasks[x]));if(n)openPanel(n,q);else{closePanel();toast('All reviews done')}},1200)}loop()}
const decided=t=>t.decision&&t.decision.created_at>=(t.brief_at||0)?t.decision:null;
function openDlg(o){const a=document.activeElement;dback=dopen?dback:(a&&a.id)||'';dopen=true;const d=$('dlg');d.style.setProperty('--w',(o.w||400)+'px');
  d.innerHTML='<h2 id="d-title">'+o.title+'</h2>'+(o.body?'<p>'+o.body+'</p>':'')+(o.html||'')+'<div class="da"><button type="button" class="bt" id="dx">'+(o.cancel||'Cancel')+'</button>'+(o.ok?'<button type="button" class="bt '+(o.ok.c||'p')+'" id="dok">'+o.ok.label+'</button>':'')+'</div>';
  $('dx').onclick=closeDlg;if(o.ok)$('dok').onclick=o.ok.fn;$('dbd').hidden=d.hidden=false;$('dbd').onclick=closeDlg;($(o.focus||(o.ok?'dok':'dx'))).focus()}
function closeDlg(){if(!dopen)return;dopen=false;$('dbd').hidden=$('dlg').hidden=true;const o=$(dback)||(sel&&$('pc'));if(o)o.focus({preventScroll:true})}
const trap=(e,box)=>{if(e.key!=='Tab')return;const f=[...box.querySelectorAll('button,a[href],input,select,textarea,pre[tabindex]')].filter(x=>!x.disabled&&x.offsetParent!==null);if(!f.length)return;
  if(e.shiftKey&&document.activeElement===f[0]){e.preventDefault();f[f.length-1].focus()}else if(!e.shiftKey&&document.activeElement===f[f.length-1]){e.preventDefault();f[0].focus()}};
$('dlg').onkeydown=e=>trap(e,$('dlg'));$('panel').onkeydown=e=>trap(e,$('panel'));
async function standup(){
  openDlg({title:'Standup summary',body:'Plain text, ready to paste.',html:'<textarea id="sut" readonly rows="9" aria-label="Standup text">Loading...</textarea>',cancel:'Close',w:560,
    ok:{label:'Copy to clipboard',fn:()=>{const t=$('sut');if(!t.dataset.l)return;copy(t.value,'Standup copied');closeDlg()}}});
  try{const t=(await api('/api/standup?off='+-new Date().getTimezoneOffset()*60)).text,a=$('sut');if(a){a.value=t;a.dataset.l=1}}catch(x){const a=$('sut');if(a)a.value='Could not load the standup: '+x.message}
}
function help(){openDlg({title:'Keyboard shortcuts',cancel:'Close',w:420,html:'<div class="kg">'+[[['/'],'Search tasks'],[['j','k'],'Next / previous card (or ↓ ↑ on a card)'],[['h','l'],'Previous / next column (or ← →)'],[['Enter'],'Open the focused item'],[['Esc'],'Close the review, reply box or search'],[['n'],'Jump to Needs you'],[['1-5'],'In a review: choose option'],[['R'],'In a review: reply (Ctrl+Enter sends)'],[['J','K'],'Next / previous review'],[['←','→'],'Switch sidebar tab'],[[']'],'Toggle the sidebar'],[['?'],'Show this list']]
  .map(([k,l])=>'<span>'+k.map(x=>'<kbd>'+x+'</kbd>').join('')+'</span><span>'+l+'</span>').join('')+'</div><p><a href="/architecture" target="_blank" rel="noopener">How agent-board works</a></p>'})}
function sres(){const q=qv.trim().toLowerCase().replace(/^task\s*/,'');
  qres=q?Object.values(tasks).filter(t=>(t.key+' '+t.title+' '+t.branch+' '+t.project+' '+t.next).toLowerCase().includes(q)).sort((a,b)=>(a.key.toLowerCase()==='t'+q?0:1)-(b.key.toLowerCase()==='t'+q?0:1)||act(b)-act(a)).slice(0,8):[];
  qi=Math.min(qi,Math.max(0,qres.length-1));const r=$('res');r.hidden=!(qf&&qv.trim());
  r.innerHTML=qres.map((t,i)=>'<div role="option" class="ro" aria-selected="'+(i===qi)+'" data-q="'+esc(t.key)+'">'+gl(t.status)+'<span class="k">'+esc(t.key)+'</span><span class="rt">'+esc(t.title)+'</span><span class="mu">'+(t.parent?esc(t.parent)+' · ':'')+ST[t.status][0]+'</span></div>').join('')||'<div class="ne">No task matches "'+esc(qv.trim())+'".</div>'}
function pick(k){opener='';qv='';$('q').value='';qf=false;$('q').blur();document.body.classList.remove('so');sres();openPanel(k)}
$('q').oninput=e=>{qv=e.target.value;qi=0;sres()};$('q').onfocus=()=>{qf=true;sres()};$('q').onblur=()=>setTimeout(()=>{qf=false;sres()},120);
$('q').onkeydown=e=>{if(e.key==='ArrowDown'||e.key==='ArrowUp'){e.preventDefault();qi=Math.max(0,Math.min(qres.length-1,qi+(e.key==='ArrowDown'?1:-1)));sres()}else if(e.key==='Enter'&&qres[qi]){e.preventDefault();pick(qres[qi].key)}};
$('res').onmousedown=e=>{const o=e.target.closest('[data-q]');if(o){e.preventDefault();pick(o.dataset.q)}};
function openSearch(){document.body.classList.add('so');$('q').focus()}
function clearSearch(){qv='';$('q').value='';qf=false;$('q').blur();document.body.classList.remove('so');sres()}
$('sbtn').onclick=()=>document.body.classList.contains('so')?clearSearch():openSearch();
$('hp').onclick=help;$('sb').onclick=standup;
$('fsf').onclick=()=>{feedMode='failures';setTab('feed');render()};
$('flt').onchange=e=>{feedTask=e.target.value;render()};$('fall').onchange=e=>{allK=e.target.checked;render()};
function setTab(k){document.body.dataset.tab=k;scrollTo(0,0);render()}
document.getElementById('app').addEventListener('click',e=>{const t=e.target;if(t.closest('a'))return;let x;
  if(x=t.closest('[data-fd]'))openDrawer(x.dataset.fd);
  else if(x=t.closest('[data-pf]')){pf=pf===x.dataset.pf?'':x.dataset.pf;ls('pf',pf);render()}
  else if(t.closest('#pfx')){pf='';ls('pf','');render()}
  else if(x=t.closest('[data-ex]')){const s=x.dataset.ex;if(ex[s])delete ex[s];else ex[s]=1;ls('ex',JSON.stringify(ex));render();const o=$((ex[s]?'cx-':'rl-')+s);if(o)o.focus({preventScroll:true})}
  else if(x=t.closest('.rtl button'))rtab(x.dataset.rt,1);
  else if(t.closest('#sbt'))toggleSb();
  else if(x=t.closest('[data-k]')){const nd=x.closest('.nd');openPanel(x.dataset.k,nd?Object.values(tasks).filter(y=>y.needs_you&&y.brief).sort((a,b)=>act(b)-act(a)).map(y=>y.key):[],nd?'rv-'+x.dataset.k:'')}
  else if(x=t.closest('[data-at]')){const k=x.dataset.at;filter=filter===k?null:k;if(filter&&matchMedia('(max-width:699px)').matches)document.body.dataset.tab='board';render()}
  else if(t.closest('#cf')){filter=null;render()}
  else if(t.closest('#er')){earlier=!earlier;render()}
  else if(x=t.closest('[data-h]')){hist=x.dataset.h;render()}
  else if(x=t.closest('[data-f]')){feedMode=x.dataset.f;render()}
  else if(x=t.closest('[data-d]')){dens=x.dataset.d;ls('dens',dens);render()}
  else if(x=t.closest('[data-tb]'))setTab(x.dataset.tb)});
document.getElementById('app').addEventListener('keydown',e=>{const r=e.target,d=r.dataset||{};
  if((e.key==='Enter'||e.key===' ')&&r.getAttribute('role')==='button'&&(d.k||d.fd)){e.preventDefault();d.fd?openDrawer(d.fd):openPanel(d.k)}
  else if(r.getAttribute('role')==='tab'&&/^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)){e.preventDefault();rtab(e.key==='Home'?'ov':e.key==='End'?'ac':rt==='ov'?'ac':'ov',1)}});
document.addEventListener('keydown',e=>{
  const typing=/TEXTAREA|SELECT/.test(e.target.tagName)||e.target.tagName==='INPUT'&&e.target.type!=='checkbox';
  if(e.key==='Escape'){if(dopen){e.preventDefault();closeDlg()}else if(xid!==null){e.preventDefault();closeDrawer()}else if(sel&&pr.open){e.preventDefault();pr.open=0;paint();const f=$('rv-rb');if(f)f.focus()}else if(sel){e.preventDefault();closePanel()}else if(qf||document.body.classList.contains('so')||qv)clearSearch();else if(filter||pf){if(filter)filter=null;else{pf='';ls('pf','')}render()}return}
  if(typing||e.metaKey||e.ctrlKey||e.altKey||dopen)return;
  if(e.key==='/'){e.preventDefault();openSearch();return}
  if(e.key==='?'){e.preventDefault();help();return}
  if(sel){const t=tasks[sel],k=e.key.toLowerCase(),b=t&&t.brief&&ptab==='rv'?$('rv-opts').querySelectorAll('button'):[];
    if(k==='j'||k==='k')qnav(k==='j'?1:-1);
    else if(b.length&&/^[1-5r]$/.test(k)){const x=k==='r'?$('rv-rb'):b[+k-1];if(x&&!x.disabled&&(k==='r'||x.dataset.opt)){e.preventDefault();x.click()}}
    return}
  if(xid!==null)return;
  if(e.key===']'){if(matchMedia('(min-width:1100px)').matches){e.preventDefault();toggleSb()}return}
  if(e.key==='n'){if(matchMedia('(max-width:699px)').matches&&document.body.dataset.tab!=='now')setTab('now');const r=document.querySelector('[data-needs]');if(r){e.preventDefault();r.focus()}else toast('Nothing needs you.');return}
  const mv={j:1,k:-1,ArrowDown:1,ArrowUp:-1,h:'L',ArrowLeft:'L',l:'R',ArrowRight:'R'};if(!(e.key in mv))return;
  const cs=[...document.querySelectorAll('[data-col]')].filter(x=>x.offsetParent),cu=cs.indexOf(document.activeElement),d=mv[e.key];
  if(!cs.length||(cu<0&&e.key.length>1))return;e.preventDefault();if(cu<0){cs[0].focus();return}
  if(typeof d==='number'){(cs[cu+d]||cs[cu]).focus();return}
  const c=+cs[cu].dataset.col,r=+cs[cu].dataset.row,ks=[...new Set(cs.map(x=>+x.dataset.col))],ci=ks.indexOf(c)+(d==='R'?1:-1);
  if(ci<0||ci>=ks.length)return;const tg=cs.filter(x=>+x.dataset.col===ks[ci]);tg[Math.min(r,tg.length-1)].focus()},true);
const dirty=()=>{const t=tasks[sel];return!!(t&&dr&&(dr.status!==t.status||dr.waiting!==t.waiting||dr.next!==t.next))};
function edit(){const d=dirty();$('psv').disabled=!d||saving;$('psv').classList.toggle('p',d);$('psv').textContent=saving?'Saving…':'Save';$('pdc').hidden=!d;$('phn').textContent=d?'Unsaved changes':'';$('pnb').disabled=!$('pn').value.trim()}
function setEdit(){for(const [id,v] of [['pst',dr.status],['pw',dr.waiting],['pnx',dr.next]])if(document.activeElement!==$(id))$(id).value=v;edit()}
$('pst').innerHTML=S.map(s=>'<option value="'+s+'">'+ST[s][0]+'</option>').join('');
$('pw').innerHTML=['me','agent','qc','po','none',''].map(w=>'<option value="'+w+'">'+WT[w]+'</option>').join('');
$('pst').onchange=e=>{dr.status=e.target.value;edit()};$('pw').onchange=e=>{dr.waiting=e.target.value;edit()};$('pnx').oninput=e=>{dr.next=e.target.value;edit()};
$('pdc').onclick=()=>{const t=tasks[sel];dr={status:t.status,waiting:t.waiting,next:t.next};setEdit();$('pst').focus()};
$('psv').onclick=async()=>{const t=tasks[sel],k=sel,b={key:k};if(!t||!dirty())return;for(const f of['status','waiting','next'])if(dr[f]!==t[f])b[f]=dr[f];saving=true;edit();
  try{const o=await api('/api/task',b);tasks[k]=o;toast(k+' saved')}catch(e){toast('Could not save: '+e.message,1)}saving=false;edit();loop()};
const addNote=async()=>{const v=$('pn').value.trim();if(!v)return;try{await api('/api/task',{key:sel,note:v});$('pn').value='';edit();toast('Note added');loop()}catch(e){toast('Could not add the note: '+e.message,1)}};
$('pnb').onclick=addNote;$('pn').oninput=edit;$('pn').onkeydown=e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();addNote()}};
$('par').onclick=()=>openDlg({title:'Archive '+esc(sel)+'?',body:'It leaves the board and search. Its agents, events and notes are kept.',cancel:'Cancel',focus:'dx',
  ok:{label:'Archive',c:'d',fn:async()=>{const k=sel;try{await api('/api/archive',{key:k});closeDlg();closePanel();toast(k+' archived');loop()}catch(e){closeDlg();toast('Could not archive: '+e.message,1)}}}});
$('tlb').onclick=()=>{tlAll=!tlAll;paint()};
$('pdb').onclick=e=>{if(!tasks[sel])return;let x;
  if(x=e.target.closest('[data-opt]'))decide(sel,{action:'option',option:x.dataset.opt},x.dataset.lb);
  else if(e.target.closest('#rv-rb')){pr.open=pr.open?0:1;paint();const f=$(pr.open?'rv-rt':'rv-rb');if(f)f.focus()}
  else if(e.target.closest('#rv-rs')){const v=$('rv-rt').value.trim();if(!v)toast('Write a reply first.',1);else decide(sel,{action:'reply',text:v})}
  else if(e.target.closest('#rv-rx')){pr.open=0;paint();$('rv-rb').focus()}};
$('pdb').onkeydown=e=>{if(e.target.id==='rv-rt'&&e.key==='Enter'&&(e.ctrlKey||e.metaKey)){e.preventDefault();$('rv-rs').click()}};
$('rv-md').onchange=e=>{if(e.target.type!=='checkbox')return;ls('ck:'+curCk,JSON.stringify([...$('rv-md').querySelectorAll('input')].map(x=>x.checked)));ckCount()};
function ckCount(){const a=[...$('rv-md').querySelectorAll('input')],n=$('ckn');if(n)n.textContent=a.length?a.filter(x=>x.checked).length+' of '+a.length+' checked':''}
$('pl').onclick=e=>{if(e.target.closest('#cwt')&&tasks[sel])copy(tasks[sel].worktree,'Path copied')};
const kidsOf=t=>t&&t.children&&t.children.length;
function ptabApply(){const t=tasks[sel],b=!!(t&&t.brief),c=!!kidsOf(t);if(ptab==='rv'&&!b||ptab==='st'&&!c)ptab=c?'st':'dt';$('ptb').hidden=!(b||c);$('ptab-rv').hidden=!b;$('ptab-st').hidden=!c;
  document.querySelectorAll('#ptb [role=tab]').forEach(x=>{const on=x.dataset.pt===ptab;x.setAttribute('aria-selected',on);x.tabIndex=on?0:-1});$('pv-rev').hidden=ptab!=='rv';$('pv-st').hidden=ptab!=='st';$('pv-det').hidden=ptab!=='dt';$('pdb').hidden=!(b&&ptab==='rv')}
$('ptb').onclick=e=>{const x=e.target.closest('[data-pt]');if(x){ptab=x.dataset.pt;ptabApply();$('pbody').scrollTop=0}};
$('ptb').onkeydown=e=>{if(/^(ArrowLeft|ArrowRight|Home|End)$/.test(e.key)){e.preventDefault();const v=[...document.querySelectorAll('#ptb [role=tab]')].filter(x=>!x.hidden).map(x=>x.dataset.pt),i=v.indexOf(ptab);
  ptab=v[e.key==='Home'?0:e.key==='End'?v.length-1:(i+(e.key==='ArrowRight'?1:-1)+v.length)%v.length];ptabApply();$('pbody').scrollTop=0;$('ptab-'+ptab).focus()}};
$('pv-st').onclick=e=>{const b=e.target.closest('[data-k],[data-pf]');if(!b)return;
  if(b.dataset.pf){const was=pf===b.dataset.pf;pf=was?'':b.dataset.pf;ls('pf',pf);if(was){paint();render()}else closePanel()}else openPanel(b.dataset.k,[],'')};
function qnav(d){const i=queue.indexOf(sel),n=queue[i+d];if(i>=0&&n&&tasks[n])openPanel(n,queue)}
$('pqp').onclick=()=>qnav(-1);$('pqx').onclick=()=>qnav(1);
$('pc').onclick=()=>closePanel();$('bd').onclick=()=>xid!==null?closeDrawer():closePanel();
function openPanel(k,q,op,deepLink){const t=tasks[k];if(!t)return;if(xid!==null)closeDrawer(1);
  if(!sel){const a=document.activeElement;opener=op||(a&&a.id&&a!==document.body?a.id:'t-'+k);document.body.classList.add('dlg-open');if(!deepLink){history.pushState(null,'','#'+encodeURIComponent(k));pushed=true}}
  else history.replaceState(null,'','#'+encodeURIComponent(k));
  sel=k;queue=q||[];pr={open:0};ptab=kidsOf(t)?'st':t.brief?'rv':'dt';dr={status:t.status,waiting:t.waiting,next:t.next};tlAll=false;pev=[];$('pn').value='';$('pbody').scrollTop=0;
  $('panel').hidden=$('bd').hidden=false;setEdit();paint();loadPanel();render();setTimeout(()=>$('pc').focus(),30)}
function hide(){const k=sel;sel=null;queue=[];$('panel').hidden=$('bd').hidden=true;document.body.classList.remove('dlg-open');render();const o=$(opener)||$('t-'+k);if(o)o.focus({preventScroll:true})}
function closePanel(){if(!sel)return;if(pushed){pushed=false;history.back()}else{history.replaceState(null,'',location.pathname+location.search);hide()}}
onpopstate=()=>{pushed=false;if(sel)hide()};
async function loadPanel(){const k=sel;if(!k||!tasks[k])return;try{pev=await api('/api/task/'+encodeURIComponent(k)+'/events')}catch(e){}if(sel===k)paint()}
function paint(){const k=sel,t=tasks[k];if(!t)return;
  if(!dirty()){dr={status:t.status,waiting:t.waiting,next:t.next};setEdit()}else edit();
  const m=safeUrl(t.mr_url),w=t.waiting&&t.waiting!=='none'&&t.status!=='done'?t.waiting==='qc'?'QC':t.waiting==='po'?'PO':t.waiting:'';
  $('pk').textContent=t.key;$('p-title').textContent=t.title;
  put('pp',pill(t.status)+(t.needs_you?nwb()+tm0('for ',t.brief_at||t.updated_at||t.created_at||0):w?'<span class="bg">waiting: '+esc(w)+'</span>':''));
  if(kidsOf(t)){const kids=t.children.map(x=>tasks[x]).filter(Boolean);
    put('stl','<div class="ar stf"><span class="sm">'+t.progress.done+' of '+t.progress.total+' done</span><span class="sp"></span><button type="button" class="bt" data-pf="'+esc(k)+'">'+(pf===k?'Show all tasks':'Show only these')+'</button></div>'+
      kids.map(x=>{const xm=safeUrl(x.mr_url),xw=x.waiting&&x.waiting!=='none'&&x.status!=='done'?x.waiting==='qc'?'QC':x.waiting==='po'?'PO':x.waiting:'';
        return'<div class="stk"><div class="col2"><div class="cc"><span class="bg kc">'+esc(x.key)+'</span>'+pill(x.status)+(x.needs_you?nwb():xw?'<span class="bg">waiting: '+esc(xw)+'</span>':'')+(xm?mrc(xm):'')+'</div><span class="tt2">'+esc(x.title)+'</span>'+(x.next?'<span class="sm">Next: '+esc(x.next)+'</span>':'')+'</div>'+
        '<button type="button" class="bt'+(x.needs_you?' p':'')+'" data-k="'+esc(x.key)+'">'+(x.brief?'Open review':'Open task')+'</button></div>'}).join(''))}
  put('pl',pch(t)+(m?mrc(m):'')+(t.branch?'<span class="bg br" title="Branch">'+esc(t.branch)+'</span>':'')+(t.worktree?'<button type="button" class="bt" id="cwt">'+ic('copy',14)+'Copy worktree path</button>':''));
  const pq=queue.indexOf(k);$('pqw').hidden=!(pq>=0&&queue.length>1);$('pq').textContent='Review '+(pq+1)+' of '+queue.length;
  if(t.brief){const op=(t.brief_options||[]).slice().sort((a,b)=>!!b.primary-!!a.primary),d=decided(t);
    const ckk=k+':'+t.brief_at;if(ckk!==curCk){curCk=ckk;try{ck0=JSON.parse(ls('ck:'+ckk)||'[]')}catch(e){ck0=[]}}
    $('rv-at').textContent='Review posted '+when(t.brief_at);put('rv-md',md(t.brief,ck0));ckCount();
    put('rv-opts',op.map((o,i)=>'<button type="button" class="bt ob'+(o.primary?' p':'')+'" data-opt="'+esc(o.id)+'" data-lb="'+esc(o.label)+'"'+(d?' disabled':'')+'><span class="kh">'+(i+1)+'</span><span class="ol"><span>'+esc(o.label)+'</span>'+(o.hint?'<small>'+esc(o.hint)+'</small>':'')+'</span></button>').join('')+
      '<button type="button" class="bt ob" id="rv-rb" aria-expanded="'+!!pr.open+'"'+(d?' disabled':'')+'><span class="kh">R</span>Reply</button>');
    put('rv-reply',pr.open&&!d?'<textarea id="rv-rt" rows="3" aria-label="Reply to Claude" placeholder="Reply. Ctrl+Enter sends, Esc cancels."></textarea><div class="ar"><button type="button" class="bt p" id="rv-rs">Send reply</button><button type="button" class="bt g0" id="rv-rx">Cancel</button></div>':'');
    put('rv-st',d?'Decided: '+esc(d.label+(d.text?' - '+d.text.slice(0,80):''))+' · '+hm(d.created_at)+' · '+({pending:'waiting for Claude',delivered:'Claude is on it',done:'Done'})[d.status]:'')}
  ptabApply();
  const u=urls(t).filter(x=>x!==m),v=x=>x||'<span class="mu">-</span>';
  put('pm','<span>Next</span><div><span>'+(t.next?esc(t.next):'No next step set')+'</span>'+(u.length?'<div class="cc">'+u.map(lc).join('')+'</div>':'')+'</div>'+
    '<span>Waiting on</span><span>'+(t.waiting?WT[t.waiting]:'Unset (by status)')+'</span><span>Project</span><span>'+v(esc(t.project))+'</span><span>Branch</span><span class="mo">'+v(esc(t.branch))+'</span>'+
    '<span>Worktree</span><span class="mo">'+v(esc(t.worktree))+'</span>'+
    '<span>Merge req.</span><span>'+(m?'<a class="mo" style="font-weight:600" href="'+esc(m)+'" target="_blank" rel="noopener">'+esc(lab(m))+'</a>':'<span class="mu">None yet</span>')+'</span>'+
    '<span>Started</span><span style="font-variant-numeric:tabular-nums">'+(t.created_at?dayLab(t.created_at)+' '+hm(t.created_at):'-')+'</span>'+
    '<span>Total</span><span style="font-variant-numeric:tabular-nums">'+(t.created_at?(t.end_at?'took '+dur(t.end_at-t.created_at):tm('running ',t.created_at)):'-')+'</span>');
  const as=Object.values(agents).filter(a=>a.task_key===k).sort((a,b)=>b.started_at-a.started_at),c={};for(const a of as){const s=ast(a);c[s]=(c[s]||0)+1}
  $('pas').textContent=['running','stale','done','failed'].filter(s=>c[s]).map(s=>c[s]+' '+s).join(' · ');
  put('pa',as.map(a=>arow(a,0)).join('')||'<span class="mu">No agents have run on this task.</span>');
  const it=pev.filter(e=>tlAll||!noisy(e)).map(norm).concat((t.notes||[]).map(n=>({ts:n.ts,k:'note',s:n.text,x:''}))).sort((a,b)=>b.ts-a.ts),sh=tlAll?it:it.slice(0,8);
  $('tlb').hidden=it.length<=8&&!tlAll;$('tlb').textContent=tlAll?'Show recent':'Show all ('+(pev.length+(t.notes||[]).length)+')';
  let dl='',h='';for(const e of sh){const d=dayLab(e.ts);if(d!==dl){dl=d;h+='<div class="dh">'+esc(d)+'</div>'}h+=evr(e,1)}
  put('ptl',h||'<span class="mu">Nothing yet.</span>');tick()}
function openDrawer(id){const e=events.find(x=>String(x.id)===String(id));if(!e)return;const d=dj(e),n=norm(e);
  if(xid===null){const a=document.activeElement;fback=a&&a.id?a.id:'f-'+id}
  xid=e.id;const cm=d.cmd||n.s.replace(/^\S+ failed: /,''),ol=d.out?d.out.split('\n').length:0;
  $('xk').textContent=e.task_key||'unlinked';$('xt').textContent=dayLab(e.ts)+' '+hms(e.ts);$('xcm').textContent=cm;
  $('xkv').innerHTML='<span>Exit code</span><span class="mo">'+(d.exit!=null?esc(d.exit):'-')+'</span><span>Duration</span><span class="mo">'+(d.dur_ms!=null?(d.dur_ms<1000?d.dur_ms+' ms':dur(d.dur_ms/1000,1)):'-')+'</span><span>Tool</span><span class="mo">'+esc(d.tool||'-')+'</span>';
  $('xoh').textContent=ol?'Output (last '+ol+(ol===1?' line)':' lines)'):'Output';$('xo').textContent=d.out||'No output was captured for this failure.';
  $('xcp').disabled=!d.out;$('xcp').onclick=()=>copy('$ '+cm+'\n'+d.out,'Output copied');
  $('xop').hidden=!e.task_key;$('xop').onclick=()=>openPanel(e.task_key);
  $('xo').scrollTop=0;$('xd').hidden=$('bd').hidden=false;setTimeout(()=>$('xc').focus(),30)}
function closeDrawer(skip){if(xid===null)return;const id=xid;xid=null;$('xd').hidden=true;if(!sel)$('bd').hidden=true;if(skip)return;const o=$(fback)||$('f-'+id);if(o)o.focus({preventScroll:true})}
$('xc').onclick=()=>closeDrawer();$('xd').onkeydown=e=>trap(e,$('xd'));
function rtab(k,f){rt=k;ls('rt',k);$('rail').dataset.rt=k;document.querySelectorAll('.rtl [role=tab]').forEach(b=>{const on=b.dataset.rt===k;b.setAttribute('aria-selected',on);b.tabIndex=on?0:-1;if(on&&f)b.focus()})}
function sbApply(){document.body.classList.toggle('sbc',sbc);const b=$('sbt'),t=(sbc?'Expand':'Collapse')+' sidebar';b.setAttribute('aria-expanded',!sbc);b.setAttribute('aria-label',t);b.title=t+' (])'}
function toggleSb(){sbc=!sbc;ls('sbc',sbc?'1':'0');sbApply()}
const THS=['system','light','dark'];let th='system';
function themeApply(){const r=document.documentElement,b=$('thm');if(th==='system')delete r.dataset.theme;else r.dataset.theme=th;
  b.innerHTML=ic(th==='dark'?'moon':th==='light'?'sun':'sys');b.setAttribute('aria-label','Theme: '+th+'. Switch to '+THS[(THS.indexOf(th)+1)%3]);b.title='Theme: '+th}
$('thm').onclick=()=>{th=THS[(THS.indexOf(th)+1)%3];ls('theme',th);themeApply()};
document.querySelectorAll('[data-i]').forEach(e=>e.outerHTML=ic(e.dataset.i,+e.dataset.z));
dens=ls('dens')==='compact'?'compact':'comfortable';pf=ls('pf')||'';
try{ex=JSON.parse(ls('ex')||'{}')||{}}catch(e){ex={}}
th=THS.includes(ls('theme'))?ls('theme'):'system';themeApply();sbc=ls('sbc')==='1';sbApply();rtab(ls('rt')==='ac'?'ac':'ov');
loop();
</script></body></html>
"""

if __name__ == "__main__":
    main()
