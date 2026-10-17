import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request

import sheepdog

HERE = os.path.dirname(os.path.abspath(__file__))
TOKEN = "test-token-123"


class BoardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.srv = sheepdog.make_server(os.path.join(cls.tmp.name, "t.db"), "127.0.0.1", 0, TOKEN)
        cls.port = cls.srv.server_address[1]
        cls.url = "http://127.0.0.1:%d" % cls.port
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.env = dict(os.environ, SHEEPDOG_URL=cls.url, SHEEPDOG_TOKEN=TOKEN)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()
        cls.tmp.cleanup()

    # helpers
    def get(self, path, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", path, headers={"Authorization": "Bearer " + token} if token else {})
        r = c.getresponse()
        return r.status, r.read(), r

    def state(self, since=0):
        st, body, _ = self.get("/api/state?since=%d" % since)
        self.assertEqual(st, 200)
        return json.loads(body)

    def cli(self, *args, env=None):
        return subprocess.run([sys.executable, os.path.join(HERE, "sheepdog.py")] + list(args), env=env or self.env,
                              capture_output=True, text=True, timeout=20)

    def hook(self, payload):
        r = urllib.request.Request(self.url + "/api/hook", json.dumps(payload).encode(),
                                   {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        urllib.request.urlopen(r).read()

    # tests
    def test_auth(self):
        self.assertEqual(self.get("/api/state", token=None)[0], 401)
        self.assertEqual(self.get("/api/state", token="wrong")[0], 401)
        self.assertEqual(self.get("/")[0], 200)
        self.assertEqual(self.get("/", token=None)[0], 401)
        c = http.client.HTTPConnection("127.0.0.1", self.port)  # POST without token is refused
        c.request("POST", "/api/task", b"{}", {"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 401)
        st, _, r = self.get("/?token=" + TOKEN, token=None)
        self.assertEqual(st, 302)
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Strict", cookie)
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/api/state", headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual(c.getresponse().status, 200)

    def test_login_page(self):
        def req(method, body=None):
            c = http.client.HTTPConnection("127.0.0.1", self.port)
            c.request(method, "/" if method == "GET" else "/login", body,
                      {"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"})
            r = c.getresponse()
            return r.status, r.read().decode(), r
        st, html, _ = req("GET")
        self.assertEqual(st, 200)
        self.assertIn("Sign in", html)
        self.assertEqual(self.get("/", token=None)[0], 401)  # non-HTML clients keep 401
        st, html, _ = req("POST", "token=wrong")
        self.assertEqual((st, "Wrong token" in html), (401, True))
        st, _, r = req("POST", "token=" + TOKEN)
        self.assertEqual((st, r.getheader("Location")), (303, "/"))
        cookie = r.getheader("Set-Cookie")
        self.assertIn("HttpOnly", cookie)
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request("GET", "/api/state", headers={"Cookie": cookie.split(";")[0]})
        self.assertEqual(c.getresponse().status, 200)
        self.srv.fails.clear()
        codes = [req("POST", "token=wrong")[0] for _ in range(6)]
        self.assertEqual(codes, [401] * 5 + [429])
        self.assertEqual(req("POST", "token=" + TOKEN)[0], 429)
        self.srv.fails.clear()

    def test_hook_links_to_task_by_worktree(self):
        self.assertEqual(self.cli("task", "L1", "--title", "link me", "--worktree", "/work/wt/L1", "--project", "alpha").returncode, 0)
        sid = "sess-1"
        cwd = "/work/wt/L1/apps/web"
        self.hook({"hook_event_name": "SessionStart", "session_id": sid, "cwd": cwd, "source": "startup"})
        self.hook({"hook_event_name": "PreToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_1",
                   "tool_input": {"description": "Find endpoints", "prompt": "SECRET PROMPT", "subagent_type": "Explore", "model": "sonnet"}})
        self.hook({"hook_event_name": "SubagentStart", "session_id": sid, "cwd": cwd, "agent_id": "ag-1", "agent_type": "Explore"})
        self.hook({"hook_event_name": "PostToolUse", "session_id": sid, "cwd": cwd, "agent_id": "ag-1", "tool_name": "Bash",
                   "tool_input": {"command": "ls"}, "tool_response": {"stdout": "OUTPUT"}})
        s = self.state()
        mine = [e for e in s["events"] if e["task_key"] == "L1"]
        kinds = [e["kind"] for e in mine]
        for k in ("SessionStart", "PreToolUse", "SubagentStart", "PostToolUse"):
            self.assertIn(k, kinds)
        ag = [a for a in s["agents"] if a["id"] == "ag-1"][0]
        self.assertEqual((ag["task_key"], ag["status"], ag["name"], ag["model"]), ("L1", "running", "Find endpoints", "sonnet"))
        self.assertEqual(ag["tool_uses"], 1)
        self.assertFalse(any("SECRET PROMPT" in json.dumps(e) or "OUTPUT" in json.dumps(e) for e in s["events"]))
        # agent event inside the subagent links through the agent even from an unrelated cwd
        self.hook({"hook_event_name": "PostToolUse", "session_id": sid, "cwd": "/elsewhere", "agent_id": "ag-1", "tool_name": "Edit",
                   "tool_input": {"file_path": "/x/y.ts"}})
        self.hook({"hook_event_name": "SubagentStop", "session_id": sid, "cwd": cwd, "agent_id": "ag-1", "agent_type": "Explore"})
        self.hook({"hook_event_name": "PostToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_1",
                   "tool_input": {"description": "Find endpoints"},
                   "tool_response": {"status": "completed", "agentId": "ag-1", "totalTokens": 1234, "totalToolUseCount": 7}})
        s = self.state()
        ag = [a for a in s["agents"] if a["id"] == "ag-1"][0]
        self.assertEqual((ag["status"], ag["tokens"], ag["tool_uses"]), ("done", 1234, 7))
        self.assertTrue(any(e["summary"].startswith("Edit: /x/y.ts") and e["task_key"] == "L1" for e in s["events"]))
        # unrelated cwd -> unlinked
        self.hook({"hook_event_name": "SessionStart", "session_id": "s2", "cwd": "/nowhere", "source": "startup"})
        self.assertTrue(any(e["cwd"] == "/nowhere" and e["task_key"] is None for e in self.state()["events"]))
        # failed agent
        self.hook({"hook_event_name": "PreToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_2",
                   "tool_input": {"description": "Doomed"}})
        self.hook({"hook_event_name": "PostToolUseFailure", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_2",
                   "tool_input": {"description": "Doomed"}, "error": "boom"})
        self.assertTrue(any(a["name"] == "Doomed" and a["status"] == "failed" for a in self.state()["agents"]))

    def test_cli_task_lifecycle(self):
        r = self.cli("task", "C1", "--title", "first", "--status", "building", "--project", "p", "--branch", "feat/x",
                     "--worktree", "/work/wt/C1", "--mr", "https://example.com/mr/1", "--note", "hello")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = json.loads(r.stdout)
        self.assertEqual((t["key"], t["status"], t["branch"], t["notes"][0]["text"]), ("C1", "building", "feat/x", "hello"))
        t = json.loads(self.cli("task", "C1", "--status", "testing", "--note", "second").stdout)
        self.assertEqual((t["title"], t["status"], len(t["notes"])), ("first", "testing", 2))
        self.assertNotEqual(self.cli("task", "C1", "--mr", "javascript:alert(1)").returncode, 0)
        r = self.cli("agent", "cli-agent", "--task", "C1", "--name", "builder", "--model", "opus", "--status", "running")
        self.assertEqual(json.loads(r.stdout)["status"], "running")
        r = self.cli("archive", "C1")
        self.assertEqual(json.loads(r.stdout)["archived"], 1)
        self.assertNotIn("C1", [x["key"] for x in self.state()["tasks"]])
        self.assertIn("C1", [x["key"] for x in self.state(1)["tasks"] if x["archived"]])

    def test_incremental_state(self):
        cur = self.state()["cursor"]
        self.assertEqual(self.state(cur)["tasks"], [])
        self.cli("task", "I1", "--title", "inc")
        s = self.state(cur)
        self.assertEqual([t["key"] for t in s["tasks"]], ["I1"])
        self.assertTrue(s["cursor"] > cur)

    def test_redaction(self):
        cases = ["curl -H 'Authorization: Bearer abcdefghij123456' https://x", "export GITLAB_TOKEN=glpat" + "-abcdef1234567890",
                 "mysql --password hunter2 -u root", "AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY", "key AKIA" + "IOSFODNN7EXAMPLE",
                 "psql postgres://user:s3cr3tpw@db.host:5432/app", 'echo {"password": "p4ss"}', "sk" + "-abcdefghijklmnopqrstuv"]
        secrets = ["abcdefghij123456", "glpat" + "-abcdef1234567890", "hunter2", "wJalrXUtn", "AKIA" + "IOSFODNN7EXAMPLE", "s3cr3tpw", "p4ss",
                   "sk" + "-abcdefghijklmnopqrstuv"]
        for c, s in zip(cases, secrets):
            self.assertNotIn(s, sheepdog.redact(c), c)
        self.assertIn("db.host", sheepdog.redact(cases[5]))
        self.hook({"hook_event_name": "PostToolUse", "session_id": "r", "cwd": "/r", "tool_name": "Bash",
                   "tool_input": {"command": "x" * 150 + " TOKEN=topsecretvalue1234 " + "y" * 400}})
        self.hook({"hook_event_name": "UserPromptSubmit", "session_id": "r", "cwd": "/r", "prompt": "my password=abc12345 " + "z" * 300})
        evs = self.state()["events"]
        blob = json.dumps(evs)
        self.assertNotIn("topsecretvalue1234", blob)
        self.assertNotIn("abc12345", blob)
        bash = [e for e in evs if e["summary"].startswith("Bash:") and e["cwd"] == "/r"][0]
        self.assertLessEqual(len(bash["summary"]), 210)
        prompt = [e for e in evs if e["kind"] == "UserPromptSubmit" and e["cwd"] == "/r"][0]
        self.assertLessEqual(len(prompt["summary"]), 140)

    def test_hook_cli_server_down(self):
        env = dict(os.environ, SHEEPDOG_URL="http://127.0.0.1:1", SHEEPDOG_TOKEN="x")
        for stdin in (json.dumps({"hook_event_name": "SessionStart", "session_id": "a", "cwd": "/"}), "not json", ""):
            t0 = time.time()
            r = subprocess.run([sys.executable, os.path.join(HERE, "sheepdog.py"), "hook"], input=stdin, env=env,
                               capture_output=True, text=True, timeout=10)
            self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
            self.assertLess(time.time() - t0, 3)

    def test_hook_cli_delivers_and_tool_toggle(self):
        p = json.dumps({"hook_event_name": "PostToolUse", "session_id": "h", "cwd": "/hk", "tool_name": "Bash",
                        "tool_input": {"command": "echo viahook"}, "tool_response": {"stdout": "x" * 10000}})
        r = subprocess.run([sys.executable, os.path.join(HERE, "sheepdog.py"), "hook"], input=p, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertTrue(any("echo viahook" in e["summary"] for e in self.state()["events"]))
        self.assertEqual(r.stdout, "")  # nothing waiting: the hook prints nothing
        os.environ["SHEEPDOG_TOOL_EVENTS"] = "0"  # the toggle lives on the server now (the curl hook has no client logic)
        try:
            self.hook(json.loads(p.replace("viahook", "server-skipped")))
        finally:
            del os.environ["SHEEPDOG_TOOL_EVENTS"]
        self.assertFalse(any("server-skipped" in e["summary"] for e in self.state()["events"]))

    def test_agent_links_by_task_description(self):
        self.cli("task", "T7", "--title", "Seven", "--project", "alpha")
        sid, cwd = "sd", "/work/proj"  # project name in cwd must NOT link anything
        def pre(tuid, desc):
            self.hook({"hook_event_name": "PreToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": tuid,
                       "tool_input": {"description": desc, "subagent_type": "general-purpose"}})
        pre("tu_a", "Task 7 Build the thing")
        pre("tu_b", "Task 91 Brand new task")
        pre("tu_c", "no number here")
        self.hook({"hook_event_name": "SubagentStart", "session_id": sid, "cwd": cwd, "agent_id": "sa-1", "agent_type": "general-purpose"})
        s = self.state()
        ag = {a["tool_use_id"]: a for a in s["agents"] if a["session_id"] == sid}
        self.assertEqual((ag["tu_a"]["task_key"], ag["tu_a"]["name"]), ("T7", "Task 7 Build the thing"))
        self.assertEqual(ag["tu_a"]["id"], "sa-1")  # SubagentStart adopted the oldest placeholder and kept its link
        self.assertEqual(ag["tu_b"]["task_key"], "T91")
        self.assertIsNone(ag["tu_c"]["task_key"])
        t91 = [t for t in s["tasks"] if t["key"] == "T91"][0]
        self.assertEqual((t91["title"], t91["status"]), ("Brand new task", "building"))
        self.assertTrue(any(e["kind"] == "SubagentStart" and e["task_key"] == "T7" for e in s["events"]))
        starts = [e["summary"] for e in s["events"] if e["kind"] == "PreToolUse" and e["session_id"] == sid]
        self.assertIn("agent start: Build the thing (general-purpose)", starts)
        self.assertIn("agent start: no number here (general-purpose)", starts)
        self.assertEqual(json.loads(self.cli("task", "T7", "--status", "running").stdout)["status"], "building")
        self.assertIsNone(self.srv.db.link(cwd))  # no "only open task of the project" fallback

    def test_next_waiting_and_needs_you(self):
        r = self.cli("task", "N1", "--title", "n", "--next", "Test at http://localhost:3000", "--waiting", "me")
        t = json.loads(r.stdout)
        self.assertEqual((t["next"], t["waiting"], t["needs_you"]), ("Test at http://localhost:3000", "me", True))
        self.assertNotEqual(self.cli("task", "N1", "--waiting", "bogus").returncode, 0)
        up = lambda **d: self.srv.db.task_upsert(dict(key="N1", **d))
        self.assertTrue(up(status="review", waiting="")["needs_you"])  # unset waiting: review/testing needs you
        self.assertFalse(up(waiting="qc")["needs_you"])
        self.assertFalse(up(waiting="none")["needs_you"])
        self.assertFalse(up(status="building", waiting="")["needs_you"])
        self.assertTrue(up(waiting="me")["needs_you"])
        self.assertEqual(up(next="")["next"], "")
        with self.assertRaises(ValueError):
            up(waiting="x")

    def test_migrates_old_db(self):
        import sqlite3
        p = os.path.join(self.tmp.name, "old.db")
        c = sqlite3.connect(p)
        c.executescript("CREATE TABLE tasks(key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',"
                        " project TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',"
                        " mr_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]', created_at INTEGER, updated_at INTEGER,"
                        " last_event_at INTEGER, archived INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0);"
                        "INSERT INTO tasks(key,title,status,updated_at) VALUES('O1','old','done',1000),('O2','old2','review',1000);")
        c.close()
        db = sheepdog.DB(p)
        self.addCleanup(db.close)
        db.task_upsert({"key": "O2", "next": "go", "waiting": "me"})
        o1, o2 = db.get_task("O1"), db.get_task("O2")
        self.assertEqual((o1["next"], o1["waiting"], o1["done_at"], o1["needs_you"]), ("", "", 1000, False))
        self.assertEqual((o2["next"], o2["waiting"], o2["needs_you"]), ("go", "me", True))
        sheepdog.DB(p).close()  # second start is a no-op

    def test_standup_fixed_clock(self):
        db = sheepdog.DB(os.path.join(self.tmp.name, "su.db"))
        self.addCleanup(db.close)
        mon = 1792800000 - 1792800000 % 86400 + 3 * 86400  # find a Monday 12:00 UTC below
        while (mon // 86400 + 3) % 7 != 0:
            mon += 86400
        now = mon + 12 * 3600
        fri, sun = mon - 3 * 86400 + 10 * 3600, mon - 86400 + 10 * 3600
        for k, title, st, mr, at in (("A1", "friday", "done", "https://gl/x/-/merge_requests/57", fri), ("A2", "sunday", "done", "", sun),
                                     ("A3", "today", "done", "", mon + 3600), ("A4", "building", "building", "", None),
                                     ("A5", "stuck", "blocked", "", None), ("A6", "idle", "planned", "", None)):
            db.task_upsert({"key": k, "title": title, "status": st, "mr_url": mr or None})
            db.c.execute("UPDATE tasks SET done_at=? WHERE key=?", (at, k))
        self.assertEqual(db.standup(now=now, off=0).split("\n"),
                         ["Yesterday: A1 friday (!57)", "Today: A3 today; A4 building", "Blockers: A5 stuck"])
        self.assertEqual(db.standup(now=now + 3 * 86400, off=0).split("\n")[:2], ["Yesterday: None", "Today: A4 building"])  # Thursday: yesterday = Wed
        self.assertEqual(db.standup(now=mon + 3600, off=-2 * 3600).split("\n")[0], "Yesterday: A1 friday (!57)")  # still Sunday there
        db2 = sheepdog.DB(os.path.join(self.tmp.name, "su2.db"))
        self.addCleanup(db2.close)
        self.assertEqual(db2.standup(now=now, off=0), "Yesterday: None\nToday: None\nBlockers: None")
        st, body, _ = self.get("/api/standup?off=0")
        self.assertEqual(st, 200)
        self.assertIn("Blockers:", json.loads(body)["text"])

    def test_failed_commands(self):
        self.cli("task", "T9", "--title", "fails")
        self.cli("agent", "ag9", "--task", "T9", "--status", "running")
        self.hook({"hook_event_name": "PostToolUseFailure", "session_id": "s9", "cwd": "/x", "agent_id": "ag9", "tool_name": "Bash",
                   "tool_input": {"command": "false"}, "error": "boom\nsecond line"})
        st = self.state()
        self.assertEqual(st["failed"]["T9"], 1)
        self.assertNotIn("second line", [e for e in st["events"] if e["task_key"] == "T9"][0]["summary"])

    def test_failure_output_stored(self):
        self.cli("task", "T10", "--title", "fails loudly")
        self.cli("agent", "ag10", "--task", "T10", "--status", "running")
        err = "Exit code 2\n" + "\n".join("line %d password=hunter2 " % i + "x" * 300 for i in range(100))
        self.hook({"hook_event_name": "PostToolUseFailure", "session_id": "s10", "cwd": "/x", "agent_id": "ag10", "tool_name": "Bash",
                   "tool_input": {"command": "make test TOKEN=topsecretvalue1234 " + "y" * 3000}, "error": err, "duration_ms": 1234})
        ev = [e for e in self.state()["events"] if e["task_key"] == "T10" and e["kind"] == "PostToolUseFailure"][0]
        d = json.loads(ev["data"])
        lines = d["out"].split("\n")
        self.assertLessEqual(len(lines), 40)
        self.assertLessEqual(len(d["out"]), 8192)  # 40 lines of ~320 chars would be 12.8 KB: the size cap bites
        self.assertTrue(lines[-1].startswith("line 99 "))  # the LAST lines are kept
        self.assertNotIn("hunter2", ev["data"])
        self.assertNotIn("topsecretvalue1234", ev["data"])
        self.assertIn("[REDACTED]", d["out"])
        self.assertLessEqual(len(d["cmd"]), 2048)
        self.assertEqual((d["exit"], d["dur_ms"], d["tool"]), (2, 1234, "Bash"))
        self.assertEqual(self.state()["failed"]["T10"], 1)
        self.hook({"hook_event_name": "PostToolUseFailure", "session_id": "s10", "cwd": "/x", "agent_id": "ag10", "tool_name": "Bash",
                   "tool_input": {"command": "false"}, "error": "a\nb\nc"})  # short output is kept whole
        ev = [e for e in self.state()["events"] if e["task_key"] == "T10" and e["kind"] == "PostToolUseFailure"][0]
        self.assertEqual(json.loads(ev["data"])["out"], "a\nb\nc")

    def post(self, path, body, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        c.request("POST", path, json.dumps(body), h)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")

    def test_decision_endpoint(self):
        self.cli("task", "D1", "--title", "decide me", "--status", "review", "--waiting", "me", "--next", "Check http://localhost:3000/x")
        p = "/api/task/D1/decision"
        self.assertEqual(self.post(p, {"action": "approve"}, token=None)[0], 401)  # token required
        self.assertEqual(self.post(p, {"action": "approve"}, token="wrong")[0], 401)
        self.assertEqual(self.post("/api/task/NOPE/decision", {"action": "approve"})[0], 404)
        self.assertEqual(self.post(p, {"action": "merge-it"})[0], 400)
        self.assertEqual(self.post(p, {"action": "reply", "text": "  "})[0], 400)
        self.assertTrue(self.srv.db.get_task("D1")["needs_you"])  # nothing changed by the refused calls
        st, t = self.post(p, {"action": "approve"})
        self.assertEqual(st, 200)
        self.assertEqual((t["waiting"], t["status"], t["needs_you"]), ("agent", "building", False))  # the ball is back with the agent: the column follows
        self.assertEqual(t["notes"][-1]["text"], "Decision: approve and merge")
        self.cli("task", "D1", "--waiting", "me")
        st, t = self.post(p, {"action": "reply", "text": "Use the new flag, password=hunter2"})
        self.assertEqual((st, t["waiting"], len(t["notes"])), (200, "agent", 2))
        self.assertTrue(t["notes"][-1]["text"].startswith("Reply: Use the new flag"))
        self.assertNotIn("hunter2", json.dumps(t))
        evs = [e for e in self.state()["events"] if e["task_key"] == "D1" and e["kind"] == "decision"]
        self.assertEqual(len(evs), 2)
        self.assertEqual(json.loads(evs[0]["data"]), {"action": "reply"})
        self.assertFalse(any(e["kind"] == "decision" for e in self.srv.db.task_events("D1")))  # the timeline shows the note, not a second row
        self.assertIn("D1", [x["key"] for x in self.state(1)["tasks"]])

    def api_get(self, path):
        st, body, _ = self.get(path)
        return st, json.loads(body)

    def test_brief_moves_to_review_and_decision_back_to_building(self):
        opts = [{"id": "go", "label": "Go ahead"}]
        self.cli("task", "ST1", "--title", "work", "--status", "building", "--next", "writing code")
        st, t = self.post("/api/task/ST1/brief", {"brief": "## r", "cwd": "/work/st1", "options": opts})
        self.assertEqual((st, t["status"], t["next"], t["waiting"]), (200, "review", "Review card ready", "me"))  # waiting for review, not building
        self.assertEqual(json.loads(self.cli("task", "ST1", "--status", "testing").stdout)["status"], "testing")  # a later explicit status still wins
        self.post("/api/task/ST1/brief", {"brief": "## again", "cwd": "/work/st1", "options": opts})
        st, t = self.post("/api/task/ST1/decision", {"action": "option", "option": "go"})
        self.assertEqual((st, t["status"], t["next"], t["waiting"]), (200, "building", "Acting on: Go ahead", "agent"))
        self.post("/api/task/ST1/brief", {"brief": "## third", "cwd": "/work/st1", "options": opts})
        st, t = self.post("/api/task/ST1/decision", {"action": "reply", "text": "use the flag"})
        self.assertEqual((t["status"], t["next"]), ("building", "Acting on: your reply"))
        self.cli("task", "ST2", "--title", "shipped", "--status", "done", "--next", "merged")
        for call in (("brief", {"brief": "x", "cwd": "/work/st2", "options": opts}), ("decision", {"action": "option", "option": "go"})):
            st, t = self.post("/api/task/ST2/" + call[0], call[1])
            self.assertEqual((st, t["status"], t["next"]), (200, "done", "merged"), call[0])  # a done task keeps its status and next step

    def brief(self, key, cwd, text="## Review\nAll good", options=None):
        self.cli("task", key, "--title", "t " + key, "--status", "review")
        opts = [{"id": "merge", "label": "Approve and merge", "hint": "ships", "primary": True}, {"id": "hold", "label": "Hold"}]
        return self.post("/api/task/%s/brief" % key, {"brief": text, "cwd": cwd, "options": opts if options is None else options})

    def run_hook(self, payload):
        r = subprocess.run([sys.executable, os.path.join(HERE, "sheepdog.py"), "hook"], input=json.dumps(payload), env=self.env,
                           capture_output=True, text=True, timeout=20)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r.stdout

    def test_brief_endpoint_and_cli(self):
        st, t = self.brief("BR1", "/work/br1", text="## Plan\npassword=hunter2 ok")
        self.assertEqual(st, 200)
        self.assertEqual((t["waiting"], t["brief_cwd"], [o["id"] for o in t["brief_options"]], t["brief_options"][0]["primary"]),
                         ("me", "/work/br1", ["merge", "hold"], True))
        self.assertNotIn("hunter2", t["brief"])
        self.assertTrue(any(e["kind"] == "brief" and e["task_key"] == "BR1" for e in self.state()["events"]))
        p = "/api/task/BR1/brief"
        self.assertEqual(self.post(p, {"brief": "second", "options": [], "cwd": "/work/br1"})[1]["brief"], "second")  # replaces
        too_many = [{"id": "a%d" % i, "label": "l"} for i in range(6)]
        for bad in ({"brief": ""}, {"brief": "x", "options": [{"id": "Bad Id", "label": "x"}]}, {"brief": "x", "options": too_many},
                    {"brief": "x", "cwd": "relative"}):
            self.assertEqual(self.post(p, bad)[0], 400, bad)
        self.assertEqual(self.post("/api/task/NOPE/brief", {"brief": "x"})[0], 404)
        self.assertEqual(self.post(p, {"brief": "x"}, token=None)[0], 401)
        f = os.path.join(self.tmp.name, "b.md")
        with open(f, "w") as fh:
            fh.write("## From CLI\n- [ ] one")
        r = self.cli("brief", "BR1", "--file", f, "--option", "merge|Approve and merge|ships it", "--option", "hold|Hold",
                     "--primary", "merge", "--cwd", "/work/cli")
        self.assertEqual(r.returncode, 0, r.stderr)
        t = json.loads(r.stdout)
        o = t["brief_options"]
        self.assertEqual((t["brief_cwd"], o[0]["hint"], o[0]["primary"], o[1]["primary"]), ("/work/cli", "ships it", True, False))
        self.assertNotEqual(self.cli("brief", "BR1", "--file", f, "--option", "a|A", "--primary", "zzz").returncode, 0)
        self.assertEqual(sheepdog.parse_options(["a|A|h", "b|B"], "b"),
                         [{"id": "a", "label": "A", "hint": "h", "primary": False}, {"id": "b", "label": "B", "hint": "", "primary": True}])

    def test_env_falls_back_to_the_old_board_names(self):
        keep = {k: os.environ.pop(k, None) for k in ("SHEEPDOG_TOKEN", "BOARD_TOKEN")}
        try:
            self.assertIsNone(sheepdog.env("SHEEPDOG_TOKEN", "BOARD_TOKEN"))
            os.environ["BOARD_TOKEN"] = "old"
            self.assertEqual(sheepdog.env("SHEEPDOG_TOKEN", "BOARD_TOKEN"), "old")
            os.environ["SHEEPDOG_TOKEN"] = "new"
            self.assertEqual(sheepdog.env("SHEEPDOG_TOKEN", "BOARD_TOKEN"), "new")
        finally:
            for k, v in keep.items():
                os.environ.pop(k, None)
                if v is not None:
                    os.environ[k] = v
        env = {k: v for k, v in self.env.items() if k not in ("SHEEPDOG_TOKEN", "SHEEPDOG_URL")}
        env.update(BOARD_TOKEN=TOKEN, BOARD_URL=self.url)  # a client with only the old names still reaches the server
        r = self.cli("task", "ENV1", "--title", "old names", env=env)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_default_data_dir_falls_back_to_the_old_folder(self):
        home = os.environ.get("HOME")
        with tempfile.TemporaryDirectory() as h:
            os.environ["HOME"] = h
            try:
                new, old = [os.path.join(h, ".local/share", d, "board.db") for d in ("sheepdog", "agent-board")]
                self.assertEqual(sheepdog.data_db(), new)  # nothing yet: the new folder is made
                self.assertTrue(os.path.isdir(os.path.dirname(new)))
                os.makedirs(os.path.dirname(old))
                open(old, "w").close()
                self.assertEqual(sheepdog.data_db(), old)  # only the old file exists
                open(new, "w").close()
                self.assertEqual(sheepdog.data_db(), new)  # the new one wins once it exists
            finally:
                os.environ["HOME"] = home

    def test_brand_files_and_name(self):
        st, body, r = self.get("/brand/icon-64.png", token=None)  # no sign-in needed
        self.assertEqual((st, r.getheader("Content-Type").split(";")[0], r.getheader("Cache-Control"), body[:4]), (200, "image/png", "max-age=86400", b"\x89PNG"))
        for bad in ("/brand/../sheepdog.py", "/brand/brand-sheet.png", "/brand/%2e%2e/sheepdog.py", "/brand/"):
            self.assertEqual(self.get(bad, token=None)[0], 404, bad)
        for page in ("/", "/architecture"):
            html_ = self.get(page)[1].decode()
            self.assertIn("/brand/favicon-32.png", html_)
            self.assertIn("sheepdog", html_)

    def test_long_reply_is_stored_whole_or_refused(self):
        db = self.srv.db
        self.brief("L1", "/work/l1")
        p = "/api/task/L1/decision"
        big = "x" * 25000
        st, t = self.post(p, {"action": "reply", "text": big})
        self.assertEqual(st, 400)
        self.assertIn("Too long: 25000 / 20000", t["error"])
        self.assertEqual(db.q1("SELECT COUNT(*) n FROM decisions WHERE task_key='L1'")["n"], 0)
        self.assertFalse(any(n["text"].startswith("Reply") for n in db.get_task("L1")["notes"]))
        ten = "ab" * 5000
        st, t = self.post(p, {"action": "reply", "text": ten})
        self.assertEqual(st, 200)
        self.assertEqual(t["notes"][-1]["text"], "Reply: " + ten)
        self.assertEqual(db.q1("SELECT text FROM decisions WHERE task_key='L1'")["text"], ten)

    def test_decision_exactly_once_and_scoping(self):
        db = self.srv.db
        self.brief("W1", "/work/w1/app")
        p = "/api/task/W1/decision"
        self.assertEqual(self.post(p, {"action": "option", "option": "nope"})[0], 400)
        self.assertEqual(db.q1("SELECT COUNT(*) n FROM decisions WHERE task_key='W1'")["n"], 0)
        st, t = self.post(p, {"action": "option", "option": "hold"})
        self.assertEqual((st, t["waiting"], t["notes"][-1]["text"], t["decision"]["status"], t["waiter"]), (200, "agent", "Decision: Hold", "pending", False))
        rows = db.q("SELECT * FROM decisions WHERE task_key='W1'")
        self.assertEqual([(r["option_id"], r["label"], r["cwd"], r["status"]) for r in rows], [("hold", "Hold", "/work/w1/app", "pending")])
        for other in ("/work/other", "/work/w1/app2", "/work/w1/app/sub"):  # another folder, a sibling prefix, a deeper folder: nothing
            self.assertEqual(self.api_get("/api/decisions/wait?cwd=%s&timeout=0" % other)[1], [], other)
        self.assertEqual(self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "o", "cwd": "/elsewhere", "prompt": "hi"}), "")
        st, got = self.api_get("/api/decisions/wait?cwd=/work/w1&timeout=0")  # a parent folder owns the app folder
        self.assertEqual((len(got), got[0]["delivered_to"], got[0]["status"]), (1, "waiter", "delivered"))
        self.assertEqual(self.api_get("/api/decisions/wait?cwd=/work/w1&timeout=0")[1], [])  # second wait: nothing
        self.assertEqual(self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/w1/app", "prompt": "hi"}), "")
        st, t = self.post(p, {"action": "reply", "text": "use the flag"})
        self.assertTrue(t["waiter"])  # a wait for this folder was seen a moment ago
        for no in ("/work/w1", "/work", "/work/w1/app2"):  # hook is one-way: a parent or a sibling-prefix folder never gets it
            self.assertEqual(self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": no, "prompt": "hi"}), "", no)
        out = self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/w1/app/wt", "prompt": "hi"})  # a worktree under the project does
        ctx =json.loads(out)["hookSpecificOutput"]
        self.assertEqual(ctx["hookEventName"], "UserPromptSubmit")
        did = db.q1("SELECT id FROM decisions WHERE task_key='W1' AND label='Reply'")["id"]
        self.assertIn("BOARD DECISION #%d W1: Reply\nuse the flag\n(ack: sheepdog.py done %d)" % (did, did), ctx["additionalContext"])
        self.assertEqual(db.q1("SELECT delivered_to FROM decisions WHERE id=?", (did,))["delivered_to"], "hook:s1")
        self.assertEqual(self.api_get("/api/decisions/wait?cwd=/work/w1&timeout=0")[1], [])  # claimed by the hook: the waiter gets nothing
        self.assertEqual(self.run_hook({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/w1/app", "prompt": "again"}), "")
        self.assertEqual(self.post("/api/decision/%d/done" % did, {}, token=None)[0], 401)
        self.assertEqual(self.post("/api/decision/999999/done", {})[0], 404)
        self.assertEqual(self.post("/api/decision/%d/done" % did, {})[1]["status"], "done")
        self.assertEqual(db.get_task("W1")["decision"]["status"], "done")
        r = self.cli("done", str(did))
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_wait_long_poll_does_not_block_server(self):
        self.brief("W2", "/work/w2")
        res = []
        th = threading.Thread(target=lambda: res.append(self.api_get("/api/decisions/wait?cwd=/work/w2&timeout=10")))
        th.start()
        time.sleep(0.5)
        t0 = time.time()
        self.state()
        self.assertLess(time.time() - t0, 1)  # other requests are served while a waiter is parked
        self.post("/api/task/W2/decision", {"action": "option", "option": "merge"})
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertEqual(res[0][1][0]["task_key"], "W2")

    def test_wait_cli_prints_block(self):
        self.brief("W3", "/work/w3")
        self.post("/api/task/W3/decision", {"action": "reply", "text": "line one"})
        r = self.cli("wait", "--cwd", "/work/w3")
        did = self.srv.db.q1("SELECT id FROM decisions WHERE task_key='W3'")["id"]
        self.assertEqual((r.returncode, r.stdout), (0, "BOARD DECISION #%d W3: Reply\nline one\n(ack: sheepdog.py done %d)\n" % (did, did)))

    def test_resumed_agent_runs_again_and_relinks(self):
        sid, cwd = "sr", "/work/resume"
        base = {"session_id": sid, "cwd": cwd}
        self.hook(dict(base, hook_event_name="PreToolUse", tool_name="Agent", tool_use_id="tu_r1",
                       tool_input={"description": "Task 41 Build it", "subagent_type": "general-purpose"}))
        self.hook(dict(base, hook_event_name="SubagentStart", agent_id="ra-1", agent_type="general-purpose"))
        self.hook(dict(base, hook_event_name="SubagentStop", agent_id="ra-1", agent_type="general-purpose"))
        ag = lambda: [a for a in self.state()["agents"] if a["id"] == "ra-1"][0]
        self.assertEqual((ag()["status"], ag()["task_key"]), ("done", "T41"))
        self.assertIsNotNone(ag()["ended_at"])
        self.hook(dict(base, hook_event_name="SubagentStart", agent_id="ra-1", agent_type="general-purpose"))  # resumed
        self.assertEqual((ag()["status"], ag()["ended_at"]), ("running", None))
        self.assertIn("ra-1", [a["id"] for a in self.state()["agents"] if a["status"] == "running"])
        msg = lambda to, text: self.hook(dict(base, hook_event_name="PreToolUse", tool_name="SendMessage", tool_input={"to": to, "message": text}))
        msg("ra-1", "carry on, no prefix")
        self.assertEqual(ag()["task_key"], "T41")  # no "Task N" prefix: the link stays
        msg("ra-1", "Task 42 now review the other thing")
        self.assertEqual(ag()["task_key"], "T42")
        self.assertIn("T42", [t["key"] for t in self.state()["tasks"]])  # created like a spawn would
        msg("Task 41 Build it", "Task 43 and again")  # addressed by name
        self.assertEqual(ag()["task_key"], "T43")
        msg("nobody", "Task 44 lost message")  # unknown target: nothing moves, nothing is created
        self.assertEqual(ag()["task_key"], "T43")
        self.assertNotIn("T44", [t["key"] for t in self.state()["tasks"]])
        self.hook(dict(base, hook_event_name="SubagentStop", agent_id="ra-1", agent_type="general-purpose"))
        self.assertEqual(ag()["status"], "done")  # start/stop cycles stay correct

    def post_raw(self, path, body, token=TOKEN):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Content-Type": "application/json"}
        if token:
            h["Authorization"] = "Bearer " + token
        c.request("POST", path, body if isinstance(body, bytes) else json.dumps(body), h)
        r = c.getresponse()
        return r.status, r.read()

    def test_raw_hook_payload_is_trimmed_redacted_and_stored(self):
        self.cli("task", "RH1", "--title", "raw hook")
        self.cli("agent", "rh-ag", "--task", "RH1", "--status", "running")
        raw = {"hook_event_name": "PostToolUseFailure", "session_id": "rh", "cwd": "/x", "agent_id": "rh-ag", "tool_name": "Bash",
               "transcript_path": "/secret/transcript.jsonl", "permission_mode": "default", "tool_use_id": "tu",
               "tool_input": {"command": "make", "description": "d", "timeout": 5, "huge": "y" * 100000},
               "tool_response": {"content": "o" * 3_000_000, "stderr": "boom\npassword=hunter2\nlast line", "interrupted": False},
               "error": "Exit code 3\nboom\npassword=hunter2\nlast line"}
        st, body = self.post_raw("/api/hook", raw)
        self.assertEqual((st, body), (200, b""))  # nothing waiting: an EMPTY reply (curl prints the body into Claude)
        ev = [e for e in self.state()["events"] if e["task_key"] == "RH1" and e["kind"] == "PostToolUseFailure"][0]
        self.assertNotIn("hunter2", ev["data"])
        self.assertNotIn("transcript", json.dumps(ev))
        d = json.loads(ev["data"])
        self.assertLessEqual(len(d["out"]), 8192)
        self.assertLessEqual(len(d["out"].split("\n")), 40)
        self.assertIn("last line", d["out"])
        self.assertEqual(d["exit"], 3)
        once = sheepdog.reduce_hook(raw)
        self.assertEqual(sheepdog.reduce_hook(once), once)  # idempotent: an already reduced payload (old `sheepdog.py hook` client) is fine
        self.assertLess(len(json.dumps(once)), 100000)
        self.assertEqual(self.post_raw("/api/hook", once)[1], b"")
        self.assertEqual(self.post_raw("/api/hook", {"hook_event_name": "Stop"}, token=None)[0], 401)  # curl -f then prints nothing
        self.assertEqual(self.post_raw("/api/hook", {"tool_name": "Bash"})[0], 400)  # no hook_event_name

    def test_hook_reply_is_empty_or_exact_json(self):
        self.brief("H1", "/work/h1")
        self.post("/api/task/H1/decision", {"action": "option", "option": "hold"})
        prompt = {"hook_event_name": "UserPromptSubmit", "session_id": "sh", "prompt": "hi"}
        st, body = self.post_raw("/api/hook", dict(prompt, cwd="/work/other"))
        self.assertEqual((st, body), (200, b""))  # another folder: nothing
        self.assertEqual(self.post_raw("/api/hook", {"hook_event_name": "Stop", "session_id": "sh", "cwd": "/work/h1"}), (200, b""))  # only prompts deliver
        st, body = self.post_raw("/api/hook", dict(prompt, cwd="/work/h1/sub"))
        did = self.srv.db.q1("SELECT id FROM decisions WHERE task_key='H1'")["id"]
        want = sheepdog.hook_json([dict(self.srv.db.q1("SELECT * FROM decisions WHERE id=?", (did,)))])
        self.assertEqual((st, body.decode()), (200, want))
        self.assertEqual(json.loads(body), {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext":
                         "Board decisions waiting for you:\n\nBOARD DECISION #%d H1: Hold\n(ack: sheepdog.py done %d)" % (did, did)}})
        self.assertEqual(self.post_raw("/api/hook", dict(prompt, cwd="/work/h1/sub")), (200, b""))  # delivered once

    def test_body_caps(self):
        big = json.dumps({"hook_event_name": "Stop", "session_id": "big", "cwd": "/big", "pad": "x" * (5 << 20)}).encode()
        self.assertEqual(self.post_raw("/api/hook", big), (200, b""))  # 5 MB is fine on /api/hook
        self.assertEqual(self.post_raw("/api/task", big)[0], 413)  # 1 MB everywhere else
        self.assertEqual(self.post_raw("/api/hook", b" " * (9 << 20))[0], 413)  # and 8 MB is the ceiling

    def test_big_failure_payload_does_not_stall_the_server(self):
        big = json.dumps({"hook_event_name": "PostToolUseFailure", "session_id": "bf", "cwd": "/x", "tool_name": "Bash",
                          "tool_input": {"command": "cat big"}, "tool_response": {"stdout": "x" * (5 << 20)}, "error": "Exit code 1"}).encode()
        res = {}

        def go():
            t0 = time.perf_counter()
            res["st"] = self.post_raw("/api/hook", big)[0]
            res["dt"] = time.perf_counter() - t0
        th = threading.Thread(target=go)
        th.start()
        time.sleep(0.02)
        t0 = time.perf_counter()
        self.post_raw("/api/hook", {"hook_event_name": "Stop", "session_id": "bf2", "cwd": "/y"})
        small = time.perf_counter() - t0
        th.join(20)
        self.assertEqual(res["st"], 200)
        self.assertLess(res["dt"], 0.6)  # a 5 MB failure is reduced and stored quickly
        self.assertLess(small, 0.25)  # and a concurrent small hook is not held up (regexes hold the GIL)
        for hostile in ("x" * 200000, ".".join(["ab"] * 30000), "password" * 5000):  # redaction is linear on long unbroken runs
            t0 = time.perf_counter()
            sheepdog.redact(hostile)
            self.assertLess(time.perf_counter() - t0, 0.5)

    def test_board_page_size_budget(self):
        st, body, _ = self.get("/")  # the rendered page as served, not the template files
        self.assertEqual(st, 200)
        self.assertEqual(body, sheepdog.PAGE.encode())
        self.assertLess(len(body), 90 * 1000)
        self.assertNotRegex(body.decode(), r"\{\{(?:THEME_JS|BASE_CSS)\}\}")  # every placeholder was filled

    def test_architecture_page_covers_routes_tables_and_events(self):
        import html
        import inspect
        import re
        self.assertEqual(self.get("/architecture", token=None)[0], 401)
        st, body, _ = self.get("/architecture")
        page = body.decode()
        self.assertEqual(st, 200)
        self.assertLess(len(body), 16 * 1024)  # its own budget (the drawn diagram and the generated tables are most of it)
        self.assertIn("<svg", page)
        self.assertRegex(page, r"<code>tasks</code></td><td>[^<]*\bparent\b")  # the generated table lists tasks.parent
        self.assertIn("Parents and sub-tasks", page)  # and the hand-written rule is there
        self.assertLess(page.index("<svg"), page.index("<h2>"))  # the diagram comes first, right under the title
        for _m, rx, name, _doc in sheepdog.ROUTES:
            self.assertTrue(hasattr(sheepdog.Handler, "r_" + name), name)
            self.assertIn(html.escape(re.sub(r"\(\?P<(\w+)>[^)]*\)", r"<\1>", rx)), page, rx)
        live = [r["name"] for r in self.srv.db.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        for t in set(live) | set(re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", sheepdog.SCHEMA)):
            self.assertIn("<code>%s</code>" % t, page)
            for col in self.srv.db.q("PRAGMA table_info(%s)" % t):
                self.assertIn(col["name"], page)
        for ev in set(re.findall(r'ev == "(\w+)"', inspect.getsource(sheepdog.DB.ingest))) | set(sheepdog.HOOK_EVENTS):
            self.assertIn(ev, sheepdog.HOOK_EVENTS)  # a handled event nobody documented fails here
            self.assertIn("<code>%s</code>" % ev, page)
        groups = {"/".join(rx.split("/")[:3]) for _m, rx, _n, _d in sheepdog.ROUTES if rx.startswith("/api/")}
        self.assertTrue({"/api/hook", "/api/state", "/api/task", "/api/decisions", "/api/agent"} <= groups)
        for g in groups:
            self.assertIn(g, sheepdog.ARCH_DIAGRAM, "the hand-written diagram does not mention " + g)
        self.assertNotIn('"/api/', inspect.getsource(sheepdog.Handler))  # every route lives in ROUTES, so none can hide from this page

    def test_parent_validation(self):
        up = lambda k, **d: self.srv.db.task_upsert(dict(key=k, **d))
        up("PV1", title="big one")
        up("PV2", title="another big one")
        with self.assertRaises(ValueError):
            up("PV1", parent="PV1")  # not its own parent
        with self.assertRaises(ValueError):
            up("PVX", parent="NOPE-PARENT")  # the parent must exist
        self.assertIsNone(self.srv.db.get_task("PVX"))  # and the refused create left nothing behind
        c = up("PVC", title="kid", parent="PV1")
        self.assertEqual((c["parent"], c["parent_title"]), ("PV1", "big one"))
        with self.assertRaises(ValueError):
            up("PVG", parent="PVC")  # one level only: a sub-task cannot be a parent
        with self.assertRaises(ValueError):
            up("PV1", parent="PV2")  # a task with sub-tasks cannot become a sub-task
        self.assertIsNone(up("PVC", parent="")["parent"])  # "" clears
        self.assertEqual(self.srv.db.get_task("PV1")["children"], [])
        self.assertIsNone(self.srv.db.get_task("PV1")["progress"])
        self.assertEqual(self.srv.db.get_task("PV1")["status"], "planned")  # no children: its own status

    def test_parent_rollup_progress_and_needs_you(self):
        db = self.srv.db
        up = lambda k, **d: db.task_upsert(dict(key=k, **d))

        def parent(p, *statuses):
            up(p, title="parent " + p)
            for i, s in enumerate(statuses):
                up("%s-%d" % (p, i), title="kid %d" % i, status=s, parent=p, waiting="agent")
            return db.get_task(p)
        cases = [(("done", "done"), "done"), (("done", "blocked", "building"), "blocked"), (("building", "testing", "planned"), "review"),
                 (("planned", "review"), "review"), (("analysing", "planned"), "building"), (("planned", "planned"), "planned")]
        for n, (kids, want) in enumerate(cases):
            self.assertEqual(parent("RU%d" % n, *kids)["status"], want, kids)
        p = db.get_task("RU1")
        self.assertEqual(p["children"], ["RU1-0", "RU1-1", "RU1-2"])  # in creation order
        self.assertEqual(p["progress"], {"total": 3, "by_status": {"building": 1, "blocked": 1, "done": 1}, "done": 1})
        d = db.get_task("RU0")  # all done: started = the earliest child, total runs until the last child is done
        kids = [db.get_task(k) for k in d["children"]]
        self.assertEqual((d["created_at"], d["end_at"]), (min(k["created_at"] for k in kids), max(k["end_at"] for k in kids)))
        self.assertFalse(db.get_task("RU2")["needs_you"])  # nobody waits on me
        up("RU2-0", waiting="me")  # a sub-task that needs you makes the parent need you
        self.assertTrue(db.get_task("RU2")["needs_you"])
        up("RU2-0", waiting="agent")
        self.assertFalse(db.get_task("RU2")["needs_you"])
        up("RU2-1", status="review", waiting="")  # review with no waiting set counts as needing you
        self.assertTrue(db.get_task("RU2")["needs_you"])
        cur = self.state()["cursor"]
        up("RU4-0", status="done")  # a changed child re-sends its parent (the page only polls what changed)
        self.assertIn("RU4", [t["key"] for t in self.state(cur)["tasks"]])
        child = [t for t in self.state()["tasks"] if t["key"] == "RU4-0"][0]
        self.assertEqual((child["parent"], child["parent_title"]), ("RU4", "parent RU4"))

    def test_cli_parent_set_and_clear(self):
        self.cli("task", "CP1", "--title", "big")
        t = json.loads(self.cli("task", "CP2", "--title", "piece", "--parent", "CP1").stdout)
        self.assertEqual(t["parent"], "CP1")
        self.assertEqual(json.loads(self.cli("task", "CP2", "--note", "x").stdout)["parent"], "CP1")  # omitted: unchanged
        self.assertEqual(json.loads(self.cli("task", "CP2", "--parent", "").stdout)["parent"], None)
        r = self.cli("task", "CP3", "--parent", "NOPE-PARENT")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("no such parent", r.stderr)

    def test_stale_agent(self):
        self.cli("task", "ST1", "--title", "stale")
        self.cli("agent", "old-agent", "--task", "ST1", "--status", "running")
        self.cli("agent", "new-agent", "--task", "ST1", "--status", "running")
        self.srv.db.c.execute("UPDATE agents SET last_event_at=?, rev=? WHERE id='old-agent'",
                              (int(time.time()) - 3 * 3600, self.srv.db.bump()))
        st = {a["id"]: a["status"] for a in self.state()["agents"]}
        self.assertEqual((st["old-agent"], st["new-agent"]), ("stale", "running"))

    def test_prune(self):
        db = sheepdog.DB(os.path.join(self.tmp.name, "prune.db"))
        self.addCleanup(db.close)
        old = int(time.time()) - 40 * 86400
        for i in range(5):
            db.event("x", "old%d" % i, ts=old)
        for i in range(10):
            db.event("x", "new%d" % i)
        db.prune()
        self.assertEqual(len(db.q("SELECT * FROM events")), 10)
        db.prune(cap=4)
        left = [e["summary"] for e in db.q("SELECT * FROM events ORDER BY id")]
        self.assertEqual(left, ["new%d" % i for i in range(6, 10)])


class AllowlistTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = os.path.realpath(self.tmp.name)
        for d in ("proj/app", "proj-old", "other/proj"):
            os.makedirs(os.path.join(self.root, d))
        os.symlink(os.path.join(self.root, "proj"), os.path.join(self.root, "link"))

    def test_matching(self):
        j = lambda *a: os.path.join(self.root, *a)
        roots = sheepdog.parse_allow(j("proj"))
        for cwd, want in ((j("proj"), True), (j("proj", "app"), True), (j("proj", "gone", "deep"), True), (j("link", "app"), True),
                          (j("proj-old"), False), (j("other", "proj"), False), (j("proj", "..", "other"), False), ("relative/proj", False),
                          ("/", False), ("", True)):
            self.assertEqual(sheepdog.allowed(roots, cwd), want, cwd)
        self.assertTrue(sheepdog.allowed([], "/anywhere"))  # no allowlist: every folder
        two = sheepdog.parse_allow(j("proj") + os.pathsep + j("other"), [j("proj-old")])
        self.assertEqual(len(two), 3)
        self.assertTrue(sheepdog.allowed(two, j("proj-old")) and sheepdog.allowed(two, j("other", "proj")))
        self.assertEqual(sheepdog.parse_allow("~/x"), [os.path.realpath(os.path.expanduser("~/x"))])

    def test_outside_hooks_change_nothing_and_never_deliver(self):
        db = sheepdog.DB(os.path.join(self.tmp.name, "a.db"))
        self.addCleanup(db.close)
        db.allow = sheepdog.parse_allow("/work/in")
        db.task_upsert({"key": "T1", "title": "t", "worktree": "/work/out/wt"})
        db.set_brief("T1", {"brief": "x", "cwd": "/work/in", "options": [{"id": "go", "label": "Go"}]})
        db.decide("T1", {"action": "option", "option": "go"})
        rows = lambda: [db.q1("SELECT COUNT(*) n FROM %s" % t)["n"] for t in ("events", "agents", "tasks", "decisions")]
        before, rev = rows(), db.rev
        for p in ({"hook_event_name": "SessionStart", "session_id": "o", "cwd": "/work/out/wt"}, {"hook_event_name": "SessionStart", "session_id": "o", "cwd": "/work/in-old"},
                  {"hook_event_name": "PreToolUse", "session_id": "o", "cwd": "/work/out", "tool_name": "Agent", "tool_use_id": "t", "tool_input": {"description": "Task 9 x"}},
                  {"hook_event_name": "PreToolUse", "session_id": "o", "cwd": "/work/out", "tool_name": "SendMessage", "tool_input": {"to": "a", "message": "Task 5 x"}},
                  {"hook_event_name": "UserPromptSubmit", "session_id": "o", "cwd": "/work/out/wt", "prompt": "hi"}):
            self.assertEqual(db.ingest(p), [])
        self.assertEqual((rows(), db.rev), (before, rev))
        self.assertEqual(db.q1("SELECT status FROM decisions")["status"], "pending")  # an outside prompt never delivers
        self.assertEqual(len(db.ingest({"hook_event_name": "UserPromptSubmit", "session_id": "i", "cwd": "/work/in/x", "prompt": "hi"})), 1)
        db.ingest({"hook_event_name": "Stop", "session_id": "nocwd"})  # no cwd at all: accepted
        self.assertEqual(db.q1("SELECT COUNT(*) n FROM events WHERE kind='Stop'")["n"], 1)
        with self.assertRaises(ValueError):
            db.ingest({"session_id": "x", "cwd": "/work/out"})


CORPUS = json.load(open(os.path.join(HERE, "contract", "rewrite-cases.json")))


def fake_scrubber():
    c = CORPUS["config"]
    return sheepdog.Scrubber(c["roots"], c["home"], c["user"], c["host"], c["tmpdir"])


class ScrubberTest(unittest.TestCase):
    def test_corpus_and_idempotence(self):  # the Go server answers the same file (internal/store TestRewriteMatchesPython)
        s = fake_scrubber()
        for src, want in CORPUS["text"]:
            self.assertEqual(s.text(src), want, src)
            self.assertEqual(s.text(want), want, "idempotent: " + want)
        for src, want in CORPUS["cwd"]:
            self.assertEqual(s.cwd(src), want, src)
            self.assertEqual(s.cwd(want), want, "idempotent: " + want)

    def test_live_example_and_generic_user_names(self):
        live = "cd /home/ubuntu/dev/alpha/.claude/worktrees/t12-role-ui/e2e; rm -rf tests/e2e/org-ubuntu/tmp; ls"
        generic = sheepdog.Scrubber(["/home/ubuntu/dev/alpha"], home="/home/ubuntu", user="ubuntu")
        want = "cd alpha:.claude/worktrees/t12-role-ui/e2e; rm -rf tests/e2e/org-ubuntu/tmp; ls"  # only the real path changes; org-ubuntu is a folder name
        self.assertEqual(generic.text(live), want)
        self.assertEqual(generic.text(want), want)
        jdoe = sheepdog.Scrubber(["/Users/jdoe/dev/alpha"], home="/Users/jdoe", user="jdoe")
        once = jdoe.text("rm -rf tests/e2e/org-jdoe/tmp; cat /Users/jdoe/dev/alpha/x")
        self.assertEqual(once, "rm -rf tests/e2e/org-<user>/tmp; cat alpha:x")  # the /tmp after <user> is not a temp path
        self.assertEqual(jdoe.text(once), once)
        for u in ("admin", "administrator", "root", "user", "ubuntu", "debian", "ec2-user", "centos", "fedora", "dev", "developer", "runner", "git",
                  "www-data", "node", "app", "vagrant", "pi", "guest", "test", "Admin", "ab"):
            s = sheepdog.Scrubber([], home="/Users/" + u, user=u)
            self.assertEqual(s.text("the %s group, org-%s/x, /Users/%s/proj" % (u, u, u)), "the %s group, org-%s/x, ~/proj" % (u, u), u)

    def test_idempotent_on_every_input(self):
        s = fake_scrubber()
        for src, _ in CORPUS["text"]:
            once = s.text(src)
            self.assertEqual(s.text(once), once, src)
        for src, _ in CORPUS["cwd"]:
            once = s.cwd(src)
            self.assertEqual(s.cwd(once), once, src)

    def test_task_notification_shape_and_dash_encoded_paths(self):
        s = sheepdog.Scrubber(["/Users/jdoe/dev/alpha"], home="/Users/jdoe", user="jdoe")
        for src, want in {
            "<task-id>a1b2</task-id> <output-file>/private/tmp/claude-12/-Users-jdoe-dev-alpha/abc/tasks/x.output": "<task-id>a1b2</task-id> <output-file><tmp>/x.output",
            "<output-file>/private/tmp/claude-12/-Users-jdoe-dev-alpha-wo...": "<output-file><tmp>/alpha:-wo...",
            "<output-file>/private/tmp/claude-12/-Users-jdoe-wo...": "<output-file><tmp>/~-wo...",
            ">/tmp/y": "><tmp>/y", "2>/tmp/err.log": "2><tmp>/err.log", "<user>/tmp/x": "<user>/tmp/x", "<tmp>/Users/jdoe/x": "<tmp>/Users/<user>/x",
            "-Users-jdoe-dev-alpha": "alpha:", "x/-Users-jdoe-dev-alpha/s.jsonl": "x/alpha:/s.jsonl", "-Users-jdoe-dev-alpha-sub": "alpha:-sub",
            "-Users-jdoe": "~", "-home-jdoe-x": "~-x", "-Users-jdoe2-x": "-Users-jdoe2-x", "a-Users-jdoe-x": "a-Users-<user>-x",
        }.items():
            self.assertEqual(s.text(src), want, src)
            self.assertEqual(s.text(want), want, "idempotent: " + want)

    def test_rewrite_comes_before_the_cuts(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = sheepdog.DB(os.path.join(tmp, "c.db"))
            self.addCleanup(db.close)
            db.scrub = sheepdog.Scrubber(["/Users/jdoe/dev/alpha"], home="/Users/jdoe", user="jdoe")
            cmd = "c" * 1980 + " /Users/jdoe/dev/alpha/file.txt"  # the 2000-character cut would land inside the home path
            db.ingest({"hook_event_name": "PostToolUseFailure", "session_id": "s", "cwd": "/Users/jdoe/dev/alpha", "tool_name": "Bash",
                       "tool_input": {"command": cmd}, "error": "Exit code 1"})
            got = json.loads(db.q1("SELECT data FROM events WHERE kind='PostToolUseFailure'")["data"])["cmd"]
            self.assertTrue(got.endswith(" alpha:file.txt"), got[-40:])
            self.assertNotIn("jd", got)
            db.task_upsert({"key": "C1", "title": "t " + "y" * 480 + " /Users/jdoe/dev/alpha/zzz"})
            title = db.get_task("C1")["title"]
            self.assertTrue(title.endswith("alpha:zzz") and "jd" not in title, title[-30:])

    def test_symlinked_folder_lands_in_the_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            real = os.path.realpath(tmp)
            os.makedirs(os.path.join(real, "proj", "sub"))
            os.symlink(os.path.join(real, "proj"), os.path.join(real, "link"))
            s = sheepdog.Scrubber([os.path.join(real, "proj")], home="/Users/jdoe", user="jdoe")
            self.assertEqual(s.cwd(os.path.join(real, "link", "sub")), "proj:sub")
            self.assertEqual(s.cwd(os.path.join(real, "proj")), "proj:")

    def test_degenerate_names_are_left_alone(self):
        s = sheepdog.Scrubber([], home="/root", user="root", host="localhost")
        self.assertEqual(s.text("root at localhost, user host"), "root at localhost, user host")
        self.assertEqual(sheepdog.Scrubber([], user="ab").text("ab ab"), "ab ab")


class PrivacyTest(unittest.TestCase):
    """nothing that names the person or the machine reaches the database, the API or the CLI output, and delivery still matches folders."""
    FORBIDDEN = ("jdoe", "jdoes-mbp", "/Users/", "/home/", "/private/var/folders", "var/folders", "/tmp/", "abc123")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.srv = sheepdog.make_server(os.path.join(self.tmp.name, "p.db"), "127.0.0.1", 0, TOKEN)
        self.srv.db.scrub = fake_scrubber()
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.srv.shutdown(), self.srv.server_close()))
        self.env = dict(os.environ, SHEEPDOG_URL="http://127.0.0.1:%d" % self.port, SHEEPDOG_TOKEN=TOKEN)

    def call(self, method, path, body=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port)
        c.request(method, path, json.dumps(body) if body is not None else None, {"Authorization": "Bearer " + TOKEN, "Content-Type": "application/json"})
        r = c.getresponse()
        return r.status, r.read().decode()

    def cli(self, *a):
        return subprocess.run([sys.executable, os.path.join(HERE, "sheepdog.py")] + list(a), env=self.env, capture_output=True, text=True, timeout=20)

    def hook(self, **p):
        base = {"session_id": "s-1", "cwd": "/Users/jdoe/dev/alpha/web"}
        self.assertEqual(self.call("POST", "/api/hook", dict(base, **p))[0], 200)

    def test_nothing_identifying_is_stored_or_served(self):
        home, a, tmp = "/Users/jdoe", "/Users/jdoe/dev/alpha", "/private/var/folders/xy/abc123/T"
        self.cli("task", "P1", "--title", "fix %s/x.go for jdoe" % a, "--project", "jdoes-mbp", "--branch", "jdoe/fix", "--worktree", a + "/wt",
                 "--next", "ask jdoe@example.com about %s/.ssh" % home, "--note", "ran in %s/run1 on jdoes-mbp.local" % tmp)
        self.cli("agent", "ag-1", "--task", "P1", "--name", "jdoe's helper in %s" % a, "--model", "model-jdoe")
        md = os.path.join(self.tmp.name, "b.md")
        with open(md, "w") as f:
            f.write("## Review %s/web/a.go\nrun by jdoe on jdoes-mbp, logs /home/jdoe/logs/x.log and %s/out.txt\n" % (a, tmp))
        r = self.cli("brief", "P1", "--file", md, "--option", "go|Go for jdoe|%s/hint" % home, "--cwd", a + "/web")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.call("POST", "/api/task/P1/decision", {"action": "reply", "text": "use %s/other and ask jdoe" % a})
        ev = {"SessionStart": dict(source="startup in /Users/jdoe", model="m-jdoe"), "SessionEnd": dict(reason="done at /Users/jdoe/x"), "Stop": {},
              "UserPromptSubmit": dict(prompt="look at %s/z.go, jdoe@example.com, token=abc123secret, %s/f" % (a, tmp)),
              "Notification": dict(message="jdoe needs %s/perm" % a, notification_type="permission_prompt"),
              "SubagentStart": dict(agent_id="ag-1", agent_type="type-jdoe"), "SubagentStop": dict(agent_id="ag-1", agent_type="type-jdoe"),
              "PostToolUse": dict(tool_name="Bash", agent_id="ag-1", tool_input={"command": "cd %s && make && cat /home/jdoe/.zshrc %s/o" % (a, tmp), "description": "jdoes-mbp"}),
              "PostToolUseFailure": dict(tool_name="Bash", agent_id="ag-1", tool_input={"command": "go test %s/..." % a}, error="Exit code 2\n%s/a.go:3: jdoe broke it\nat %s/b" % (a, tmp),
                                         tool_response={"stderr": "/home/jdoe/go/pkg/mod/x\n/Users/jdoe/Library/y", "stdout": "host jdoes-mbp.local"}),
              "PreToolUse": dict(tool_name="Agent", tool_use_id="tu-1", tool_input={"description": "Task 5 edit %s/q.go as jdoe" % a, "subagent_type": "t-jdoe", "prompt": "x"}),
              "PreCompact": dict(trigger="jdoe")}
        for name, extra in ev.items():
            self.hook(hook_event_name=name, **extra)
        self.hook(hook_event_name="PostToolUse", tool_name="Edit", tool_input={"file_path": a + "/web/e.go"})
        self.hook(hook_event_name="PostToolUse", tool_name="Grep", tool_input={"pattern": "jdoe", "path": home + "/src"})
        self.hook(hook_event_name="PostToolUse", tool_name="Agent", tool_use_id="tu-1", tool_input={"description": "Task 5 edit %s/q.go" % a},
                  tool_response={"status": "completed", "agentId": "ag-9", "totalTokens": 5})
        self.hook(hook_event_name="PreToolUse", tool_name="SendMessage", tool_input={"to": "ag-1", "message": "Task 6 go to %s/x, jdoe" % a})
        self.hook(hook_event_name="UserPromptSubmit", prompt="deliver", cwd=a + "/web/deep")
        c = sqlite3.connect(self.srv.db.c.execute("PRAGMA database_list").fetchone()[2])
        dump = []
        for t in ("tasks", "agents", "events", "decisions"):
            dump += [dict(zip([d[0] for d in c.execute("SELECT * FROM %s" % t).description], row)) for row in c.execute("SELECT * FROM %s" % t)]
        c.close()
        out = [json.dumps(dump), self.call("GET", "/api/state")[1], self.call("GET", "/api/standup?off=0")[1], self.call("GET", "/api/task/P1/events")[1],
               self.call("GET", "/api/decisions/wait?cwd=%s&timeout=0" % a)[1], self.call("GET", "/")[1], self.call("GET", "/architecture")[1]]
        out += [self.cli("task", "P1", "--note", "again from %s" % a).stdout]
        for blob in out:
            for bad in self.FORBIDDEN:
                self.assertNotIn(bad, blob.replace("/Users/<name>", ""), bad)
        self.assertIn("alpha:web/a.go", self.call("GET", "/api/state")[1])  # rewritten, not dropped

    def test_delivery_still_matches_rewritten_folders(self):
        a = "/Users/jdoe/dev/alpha"
        md = os.path.join(self.tmp.name, "b.md")
        with open(md, "w") as f:
            f.write("## r")
        for key, cwd in (("D1", a + "/app"), ("D2", a), ("D3", "/Users/jdoe/other")):
            self.cli("task", key, "--title", key, "--status", "review")
            self.assertEqual(self.cli("brief", key, "--file", md, "--option", "go|Go", "--cwd", cwd).returncode, 0)
            self.assertEqual(self.call("POST", "/api/task/%s/decision" % key, {"action": "option", "option": "go"})[0], 200)
        self.assertEqual({r["task_key"]: r["cwd"] for r in self.srv.db.q("SELECT task_key, cwd FROM decisions")}, {"D1": "alpha:app", "D2": "alpha:", "D3": "~/other"})
        claim = lambda cwd: [d["task_key"] for d in self.srv.db.ingest({"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": cwd, "prompt": "hi"})]
        self.assertEqual(claim(a + "/appx"), ["D2"])  # a sibling of app is not under D1; the brief for the root itself covers everything below it
        self.cli("brief", "D2", "--file", md, "--option", "go|Go", "--cwd", a)
        self.assertEqual(claim("/Users/jdoe/dev"), [])  # a parent of the root claims nothing
        self.assertEqual(claim(a + "/app/src/deep"), ["D1"])  # a sub-folder of the brief's folder claims it
        self.assertEqual(claim("/Users/jdoe/other/x"), ["D3"])  # also outside any root (the "~" form)
        # the waiter: --cwd is rewritten the same way, so a wait for the root claims what is left under it
        self.cli("brief", "D1", "--file", md, "--option", "go|Go", "--cwd", a + "/app")
        self.call("POST", "/api/task/D1/decision", {"action": "option", "option": "go"})
        st, body = self.call("GET", "/api/decisions/wait?cwd=%s&timeout=0" % a)
        self.assertEqual([d["task_key"] for d in json.loads(body)], ["D1"])
        self.assertTrue(self.srv.db.waiter_for("alpha:app"))


def span(s):  # Python twin of the page's span(): compact units
    s = max(0, int(s))
    return "%ds" % s if s < 60 else "%dm" % (s // 60) if s < 3600 else "%dh %dm" % (s // 3600, s % 3600 // 60) if s < 86400 else "%dd %dh" % (s // 86400, s % 86400 // 3600)


class TotalTimeTest(unittest.TestCase):
    def test_span_and_total(self):
        self.assertEqual([span(x) for x in (-5, 45, 60, 720, 4980, 100800)], ["0s", "45s", "1m", "12m", "1h 23m", "1d 4h"])
        with tempfile.TemporaryDirectory() as tmp:
            db = sheepdog.DB(os.path.join(tmp, "t.db"))
            self.addCleanup(db.close)
            db.task_upsert({"key": "D1", "title": "x", "status": "done"})
            db.task_upsert({"key": "R1", "title": "x", "status": "building"})
            db.c.execute("UPDATE tasks SET created_at=1000, updated_at=1000+4980, done_at=NULL WHERE key='D1'")  # old done task, no done_at
            d, r = db.get_task("D1"), db.get_task("R1")
            self.assertEqual(span(d["end_at"] - d["created_at"]), "1h 23m")  # fell back to updated_at
            self.assertIsNone(r["end_at"])  # running: the page computes now - created_at


CHROME = next(filter(os.path.exists, ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]), None) \
    or next(filter(None, map(shutil.which, ["google-chrome", "google-chrome-stable", "chromium", "chromium-browser"])), None)


@unittest.skipUnless(CHROME, "no Chrome binary")
class PageSmokeTest(unittest.TestCase):
    """runs the real page JS headless: catches errors no Python test sees (e.g. const reassignment)"""

    def dom(self, mangle=None, hash=""):
        with tempfile.TemporaryDirectory() as tmp:
            srv = sheepdog.make_server(os.path.join(tmp, "t.db"), "127.0.0.1", 0, None)
            if mangle:
                sheepdog.PAGE, orig = mangle(sheepdog.PAGE), sheepdog.PAGE
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            url = "http://127.0.0.1:%d" % srv.server_address[1]
            env = dict(os.environ, SHEEPDOG_URL=url)
            cli = lambda *a: subprocess.run([sys.executable, os.path.join(HERE, "sheepdog.py")] + list(a), env=env, check=True, capture_output=True)
            cli("task", "S1", "--title", "needs", "--status", "review", "--waiting", "me")
            cli("task", "S2", "--title", "building", "--status", "building")
            cli("task", "S3", "--title", "finished", "--status", "done")
            cli("agent", "a1", "--name", "Runner", "--task", "S2", "--status", "running")
            cli("agent", "a3", "--name", "Finisher", "--task", "S3", "--status", "done")
            for aid in ("a1", "a3"):  # one failed shell command on an active task and one on a done task
                urllib.request.urlopen(urllib.request.Request(url + "/api/hook", json.dumps({
                    "hook_event_name": "PostToolUseFailure", "session_id": "s", "cwd": "/x", "agent_id": aid, "tool_name": "Bash",
                    "tool_input": {"command": "rg nothing"}, "error": "Exit code 1"}).encode(), {"Content-Type": "application/json"})).read()
            cli("task", "S4", "--title", "big task")  # a parent with one done and one open sub-task
            cli("task", "S5", "--title", "open piece", "--status", "building", "--waiting", "me", "--parent", "S4")
            cli("task", "S6", "--title", "done piece", "--status", "done", "--parent", "S4")
            md = os.path.join(tmp, "brief.md")
            with open(md, "w") as fh:  # hostile markdown: the page must escape it and link only http(s)
                fh.write("## Check\nA <script>alert(1)</script> [bad](javascript:alert(1)) [ok](https://example.com/x)\n"
                         "- [ ] item **bold**\n\n| a | b |\n|---|---|\n| 1 | 2 |\n")
            cli("brief", "S1", "--file", md, "--option", "merge|Approve and merge|ships it", "--option", "hold|Hold", "--primary", "merge", "--cwd", "/x")
            base = {"session_id": "s", "cwd": "/x", "tool_name": "Agent", "tool_use_id": "t1", "tool_input": {"description": "Doomed"}}
            for ev in ("PreToolUse", "PostToolUseFailure"):
                urllib.request.urlopen(urllib.request.Request(url + "/api/hook", json.dumps(dict(base, hook_event_name=ev, error="boom")).encode(),
                                                              {"Content-Type": "application/json"})).read()
            try:
                return subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--virtual-time-budget=4000", "--user-data-dir=" + tmp + "/c",
                                       "--dump-dom", url + ("#" + hash if hash else "")], capture_output=True, text=True, timeout=60).stdout
            finally:
                if mangle:
                    sheepdog.PAGE = orig
                srv.shutdown()
                srv.server_close()

    def check(self, dom):
        self.assertRegex(dom, r'id="upd">Live')
        self.assertNotIn("data-err", dom)
        self.assertIn("finished", dom)
        for marker in ('id="n-S1"',  # needs-you dock row
                       'id="t-S2"', 'id="at-needs"',  # board card, attention chip
                       'id="rl-planned"', 'id="tab-ov"', 'id="tab-ac"', 'id="sbt"', 'id="thm"',  # empty-column rail, sidebar tabs, collapse toggle, theme switch
                       'id="t-S4"', '1 / 2 done', 'class="pbar"', 'data-pf="S4"',  # the parent card: progress count + bar + "Show only these"
                       'class="bg pc"'):  # a sub-task wears its parent as a chip
            if marker == 'class="bg pc"':
                self.assertRegex(dom, r'class="bg pc"[^>]*data-k="S4"')
                continue
            self.assertIn(marker, dom)

    def test_page_renders(self):
        dom = self.dom()
        self.check(dom)
        self.assertRegex(dom, r'id="rv-S1"[^>]*>Open review<')  # the dock row has ONE button
        import re
        card = lambda k: re.search(r'id="t-%s".*?(?=id="t-|\Z)' % k, dom, re.S).group(0)
        self.assertIn("1 failed</span>", card("S2"))  # an active task keeps its failed-commands chip
        self.assertNotIn("failed</span>", card("S3"))  # a done task shows none (the header counter is unchanged)
        self.assertNotIn('data-opt="merge"', dom)  # no option buttons in the dock
        self.assertNotIn('id="ap-S1"', dom)  # no Approve & merge
        self.assertRegex(dom, r'<div id="panel"[^>]*hidden')  # closed until opened

    def test_review_section_renders_and_escapes(self):
        dom = self.dom(hash="S1")  # deep link opens the central review dialog
        self.assertNotIn("data-err", dom)
        panel = dom[dom.index('<div id="panel"'):].split(">", 1)[0]
        self.assertIn('role="dialog"', panel)
        self.assertIn('aria-modal="true"', panel)
        self.assertNotIn("hidden", panel)  # open
        self.assertRegex(dom, r'id="ptab-rv"[^>]*aria-selected="true"')  # Review tab is the default with a brief
        for marker in ('id="rv-md"', 'id="rv-opts"', 'data-opt="merge"', 'data-opt="hold"', '<input type="checkbox"', "<table>",
                       'href="https://example.com/x"', "&lt;script&gt;alert(1)&lt;/script&gt;"):
            self.assertIn(marker, dom)
        self.assertNotIn("<script>alert(1)", dom)  # escaped, never raw
        self.assertNotIn('href="javascript', dom)  # only http(s) becomes a link

    def test_css_colors_live_only_in_root_tokens(self):
        import re
        for name, src in (("page", sheepdog.PAGE), ("login", sheepdog.LOGIN), ("architecture", None)):
            if src is None:
                tmp = tempfile.TemporaryDirectory()
                self.addCleanup(tmp.cleanup)
                db = sheepdog.DB(os.path.join(tmp.name, "arch.db"))
                self.addCleanup(db.close)  # cleanups run last-in first-out: the DB closes before its directory goes
                src = sheepdog.arch_page(db)
            full = "".join(re.findall(r"<style>(.*?)</style>", src, re.S))
            media = re.findall(r"@media\(prefers-color-scheme:dark\)\{:root:not\(\[data-theme=light\]\)\{([^{}]*)\}\}", full)
            dark = re.findall(r"\[data-theme=dark\]\{([^{}]*)\}", full)
            light = re.findall(r":root\{([^{}]*)\}", full)
            self.assertTrue(media and dark and light, name + ": light, dark and system blocks")
            toks = lambda b: set(re.findall(r"--([\w-]+):", b))
            self.assertEqual(toks(light[0]) - toks(dark[0]), set(), name + ": a light token missing from dark")
            self.assertEqual(toks(dark[0]), toks(light[0]), name + ": dark has extra tokens")
            self.assertEqual("".join(media[0].split()), "".join(dark[0].split()), name + ": the System block must equal the dark block")
            css = re.sub(r"@media\(prefers-color-scheme:dark\)\{:root:not\(\[data-theme=light\]\)\{[^{}]*\}\}", "", full)
            css = re.sub(r"\[data-theme=dark\]\{[^{}]*\}", "", css)
            css = re.sub(r":root\{[^{}]*\}", "", css)  # the three theme blocks are the only place colors may live
            bodies = "".join(re.findall(r"\{([^{}]*)\}", css))  # declarations only, so #id selectors are not mistaken for colors
            self.assertEqual(re.findall(r"#[0-9A-Fa-f]{3,8}\b|rgba?\(", bodies), [], name)
            self.assertLess(src.index("localStorage.getItem('theme')"), src.index("<style>"), name + ": theme applied before first paint")
            root = light[0]
            used = set(re.findall(r"var\(--([\w-]+)\)", css))
            js_set = {"gc", "w"}  # set from the page's script (grid columns, dialog width)
            self.assertEqual({u for u in used - js_set if "--%s:" % u not in root and "--%s:" % u not in css}, set(), name + ": a var() with no token")

    def test_body_classes_do_not_collide_with_utility_classes(self):
        # a class toggled on <body> that is also a standalone utility selector (e.g. `.mo{font:12px mono}`) restyles the whole page
        import re
        names = set(re.findall(r"document\.body\.classList\.(?:add|toggle|remove)\('([\w-]+)'", sheepdog.PAGE))
        self.assertIn("dlg-open", names)
        for n in names:
            self.assertIsNone(re.search(r"(?:^|[}{,;\s])\.%s\s*[{,]" % re.escape(n), sheepdog.PAGE, re.M), n)
        self.assertNotIn("classList.add('mo')", sheepdog.PAGE)

    def test_catches_const_reassign(self):  # proves the smoke test has teeth
        with self.assertRaises(AssertionError):
            self.check(self.dom(lambda h: h.replace("let F={}", "const F={}")))


if __name__ == "__main__":
    unittest.main()
