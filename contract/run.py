#!/usr/bin/env python3
"""Contract test for a sheepdog server (Python today, Go next). Standard library only.

  python3 contract/run.py --server "python3 sheepdog.py serve" --record     write contract/golden/*.json
  python3 contract/run.py --server "python3 sheepdog.py serve"              compare with the golden files; exit 1 on any difference
  python3 contract/run.py --server "./sheepdog-go serve" --cli "python3 sheepdog.py"

It starts the server on a free port with a fresh copy of the fixture database, drives one scripted scenario over HTTP and through
the CLI, records {status, content-type, a few headers, normalized body} per step, then dumps the database. Normalization rules:
contract/README.md. The CLI is always a client; by default the Python one next to this folder.
"""
import argparse
import difflib
import hashlib
import http.client
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import ROOT, TOKEN, Server  # noqa: E402
import fixture  # noqa: E402

TS_KEYS = {"ts", "created_at", "updated_at", "started_at", "ended_at", "last_event_at", "done_at", "brief_at", "delivered_at", "end_at"}
REV_KEYS = {"rev", "cursor"}
JSON_COLS = {"notes", "data", "brief_options"}  # columns that hold JSON text
KEEP_HEADERS = ("Cache-Control", "X-Content-Type-Options", "Location", "Set-Cookie", "Retry-After")
SYNTAX = "<json syntax error>"


def norm(v):
    """rules 1 to 3 of contract/README.md: timestamps and revs become placeholders, events' data is compared parsed, nothing else changes."""
    if isinstance(v, dict):
        return {k: (norm(jsonish(x)) if k == "data" and isinstance(x, str) and isinstance(jsonish(x), dict) else
                    "<ts>" if k in TS_KEYS and isinstance(x, int) and not isinstance(x, bool) else
                    "<rev>" if k in REV_KEYS and isinstance(x, int) and not isinstance(x, bool) else norm(x)) for k, x in v.items()}
    if isinstance(v, list):
        return [norm(x) for x in v]
    return v


def jsonish(text):
    try:
        return json.loads(text)
    except ValueError:
        return text


class Rec:
    def __init__(self, server, cli, tmp):
        self.srv, self.cli_cmd, self.tmp = server, cli, tmp
        self.http_steps, self.cli_steps, self.page_diffs, self.n = [], [], [], 0

    def name(self, name):
        self.n += 1
        return "%03d-%s" % (self.n, name)

    # -- HTTP
    def h(self, name, method, path, body=None, auth=True, hdr=None, page=None, syntax=False, token=TOKEN):
        headers = dict(hdr or {})
        if auth:
            headers["Authorization"] = "Bearer " + token
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode()
            headers.setdefault("Content-Type", "application/json")
        c = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=60)
        c.request(method, path, body, headers)
        r = c.getresponse()
        data = r.read()
        c.close()
        ct = r.getheader("Content-Type") or ""
        if ct.startswith("application/json"):
            parsed = json.loads(data) if data else ""
            if syntax and r.status == 400 and isinstance(parsed, dict) and "error" in parsed:
                parsed = dict(parsed, error=SYNTAX)  # rule 4
            shown = norm(parsed)
        elif ct.startswith("text/html") or ct.startswith("image/"):
            shown = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
            if page:
                with open(os.path.join(HERE, "baseline-pages", page), "rb") as f:
                    want = f.read()
                if want != data:
                    self.page_diffs.append("%s differs from baseline-pages/%s (%d vs %d bytes, first difference at byte %d)" % (
                        name, page, len(data), len(want), next((i for i, (a, b) in enumerate(zip(data, want)) if a != b), min(len(data), len(want)))))
            parsed = data
        else:
            parsed = data.decode("utf-8", "replace")
            shown = parsed
        step = {"step": self.name(name), "request": "%s %s" % (method, path), "status": r.status, "content_type": ct,
                "headers": {k: r.getheader(k) for k in KEEP_HEADERS if r.getheader(k) is not None}, "body": shown}
        self.http_steps.append(step)
        return r.status, parsed

    def post(self, name, path, body=None, **kw):
        return self.h(name, "POST", path, {} if body is None else body, **kw)

    def get(self, name, path, **kw):
        return self.h(name, "GET", path, **kw)

    # -- CLI
    def cli_env(self):
        return dict(os.environ, SHEEPDOG_URL=self.srv.url, SHEEPDOG_TOKEN=TOKEN, TZ="UTC")

    def cli(self, name, args, stdin=None):
        p = subprocess.run(self.cli_cmd + args, env=self.cli_env(), input=stdin, capture_output=True, text=True, timeout=60)
        return self.cli_record(name, args, p.returncode, p.stdout, p.stderr, stdin)

    def cli_record(self, name, args, code, out, err, stdin=None):
        step = {"step": self.name(name), "args": " ".join(args).replace(self.tmp, "<tmp>"), "stdin": stdin and stdin[:80], "exit": code,
                "stdout": norm(jsonish(out)) if out.strip() else "", "stderr": err.strip()}
        self.cli_steps.append(step)
        return jsonish(out)

    def md(self, text):
        p = os.path.join(self.tmp, "m%d.md" % self.n)
        with open(p, "w") as f:
            f.write(text)
        return p


def hook(sid="sess-e2e", cwd="/work/e2e", **p):
    return dict({"session_id": sid, "cwd": cwd}, **p)


