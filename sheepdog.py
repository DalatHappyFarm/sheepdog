#!/usr/bin/env python3
"""sheepdog: a tiny self-hosted task board for AI agent workflows. Standard library only.

  sheepdog.py serve                       run the server (needs SHEEPDOG_TOKEN, or --no-auth)
  sheepdog.py task T2 --title ... --status testing --note "..."
  sheepdog.py agent <id> --task T2 --status running|done|failed
  sheepdog.py archive T2
  sheepdog.py brief T2 --file review.md --option 'merge|Approve and merge|ships it' --primary merge [--cwd DIR]
  sheepdog.py wait [--cwd DIR]            block until the developer decides on the board, print it, exit 0
  sheepdog.py done <id>                   acknowledge a board decision
  sheepdog.py hook                        Claude Code hook: JSON on stdin, always exits 0
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

def env(new, old, default=None):
    """SHEEPDOG_* setting; the old BOARD_* name still works while the new one is unset."""
    return os.environ.get(new) or os.environ.get(old) or default


def data_db():
    """the default database: ~/.local/share/sheepdog/board.db, or the old agent-board folder's file when only that exists."""
    new = os.path.expanduser("~/.local/share/sheepdog/board.db")
    old = os.path.expanduser("~/.local/share/agent-board/board.db")
    if not os.path.exists(new) and os.path.exists(old):
        print("sheepdog: using the old database %s; move it to %s" % (old, new), flush=True)
        return old
    os.makedirs(os.path.dirname(new), exist_ok=True)
    return new


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
    if base.endswith(":"):  # a rewritten root itself ("alpha:"): its children are "alpha:sub"
        return path.startswith(base)
    return path == base or path.startswith(base + "/")


# ---------------------------------------------------------------- privacy: paths and names are rewritten before anything is stored
_NAMECH = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-")  # the characters of a path component
_WORDCH = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")  # the characters of a standalone token
_PATHEND = frozenset(" \t\r\n\"'`<>|;&,()[]{}:*?=\\")  # the characters that end a path in free text
_SKIP_TOKENS = frozenset(("user", "host", "tmp", "root", "localhost"))
# account names that identify nobody (and are ordinary words in paths): the user-name rule skips them; their home paths are still rewritten
_GENERIC_USERS = frozenset(("admin", "administrator", "root", "user", "ubuntu", "debian", "ec2-user", "centos", "fedora", "dev", "developer", "runner", "git",
                            "www-data", "node", "app", "vagrant", "pi", "guest", "test"))
_LOWER = {c: c + 32 for c in range(65, 91)}


def _lower(s):
    return s.translate(_LOWER)  # ASCII only: other characters keep their length and place


def _valid_token(t):
    return len(t) >= 3 and _lower(t) not in _SKIP_TOKENS and all(c in _NAMECH for c in t)


