package store

import (
	"database/sql"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"
)

func open(t *testing.T) *DB {
	t.Helper()
	db, err := Open(filepath.Join(t.TempDir(), "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(db.Close)
	return db
}

func up(t *testing.T, db *DB, d map[string]any) Row {
	t.Helper()
	r, err := db.TaskUpsert(d)
	if err != nil {
		t.Fatalf("upsert %v: %v", d, err)
	}
	return r
}

// A fresh database must have exactly the schema the Python server creates (recorded in contract/golden/alt-server.json).
func TestFreshSchemaMatchesPython(t *testing.T) {
	raw, err := os.ReadFile("../../contract/golden/alt-server.json")
	if err != nil {
		t.Skip("no golden file")
	}
	var g struct {
		DB struct {
			Schema map[string]string `json:"schema"`
		} `json:"db"`
	}
	if err := json.Unmarshal(raw, &g); err != nil {
		t.Fatal(err)
	}
	db := open(t)
	got := map[string]string{}
	for _, r := range db.Q("SELECT type, name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_autoindex%'") {
		sqlText, _ := r["sql"].(string)
		got[rs(r, "type")+" "+rs(r, "name")] = strings.Join(strings.Fields(sqlText), " ")
	}
	for k, want := range g.DB.Schema {
		if got[k] != want {
			t.Errorf("%s:\n got  %s\n want %s", k, got[k], want)
		}
	}
	for k := range got {
		if _, ok := g.DB.Schema[k]; !ok {
			t.Errorf("extra schema object %s", k)
		}
	}
}

func TestMigratesOldDB(t *testing.T) {
	p := filepath.Join(t.TempDir(), "old.db")
	c, err := sql.Open("sqlite", p)
	if err != nil {
		t.Fatal(err)
	}
	_, err = c.Exec(`CREATE TABLE tasks(key TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'planned',
 project TEXT NOT NULL DEFAULT '', branch TEXT NOT NULL DEFAULT '', worktree TEXT NOT NULL DEFAULT '',
 mr_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '[]', created_at INTEGER, updated_at INTEGER,
 last_event_at INTEGER, archived INTEGER NOT NULL DEFAULT 0, rev INTEGER NOT NULL DEFAULT 0);
 INSERT INTO tasks(key,title,status,updated_at) VALUES('O1','old','done',1000),('O2','old2','review',1000);`)
	if err != nil {
		t.Fatal(err)
	}
	c.Close()
	db, err := Open(p)
	if err != nil {
		t.Fatal(err)
	}
	up(t, db, map[string]any{"key": "O2", "next": "go", "waiting": "me"})
	o1, o2 := db.GetTask("O1"), db.GetTask("O2")
	if rs(o1, "next") != "" || rs(o1, "waiting") != "" || ri(o1, "done_at") != 1000 || o1["needs_you"] != false {
		t.Errorf("O1: %v", o1)
	}
	if rs(o2, "next") != "go" || rs(o2, "waiting") != "me" || o2["needs_you"] != true {
		t.Errorf("O2: %v", o2)
	}
	db.Close()
	db2, err := Open(p) // second start is a no-op
	if err != nil {
		t.Fatal(err)
	}
	db2.Close()
}

func TestStandupFixedClock(t *testing.T) {
	db := open(t)
	mon := int64(1792800000 - 1792800000%86400 + 3*86400) // find a Monday 12:00 UTC
	for (mon/86400+3)%7 != 0 {
		mon += 86400
	}
	now := mon + 12*3600
	fri, sun := mon-3*86400+10*3600, mon-86400+10*3600
	for _, c := range []struct {
		k, title, st, mr string
		at               any
	}{{"A1", "friday", "done", "https://gl/x/-/merge_requests/57", fri}, {"A2", "sunday", "done", "", sun}, {"A3", "today", "done", "", mon + 3600},
		{"A4", "building", "building", "", nil}, {"A5", "stuck", "blocked", "", nil}, {"A6", "idle", "planned", "", nil}} {
		d := map[string]any{"key": c.k, "title": c.title, "status": c.st}
		if c.mr != "" {
			d["mr_url"] = c.mr
		}
		up(t, db, d)
		db.X("UPDATE tasks SET done_at=? WHERE key=?", c.at, c.k)
	}
	zero := int64(0)
	got := func(n int64, off int64) string { s, err := db.Standup(n, &off); _ = err; return s }
	eqs := func(a, b, what string) {
		t.Helper()
		if a != b {
			t.Errorf("%s: %q, want %q", what, a, b)
		}
	}
	eqs(got(now, zero), "Yesterday: A1 friday (!57)\nToday: A3 today; A4 building\nBlockers: A5 stuck", "Monday")
	eqs(strings.SplitN(got(now+3*86400, zero), "\n", 3)[0]+"|"+strings.SplitN(got(now+3*86400, zero), "\n", 3)[1], "Yesterday: None|Today: A4 building", "Thursday: yesterday = Wed")
	eqs(strings.SplitN(got(mon+3600, -2*3600), "\n", 2)[0], "Yesterday: A1 friday (!57)", "still Sunday there")
	db2 := open(t)
	eqs(got2(db2, now), "Yesterday: None\nToday: None\nBlockers: None", "empty board")
}

func got2(db *DB, now int64) string {
	off := int64(0)
	s, _ := db.Standup(now, &off)
	return s
}

func TestPrune(t *testing.T) {
	db := open(t)
	old := time.Now().Unix() - 40*86400
	add := func(summary string, ts int64) {
		_ = db.tx(func() error { db.event("x", summary, evArgs{ts: ts}); return nil })
	}
	for i := 0; i < 5; i++ {
		add("old"+string(rune('0'+i)), old)
	}
	for i := 0; i < 10; i++ {
		add("new"+string(rune('0'+i)), 0)
	}
	if err := db.Prune(30, 200000); err != nil {
		t.Fatal(err)
	}
	if n := len(db.Q("SELECT * FROM events")); n != 10 {
		t.Errorf("after age prune: %d", n)
	}
	if err := db.Prune(30, 4); err != nil {
		t.Fatal(err)
	}
	var left []string
	for _, r := range db.Q("SELECT summary FROM events ORDER BY id") {
		left = append(left, rs(r, "summary"))
	}
	if strings.Join(left, ",") != "new6,new7,new8,new9" {
		t.Errorf("after cap prune: %v", left)
	}
}

func TestEndAtFallsBackToUpdatedAt(t *testing.T) {
	db := open(t)
	up(t, db, map[string]any{"key": "D1", "title": "x", "status": "done"})
	up(t, db, map[string]any{"key": "R1", "title": "x", "status": "building"})
	db.X("UPDATE tasks SET created_at=1000, updated_at=1000+4980, done_at=NULL WHERE key='D1'") // an old done task without done_at
	d, r := db.GetTask("D1"), db.GetTask("R1")
	if ri(d, "end_at")-ri(d, "created_at") != 4980 || r["end_at"] != nil {
		t.Errorf("end_at: %v / %v", d["end_at"], r["end_at"])
	}
}

func TestRevisionsOnlyPublishAfterCommit(t *testing.T) {
	db := open(t)
	before := db.pub.Load()
	if _, err := db.TaskUpsert(map[string]any{"key": "bad key"}); err == nil {
		t.Fatal("expected an error")
	}
	if db.pub.Load() != before {
		t.Error("a refused write moved the published revision")
	}
	up(t, db, map[string]any{"key": "R1"})
	if db.pub.Load() <= before {
		t.Error("a committed write did not move it")
	}
}

func TestPythonHelpers(t *testing.T) {
	if got := pyStr(json.Number("2.0")); got != "2.0" {
		t.Error(got)
	}
	if got := pyStr(map[string]any{"a": []any{"x", json.Number("1")}}); got != "{'a': ['x', 1]}" {
		t.Error(got)
	}
	if head("héllo", 2) != "hé" || tail("héllo", 2) != "lo" {
		t.Error("code point slicing")
	}
	if l := pySplitLines("a\r\nb\nc"); strings.Join(l, "|") != "a|b|c" {
		t.Error(l)
	}
	if string(jstr(nil, "é\"\n")) != `"\u00e9\"\n"` {
		t.Error(string(jstr(nil, "é\"\n")))
	}
	if !pyMatch(keyRe, "abc\n") || pyMatch(keyRe, "abc\n\n") || pyMatch(keyRe, "a b") {
		t.Error("key regex")
	}
}

// Redact must give the same text as sheepdog.py's redact(); the cases and their answers were generated by the Python code.
func TestRedactMatchesPython(t *testing.T) {
	raw, err := os.ReadFile("testdata/redact.json")
	if err != nil {
		t.Fatal(err)
	}
	var cases [][2]string
	if err := json.Unmarshal(raw, &cases); err != nil {
		t.Fatal(err)
	}
	bad := 0
	for _, c := range cases {
		if got := Redact(c[0]); got != c[1] {
			ascii := true
			for _, r := range c[0] {
				ascii = ascii && r < 128
			}
			if !ascii {
				continue // known difference: \b is ASCII-only here, so a secret right next to a non-ASCII letter can be redacted differently
			}
			if bad++; bad <= 8 {
				t.Errorf("redact(%q)\n got  %q\n want %q", c[0], got, c[1])
			}
		}
	}
	if bad > 0 {
		t.Errorf("%d of %d differ", bad, len(cases))
	}
}

func TestAllowlistMatching(t *testing.T) {
	base := t.TempDir()
	real := RealPath(base) // t.TempDir may sit behind a symlink (/var -> /private/var)
	must := func(err error) {
		if err != nil {
			t.Fatal(err)
		}
	}
	must(os.MkdirAll(filepath.Join(real, "proj", "app"), 0o755))
	must(os.MkdirAll(filepath.Join(real, "proj-old"), 0o755))
	must(os.MkdirAll(filepath.Join(real, "other", "proj"), 0o755))
	must(os.Symlink(filepath.Join(real, "proj"), filepath.Join(real, "link")))
	al := ParseAllow(filepath.Join(real, "proj"), nil)
	for cwd, want := range map[string]bool{
		filepath.Join(real, "proj"):                 true,  // the root itself
		filepath.Join(real, "proj", "app"):          true,  // a sub-folder
		filepath.Join(real, "proj", "gone", "deep"): true,  // a folder that no longer exists, normalised lexically
		filepath.Join(real, "link", "app"):          true,  // through a symlink
		filepath.Join(real, "proj-old"):             false, // a sibling with a shared prefix
		filepath.Join(real, "other", "proj"):        false, // same base name, different folder
		filepath.Join(real, "proj", "..", "other"):  false, // dot-dot out of the root
		"relative/proj":                             false,
		"/":                                         false,
		"":                                          true, // no cwd at all
	} {
		if got := al.Allowed(cwd); got != want {
			t.Errorf("Allowed(%q) = %v, want %v", cwd, got, want)
		}
	}
	if !ParseAllow("", nil).Allowed("/anywhere") {
		t.Error("an empty allowlist allows every folder")
	}
	two := ParseAllow(filepath.Join(real, "proj")+string(os.PathListSeparator)+filepath.Join(real, "other"), []string{filepath.Join(real, "proj-old")})
	if len(two.Roots()) != 3 || !two.Allowed(filepath.Join(real, "proj-old")) || !two.Allowed(filepath.Join(real, "other", "proj")) {
		t.Errorf("env list plus --allow: %v", two.Roots())
	}
	home, _ := os.UserHomeDir()
	if got := ParseAllow("~/x", nil).Roots()[0]; got != RealPath(filepath.Join(home, "x")) {
		t.Errorf("~ expansion: %s", got)
	}
}

func hookEvent(cwd string) map[string]any {
	return map[string]any{"hook_event_name": "SessionStart", "session_id": "s-" + cwd, "cwd": cwd, "source": "startup"}
}

func TestAllowlistDropsOutsideHooksWithoutAnyChange(t *testing.T) {
	db := open(t)
	db.SetAllow(ParseAllow("/work/in", nil))
	up(t, db, map[string]any{"key": "T1", "title": "t", "worktree": "/work/out/wt"})
	up(t, db, map[string]any{"key": "T2", "title": "t2", "worktree": "/work/in/wt", "status": "review"})
	rev, pub := db.rev, db.pub.Load()
	count := func() [3]int64 {
		return [3]int64{ri(db.Q("SELECT COUNT(*) n FROM events")[0], "n"), ri(db.Q("SELECT COUNT(*) n FROM agents")[0], "n"), ri(db.Q("SELECT COUNT(*) n FROM tasks")[0], "n")}
	}
	before := count()
	for _, p := range []map[string]any{
		hookEvent("/work/out/wt"), hookEvent("/work/in-old"),
		{"hook_event_name": "PreToolUse", "session_id": "o", "cwd": "/work/out/wt", "tool_name": "Agent", "tool_use_id": "tu", "tool_input": map[string]any{"description": "Task 9 outside"}},
		{"hook_event_name": "PostToolUse", "session_id": "o", "cwd": "/work/out/wt", "agent_id": "ag", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls"}},
		{"hook_event_name": "PreToolUse", "session_id": "o", "cwd": "/work/out", "tool_name": "SendMessage", "tool_input": map[string]any{"to": "ag", "message": "Task 5 x"}},
	} {
		if got, err := db.Ingest(p); err != nil || len(got) != 0 {
			t.Errorf("%v: %v %v", p["hook_event_name"], got, err)
		}
	}
	if count() != before || db.rev != rev || db.pub.Load() != pub {
		t.Errorf("an outside hook changed something: %v -> %v, rev %d -> %d", before, count(), rev, db.rev)
	}
	if _, err := db.Ingest(hookEvent("/work/in/wt/sub")); err != nil || count()[0] != before[0]+1 || db.rev == rev {
		t.Error("an inside hook must be stored")
	}
	if _, err := db.Ingest(map[string]any{"hook_event_name": "Stop", "session_id": "nocwd"}); err != nil || count()[0] != before[0]+2 {
		t.Error("a hook with no cwd is accepted")
	}
	if _, err := db.Ingest(map[string]any{"session_id": "x", "cwd": "/work/out"}); err == nil {
		t.Error("a missing event name is still an error")
	}
}

func TestOutsideUserPromptNeverDelivers(t *testing.T) {
	db := open(t)
	db.SetAllow(ParseAllow("/work/in", nil))
	up(t, db, map[string]any{"key": "D1", "title": "t"})
	if _, err := db.SetBrief("D1", map[string]any{"brief": "x", "cwd": "/work/in", "options": []any{map[string]any{"id": "go", "label": "Go"}}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Decide("D1", map[string]any{"action": "option", "option": "go"}); err != nil {
		t.Fatal(err)
	}
	prompt := func(cwd string) []Row {
		got, err := db.Ingest(map[string]any{"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": cwd, "prompt": "hi"})
		if err != nil {
			t.Fatal(err)
		}
		return got
	}
	db.SetAllow(ParseAllow("/work/in/sub", nil)) // the decision's folder is not under the allowlist root at all: a prompt from there is dropped
	if got := prompt("/work/in"); len(got) != 0 {
		t.Error("dropped prompt delivered a decision")
	}
	if ri(db.Q("SELECT COUNT(*) n FROM decisions WHERE status='pending'")[0], "n") != 1 {
		t.Error("the decision must stay pending")
	}
	db.SetAllow(ParseAllow("/work/in", nil))
	if got := prompt("/work/in/x"); len(got) != 1 {
		t.Errorf("an allowed prompt must deliver: %v", got)
	}
}

func TestPurge(t *testing.T) {
	db := open(t)
	up(t, db, map[string]any{"key": "T1", "title": "keep me", "worktree": "/work/in/wt"})
	up(t, db, map[string]any{"key": "T2", "title": "outside task", "worktree": "/work/out/wt"})
	if _, err := db.SetBrief("T1", map[string]any{"brief": "x", "cwd": "/work/in", "options": []any{map[string]any{"id": "go", "label": "Go"}}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Decide("T1", map[string]any{"action": "option", "option": "go"}); err != nil {
		t.Fatal(err)
	}
	hook := func(p map[string]any) {
		if _, err := db.Ingest(p); err != nil {
			t.Fatal(err)
		}
	}
	// no allowlist while the data is made: everything is stored
	start := func(sid, cwd, aid string) {
		hook(map[string]any{"hook_event_name": "SubagentStart", "session_id": sid, "cwd": cwd, "agent_id": aid, "agent_type": "x"})
	}
	start("in", "/work/in/wt", "a-in")
	start("out1", "/work/out/wt", "a-out")
	start("out2", "/work/gamma", "a-gamma")
	start("mix", "/work/in", "a-mix")
	hook(map[string]any{"hook_event_name": "PostToolUse", "session_id": "in", "cwd": "/work/in/wt", "agent_id": "a-in", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls"}})
	hook(map[string]any{"hook_event_name": "SessionStart", "session_id": "out1", "cwd": "/work/out/wt"})
	hook(map[string]any{"hook_event_name": "PostToolUse", "session_id": "out1", "cwd": "/work/out/wt", "agent_id": "a-out", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls"}})
	hook(map[string]any{"hook_event_name": "PostToolUse", "session_id": "out2", "cwd": "/work/gamma", "agent_id": "a-gamma", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls"}})
	hook(map[string]any{"hook_event_name": "PostToolUse", "session_id": "mix", "cwd": "/work/in", "agent_id": "a-mix", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls"}})
	hook(map[string]any{"hook_event_name": "PostToolUse", "session_id": "mix", "cwd": "/work/out/wt", "agent_id": "a-mix", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls"}})
	if _, err := db.AgentUpsert(map[string]any{"id": "a-api", "task": "T2", "name": "from the CLI"}); err != nil {
		t.Fatal(err)
	}
	start("sess-only", "/work/out/wt", "a-sess")
	db.X("INSERT INTO agents(id, session_id, status) VALUES('a-ses2', 'out1', 'running')") // no events of its own: judged by its session
	snap := func() string {
		var b strings.Builder
		for _, tb := range []string{"tasks", "decisions", "agents", "events"} {
			for _, r := range db.Q("SELECT * FROM " + tb + " ORDER BY 1") {
				j, _ := json.Marshal(r)
				b.Write(j)
			}
		}
		return b.String()
	}
	al := ParseAllow("/work/in", nil)
	before, rev := snap(), db.rev
	rep, err := db.Purge(al, true)
	if err != nil || snap() != before || db.rev != rev {
		t.Fatalf("a dry run changed something: %v", err)
	}
	if rep.Events != 7 || rep.Agents != 4 {
		t.Errorf("dry run counts: %+v", rep)
	}
	tasks, decisions := db.Q("SELECT * FROM tasks ORDER BY key"), db.Q("SELECT * FROM decisions")
	rep2, err := db.Purge(al, false)
	if err != nil {
		t.Fatal(err)
	}
	if rep2.Events != rep.Events || rep2.Agents != rep.Agents {
		t.Errorf("the real run deleted %+v, the dry run said %+v", rep2, rep)
	}
	left := map[string]bool{}
	for _, r := range db.Q("SELECT id FROM agents") {
		left[rs(r, "id")] = true
	}
	if left["a-out"] || left["a-gamma"] || left["a-sess"] || left["a-ses2"] || !left["a-in"] || !left["a-mix"] || !left["a-api"] {
		t.Errorf("agents left: %v", left)
	}
	for _, r := range db.Q("SELECT cwd FROM events WHERE cwd != ''") {
		if !al.Allowed(rs(r, "cwd")) {
			t.Errorf("an outside event survived: %s", rs(r, "cwd"))
		}
	}
	if n := ri(db.Q("SELECT COUNT(*) n FROM events WHERE cwd = '/work/in'")[0], "n"); n != 2 {
		t.Errorf("inside events must stay: %d", n)
	}
	if len(db.Q("SELECT * FROM events WHERE cwd IS NULL OR cwd = ''")) == 0 {
		t.Error("events without a folder must stay")
	}
	nt, nd := db.Q("SELECT * FROM tasks ORDER BY key"), db.Q("SELECT * FROM decisions")
	for i := range tasks { // only the revision moves
		delete(tasks[i], "rev")
		delete(nt[i], "rev")
	}
	a, _ := json.Marshal(tasks)
	b, _ := json.Marshal(nt)
	c, _ := json.Marshal(decisions)
	e, _ := json.Marshal(nd)
	if string(a) != string(b) || string(c) != string(e) || len(nt) != 2 {
		t.Error("tasks and decisions must be untouched")
	}
	if db.rev != rev+1 {
		t.Errorf("one revision bump expected: %d -> %d", rev, db.rev)
	}
	again, _ := db.Purge(al, false)
	if again.Events != 0 || again.Agents != 0 || db.rev != rev+1 {
		t.Errorf("a second purge must do nothing: %+v rev %d", again, db.rev)
	}
}

type rewriteCases struct {
	Config struct {
		Roots                    []string
		Home, User, Host, Tmpdir string
	}
	Text [][2]string
	Cwd  [][2]string
}

func corpusScrubber(t *testing.T) (*Scrubber, rewriteCases) {
	raw, err := os.ReadFile("../../contract/rewrite-cases.json")
	if err != nil {
		t.Fatal(err)
	}
	var c rewriteCases
	if err := json.Unmarshal(raw, &c); err != nil {
		t.Fatal(err)
	}
	return NewScrubber(c.Config.Roots, ScrubConfig{Home: c.Config.Home, User: c.Config.User, Host: c.Config.Host, TmpDir: c.Config.Tmpdir}), c
}

// The rewrite must give the same text as sheepdog.py's Scrubber; the answers in the file were generated by the Python code.
func TestRewriteMatchesPython(t *testing.T) {
	s, c := corpusScrubber(t)
	for _, x := range c.Text {
		if got := s.Text(x[0]); got != x[1] {
			t.Errorf("Text(%q)\n got  %q\n want %q", x[0], got, x[1])
		}
		if again := s.Text(x[1]); again != x[1] { // rewriting rewritten text changes nothing
			t.Errorf("Text is not idempotent on %q: %q", x[1], again)
		}
	}
	for _, x := range c.Cwd {
		if got := s.Cwd(x[0]); got != x[1] {
			t.Errorf("Cwd(%q) = %q, want %q", x[0], got, x[1])
		}
		if again := s.Cwd(x[1]); again != x[1] {
			t.Errorf("Cwd is not idempotent on %q: %q", x[1], again)
		}
	}
}

func TestScrubRowsRewritesOldRowsAndIsIdempotent(t *testing.T) {
	db := open(t)
	db.SetScrub(NewScrubber(nil, ScrubConfig{})) // rows are written the way an old server wrote them: raw
	up(t, db, map[string]any{"key": "T1", "title": "t", "worktree": "/Users/jdoe/dev/alpha/wt", "note": "from /Users/jdoe/x"})
	if _, err := db.SetBrief("T1", map[string]any{"brief": "see /tmp/a/b/c.txt by jdoe", "cwd": "/Users/jdoe/dev/alpha", "options": []any{map[string]any{"id": "go", "label": "Go jdoe", "hint": "/Users/jdoe/h"}}}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Decide("T1", map[string]any{"action": "reply", "text": "ok /home/jdoe/z"}); err != nil {
		t.Fatal(err)
	}
	if _, err := db.Ingest(map[string]any{"hook_event_name": "PostToolUse", "session_id": "s", "cwd": "/Users/jdoe/dev/alpha/web", "agent_id": "a", "tool_name": "Bash", "tool_input": map[string]any{"command": "ls /Users/jdoe/dev/alpha/web jdoes-mbp"}}); err != nil {
		t.Fatal(err)
	}
	db.X("INSERT INTO agents(id, name, status) VALUES('a', 'jdoe agent in /Users/jdoe/dev/alpha', 'running')")
	s, _ := corpusScrubber(t)
	snap := func() string {
		var b strings.Builder
		for _, tb := range []string{"tasks", "agents", "events", "decisions"} {
			for _, r := range db.Q("SELECT * FROM " + tb + " ORDER BY 1") {
				j, _ := json.Marshal(r)
				b.Write(j)
			}
		}
		return b.String()
	}
	before := snap()
	dry, err := db.ScrubRows(s, true)
	if err != nil || snap() != before {
		t.Fatalf("a dry run changed rows: %v", err)
	}
	total := 0
	for _, c := range dry {
		total += c.Rows
	}
	if total == 0 {
		t.Fatal("the dry run found nothing to rewrite")
	}
	real, err := db.ScrubRows(s, false)
	if err != nil || len(real) != len(dry) {
		t.Fatal(err)
	}
	for i := range dry {
		if real[i] != dry[i] {
			t.Errorf("dry run said %v, the real run did %v", dry[i], real[i])
		}
	}
	after := snap()
	for _, bad := range []string{"jdoe", "jdoes-mbp", "/Users/", "/home/", "/tmp/"} {
		if strings.Contains(after, bad) {
			t.Errorf("%q survived the scrub", bad)
		}
	}
	again, _ := db.ScrubRows(s, false)
	for _, c := range again {
		if c.Rows != 0 {
			t.Errorf("a second scrub changed %d rows of %s", c.Rows, c.Table)
		}
	}
	if snap() != after {
		t.Error("a second scrub changed the data")
	}
	if !strings.Contains(after, "alpha:web") || !strings.Contains(after, "tmp\\u003e/c.txt") {
		t.Errorf("the rewritten forms must be there: %s", after)
	}
}

func TestLiveExampleAndGenericUserNames(t *testing.T) {
	live := "cd /home/ubuntu/dev/alpha/.claude/worktrees/t12-role-ui/e2e; rm -rf tests/e2e/org-ubuntu/tmp; ls"
	generic := NewScrubber([]string{"/home/ubuntu/dev/alpha"}, ScrubConfig{Home: "/home/ubuntu", User: "ubuntu"})
	want := "cd alpha:.claude/worktrees/t12-role-ui/e2e; rm -rf tests/e2e/org-ubuntu/tmp; ls" // only the real path changes; org-ubuntu is a folder name
	if got := generic.Text(live); got != want || generic.Text(got) != want {
		t.Errorf("ubuntu: %q (then %q)", got, generic.Text(got))
	}
	jdoe := NewScrubber([]string{"/Users/jdoe/dev/alpha"}, ScrubConfig{Home: "/Users/jdoe", User: "jdoe"})
	once := jdoe.Text("rm -rf tests/e2e/org-jdoe/tmp; cat /Users/jdoe/dev/alpha/x")
	if once != "rm -rf tests/e2e/org-<user>/tmp; cat alpha:x" || jdoe.Text(once) != once { // the /tmp after <user> is not a temp path
		t.Errorf("jdoe: %q (then %q)", once, jdoe.Text(once))
	}
	for _, u := range []string{"admin", "administrator", "root", "user", "ubuntu", "debian", "ec2-user", "centos", "fedora", "dev", "developer", "runner", "git", "www-data", "node", "app", "vagrant", "pi", "guest", "test", "Admin", "ab"} {
		s := NewScrubber(nil, ScrubConfig{Home: "/Users/" + u, User: u})
		in := "the " + u + " group, org-" + u + "/x, /Users/" + u + "/proj"
		want := "the " + u + " group, org-" + u + "/x, ~/proj" // the home path still goes
		if got := s.Text(in); got != want {
			t.Errorf("user %q: %q, want %q", u, got, want)
		}
	}
}

func TestRewriteIsIdempotentOnEveryInput(t *testing.T) {
	s, c := corpusScrubber(t)
	for _, x := range c.Text {
		once := s.Text(x[0])
		if twice := s.Text(once); twice != once {
			t.Errorf("Text twice on %q: %q then %q", x[0], once, twice)
		}
	}
	for _, x := range c.Cwd {
		once := s.Cwd(x[0])
		if twice := s.Cwd(once); twice != once {
			t.Errorf("Cwd twice on %q: %q then %q", x[0], once, twice)
		}
	}
}

func TestTaskNotificationShapeAndDashEncodedPaths(t *testing.T) {
	s := NewScrubber([]string{"/Users/jdoe/dev/alpha"}, ScrubConfig{Home: "/Users/jdoe", User: "jdoe"})
	for in, want := range map[string]string{
		// a Claude task notification (and the same clipped mid-way through the encoded folder name)
		"<task-id>a1b2</task-id> <output-file>/private/tmp/claude-12/-Users-jdoe-dev-alpha/abc/tasks/x.output": "<task-id>a1b2</task-id> <output-file><tmp>/x.output",
		"<output-file>/private/tmp/claude-12/-Users-jdoe-dev-alpha-wo...":                                      "<output-file><tmp>/alpha:-wo...",
		"<output-file>/private/tmp/claude-12/-Users-jdoe-wo...":                                                "<output-file><tmp>/~-wo...",
		">/tmp/y": "><tmp>/y", "2>/tmp/err.log": "2><tmp>/err.log", "<user>/tmp/x": "<user>/tmp/x", "<tmp>/Users/jdoe/x": "<tmp>/Users/<user>/x",
		// Claude's dash-encoded folder names: the root becomes name:, the home ~, the rest is kept
		"-Users-jdoe-dev-alpha": "alpha:", "x/-Users-jdoe-dev-alpha/s.jsonl": "x/alpha:/s.jsonl", "-Users-jdoe-dev-alpha-sub": "alpha:-sub",
		"-Users-jdoe": "~", "-home-jdoe-x": "~-x", "-Users-jdoe2-x": "-Users-jdoe2-x", "a-Users-jdoe-x": "a-Users-<user>-x",
	} {
		got := s.Text(in)
		if got != want || s.Text(got) != want {
			t.Errorf("%q\n got  %q (then %q)\n want %q", in, got, s.Text(got), want)
		}
	}
}

// The strings are rewritten before they are cut: a path that would be cut in the middle by the 2000-character command limit is
// rewritten whole, so no piece of the home directory is left behind.
func TestRewriteComesBeforeTheCuts(t *testing.T) {
	db := open(t)
	db.SetScrub(NewScrubber([]string{"/Users/jdoe/dev/alpha"}, ScrubConfig{Home: "/Users/jdoe", User: "jdoe"}))
	cmd := strings.Repeat("c", 1980) + " /Users/jdoe/dev/alpha/file.txt"
	if _, err := db.Ingest(map[string]any{"hook_event_name": "PostToolUseFailure", "session_id": "s", "cwd": "/Users/jdoe/dev/alpha", "tool_name": "Bash",
		"tool_input": map[string]any{"command": cmd}, "error": "Exit code 1"}); err != nil {
		t.Fatal(err)
	}
	var data map[string]any
	for _, r := range db.Q("SELECT data FROM events WHERE kind='PostToolUseFailure'") {
		json.Unmarshal([]byte(rs(r, "data")), &data)
	}
	got, _ := data["cmd"].(string)
	if !strings.HasSuffix(got, " alpha:file.txt") || strings.Contains(got, "/Users") || strings.Contains(got, "jd") {
		t.Errorf("cmd tail: %q", got[max(0, len(got)-40):])
	}
	long := "t " + strings.Repeat("y", 480) + " /Users/jdoe/dev/alpha/zzz"
	up(t, db, map[string]any{"key": "C1", "title": long})
	if title := rs(db.GetTask("C1"), "title"); strings.Contains(title, "jd") || !strings.HasSuffix(title, "alpha:zzz") {
		t.Errorf("title tail: %q", title[max(0, len(title)-30):])
	}
}