def scenario(r):
    # ---------------------------------------------------------------- pages and auth
    r.get("board-page", "/", page="board.html")
    r.get("login-page-no-token", "/", auth=False, hdr={"Accept": "text/html"}, page="login.html")
    r.get("root-no-token-plain", "/", auth=False)
    r.get("state-no-token", "/api/state", auth=False)
    r.get("state-bad-token", "/api/state", token="nope")
    r.get("standup-lowercase-bearer", "/api/standup?off=0", auth=False, hdr={"Authorization": "bearer " + TOKEN})
    r.get("token-query-signs-in", "/?token=" + TOKEN, auth=False)
    r.get("token-query-wrong", "/?token=wrong", auth=False)
    r.get("token-query-wrong-html", "/?token=wrong", auth=False, hdr={"Accept": "text/html"}, page="login.html")
    r.get("cookie-ok", "/api/standup?off=0", auth=False, hdr={"Cookie": "other=1; board_token=" + urllib.parse.quote(TOKEN)})
    r.get("cookie-bad", "/api/standup?off=0", auth=False, hdr={"Cookie": "board_token=wrong"})
    r.post("post-no-token", "/api/task", {"key": "X"}, auth=False)
    r.post("post-unknown-no-token", "/api/nope", auth=False)
    r.get("architecture", "/architecture", page="architecture.html")
    r.get("architecture-no-token", "/architecture", auth=False)
    for n in ("icon-126.png", "icon-64.png", "favicon-32.png", "hero.png"):
        r.get("brand-" + n, "/brand/" + n, auth=False)
    r.get("brand-with-token-too", "/brand/icon-64.png", auth=True)
    r.get("brand-unknown", "/brand/nope.png", auth=False)
    r.get("brand-not-listed-file", "/brand/brand-sheet.png", auth=False)
    r.get("brand-traversal-dots", "/brand/../sheepdog.py", auth=False)
    r.get("brand-traversal-encoded", "/brand/..%2Fsheepdog.py", auth=False)
    r.get("brand-traversal-encoded-dots", "/brand/%2e%2e/README.md", auth=False)
    r.get("brand-empty-name", "/brand/", auth=False)
    r.post("login-wrong", "/login", b"token=wrong", auth=False, hdr={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"}, page="login-error.html")
    r.post("login-json-body", "/login", {"token": TOKEN}, auth=False)
    r.post("login-ok", "/login", ("token=" + urllib.parse.quote(TOKEN)).encode(), auth=False, hdr={"Content-Type": "application/x-www-form-urlencoded"})

    # ---------------------------------------------------------------- reads on the fixture
    _, st = r.get("state-full", "/api/state")
    cursor = st["cursor"]
    r.get("state-empty-delta", "/api/state?since=%d" % cursor)
    r.get("state-since-garbage", "/api/state?since=abc")
    r.get("state-since-negative", "/api/state?since=-5")
    r.get("standup-utc", "/api/standup?off=0")
    r.get("standup-default-tz", "/api/standup")
    r.get("standup-garbage-off", "/api/standup?off=abc")
    r.get("events-f3", "/api/task/F3/events")
    r.get("events-t3-from-hook", "/api/task/T3/events")
    r.get("events-unknown-task", "/api/task/NOPE/events")
    r.get("events-escaped-key", "/api/task/F%203/events")
    r.get("unknown-get", "/api/nope")
    r.get("get-on-post-route", "/api/hook")
    r.post("post-on-get-route", "/api/state")
    r.post("post-root", "/")
    r.post("post-unknown", "/api/nope")

    # ---------------------------------------------------------------- tasks
    r.post("task-create", "/api/task", {"key": "N1", "title": "New task", "project": "gamma", "branch": "feat/n1", "worktree": "/work/gamma/n1",
                                        "mr_url": "https://gitlab.example.com/g/p/-/merge_requests/9", "next": "start", "waiting": "agent", "note": "first note"})
    r.post("task-update-status-alias", "/api/task", {"key": "N1", "status": "running"})
    r.post("task-update-same-status", "/api/task", {"key": "N1", "status": "building", "title": "Renamed"})
    r.post("task-done", "/api/task", {"key": "N1", "status": "done", "note": "token=abc123secret all good"})
    r.post("task-reopen", "/api/task", {"key": "N1", "status": "testing"})
    r.post("task-secrets-redacted", "/api/task", {"key": "N1", "next": "use Authorization: Bearer abcdefghijkl123 and password=hunter22", "note": "AKIA" + "ABCDEFGHIJKLMNOP key"})
    r.post("task-minimal", "/api/task", {"key": "N2"})
    r.post("task-title-clip-500", "/api/task", {"key": "N3", "title": "t" * 600})
    r.post("task-notes-clip", "/api/task", {"key": "N3", "note": "n" * 20100})
    r.post("task-waiting-clear", "/api/task", {"key": "N1", "waiting": ""})
    r.post("task-bad-key", "/api/task", {"key": "bad key!"})
    r.post("task-no-key", "/api/task", {})
    r.post("task-bad-waiting", "/api/task", {"key": "N1", "waiting": "nobody"})
    r.post("task-bad-status", "/api/task", {"key": "N1", "status": "flying"})
    r.post("task-relative-worktree", "/api/task", {"key": "N1", "worktree": "rel/path"})
    r.post("task-bad-mr", "/api/task", {"key": "N1", "mr_url": "ftp://x"})
    r.post("task-parent-set", "/api/task", {"key": "N2", "parent": "N1"})
    r.post("task-parent-self", "/api/task", {"key": "N1", "parent": "N1"})
    r.post("task-parent-missing", "/api/task", {"key": "N3", "parent": "ZZZ"})
    r.post("task-parent-is-child", "/api/task", {"key": "N3", "parent": "N2"})
    r.post("task-parent-with-kids-cannot-be-child", "/api/task", {"key": "N1", "parent": "F1"})
    r.post("task-parent-clear", "/api/task", {"key": "N2", "parent": ""})
    r.post("task-new-child-of-fixture-parent", "/api/task", {"key": "C4", "title": "Another sub-task", "status": "analysing", "parent": "P1"})
    r.post("task-empty-body", "/api/task", None)
    r.get("task-get-after", "/api/task/N1/events")

    # ---------------------------------------------------------------- agents
    r.post("agent-create", "/api/agent", {"id": "ag-n1", "task": "N1", "name": "Task 1 worker", "model": "sonnet", "status": "running", "tokens": 100})
    r.post("agent-update-done", "/api/agent", {"id": "ag-n1", "status": "done", "tokens": 250})
    r.post("agent-task-key-alias", "/api/agent", {"id": "ag-n2", "task_key": "N3"})
    r.post("agent-no-task", "/api/agent", {"id": "ag-n3", "name": "Task 5 loose"})
    r.post("agent-failed", "/api/agent", {"id": "ag-n3", "status": "failed"})
    r.post("agent-long-id-and-name", "/api/agent", {"id": "i" * 150, "name": "x" * 300})
    r.post("agent-bad-status", "/api/agent", {"id": "ag-n1", "status": "sleeping"})
    r.post("agent-no-id", "/api/agent", {})
    r.post("agent-unknown-task", "/api/agent", {"id": "ag-n4", "task": "NOPE"})

    # ---------------------------------------------------------------- archive
    r.post("archive-ok", "/api/archive", {"key": "N3"})
    r.post("archive-unknown", "/api/archive", {"key": "NOPE"})
    r.post("archive-no-key", "/api/archive", {})

    # ---------------------------------------------------------------- briefs
    r.post("brief-ok", "/api/task/N1/brief", {"brief": "# Review N1\n\nAll good, token=zzzzzzzz1\n", "cwd": "/work/n1",
                                              "options": [{"id": "merge", "label": "Approve and merge", "hint": "ships it", "primary": True}, {"id": "hold", "label": "Hold"}]})
    r.post("brief-on-done-task-keeps-status", "/api/task/F6/brief", {"brief": "late card"})
    r.post("brief-empty", "/api/task/N1/brief", {"brief": "   "})
    r.post("brief-too-many-options", "/api/task/N1/brief", {"brief": "x", "options": [{"id": "o%d" % i, "label": "L"} for i in range(6)]})
    r.post("brief-options-not-list", "/api/task/N1/brief", {"brief": "x", "options": "merge"})
    r.post("brief-bad-option-id", "/api/task/N1/brief", {"brief": "x", "options": [{"id": "Bad Id", "label": "L"}]})
    r.post("brief-option-no-label", "/api/task/N1/brief", {"brief": "x", "options": [{"id": "ok"}]})
    r.post("brief-duplicate-option", "/api/task/N1/brief", {"brief": "x", "options": [{"id": "a", "label": "A"}, {"id": "a", "label": "B"}]})
    r.post("brief-relative-cwd", "/api/task/N1/brief", {"brief": "x", "cwd": "rel"})
    r.post("brief-unknown-task", "/api/task/NOPE/brief", {"brief": "x"})
    r.post("brief-clipped-to-20000", "/api/task/N2/brief", {"brief": "b" * 20500, "cwd": "/work/n2", "options": [{"id": "go", "label": "L" * 100, "hint": "h" * 300}]})

    # ---------------------------------------------------------------- decisions, wait, done
    _, d = r.post("decision-option", "/api/task/N1/decision", {"action": "option", "option": "hold"})
    _, d = r.post("decision-approve-no-row", "/api/task/F12/decision", {"action": "approve"})
    r.post("decision-unknown-option", "/api/task/N1/decision", {"action": "option", "option": "zzz"})
    r.post("decision-reply-empty", "/api/task/N1/decision", {"action": "reply", "text": "  "})
    r.post("decision-bad-action", "/api/task/N1/decision", {"action": "dance"})
    r.post("decision-no-action", "/api/task/N1/decision", {})
    r.post("decision-reply-too-long", "/api/task/N1/decision", {"action": "reply", "text": "r" * 20001})
    r.post("decision-reply-exactly-max", "/api/task/N2/decision", {"action": "reply", "text": "m" * 20000})
    r.post("decision-unknown-task", "/api/task/NOPE/decision", {"action": "approve"})
    r.post("decision-no-cwd-reply", "/api/task/F1/decision", {"action": "reply", "text": "no brief cwd, only a note"})
    r.get("wait-relative-cwd", "/api/decisions/wait?cwd=rel")
    r.get("wait-no-cwd", "/api/decisions/wait")
    _, got = r.get("wait-claims-under-folder", "/api/decisions/wait?cwd=/work/alpha&timeout=0")  # F4's pending decision from the fixture
    r.get("wait-nothing-left", "/api/decisions/wait?cwd=/work/alpha&timeout=-5")
    r.post("decision-after-waiter-seen", "/api/task/F13/decision", {"action": "reply", "text": "rename it, then merge"})  # waiter is true now
    r.get("wait-claims-second", "/api/decisions/wait?cwd=/work/alpha/f13&timeout=0")
    r.get("events-f13-has-no-notes", "/api/task/F13/events")
    ids = [x["id"] for x in got] + [1]
    r.post("done-ok", "/api/decision/%d/done" % ids[0], {})
    r.post("done-again", "/api/decision/%d/done" % ids[0], {})
    r.post("done-unknown", "/api/decision/99999/done", {})
    r.post("done-fixture-delivered", "/api/decision/2/done", {})

    # ---------------------------------------------------------------- raw hook payloads, one per event type
    r.post("hook-session-start", "/api/hook", hook(hook_event_name="SessionStart", source="startup", model="claude-opus"))
    r.post("hook-session-start-bare", "/api/hook", hook(hook_event_name="SessionStart"))
    r.post("hook-session-end", "/api/hook", hook(hook_event_name="SessionEnd", reason="clear"))
    r.post("hook-prompt-no-decisions", "/api/hook", hook(hook_event_name="UserPromptSubmit", prompt="hello token=abcd1234 world " + "p" * 300))
    r.post("hook-prompt-no-cwd", "/api/hook", {"hook_event_name": "UserPromptSubmit", "session_id": "s-nocwd", "prompt": "x"})
    r.post("task-for-hook-link", "/api/task", {"key": "E1", "title": "Linked by worktree", "worktree": "/work/e2e/e1", "status": "building"})
    r.post("hook-notification", "/api/hook", hook(cwd="/work/e2e/e1/sub", hook_event_name="Notification", message="Claude needs your permission", notification_type="permission_prompt"))
    r.post("hook-stop", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="Stop"))
    r.post("hook-pre-agent", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="Agent", tool_use_id="tu-77",
                                                tool_input={"description": "Task 77 Build the widget", "subagent_type": "general-purpose", "model": "sonnet", "prompt": "ignored"}))
    r.post("hook-pre-agent-repeat", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="Agent", tool_use_id="tu-77",
                                                       tool_input={"description": "Task 77 Build the widget", "subagent_type": "general-purpose"}))
    r.post("hook-subagent-start-adopts", "/api/hook", hook(hook_event_name="SubagentStart", agent_id="ag-77", agent_type="general-purpose"))
    r.post("hook-subagent-start-no-id", "/api/hook", hook(hook_event_name="SubagentStart", agent_type="Explore"))
    r.post("hook-post-bash-counts", "/api/hook", hook(hook_event_name="PostToolUse", agent_id="ag-77", tool_name="Bash", tool_input={"command": "make test token=secretvalue1"},
                                                       tool_response={"stdout": "ok"}))
    r.post("hook-post-edit", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUse", tool_name="Edit", tool_input={"file_path": "/work/e2e/e1/a.go"}))
    r.post("hook-post-grep", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUse", tool_name="Grep", tool_input={"pattern": "TODO"}))
    r.post("hook-post-glob-path", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUse", tool_name="Glob", tool_input={"path": "/work/e2e/e1/src"}))
    r.post("hook-post-no-detail", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUse", tool_name="TodoWrite"))
    r.post("hook-pre-ordinary-ignored", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="Bash", tool_input={"command": "ls"}))
    r.post("hook-pre-sendmessage-relinks", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="SendMessage", tool_input={"to": "ag-77", "message": "Task 78 now do the other thing"}))
    r.post("hook-pre-sendmessage-no-prefix", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="SendMessage", tool_input={"to": "ag-77", "message": "just talk"}))
    r.post("hook-failure-bash", "/api/hook", hook(hook_event_name="PostToolUseFailure", agent_id="ag-77", tool_name="Bash", tool_input={"command": "make build password=swordfish99"},
                                                   error="Exit code 2\n" + "\n".join("line %d token=tok%08d" % (i, i) for i in range(60)),
                                                   tool_response={"stderr": "boom\nsecret=s3cr3tvalue", "stdout": "partial", "exit_code": 2}, duration_ms=4321))
    r.post("hook-failure-exit-from-error", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "false"}, error="Exit code 1"))
    r.post("hook-failure-exit-code-top-level", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "x"}, error="oops", exit_code="9"))
    r.post("hook-failure-string-response", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUseFailure", tool_name="Read", tool_input={"file_path": "/nope"}, tool_response="no such file"))
    r.post("hook-failure-huge-output", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "c" * 3000},
                                                          error="Exit code 3", tool_response={"stderr": "L" * 90000}))
    r.post("hook-pre-agent-bg", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="Agent", tool_use_id="tu-79", tool_input={"description": "Task 79 background job", "subagent_type": "Explore"}))
    r.post("hook-post-agent-async", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-79", tool_input={"description": "Task 79 background job", "subagent_type": "Explore"},
                                                       tool_response={"status": "async_launched", "agentId": "ag-79"}))
    r.post("hook-post-agent-done", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-79", tool_input={"description": "Task 79 background job", "subagent_type": "Explore"},
                                                      tool_response={"status": "completed", "agentId": "ag-79", "totalTokens": 4242, "totalToolUseCount": 7, "resolvedModel": "claude-sonnet"}))
    r.post("hook-pre-agent-fails", "/api/hook", hook(hook_event_name="PreToolUse", tool_name="Task", tool_use_id="tu-80", tool_input={"description": "Task 80 doomed", "model": "haiku"}))
    r.post("hook-post-agent-failure", "/api/hook", hook(hook_event_name="PostToolUseFailure", tool_name="Task", tool_use_id="tu-80", tool_input={"description": "Task 80 doomed", "model": "haiku"}, error="boom"))
    r.post("hook-post-agent-without-pre", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-81", tool_input={"description": "Task 81 late hook", "subagent_type": "general-purpose"},
                                                             tool_response={"status": "completed", "agentId": "ag-late", "totalTokens": 5}))
    r.post("hook-subagent-stop", "/api/hook", hook(hook_event_name="SubagentStop", agent_id="ag-77", agent_type="general-purpose"))
    r.post("hook-unknown-event", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PreCompact", trigger="auto"))
    r.post("hook-tool-input-not-dict", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Bash", tool_input="ls", tool_response=[1]))
    r.post("hook-agent-id-not-string", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Bash", agent_id=5, tool_input={"command": "x"}))
    r.post("hook-missing-event-name", "/api/hook", hook(tool_name="Bash"))
    r.post("hook-empty-body", "/api/hook", None)
    r.post("task-for-hook-delivery", "/api/task", {"key": "E2", "title": "Decision by prompt hook", "worktree": "/work/e3/e2"})
    r.post("brief-for-hook-delivery", "/api/task/E2/brief", {"brief": "## E2\nship it?", "cwd": "/work/e3", "options": [{"id": "ship", "label": "Ship", "primary": True}]})
    r.post("decision-for-hook-delivery", "/api/task/E2/decision", {"action": "option", "option": "ship"})
    r.post("hook-prompt-parent-folder-no-delivery", "/api/hook", hook(sid="s-up", cwd="/work", hook_event_name="UserPromptSubmit", prompt="from the parent folder"))
    r.post("hook-prompt-sibling-no-delivery", "/api/hook", hook(sid="s-sib", cwd="/work/e3x", hook_event_name="UserPromptSubmit", prompt="from a sibling folder"))
    r.post("hook-prompt-delivers-decision", "/api/hook", hook(sid="s-deliver", cwd="/work/e3/sub/dir", hook_event_name="UserPromptSubmit", prompt="do the next thing"))
    r.post("hook-prompt-delivers-only-once", "/api/hook", hook(sid="s-deliver-2", cwd="/work/e3", hook_event_name="UserPromptSubmit", prompt="again"))
    r.get("events-t77", "/api/task/T77/events")
    r.get("events-t78", "/api/task/T78/events")
    r.get("events-e1", "/api/task/E1/events")
    r.get("events-e2", "/api/task/E2/events")

    # ---------------------------------------------------------------- request body limits and syntax errors
    r.post("bad-json", "/api/task", b"{not json", syntax=True)
    r.post("bad-json-hook", "/api/hook", b"nope", syntax=True)
    r.post("json-array-body", "/api/task", [1, 2])
    r.post("json-scalar-body", "/api/agent", b"5")
    r.post("body-over-1mb", "/api/task", b'{"key":"BIG","title":"' + b"x" * (1 << 20) + b'"}')
    r.post("hook-body-1mb-ok", "/api/hook", hook(cwd="/work/e2e/e1", hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "x"}, error="y" * (1 << 20)))
    r.post("hook-body-over-8mb", "/api/hook", b'{"hook_event_name":"Stop","pad":"' + b"x" * (8 << 20) + b'"}')

    # ---------------------------------------------------------------- the state after all that
    _, st2 = r.get("state-delta-since-first-cursor", "/api/state?since=%d" % cursor)
    r.get("state-full-end", "/api/state")
    r.get("standup-end", "/api/standup?off=0")
    r.get("architecture-end", "/architecture")

    # ---------------------------------------------------------------- the CLI (client side of the same routes)
    cli_scenario(r)

    # ---------------------------------------------------------------- login throttle (last: five failures per address per minute)
    for i in range(3):
        r.post("login-wrong-%d" % (i + 3), "/login", b"token=wrong", auth=False, hdr={"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"})
    r.post("login-sixth-attempt-throttled", "/login", b"token=wrong", auth=False, hdr={"Content-Type": "application/x-www-form-urlencoded"})
    r.post("login-right-token-also-throttled", "/login", ("token=" + urllib.parse.quote(TOKEN)).encode(), auth=False, hdr={"Content-Type": "application/x-www-form-urlencoded"})


def cli_scenario(r):
    r.cli("task-create", ["task", "G1", "--title", "CLI task", "--project", "gamma", "--branch", "feat/g1", "--worktree", "/work/gamma/g1",
                          "--mr", "https://gitlab.example.com/g/p/-/merge_requests/11", "--next", "begin", "--waiting", "agent", "--note", "created by the CLI"])
    r.cli("task-status-running", ["task", "G1", "--status", "running"])
    r.cli("task-parent", ["task", "G2", "--title", "CLI sub-task", "--parent", "G1"])
    r.cli("task-parent-clear", ["task", "G2", "--parent", ""])
    r.cli("task-server-error", ["task", "G1", "--worktree", "relative"])
    r.cli("agent-create", ["agent", "cli-ag", "--task", "G1", "--name", "Task 1 cli agent", "--model", "opus", "--status", "running"])
    r.cli("agent-done", ["agent", "cli-ag", "--status", "done"])
    r.cli("agent-server-error", ["agent", "cli-ag2", "--task", "NOPE"])
    brief = r.md("# Review G1\n\nLooks fine.\n\n- [ ] item\n")
    r.cli("brief-options", ["brief", "G1", "--file", brief, "--option", "go|Go ahead|ships it", "--option", "stop|Stop", "--option", "rework|Rework|needs changes",
                            "--primary", "go", "--cwd", "/work/gamma/g1"])
    r.cli("brief-stdin", ["brief", "G2", "--file", "-", "--cwd", "/work/gamma/g2"], stdin="## from stdin\nbody\n")
    r.cli("brief-bad-primary", ["brief", "G1", "--file", brief, "--option", "go|Go", "--primary", "nope"])
    r.cli("brief-too-many", ["brief", "G1", "--file", brief] + sum([["--option", "o%d|L" % i] for i in range(6)], []))
    r.cli("brief-unknown-task", ["brief", "NOPE", "--file", brief])
    # wait: the long-poll starts first, the developer then decides on the board, the poll delivers it
    p = subprocess.Popen(r.cli_cmd + ["wait", "--cwd", "/work/gamma"], env=r.cli_env(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(1.5)
    _, dec = r.post("cli-wait-decision-while-waiting", "/api/task/G1/decision", {"action": "option", "option": "go"})
    out, err = p.communicate(timeout=30)
    r.cli_record("wait-delivers-decision", ["wait", "--cwd", "/work/gamma"], p.returncode, out, err)
    did = int(out.split("#", 1)[1].split()[0])
    r.cli("done", ["done", str(did)])
    r.cli("done-unknown", ["done", "99999"])
    r.cli("archive", ["archive", "G2"])
    r.cli("archive-unknown", ["archive", "NOPE"])
    # the hook command: reduces the payload, posts it, prints the reply only when decisions wait, always exits 0
    r.post("cli-hook-task", "/api/task", {"key": "H1", "title": "Hook CLI task", "worktree": "/work/hk/h1"})
    r.post("cli-hook-brief", "/api/task/H1/brief", {"brief": "## H1\ngo?", "cwd": "/work/hk", "options": [{"id": "yes", "label": "Yes"}]})
    r.post("cli-hook-decision", "/api/task/H1/decision", {"action": "reply", "text": "yes, and add a test"})
    r.cli("hook-prompt-delivers", ["hook"], stdin=json.dumps(hook(sid="s-cli", cwd="/work/hk/h1", hook_event_name="UserPromptSubmit", prompt="next", transcript_path="/x")))
    r.cli("hook-prompt-second-time-empty", ["hook"], stdin=json.dumps(hook(sid="s-cli", cwd="/work/hk/h1", hook_event_name="UserPromptSubmit", prompt="next")))
    r.cli("hook-session-start", ["hook"], stdin=json.dumps(hook(cwd="/work/hk/h1", hook_event_name="SessionStart", source="resume", model="m", transcript_path="/x")))
    r.cli("hook-post-bash", ["hook"], stdin=json.dumps(hook(cwd="/work/hk/h1", hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "go test ./...", "timeout": 5, "junk": 1},
                                                           tool_response={"stdout": "x" * 5000})))
    r.cli("hook-failure-bash", ["hook"], stdin=json.dumps(hook(cwd="/work/hk/h1", hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "go vet"},
                                                              error="Exit code 4\n" + "e" * 25000, tool_response={"stderr": "s" * 30000, "exit_code": 4}, duration_ms=88)))
    r.cli("hook-pre-agent", ["hook"], stdin=json.dumps(hook(cwd="/work/hk/h1", hook_event_name="PreToolUse", tool_name="Agent", tool_use_id="tu-cli",
                                                           tool_input={"description": "Task 90 via cli hook", "subagent_type": "general-purpose", "prompt": "dropped"})))
    r.cli("hook-post-agent", ["hook"], stdin=json.dumps(hook(cwd="/work/hk/h1", hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-cli",
                                                            tool_input={"description": "Task 90 via cli hook"}, tool_response={"status": "completed", "agentId": "ag-cli", "totalTokens": 9, "extra": "dropped"})))
    r.cli("hook-pre-ordinary-ignored", ["hook"], stdin=json.dumps(hook(hook_event_name="PreToolUse", tool_name="Read", tool_input={"file_path": "/x"})))
    r.cli("hook-subagent-stop", ["hook"], stdin=json.dumps(hook(hook_event_name="SubagentStop", agent_id="ag-cli", agent_type="general-purpose")))
    r.cli("hook-garbage-stdin-exits-0", ["hook"], stdin="not json at all")
    r.cli("hook-empty-stdin-exits-0", ["hook"], stdin="")
    r.get("cli-state-end", "/api/state")


# ---------------------------------------------------------------- database dump
def dump_db(path):
    """schema (sql with whitespace collapsed) and every table's rows in rowid order, JSON columns parsed, timestamps and revs normalized."""
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    out = {"schema": {}, "rows": {}}
    for row in c.execute("SELECT type, name, tbl_name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%' ORDER BY type, name"):
        out["schema"]["%s %s" % (row["type"], row["name"])] = " ".join((row["sql"] or "").split())
    for t in [r["name"] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]:
        rows = []
        for rw in c.execute("SELECT * FROM %s ORDER BY rowid" % t):
            d = dict(rw)
            for k in JSON_COLS & set(d):
                d[k] = jsonish(d[k]) if d[k] is not None else None
            rows.append(norm(d))
        out["rows"][t] = rows
    c.close()
    return out


# ---------------------------------------------------------------- stages
def alt_stage(cmd, tmp, cli_cmd):
    """no auth, SHEEPDOG_TOOL_EVENTS=0, a fresh empty database: the config switches, and the schema a new database gets."""
    db = os.path.join(tmp, "alt.db")
    srv = Server(cmd, db, no_auth=True, env={"SHEEPDOG_TOOL_EVENTS": "0"})
    r = Rec(srv, cli_cmd, tmp)
    try:
        r.get("state-without-token", "/api/state", auth=False)
        r.get("board-without-token", "/", auth=False, page="board.html")
        r.get("token-query-ignored", "/?token=anything", auth=False)
        r.post("login-when-no-auth", "/login", b"token=x", auth=False, hdr={"Content-Type": "application/x-www-form-urlencoded"})
        r.post("task-without-token", "/api/task", {"key": "A1", "title": "no auth"}, auth=False)
        r.post("hook-ordinary-tool-skipped", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Bash", tool_input={"command": "ls"}), auth=False)
        r.post("hook-ordinary-failure-still-logged", "/api/hook", hook(hook_event_name="PostToolUseFailure", tool_name="Bash", tool_input={"command": "ls"}, error="Exit code 1"), auth=False)
        r.post("hook-agent-tool-still-logged", "/api/hook", hook(hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-a", tool_input={"description": "Task 5 alt"}, tool_response={"status": "completed"}), auth=False)
        r.get("state-end", "/api/state", auth=False)
    finally:
        srv.stop()
    return {"steps": r.http_steps, "db": dump_db(db)}, r.page_diffs


def allow_stage(cmd, tmp, cli_cmd):
    """hooks accepted only from SHEEPDOG_ALLOW (/work/inside) plus --allow (/work/also): outside hooks are dropped with the normal empty
    reply and change nothing, a prompt from outside never delivers, a hook with no cwd is accepted, API and CLI calls are not filtered."""
    db = os.path.join(tmp, "allow.db")
    srv = Server(cmd, db, env={"SHEEPDOG_ALLOW": "/work/inside"}, args=["--allow", "/work/also"])
    r = Rec(srv, cli_cmd, tmp)
    try:
        _, st = r.get("state-empty", "/api/state")
        r.post("task-outside-folder-not-filtered", "/api/task", {"key": "A1", "title": "api calls are not filtered", "worktree": "/work/elsewhere/wt"})
        r.post("brief-for-allowed-folder", "/api/task/A1/brief", {"brief": "## A1\nok?", "cwd": "/work/inside", "options": [{"id": "go", "label": "Go"}]})
        r.post("decision-pending", "/api/task/A1/decision", {"action": "option", "option": "go"})
        _, cur = r.get("state-before-hooks", "/api/state")
        r.post("hook-outside-dropped", "/api/hook", hook(cwd="/work/elsewhere/wt", hook_event_name="SessionStart", source="startup"))
        r.post("hook-sibling-prefix-dropped", "/api/hook", hook(cwd="/work/inside-old", hook_event_name="SessionStart", source="startup"))
        r.post("hook-relative-cwd-dropped", "/api/hook", hook(cwd="work/inside", hook_event_name="SessionStart", source="startup"))
        r.post("hook-outside-agent-dropped", "/api/hook", hook(cwd="/work/elsewhere", hook_event_name="PreToolUse", tool_name="Agent", tool_use_id="tu-o", tool_input={"description": "Task 9 outside"}))
        r.post("hook-outside-prompt-never-delivers", "/api/hook", hook(cwd="/work/elsewhere/wt", hook_event_name="UserPromptSubmit", prompt="hi"))
        r.get("state-unchanged-by-outside-hooks", "/api/state?since=%d" % cur["cursor"])
        r.post("hook-inside-stored", "/api/hook", hook(cwd="/work/inside/app", hook_event_name="SessionStart", source="startup"))
        r.post("hook-second-root-stored", "/api/hook", hook(cwd="/work/also", hook_event_name="SessionStart", source="resume"))
        r.post("hook-no-cwd-accepted", "/api/hook", {"hook_event_name": "Stop", "session_id": "no-cwd"})
        r.post("hook-missing-event-name-still-400", "/api/hook", hook(cwd="/work/elsewhere", tool_name="Bash"))
        r.post("hook-inside-prompt-delivers", "/api/hook", hook(cwd="/work/inside/app", hook_event_name="UserPromptSubmit", prompt="next"))
        r.get("state-end", "/api/state")
        r.cli("cli-task-outside-folder", ["task", "A2", "--title", "cli is not filtered", "--worktree", "/work/elsewhere/wt2"])
    finally:
        srv.stop()
    return {"steps": r.http_steps + r.cli_steps, "db": dump_db(db)}, r.page_diffs


PRIVACY_ENV = {"HOME": "/Users/jdoe", "USER": "jdoe", "LOGNAME": "jdoe", "SHEEPDOG_HOSTNAME": "jdoes-mbp.local", "TMPDIR": "/private/var/folders/xy/abc123/T/",
               "SHEEPDOG_ALLOW": "/Users/jdoe/dev/alpha"}
LEAKS = ("jdoe", "jdoes-mbp", "/Users/", "/home/", "/private/var/folders", "var/folders", "/tmp/", "abc123")


def privacy_stage(cmd, tmp, cli_cmd):
    """nothing identifying is stored: paths under the allowed root, the home, the temp dirs and the user and host names are rewritten on
    the way in, by every write path, and delivery still matches a brief's folder with a hook's. Run with a fake user, home and host."""
    db = os.path.join(tmp, "privacy.db")
    srv = Server(cmd, db, env=PRIVACY_ENV)
    r = Rec(srv, cli_cmd, tmp)
    a, home, tmpd = "/Users/jdoe/dev/alpha", "/Users/jdoe", "/private/var/folders/xy/abc123/T"
    try:
        r.cli("task", ["task", "P1", "--title", "fix %s/x.go for jdoe" % a, "--project", "jdoes-mbp", "--branch", "jdoe/fix", "--worktree", a + "/wt",
                       "--next", "ask jdoe@example.com about %s/.ssh" % home, "--note", "ran in %s/run1 on jdoes-mbp.local" % tmpd])
        r.cli("agent", ["agent", "ag-1", "--task", "P1", "--name", "jdoe's helper in " + a, "--model", "model-jdoe"])
        md = r.md("## Review %s/web/a.go\nrun by jdoe on jdoes-mbp, logs /home/jdoe/logs/x.log and %s/out.txt\n" % (a, tmpd))
        r.cli("brief", ["brief", "P1", "--file", md, "--option", "go|Go for jdoe|%s/hint" % home, "--cwd", a + "/web"])
        r.post("decision-reply", "/api/task/P1/decision", {"action": "reply", "text": "use %s/other and ask jdoe" % a})
        for name, extra in (("SessionStart", {"source": "startup in /Users/jdoe", "model": "m-jdoe"}), ("SessionEnd", {"reason": "done at /Users/jdoe/x"}), ("Stop", {}),
                            ("UserPromptSubmit", {"prompt": "look at %s/z.go, jdoe@example.com, token=abc123secret, %s/f" % (a, tmpd)}),
                            ("Notification", {"message": "jdoe needs %s/perm" % a, "notification_type": "permission_prompt"}),
                            ("SubagentStart", {"agent_id": "ag-1", "agent_type": "type-jdoe"}), ("SubagentStop", {"agent_id": "ag-1", "agent_type": "type-jdoe"}),
                            ("PostToolUse", {"tool_name": "Bash", "agent_id": "ag-1", "tool_input": {"command": "cd %s && make && cat /home/jdoe/.zshrc %s/o" % (a, tmpd), "description": "jdoes-mbp"}}),
                            ("PostToolUseFailure", {"tool_name": "Bash", "agent_id": "ag-1", "tool_input": {"command": "go test %s/..." % a},
                                                    "error": "Exit code 2\n%s/a.go:3: jdoe broke it\nat %s/b" % (a, tmpd),
                                                    "tool_response": {"stderr": "/home/jdoe/go/pkg/mod/x\n/Users/jdoe/Library/y", "stdout": "host jdoes-mbp.local"}}),
                            ("PreToolUse", {"tool_name": "Agent", "tool_use_id": "tu-1", "tool_input": {"description": "Task 5 edit %s/q.go as jdoe" % a, "subagent_type": "t-jdoe", "prompt": "x"}}),
                            ("PreCompact", {"trigger": "jdoe"})):
            r.post("hook-" + name, "/api/hook", hook(cwd=a + "/web", hook_event_name=name, **extra))
        r.post("hook-edit", "/api/hook", hook(cwd=a + "/web", hook_event_name="PostToolUse", tool_name="Edit", tool_input={"file_path": a + "/web/e.go"}))
        r.post("hook-grep", "/api/hook", hook(cwd=home + "/other", hook_event_name="PostToolUse", tool_name="Grep", tool_input={"pattern": "jdoe", "path": home + "/src"}))
        r.post("hook-agent-done", "/api/hook", hook(cwd=a + "/web", hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-1", tool_input={"description": "Task 5 edit %s/q.go" % a},
                                                  tool_response={"status": "completed", "agentId": "ag-9", "totalTokens": 5}))
        r.post("hook-sendmessage", "/api/hook", hook(cwd=a + "/web", hook_event_name="PreToolUse", tool_name="SendMessage", tool_input={"to": "ag-1", "message": "Task 6 go to %s/x, jdoe" % a}))
        r.post("hook-tmp-cwd", "/api/hook", hook(cwd=tmpd + "/run/sub", hook_event_name="SessionStart", source="startup"))
        r.post("hook-prompt-delivers-from-subfolder", "/api/hook", hook(cwd=a + "/web/deep", hook_event_name="UserPromptSubmit", prompt="deliver"))
        r.cli("brief-for-the-waiter", ["brief", "P1", "--file", md, "--option", "go|Go", "--cwd", a + "/web"])
        r.post("decision-for-the-waiter", "/api/task/P1/decision", {"action": "option", "option": "go"})
        r.get("wait-for-the-root", "/api/decisions/wait?cwd=%s&timeout=0" % a)
        r.get("state", "/api/state")
        r.get("standup", "/api/standup?off=0")
        r.get("events-p1", "/api/task/P1/events")
        r.get("board-page", "/", page="board.html")
        r.get("architecture-page", "/architecture", page="architecture.html")
    finally:
        srv.stop()
    out = {"steps": r.http_steps + r.cli_steps, "db": dump_db(db)}
    answers = [{k: v for k, v in s.items() if k not in ("request", "args", "stdin")} for s in out["steps"]]  # what the server and the CLI said, not what was sent
    blob = json.dumps([answers, out["db"]])
    found = [m for m in LEAKS if m in blob]
    return out, r.page_diffs + (["privacy: %s found in the recorded privacy stage" % found] if found else [])


def migrate_stage(cmd, tmp, cli_cmd):
    """a database from before the later columns existed: the server adds them, backfills done_at, and keeps the rows."""
    db = os.path.join(tmp, "old.db")
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE tasks(key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',"
                    " project TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',"
                    " mr_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]', created_at INTEGER, updated_at INTEGER,"
                    " last_event_at INTEGER, archived INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0);"
                    "INSERT INTO tasks(key,title,status,created_at,updated_at,rev) VALUES('O1','old done','done',900,1000,3),('O2','old review','review',900,1000,4);")
    c.close()
    srv = Server(cmd, db)
    r = Rec(srv, cli_cmd, tmp)
    try:
        r.get("state-after-migration", "/api/state")
        r.post("update-migrated-task", "/api/task", {"key": "O2", "next": "go", "waiting": "me"})
        r.get("state-end", "/api/state")
    finally:
        srv.stop()
    return {"steps": r.http_steps, "db": dump_db(db)}, r.page_diffs


def run_all(cmd, cli_cmd, tmp):
    day0 = int(time.time()) // 86400
    fx = os.path.join(tmp, "fixture.db")
    fixture.build(fx)
    db = os.path.join(tmp, "board.db")
    shutil.copy(fx, db)
    srv = Server(cmd, db)
    r = Rec(srv, cli_cmd, tmp)
    try:
        scenario(r)
    finally:
        srv.stop()
    diffs = list(r.page_diffs)
    alt, d2 = alt_stage(cmd, tmp, cli_cmd)
    mig, d3 = migrate_stage(cmd, tmp, cli_cmd)
    alw, d4 = allow_stage(cmd, tmp, cli_cmd)
    prv, d5 = privacy_stage(cmd, tmp, cli_cmd)
    if int(time.time()) // 86400 != day0:
        sys.exit("the UTC date changed during the run (standup depends on it); run again")
    return {"http.json": r.http_steps, "cli.json": r.cli_steps, "db.json": dump_db(db), "alt-server.json": alt, "migrate.json": mig, "allow.json": alw, "privacy.json": prv}, diffs + d2 + d3 + d4 + d5


# ---------------------------------------------------------------- compare
def pretty(x):
    return json.dumps(x, indent=1, sort_keys=True, ensure_ascii=False).splitlines()


def compare(name, want, got, out):
    """readable differences between two golden documents; returns the number of differing parts."""
    n = 0
    if isinstance(want, list) and all(isinstance(x, dict) and "step" in x for x in want + got):
        w, g = {x["step"]: x for x in want}, {x["step"]: x for x in got}
        parts = [(k, w.get(k), g.get(k)) for k in list(w) + [k for k in g if k not in w]]
    elif isinstance(want, dict) and "steps" in want:  # a stage: steps plus a db dump
        return compare(name + ":steps", want["steps"], got["steps"], out) + compare(name + ":db", want["db"], got["db"], out)
    elif isinstance(want, dict) and set(want) == {"schema", "rows"}:  # a database dump: one part per schema object and per table
        return sum(compare("%s %s" % (name, sec), want[sec], got[sec], out) for sec in ("schema", "rows"))
    elif isinstance(want, dict):
        parts = [(k, want.get(k), got.get(k)) for k in list(want) + [k for k in got if k not in want]]
    else:
        parts = [("", want, got)]
    for k, a, b in parts:
        if a != b:
            n += 1
            out.append("--- %s %s" % (name, k))
            d = list(difflib.unified_diff(pretty(a), pretty(b), "golden", "actual", lineterm="", n=2))
            out.extend(d[:60] + (["... (%d more diff lines)" % (len(d) - 60)] if len(d) > 60 else []))
    return n


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="python3 sheepdog.py serve", help="command that starts a server; --host --port --db [--no-auth] are appended, SHEEPDOG_TOKEN is in the env")
    ap.add_argument("--cli", default="python3 sheepdog.py", help="the CLI used as client (task, agent, hook, brief, wait, done, archive)")
    ap.add_argument("--golden", default=os.path.join(HERE, "golden"))
    ap.add_argument("--record", action="store_true", help="write the golden files instead of comparing")
    a = ap.parse_args()
    cli_cmd = shlex.split(a.cli)
    with tempfile.TemporaryDirectory() as tmp:
        results, page_diffs = run_all(a.server, cli_cmd, tmp)
    if a.record:
        os.makedirs(a.golden, exist_ok=True)
        for fn, doc in results.items():
            with open(os.path.join(a.golden, fn), "w") as f:
                json.dump(doc, f, indent=1, sort_keys=True, ensure_ascii=False)
                f.write("\n")
        steps = sum(len(v) for k, v in results.items() if k in ("http.json", "cli.json")) + len(results["alt-server.json"]["steps"]) + len(results["migrate.json"]["steps"]) + len(results["allow.json"]["steps"]) + len(results["privacy.json"]["steps"])
        print("recorded %d files in %s (%d steps)" % (len(results), a.golden, steps))
        print("\n".join(page_diffs))
        return 1 if page_diffs else 0
    out, bad = [], 0
    for fn, got in results.items():
        with open(os.path.join(a.golden, fn)) as f:
            bad += compare(fn, json.load(f), json.loads(json.dumps(got, ensure_ascii=False)), out)
    out += page_diffs
    bad += len(page_diffs)
    print("\n".join(out))
    print("contract: %s (%d differing parts)" % ("FAIL" if bad else "ok", bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