class Scrubber:
    """Rewrites every string that can carry a path or a name. Rules, in order: (a) a path under an allowed root -> <rootname>:<relative>
    (the root itself: <rootname>:); (b) the home directory (also /Users/<user>, /home/<user>, the real path of $HOME) -> ~, the rest
    kept; (c) temp dirs (/tmp, /private/tmp, /var/folders/..., $TMPDIR) -> <tmp>/<base name of the rest>; (d) the login user name and
    the host name as standalone tokens -> <user> / <host>. The Go server has the same rules (contract/rewrite-cases.json)."""

    def __init__(self, roots=(), home=None, user=None, host=None, tmpdir=None):
        self.roots = []
        for r in roots:
            r = r.rstrip("/")
            if len(r) > 1 and r.rsplit("/", 1)[1]:
                self.roots.append((r, r.rsplit("/", 1)[1]))
        self.roots.sort(key=lambda x: -len(x[0]))  # stable: longest first
        self.homes, self.tmps, self.tokens = [], [], []

        def add(lst, p):
            p = p.rstrip("/")
            if len(p) > 1 and p[0] == "/" and p not in lst:
                lst.append(p)
        if home:
            add(self.homes, home)
            add(self.homes, os.path.realpath(home))
        if user and all(c in _NAMECH for c in user):
            add(self.homes, "/Users/" + user)
            add(self.homes, "/home/" + user)
            if _valid_token(user) and _lower(user) not in _GENERIC_USERS:
                self.tokens.append((user, "<user>"))
        for h in ((host or ""), (host or "").split(".", 1)[0]):
            if _valid_token(h):
                self.tokens.append((h, "<host>"))
        add(self.tmps, "/private/tmp")
        add(self.tmps, "/tmp")
        if tmpdir:
            add(self.tmps, tmpdir)
            add(self.tmps, os.path.realpath(tmpdir))
        self.homes.sort(key=lambda x: -len(x))
        self.tmps.sort(key=lambda x: -len(x))
        self.tokens.sort(key=lambda x: -len(x[0]))
        enc = lambda p: p.replace("/", "-").replace(".", "-")  # how Claude Code names a folder in its own paths
        self.enc_roots = [(enc(path), name) for path, name in self.roots]
        self.enc_homes = [enc(h) for h in self.homes]

    @classmethod
    def from_env(cls, roots=()):
        home = os.environ.get("HOME") or os.path.expanduser("~")
        user = os.environ.get("USER") or os.environ.get("LOGNAME")
        if not user:
            try:
                import getpass
                user = getpass.getuser()
            except Exception:  # noqa: BLE001
                user = ""
        host = os.environ.get("SHEEPDOG_HOSTNAME")
        if not host:
            import socket
            host = socket.gethostname()
        return cls(roots, home, user, host, os.environ.get("TMPDIR"))

    @staticmethod
    def _left_ok(s, i):
        """a home, temp or root path may only start at a real path start: the beginning, whitespace, a quote, or one of > = ( [ { , ; | & :
        (also a URL's "file:///" and the JSON escapes \\n \\r \\t of stored JSON text). Never after a word character or - _ . ~ /, nor right
        after <user>, <host> or <tmp>, so "src/tmp", "~/tmp" and "org-<user>/tmp" are not paths to rewrite, and rewriting stays idempotent."""
        if i == 0:
            return True
        p = s[i - 1]
        if p == ">":  # after a tag or a redirect, but not right after one of our own placeholders (the end of a name, not a path start)
            return not (s.endswith("<tmp>", 0, i) or s.endswith("<user>", 0, i) or s.endswith("<host>", 0, i))
        if p in " \t\r\n\"'`=([{,;|&:":
            return True
        if p == "/" and i >= 3 and s[i - 2] == "/" and s[i - 3] == ":":  # file:///tmp/x
            return True
        return p in "nrt" and i >= 2 and s[i - 2] == "\\"

    @staticmethod
    def _right_ok(s, end):
        return end >= len(s) or s[end] == "/" or s[end] not in _NAMECH

    @staticmethod
    def _var_folders(s, i):
        """/var/folders/<a>/<b>[/<one char>] (also behind /private) at i: the end of that prefix, or -1."""
        for pre in ("/private/var/folders/", "/var/folders/"):
            if s.startswith(pre, i):
                i += len(pre)
                break
        else:
            return -1

        def comp(i):
            j = i
            while j < len(s) and s[j] in _NAMECH:
                j += 1
            return j
        j = comp(i)
        if j == i or j >= len(s) or s[j] != "/":
            return -1
        i = j + 1
        j = comp(i)
        if j == i:
            return -1
        i = j
        if i + 2 <= len(s) and s[i] == "/" and s[i + 1] in _NAMECH and (i + 2 == len(s) or s[i + 2] not in _NAMECH) and s[i + 1] not in ".-":
            i += 2  # the single-character level (T, C, 0) that holds the temp files
        return i

    def _match(self, s, i):
        """the path rules at s[i] (a slash): (end, replacement, swallow a following slash) or None."""
        for path, name in self.roots:
            if s.startswith(path, i) and self._right_ok(s, i + len(path)):
                return i + len(path), name + ":", True
        for h in self.homes:
            if s.startswith(h, i) and self._right_ok(s, i + len(h)):
                return i + len(h), "~", False
        tmp_end = self._var_folders(s, i)
        for t in self.tmps:
            if s.startswith(t, i) and i + len(t) > tmp_end:
                tmp_end = i + len(t)
        if tmp_end >= 0 and self._right_ok(s, tmp_end):
            if tmp_end < len(s) and s[tmp_end] == "/":  # keep only the base name of what follows
                j = tmp_end + 1
                while j < len(s) and s[j] not in _PATHEND:
                    j += 1
                base = s[tmp_end + 1:j].rsplit("/", 1)[-1]
                if self.enc_roots or self.enc_homes:
                    base = self._paths(base)  # a clipped dash-encoded folder name can be what is left of the path
                return j, "<tmp>/" + base, False
            return tmp_end, "<tmp>", False
        return None

    def _match_enc(self, s, i):
        """the dash-encoded rules at s[i] (a "-"): an encoded root becomes <rootname>:, an encoded home ~, the rest kept. It starts at a path
        start or right after a "/" and ends at "-", "/" or any other non-name character (so -Users-jdoe2 is not -Users-jdoe)."""
        if not (self._left_ok(s, i) or s[i - 1] == "/"):
            return None

        def right(end):
            return end >= len(s) or s[end] == "-" or s[end] not in _NAMECH
        for path, name in self.enc_roots:
            if s.startswith(path, i) and right(i + len(path)):
                return i + len(path), name + ":"
        for h in self.enc_homes:
            if s.startswith(h, i) and right(i + len(h)):
                return i + len(h), "~"
        return None

    def _paths(self, s):
        out, last, i = [], 0, 0
        enc = bool(self.enc_roots or self.enc_homes)
        while True:
            k = s.find("/", i)
            if enc:
                k2 = s.find("-", i)
                if k2 >= 0 and (k < 0 or k2 < k):
                    k = k2
            if k < 0:
                break
            i = k
            if s[i] == "-":
                m = self._match_enc(s, i)
                if m is None:
                    i += 1
                    continue
                out.append(s[last:i])
                out.append(m[1])
                last = i = m[0]
                continue
            if not self._left_ok(s, i):
                i += 1
                continue
            m = self._match(s, i)
            if m is None:
                i += 1
                continue
            end, rep, swallow = m
            out.append(s[last:i])
            out.append(rep)
            if swallow and end < len(s) and s[end] == "/":
                end += 1
            last = i = end
        if last == 0:
            return s
        out.append(s[last:])
        return "".join(out)

    @staticmethod
    def _token(s, tok, rep):
        if len(s) < len(tok):
            return s
        low, t = _lower(s), _lower(tok)
        out, last, i = [], 0, 0
        while True:
            j = low.find(t, i)
            if j < 0:
                break
            e = j + len(t)
            if (j == 0 or s[j - 1] not in _WORDCH) and (e >= len(s) or s[e] not in _WORDCH):
                out.append(s[last:j])
                out.append(rep)
                last = i = e
            else:
                i = j + 1
        if last == 0:
            return s
        out.append(s[last:])
        return "".join(out)

    def _names(self, s):
        for tok, rep in self.tokens:
            s = self._token(s, tok, rep)
        return s

    def text(self, s):
        """paths and names in free text."""
        return self._names(self._paths(s))

    def cwd(self, raw):
        """a folder: symlinks resolved first, so a brief's folder and a hook's folder end up in the same form and delivery still matches by prefix."""
        if not raw.startswith("/"):
            return self.text(raw)
        real = os.path.realpath(raw)
        for path, name in self.roots:
            if under(real, path):
                return self._names(name + ":" + real[len(path):].lstrip("/"))
        return self.text(real)

    _IDENT = frozenset(("hook_event_name", "tool_name", "session_id", "tool_use_id", "agent_id", "notification_type"))

    def _value(self, v):
        if isinstance(v, str):
            return self.text(v)
        if isinstance(v, list):
            return [self._value(x) for x in v]
        if isinstance(v, dict):
            return {k: self._value(x) for k, x in v.items()}
        return v

    def payload(self, p):
        """a copy of a hook payload with every string rewritten (not the identifiers); the top-level cwd goes through cwd()."""
        out = {}
        for k, v in p.items():
            if k in self._IDENT:
                out[k] = v
            elif k == "cwd" and isinstance(v, str):
                out[k] = self.cwd(v)[:500]
            else:
                out[k] = self._value(v)
        return out


def base_of(cwd):
    """the project name for a stored folder: its last component ("alpha:" is the root itself, named alpha)."""
    c = cwd.rstrip("/")
    if c.endswith(":"):
        return c[:-1].rsplit("/", 1)[-1]
    return c[max(c.rfind("/"), c.rfind(":")) + 1:]


def parse_allow(value, extra=()):
    """the hook allowlist: folder roots from SHEEPDOG_ALLOW (separated by os.pathsep) plus --allow flags, ~ expanded, symlinks resolved."""
    parts = (value or "").split(os.pathsep) + list(extra or [])
    return [os.path.realpath(os.path.expanduser(x)) for x in (p.strip() for p in parts) if x]


def allowed(roots, cwd):
    """a hook from this folder is accepted: no allowlist, no cwd at all, or the real cwd is a root or inside one (never by base name)."""
    if not roots or not cwd:
        return True
    return cwd.startswith("/") and any(under(os.path.realpath(cwd), r) for r in roots)


def decision_block(d):
    """the text a Claude session sees for one decision (same for `wait` and the hook)."""
    return "\n".join(["BOARD DECISION #%d %s: %s" % (d["id"], d["task_key"], d["label"])] + ([d["text"]] if d["text"] else []) +
                     ["(ack: sheepdog.py done %d)" % d["id"]])


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
    ("GET", r"/brand/(?P<name>[^/]+)", "brand", "the four brand PNGs (fixed list, no sign-in: the sign-in page uses them)"),
    ("POST", r"/login", "login", "token form, sets the cookie"),
]
HOOK_EVENTS = {  # the hook events DB.ingest handles; a test keeps this in step with the code
    "SessionStart": "logged", "SessionEnd": "logged", "Stop": "turn finished, logged", "Notification": "logged",
    "UserPromptSubmit": "logged; also hands over waiting decisions for the prompt's folder",
    "SubagentStart": "agent starts, or is resumed (status running)", "SubagentStop": "agent done",
    "PreToolUse": "Agent spawns an agent row; SendMessage re-links a resumed agent; other tools ignored",
    "PostToolUse": "agent finished, or an ordinary tool counted and logged (SHEEPDOG_TOOL_EVENTS=0 skips it)",
    "PostToolUseFailure": "failed command: command, last 40 output lines (redacted), exit code, duration",
}
WEB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")  # the page files shared with the Go server; see web/README.md


def web(name):
    with open(os.path.join(WEB, name), encoding="utf-8", newline="") as f:
        return f.read()


def fill(tpl, **vals):
    """replace each {{NAME}} that is a key of vals, in one pass (inserted text is never rescanned); any other {{...}} stays as is."""
    return re.sub(r"\{\{(%s)\}\}" % "|".join(vals), lambda m: vals[m.group(1)], tpl)


THEME_JS = web("theme.js")  # the stored System/Light/Dark choice, applied before first paint; every page wraps it in <script>
ARCH_DIAGRAM = web("arch-diagram.html")
ARCH_TPL = web("arch.html").replace("\n", "")  # arch.html: its newlines are formatting only, not part of the page
BASE_CSS = web("base.css")  # the board's own tokens and base type, shared by the board and architecture pages


BRAND = {"icon-126.png", "icon-64.png", "favicon-32.png", "hero.png"}
TEXT_MAX = 20000  # briefs, notes and replies; a reply over it is refused, never cut


def hook_json(decisions):
    """the exact JSON Claude Code reads from a UserPromptSubmit hook (curl prints the reply body verbatim)."""
    return json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                              "additionalContext": "Board decisions waiting for you:\n\n" + "\n\n".join(map(decision_block, decisions))}})


def arch_page(db):
    """GET /architecture: a hand-written diagram and prose, plus parts generated from ROUTES, the live tables and HOOK_EVENTS."""
    e = html.escape
    show = lambda rx: re.sub(r"\(\?P<(\w+)>[^)]*\)", r"<\1>", rx)
    routes = "".join("<tr><td>%s</td><td><code>%s</code></td><td>%s</td></tr>" % (m, e(show(rx)), e(doc)) for m, rx, _n, doc in ROUTES)
    with db.lock:
        tabs = [r["name"] for r in db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        tables = "".join("<tr><td><code>%s</code></td><td>%s</td></tr>" % (t, e(", ".join(c["name"] for c in db.q("PRAGMA table_info(%s)" % t)))) for t in tabs)
    events = "".join("<tr><td><code>%s</code></td><td>%s</td></tr>" % (k, e(v)) for k, v in HOOK_EVENTS.items())
    poll = (re.search(r"setTimeout\(loop,(\d+)\)", PAGE) or [0, 0])[1]
    nums = ["board page %.1f KB of the 90 KB budget" % (len(PAGE.encode()) / 1000), "page polls /api/state every %g s" % (int(poll) / 1000),
            "board state: cached per revision, rebuilt after a write or after 2 s",
            "hook: curl gives up after 2 s (sheepdog hook: 1 s); the reply is empty unless decisions wait",
            "request bodies: %d MB on /api/hook, %d MB elsewhere" % (HOOK_MAX >> 20, BODY_MAX >> 20), "long-poll: at most %d s per call" % WAIT_MAX,
            "an agent silent for %d h shows as stale; events are pruned after 30 days" % (STALE_SECS // 3600)]
    return fill(ARCH_TPL, THEME_JS=THEME_JS, BASE_CSS=BASE_CSS, DIAGRAM=ARCH_DIAGRAM, NUMBERS="".join("<li>%s</li>" % e(n) for n in nums),
                ROUTES=routes, TABLES=tables, EVENTS=events)


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
        self.scrub = Scrubber.from_env()  # rewrites paths and names before anything is stored
        self.allow = []  # hook allowlist (real folder roots); empty = every folder
        self.wait_seen = {}  # cwd -> last time a `sheepdog.py wait` long-poll for it was alive
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
            if w and under(cwd, w) and (best is None or len(w) > len(best[0])):
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
        return (t["project"] if t and t["project"] else base_of(cwd)) if (t or cwd) else ""

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
                # nothing that names the person or the machine is stored: rewrite first, cut after, so a path is never cut where the rules cannot see it
                f[k] = str(d[k]) if k == "worktree" else self.scrub.text(str(d[k]))[:500]  # the worktree is checked raw, then rewritten and cut below
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
        if f.get("worktree"):
            f["worktree"] = self.scrub.cwd(f["worktree"])[:500]
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
        text = redact(self.scrub.text(text))[:TEXT_MAX]
        notes = self.get_task(key)["notes"] + [{"ts": now, "text": text}]
        self.c.execute("UPDATE tasks SET notes=?, updated_at=?, rev=? WHERE key=?", (json.dumps(notes), now, self.bump(), key))
        self.event("note", clip(text), task=key, ts=now)

    def set_brief(self, key, d):
        """a review card: markdown + up to 5 option buttons. Replaces the previous one and hands the task to the developer."""
        brief = redact(self.scrub.text(str(d.get("brief") or ""))).strip()[:TEXT_MAX]
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
            clean.append({"id": o["id"], "label": redact(self.scrub.text(str(o["label"]).strip()))[:80], "hint": redact(self.scrub.text(str(o.get("hint") or "").strip()))[:200],
                          "primary": bool(o.get("primary"))})
        cwd = str(d.get("cwd") or "")
        if cwd and not cwd.startswith("/"):
            raise ValueError("cwd must be an absolute path")
        cwd = self.scrub.cwd(cwd)[:500]  # stored in the same form a hook's folder is compared in
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
        act, text = d.get("action"), redact(self.scrub.text(str(d.get("text") or "").strip()))
        if len(text) > TEXT_MAX:
            raise ValueError("Too long: %d / %d characters" % (len(text), TEXT_MAX))
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
        """is a `sheepdog.py wait` for this folder alive (a long-poll runs up to 55 s; 70 s of silence = gone)?"""
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
                f[k] = self.scrub.text(str(f[k]))[:200]
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
        raw_cwd = str(p.get("cwd") or "")
        if not allowed(self.allow, raw_cwd):
            return []  # outside the allowlist: dropped before anything is stored or changed, the reply is the normal empty one
        # from here on nothing holds a raw path or name; the folder is compared and stored in its rewritten form. The strings are cut only
        # after the rewrite, so a path is never cut where the rules cannot recognise it.
        p = reduce_hook(self.scrub.payload(reduce_hook(p, cut=False)))
        ti = p.get("tool_input") if isinstance(p.get("tool_input"), dict) else {}
        tr = p.get("tool_response") if isinstance(p.get("tool_response"), dict) else {}
        tool = str(p.get("tool_name") or "")
        is_agent_tool = tool in AGENT_TOOLS
        if tool and not is_agent_tool:
            if ev == "PreToolUse":
                if tool == "SendMessage":
                    self.resume_relink(ti, int(time.time()))
                return  # ordinary tools are recorded once, on PostToolUse
            if ev == "PostToolUse" and env("SHEEPDOG_TOOL_EVENTS", "BOARD_TOOL_EVENTS", "1") == "0":
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
        return self.claim(cwd, "hook:" + sid, hook=True) if ev == "UserPromptSubmit" and raw_cwd.startswith("/") else []

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


LOGIN = fill(web("login.html"), THEME_JS=THEME_JS)  # still holds {{ERR}}: "" or the wrong-token line


def _cookie(tok):
    return "board_token=%s; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000" % urllib.parse.quote(tok, safe="")


class Handler(BaseHTTPRequestHandler):
    timeout = 10
    server_version = "sheepdog"

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
        if u.path.startswith("/brand/"):
            return self.r_brand(qs, name=urllib.parse.unquote(u.path[7:]))
        if not self.authed():
            if u.path == "/" and "text/html" in self.headers.get("Accept", ""):
                return self.send(200, LOGIN.replace("{{ERR}}", "").encode(), "text/html")
            return self.send(401, {"error": "unauthorized"} if u.path != "/" else b"unauthorized: open /?token=<SHEEPDOG_TOKEN> once",
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

    def r_brand(self, qs, name):
        if name not in BRAND:  # a fixed list, so no path can leave the folder
            return self.send(404, {"error": "not found"})
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "brand", name), "rb") as f:
            body = f.read()
        self.send_response(200)
        for k, v in (("Content-Type", "image/png"), ("Content-Length", str(len(body))), ("Cache-Control", "max-age=86400"), ("X-Content-Type-Options", "nosniff")):
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

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
        cwd = db.scrub.cwd(cwd)  # the form briefs and hooks are stored and compared in
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
        self.send(401, LOGIN.replace("{{ERR}}", "<p class=err>Wrong token</p>").encode(), "text/html")

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
        got = self.server.db.ingest(d)
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


def make_server(db_path, host="127.0.0.1", port=8787, token=None, allow=None):
    srv = Server((host, port), Handler)
    srv.db = DB(db_path)
    srv.db.allow = list(allow or [])
    srv.db.scrub = Scrubber.from_env(srv.db.allow)
    srv.token = token
    srv.fails = {}
    return srv


def cmd_serve(a):
    token = env("SHEEPDOG_TOKEN", "BOARD_TOKEN")
    if token is None and not a.no_auth:
        sys.exit("SHEEPDOG_TOKEN is required (or pass --no-auth for local dev)")
    dbp = a.db or env("SHEEPDOG_DB", "BOARD_DB") or data_db()
    allow = parse_allow(os.environ.get("SHEEPDOG_ALLOW"), a.allow)
    srv = make_server(dbp, a.host, a.port, None if a.no_auth else token, allow)
    srv.db.prune()
    stop = threading.Event()

    def hourly():
        while not stop.wait(3600):
            try:
                srv.db.prune()
            except Exception:  # noqa: BLE001
                pass

    threading.Thread(target=hourly, daemon=True).start()
    print("sheepdog on http://%s:%d (db %s, auth %s)" % (a.host, srv.server_address[1], dbp, "off" if a.no_auth else "token"), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


# ---------------------------------------------------------------- client side (CLI + hook)
def call(path, payload, timeout=10):
    url = env("SHEEPDOG_URL", "BOARD_URL", "http://127.0.0.1:8787").rstrip("/") + path
    h = {"Content-Type": "application/json"}
    if env("SHEEPDOG_TOKEN", "BOARD_TOKEN"):
        h["Authorization"] = "Bearer " + env("SHEEPDOG_TOKEN", "BOARD_TOKEN")
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


def reduce_hook(p, cut=True):
    """Shrink a hook payload client-side: no tool output, no transcript, only the fields the server uses. With cut=False no string is cut:
    the server rewrites paths and names first and cuts after, so a path is never cut where the rewrite rules cannot recognise it."""
    keep = ("session_id", "cwd", "hook_event_name", "tool_name", "tool_use_id", "agent_id", "agent_type", "source", "model",
            "reason", "message", "notification_type", "duration_ms", "exit_code")
    out = {k: p[k] for k in keep if k in p}
    if "prompt" in p:
        out["prompt"] = str(p["prompt"])[:600] if cut else str(p["prompt"])  # server redacts, then keeps 120
    if "error" in p:
        e = str(p["error"])  # keep the first line (summary, "Exit code N") plus the tail; the server keeps the last 40 lines (redacted, <= 8 KB)
        out["error"] = e if len(e) <= 20000 or not cut else e.split("\n", 1)[0][:300] + "\n" + e[-20000:]
    ti = p.get("tool_input")
    if isinstance(ti, dict):
        out["tool_input"] = {k: ((str(v)[:2000] if cut else str(v)) if k in ("command", "message", "to") else v) for k, v in ti.items()
                             if k in ("command", "file_path", "description", "subagent_type", "model", "pattern", "path", "to", "message")}
    tr = p.get("tool_response")
    if isinstance(tr, dict) and p.get("tool_name") in AGENT_TOOLS:
        out["tool_response"] = {k: tr[k] for k in ("status", "agentId", "totalTokens", "totalToolUseCount", "resolvedModel") if k in tr}
    elif isinstance(tr, dict) and p.get("hook_event_name") == "PostToolUseFailure":  # only a failure ships its output
        out["tool_response"] = {k: (str(tr[k])[-20000:] if cut else str(tr[k])) for k in ("stderr", "stdout", "output", "exit_code") if k in tr}
    return out


def cmd_hook():
    """Never raises, never blocks Claude for more than ~1 s. Prints (valid hook JSON) only when the server hands back board decisions
    for this folder on a UserPromptSubmit; ponytail: if the print fails after the server claimed them, they show as delivered but are lost."""
    try:
        p = reduce_hook(json.loads(sys.stdin.read(4_000_000)), cut=False)  # the server skips what it ignores (ordinary PreToolUse, SHEEPDOG_TOOL_EVENTS=0)
        url = env("SHEEPDOG_URL", "BOARD_URL", "http://127.0.0.1:8787").rstrip("/") + "/api/hook"
        h = {"Content-Type": "application/json"}
        if env("SHEEPDOG_TOKEN", "BOARD_TOKEN"):
            h["Authorization"] = "Bearer " + env("SHEEPDOG_TOKEN", "BOARD_TOKEN")
        resp = urllib.request.urlopen(urllib.request.Request(url, json.dumps(p).encode(), h), timeout=1).read()
        if resp:  # the server's reply is empty, or exactly the hook JSON
            sys.stdout.write(resp.decode("utf-8", "replace"))
            sys.stdout.flush()
    except BaseException:  # noqa: BLE001
        pass


def fetch(path, timeout):
    h = {"Authorization": "Bearer " + env("SHEEPDOG_TOKEN", "BOARD_TOKEN")} if env("SHEEPDOG_TOKEN", "BOARD_TOKEN") else {}
    url = env("SHEEPDOG_URL", "BOARD_URL", "http://127.0.0.1:8787").rstrip("/") + path
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
    ap = argparse.ArgumentParser(description="sheepdog: tiny task board for AI agent workflows",
                                 epilog="Env: SHEEPDOG_TOKEN, SHEEPDOG_URL (client, default http://127.0.0.1:8787), SHEEPDOG_DB, SHEEPDOG_TOOL_EVENTS=0, SHEEPDOG_ALLOW (server: folder roots separated by %s, hooks from elsewhere are dropped; the old BOARD_* names still work)" % os.pathsep)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("serve", help="run the server")
    s.add_argument("--db", help="database file (default: $SHEEPDOG_DB, else ~/.local/share/sheepdog/board.db)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-auth", action="store_true", help="no token (local dev only)")
    s.add_argument("--allow", action="append", default=[], metavar="DIR", help="accept hooks only from DIR and below (repeatable; adds to $SHEEPDOG_ALLOW; none = every folder)")
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


# ---------------------------------------------------------------- the page (web/board.html)
PAGE = fill(web("board.html"), THEME_JS=THEME_JS, BASE_CSS=BASE_CSS)

if __name__ == "__main__":
    main()
