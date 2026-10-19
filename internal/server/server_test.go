package server_test

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/DalatHappyFarm/sheepdog/internal/cli"
	"github.com/DalatHappyFarm/sheepdog/internal/server"
	"github.com/DalatHappyFarm/sheepdog/internal/store"
)

const token = "test-token-123"

type env struct {
	t   *testing.T
	db  *store.DB
	srv *server.Server
	ts  *httptest.Server
	dir string
}

type obj = map[string]any

func newEnv(t *testing.T) *env {
	t.Helper()
	dir := t.TempDir()
	db, err := store.Open(filepath.Join(dir, "t.db"))
	if err != nil {
		t.Fatal(err)
	}
	tok := token
	srv := server.New(db, &tok)
	ts := httptest.NewServer(srv)
	t.Cleanup(func() { ts.Close(); db.Close() })
	t.Setenv("SHEEPDOG_URL", ts.URL)
	t.Setenv("SHEEPDOG_TOKEN", token)
	t.Setenv("BOARD_URL", "")
	t.Setenv("BOARD_TOKEN", "")
	return &env{t, db, srv, ts, dir}
}

func (e *env) do(method, path string, body []byte, hdr map[string]string) (int, []byte, http.Header) {
	e.t.Helper()
	req, _ := http.NewRequest(method, e.ts.URL+path, bytes.NewReader(body))
	for k, v := range hdr {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	defer resp.Body.Close()
	b, _ := io.ReadAll(resp.Body)
	return resp.StatusCode, b, resp.Header
}

func auth(tok string) map[string]string {
	if tok == "" {
		return nil
	}
	return map[string]string{"Authorization": "Bearer " + tok}
}

func (e *env) get(path string) (int, []byte, http.Header) { return e.do("GET", path, nil, auth(token)) }

func (e *env) apiGet(path string) (int, any) {
	st, b, _ := e.get(path)
	var v any
	_ = json.Unmarshal(b, &v)
	return st, v
}

func (e *env) postTok(path string, body any, tok string) (int, obj) {
	e.t.Helper()
	b, _ := json.Marshal(body)
	st, out, _ := e.do("POST", path, b, auth(tok))
	var m obj
	_ = json.Unmarshal(out, &m)
	return st, m
}

func (e *env) post(path string, body any) (int, obj) { return e.postTok(path, body, token) }

func (e *env) raw(path string, body []byte, tok string) (int, []byte) {
	st, b, _ := e.do("POST", path, body, auth(tok))
	return st, b
}

func (e *env) rawJSON(path string, body any, tok string) (int, []byte) {
	b, _ := json.Marshal(body)
	return e.raw(path, b, tok)
}

func (e *env) state(since int) obj {
	e.t.Helper()
	st, v := e.apiGet(fmt.Sprintf("/api/state?since=%d", since))
	if st != 200 {
		e.t.Fatalf("state: %d", st)
	}
	return v.(obj)
}

func (e *env) hook(p obj) {
	e.t.Helper()
	if st, b := e.rawJSON("/api/hook", p, token); st != 200 {
		e.t.Fatalf("hook %v: %d %s", p["hook_event_name"], st, b)
	}
}

// cli runs a client command in-process and returns its exit code, stdout and stderr.
func (e *env) cli(args ...string) (int, string, string) {
	e.t.Helper()
	return runCLI(e.t, "", args...)
}

func runCLI(t *testing.T, stdin string, args ...string) (int, string, string) {
	t.Helper()
	oo, oe := os.Stdout, os.Stderr
	ro, wo, _ := os.Pipe()
	re, we, _ := os.Pipe()
	os.Stdout, os.Stderr = wo, we
	var code int
	done := make(chan struct{})
	go func() { code = cli.Run(args); close(done) }()
	<-done
	wo.Close()
	we.Close()
	os.Stdout, os.Stderr = oo, oe
	so, _ := io.ReadAll(ro)
	se, _ := io.ReadAll(re)
	return code, string(so), string(se)
}

func asObj(v any) obj { m, _ := v.(obj); return m }
func asList(v any) []any {
	l, _ := v.([]any)
	return l
}

func (e *env) events() []obj {
	var out []obj
	for _, x := range asList(e.state(0)["events"]) {
		out = append(out, asObj(x))
	}
	return out
}

func (e *env) agent(id string) obj {
	for _, a := range asList(e.state(0)["agents"]) {
		if asObj(a)["id"] == id {
			return asObj(a)
		}
	}
	return nil
}

func (e *env) task(key string) obj {
	for _, a := range asList(e.state(0)["tasks"]) {
		if asObj(a)["key"] == key {
			return asObj(a)
		}
	}
	return nil
}

func eqv(t *testing.T, got, want any, what string) {
	t.Helper()
	if fmt.Sprint(got) != fmt.Sprint(want) {
		t.Errorf("%s: got %v, want %v", what, got, want)
	}
}

func TestAuth(t *testing.T) {
	e := newEnv(t)
	for _, tc := range []struct {
		path, tok string
		want      int
	}{{"/api/state", "", 401}, {"/api/state", "wrong", 401}, {"/", token, 200}, {"/", "", 401}} {
		if st, _, _ := e.do("GET", tc.path, nil, auth(tc.tok)); st != tc.want {
			t.Errorf("GET %s tok=%q: %d, want %d", tc.path, tc.tok, st, tc.want)
		}
	}
	if st, _ := e.raw("/api/task", []byte("{}"), ""); st != 401 {
		t.Errorf("POST without token: %d", st)
	}
	noRedirect := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, _ := noRedirect.Get(e.ts.URL + "/?token=" + token)
	if resp.StatusCode != 302 {
		t.Fatalf("token query: %d", resp.StatusCode)
	}
	cookie := resp.Header.Get("Set-Cookie")
	if !strings.Contains(cookie, "HttpOnly") || !strings.Contains(cookie, "SameSite=Strict") {
		t.Errorf("cookie: %s", cookie)
	}
	st, _, _ := e.do("GET", "/api/state", nil, map[string]string{"Cookie": strings.SplitN(cookie, ";", 2)[0]})
	if st != 200 {
		t.Errorf("cookie sign-in: %d", st)
	}
}

func TestLoginPageAndThrottle(t *testing.T) {
	e := newEnv(t)
	html := map[string]string{"Accept": "text/html", "Content-Type": "application/x-www-form-urlencoded"}
	st, b, _ := e.do("GET", "/", nil, html)
	if st != 200 || !strings.Contains(string(b), "Sign in") {
		t.Fatalf("login page: %d", st)
	}
	if st, _, _ := e.do("GET", "/", nil, nil); st != 401 {
		t.Errorf("non-HTML clients keep 401: %d", st)
	}
	st, b, _ = e.do("POST", "/login", []byte("token=wrong"), html)
	if st != 401 || !strings.Contains(string(b), "Wrong token") {
		t.Errorf("wrong token: %d", st)
	}
	noRedirect := &http.Client{CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }}
	resp, _ := noRedirect.Post(e.ts.URL+"/login", "application/x-www-form-urlencoded", strings.NewReader("token="+token))
	if resp.StatusCode != 303 || resp.Header.Get("Location") != "/" {
		t.Errorf("right token: %d %s", resp.StatusCode, resp.Header.Get("Location"))
	}
	e2 := newEnv(t) // a fresh throttle: five wrong attempts, then 429 even with the right token
	var codes []int
	for i := 0; i < 6; i++ {
		st, _, _ := e2.do("POST", "/login", []byte("token=wrong"), html)
		codes = append(codes, st)
	}
	eqv(t, codes, []int{401, 401, 401, 401, 401, 429}, "throttle")
	if st, _, h := e2.do("POST", "/login", []byte("token="+token), html); st != 429 || h.Get("Retry-After") != "60" {
		t.Errorf("right token while throttled: %d", st)
	}
}

func TestHookLinksToTaskByWorktree(t *testing.T) {
	e := newEnv(t)
	if c, _, _ := e.cli("task", "L1", "--title", "link me", "--worktree", "/work/wt/L1", "--project", "alpha"); c != 0 {
		t.Fatal("cli task")
	}
	sid, cwd := "sess-1", "/work/wt/L1/apps/web"
	e.hook(obj{"hook_event_name": "SessionStart", "session_id": sid, "cwd": cwd, "source": "startup"})
	e.hook(obj{"hook_event_name": "PreToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_1",
		"tool_input": obj{"description": "Find endpoints", "prompt": "SECRET PROMPT", "subagent_type": "Explore", "model": "sonnet"}})
	e.hook(obj{"hook_event_name": "SubagentStart", "session_id": sid, "cwd": cwd, "agent_id": "ag-1", "agent_type": "Explore"})
	e.hook(obj{"hook_event_name": "PostToolUse", "session_id": sid, "cwd": cwd, "agent_id": "ag-1", "tool_name": "Bash", "tool_input": obj{"command": "ls"}, "tool_response": obj{"stdout": "OUTPUT"}})
	kinds := map[string]bool{}
	var blob string
	for _, ev := range e.events() {
		if ev["task_key"] == "L1" {
			kinds[ev["kind"].(string)] = true
		}
		b, _ := json.Marshal(ev)
		blob += string(b)
	}
	for _, k := range []string{"SessionStart", "PreToolUse", "SubagentStart", "PostToolUse"} {
		if !kinds[k] {
			t.Errorf("no %s event linked to L1", k)
		}
	}
	ag := e.agent("ag-1")
	eqv(t, []any{ag["task_key"], ag["status"], ag["name"], ag["model"], ag["tool_uses"]}, []any{"L1", "running", "Find endpoints", "sonnet", float64(1)}, "agent")
	if strings.Contains(blob, "SECRET PROMPT") || strings.Contains(blob, "OUTPUT") {
		t.Error("prompt or tool output leaked into events")
	}
	e.hook(obj{"hook_event_name": "PostToolUse", "session_id": sid, "cwd": "/elsewhere", "agent_id": "ag-1", "tool_name": "Edit", "tool_input": obj{"file_path": "/x/y.ts"}}) // links through the agent
	e.hook(obj{"hook_event_name": "SubagentStop", "session_id": sid, "cwd": cwd, "agent_id": "ag-1", "agent_type": "Explore"})
	e.hook(obj{"hook_event_name": "PostToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_1", "tool_input": obj{"description": "Find endpoints"},
		"tool_response": obj{"status": "completed", "agentId": "ag-1", "totalTokens": 1234, "totalToolUseCount": 7}})
	ag = e.agent("ag-1")
	eqv(t, []any{ag["status"], ag["tokens"], ag["tool_uses"]}, []any{"done", float64(1234), float64(7)}, "finished agent")
	found := false
	for _, ev := range e.events() {
		if strings.HasPrefix(ev["summary"].(string), "Edit: /x/y.ts") && ev["task_key"] == "L1" {
			found = true
		}
	}
	if !found {
		t.Error("Edit event not linked through the agent")
	}
	e.hook(obj{"hook_event_name": "SessionStart", "session_id": "s2", "cwd": "/nowhere", "source": "startup"})
	unlinked := false
	for _, ev := range e.events() {
		if ev["cwd"] == "/nowhere" && ev["task_key"] == nil {
			unlinked = true
		}
	}
	if !unlinked {
		t.Error("unrelated cwd must stay unlinked")
	}
	e.hook(obj{"hook_event_name": "PreToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_2", "tool_input": obj{"description": "Doomed"}})
	e.hook(obj{"hook_event_name": "PostToolUseFailure", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": "toolu_2", "tool_input": obj{"description": "Doomed"}, "error": "boom"})
	failed := false
	for _, a := range asList(e.state(0)["agents"]) {
		if asObj(a)["name"] == "Doomed" && asObj(a)["status"] == "failed" {
			failed = true
		}
	}
	if !failed {
		t.Error("failed agent not recorded")
	}
}

func TestCLITaskLifecycle(t *testing.T) {
	e := newEnv(t)
	c, out, errs := e.cli("task", "C1", "--title", "first", "--status", "building", "--project", "p", "--branch", "feat/x", "--worktree", "/work/wt/C1", "--mr", "https://example.com/mr/1", "--note", "hello")
	if c != 0 {
		t.Fatal(errs)
	}
	var task obj
	json.Unmarshal([]byte(out), &task)
	eqv(t, []any{task["key"], task["status"], task["branch"], asObj(asList(task["notes"])[0])["text"]}, []any{"C1", "building", "feat/x", "hello"}, "created")
	_, out, _ = e.cli("task", "C1", "--status", "testing", "--note", "second")
	json.Unmarshal([]byte(out), &task)
	eqv(t, []any{task["title"], task["status"], len(asList(task["notes"]))}, []any{"first", "testing", 2}, "updated")
	if c, _, _ := e.cli("task", "C1", "--mr", "javascript:alert(1)"); c == 0 {
		t.Error("a javascript: mr url must be refused")
	}
	_, out, _ = e.cli("agent", "cli-agent", "--task", "C1", "--name", "builder", "--model", "opus", "--status", "running")
	var ag obj
	json.Unmarshal([]byte(out), &ag)
	eqv(t, ag["status"], "running", "agent")
	_, out, _ = e.cli("archive", "C1")
	json.Unmarshal([]byte(out), &task)
	eqv(t, task["archived"], float64(1), "archived")
	if e.task("C1") != nil {
		t.Error("archived task still on the board")
	}
	archived := false
	for _, x := range asList(e.state(1)["tasks"]) {
		if asObj(x)["key"] == "C1" && asObj(x)["archived"] == float64(1) {
			archived = true
		}
	}
	if !archived {
		t.Error("the delta must carry the archive")
	}
	if c, _, errs := e.cli("task", "C1", "--status", "flying"); c != 2 || !strings.Contains(errs, "invalid choice") {
		t.Errorf("bad choice: %d %s", c, errs)
	}
}

func TestIncrementalState(t *testing.T) {
	e := newEnv(t)
	cur := int(e.state(0)["cursor"].(float64))
	eqv(t, len(asList(e.state(cur)["tasks"])), 0, "empty delta")
	e.cli("task", "I1", "--title", "inc")
	s := e.state(cur)
	tasks := asList(s["tasks"])
	if len(tasks) != 1 || asObj(tasks[0])["key"] != "I1" || int(s["cursor"].(float64)) <= cur {
		t.Errorf("delta: %v", s)
	}
}

func TestRedaction(t *testing.T) {
	cases := [][2]string{{"curl -H 'Authorization: Bearer abcdefghij123456' https://x", "abcdefghij123456"}, {"export GITLAB_TOKEN=glpat" + "-abcdef1234567890", "glpat" + "-abcdef1234567890"},
		{"mysql --password hunter2 -u root", "hunter2"}, {"AWS_SECRET_ACCESS_KEY=" + "wJalrXUtnFEMI/K7MDENG/bPxRfiCY", "wJalrXUtn"}, {"key AKIA" + "IOSFODNN7EXAMPLE", "AKIA" + "IOSFODNN7EXAMPLE"},
		{"psql postgres://user:s3cr3tpw@db.host:5432/app", "s3cr3tpw"}, {`echo {"password": "p4ss"}`, "p4ss"}, {"sk" + "-abcdefghijklmnopqrstuv", "sk" + "-abcdefghijklmnopqrstuv"}}
	for _, c := range cases {
		if strings.Contains(store.Redact(c[0]), c[1]) {
			t.Errorf("not redacted: %s -> %s", c[0], store.Redact(c[0]))
		}
	}
	if !strings.Contains(store.Redact(cases[5][0]), "db.host") {
		t.Error("the host must stay")
	}
	e := newEnv(t)
	e.hook(obj{"hook_event_name": "PostToolUse", "session_id": "r", "cwd": "/r", "tool_name": "Bash", "tool_input": obj{"command": strings.Repeat("x", 150) + " TOKEN=topsecretvalue1234 " + strings.Repeat("y", 400)}})
	e.hook(obj{"hook_event_name": "UserPromptSubmit", "session_id": "r", "cwd": "/r", "prompt": "my password=abc12345 " + strings.Repeat("z", 300)})
	evs := e.events()
	b, _ := json.Marshal(evs)
	if strings.Contains(string(b), "topsecretvalue1234") || strings.Contains(string(b), "abc12345") {
		t.Error("secret in events")
	}
	for _, ev := range evs {
		s := ev["summary"].(string)
		if strings.HasPrefix(s, "Bash:") && ev["cwd"] == "/r" && len([]rune(s)) > 210 {
			t.Errorf("bash summary too long: %d", len(s))
		}
		if ev["kind"] == "UserPromptSubmit" && ev["cwd"] == "/r" && len([]rune(s)) > 140 {
			t.Errorf("prompt summary too long: %d", len(s))
		}
	}
}

func TestRedactionIsLinear(t *testing.T) {
	for _, hostile := range []string{strings.Repeat("x", 200000), strings.Repeat("ab.", 30000), strings.Repeat("password", 5000)} {
		t0 := time.Now()
		store.Redact(hostile)
		if d := time.Since(t0); d > 2*time.Second {
			t.Errorf("redaction took %v", d)
		}
	}
}

func TestHookCLIServerDown(t *testing.T) {
	t.Setenv("SHEEPDOG_URL", "http://127.0.0.1:1")
	t.Setenv("SHEEPDOG_TOKEN", "x")
	for _, stdin := range []string{`{"hook_event_name":"SessionStart","session_id":"a","cwd":"/"}`, "not json", ""} {
		t0 := time.Now()
		code, out, errs := runCLI(t, stdin, "hook")
		if code != 0 || out != "" || errs != "" || time.Since(t0) > 3*time.Second {
			t.Errorf("stdin %q: %d %q %q %v", stdin, code, out, errs, time.Since(t0))
		}
	}
}

func hookViaCLI(t *testing.T, payload string) string {
	t.Helper()
	oi := os.Stdin
	r, w, _ := os.Pipe()
	w.WriteString(payload)
	w.Close()
	os.Stdin = r
	defer func() { os.Stdin = oi }()
	code, out, errs := runCLI(t, "", "hook")
	if code != 0 || errs != "" {
		t.Fatalf("hook: %d %s", code, errs)
	}
	return out
}

func TestHookCLIDeliversAndToolToggle(t *testing.T) {
	e := newEnv(t)
	p := `{"hook_event_name":"PostToolUse","session_id":"h","cwd":"/hk","tool_name":"Bash","tool_input":{"command":"echo viahook"},"tool_response":{"stdout":"` + strings.Repeat("x", 10000) + `"}}`
	if out := hookViaCLI(t, p); out != "" {
		t.Errorf("nothing waiting: the hook prints nothing, got %q", out)
	}
	has := func(s string) bool {
		for _, ev := range e.events() {
			if strings.Contains(ev["summary"].(string), s) {
				return true
			}
		}
		return false
	}
	if !has("echo viahook") {
		t.Error("hook event not stored")
	}
	t.Setenv("SHEEPDOG_TOOL_EVENTS", "0") // the toggle lives on the server
	e.raw("/api/hook", []byte(strings.Replace(p, "viahook", "server-skipped", 1)), token)
	if has("server-skipped") {
		t.Error("SHEEPDOG_TOOL_EVENTS=0 must skip ordinary tool events")
	}
	t.Setenv("SHEEPDOG_TOOL_EVENTS", "")
	t.Setenv("BOARD_TOOL_EVENTS", "0") // the old name still works
	e.raw("/api/hook", []byte(strings.Replace(p, "viahook", "old-name-skipped", 1)), token)
	if has("old-name-skipped") {
		t.Error("BOARD_TOOL_EVENTS=0 must skip too")
	}
}

func TestAgentLinksByTaskDescription(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "T7", "--title", "Seven", "--project", "proj")
	sid, cwd := "sd", "/work/proj" // the project name in cwd must NOT link anything
	pre := func(tuid, desc string) {
		e.hook(obj{"hook_event_name": "PreToolUse", "session_id": sid, "cwd": cwd, "tool_name": "Agent", "tool_use_id": tuid, "tool_input": obj{"description": desc, "subagent_type": "general-purpose"}})
	}
	pre("tu_a", "Task 7 Build the thing")
	pre("tu_b", "Task 91 Brand new task")
	pre("tu_c", "no number here")
	e.hook(obj{"hook_event_name": "SubagentStart", "session_id": sid, "cwd": cwd, "agent_id": "sa-1", "agent_type": "general-purpose"})
	s := e.state(0)
	byTU := map[string]obj{}
	for _, a := range asList(s["agents"]) {
		if m := asObj(a); m["session_id"] == sid {
			byTU[fmt.Sprint(m["tool_use_id"])] = m
		}
	}
	eqv(t, []any{byTU["tu_a"]["task_key"], byTU["tu_a"]["name"], byTU["tu_a"]["id"]}, []any{"T7", "Task 7 Build the thing", "sa-1"}, "adopted placeholder")
	eqv(t, byTU["tu_b"]["task_key"], "T91", "new task from description")
	if byTU["tu_c"]["task_key"] != nil {
		t.Error("no number: no link")
	}
	t91 := e.task("T91")
	eqv(t, []any{t91["title"], t91["status"]}, []any{"Brand new task", "building"}, "T91")
	starts := map[string]bool{}
	for _, ev := range asList(s["events"]) {
		m := asObj(ev)
		if m["kind"] == "PreToolUse" && m["session_id"] == sid {
			starts[m["summary"].(string)] = true
		}
	}
	for _, w := range []string{"agent start: Build the thing (general-purpose)", "agent start: no number here (general-purpose)"} {
		if !starts[w] {
			t.Errorf("missing summary %q in %v", w, starts)
		}
	}
	_, out, _ := e.cli("task", "T7", "--status", "running")
	if !strings.Contains(out, `"status": "building"`) {
		t.Errorf("running alias: %s", out)
	}
}

func TestNextWaitingAndNeedsYou(t *testing.T) {
	e := newEnv(t)
	_, out, _ := e.cli("task", "N1", "--title", "n", "--next", "Test at http://localhost:3000", "--waiting", "me")
	var tk obj
	json.Unmarshal([]byte(out), &tk)
	eqv(t, []any{tk["next"], tk["waiting"], tk["needs_you"]}, []any{"Test at http://localhost:3000", "me", true}, "needs you")
	if c, _, _ := e.cli("task", "N1", "--waiting", "bogus"); c == 0 {
		t.Error("bogus waiting accepted")
	}
	up := func(d obj) obj {
		d["key"] = "N1"
		st, m := e.post("/api/task", d)
		if st != 200 {
			t.Fatalf("upsert %v: %d %v", d, st, m)
		}
		return m
	}
	eqv(t, up(obj{"status": "review", "waiting": ""})["needs_you"], true, "review with no waiting")
	eqv(t, up(obj{"waiting": "qc"})["needs_you"], false, "waiting qc")
	eqv(t, up(obj{"waiting": "none"})["needs_you"], false, "waiting none")
	eqv(t, up(obj{"status": "building", "waiting": ""})["needs_you"], false, "building")
	eqv(t, up(obj{"waiting": "me"})["needs_you"], true, "waiting me")
	eqv(t, up(obj{"next": ""})["next"], "", "clear next")
	if st, _ := e.post("/api/task", obj{"key": "N1", "waiting": "x"}); st != 400 {
		t.Errorf("bad waiting: %d", st)
	}
}

func TestFailedCommands(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "T9", "--title", "fails")
	e.cli("agent", "ag9", "--task", "T9", "--status", "running")
	e.hook(obj{"hook_event_name": "PostToolUseFailure", "session_id": "s9", "cwd": "/x", "agent_id": "ag9", "tool_name": "Bash", "tool_input": obj{"command": "false"}, "error": "boom\nsecond line"})
	s := e.state(0)
	eqv(t, asObj(s["failed"])["T9"], float64(1), "failed count")
	for _, ev := range asList(s["events"]) {
		if m := asObj(ev); m["task_key"] == "T9" && strings.Contains(m["summary"].(string), "second line") {
			t.Error("only the first error line goes in the summary")
		}
	}
}

func lastEvent(e *env, task, kind string) obj {
	for _, ev := range e.events() {
		if ev["task_key"] == task && ev["kind"] == kind {
			return ev
		}
	}
	return nil
}

func TestFailureOutputStored(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "T10", "--title", "fails loudly")
	e.cli("agent", "ag10", "--task", "T10", "--status", "running")
	var sb strings.Builder
	sb.WriteString("Exit code 2\n")
	for i := 0; i < 100; i++ {
		fmt.Fprintf(&sb, "line %d password=hunter2 %s\n", i, strings.Repeat("x", 300))
	}
	e.hook(obj{"hook_event_name": "PostToolUseFailure", "session_id": "s10", "cwd": "/x", "agent_id": "ag10", "tool_name": "Bash",
		"tool_input": obj{"command": "make test TOKEN=topsecretvalue1234 " + strings.Repeat("y", 3000)}, "error": strings.TrimSuffix(sb.String(), "\n"), "duration_ms": 1234})
	ev := lastEvent(e, "T10", "PostToolUseFailure")
	var d obj
	json.Unmarshal([]byte(ev["data"].(string)), &d)
	out := d["out"].(string)
	lines := strings.Split(out, "\n")
	if len(lines) > 40 || len(out) > 8192 || !strings.HasPrefix(lines[len(lines)-1], "line 99 ") {
		t.Errorf("out: %d lines, %d bytes, last %q", len(lines), len(out), lines[len(lines)-1][:10])
	}
	if strings.Contains(ev["data"].(string), "hunter2") || strings.Contains(ev["data"].(string), "topsecretvalue1234") || !strings.Contains(out, "[REDACTED]") {
		t.Error("redaction in the stored output")
	}
	if len([]rune(d["cmd"].(string))) > 2048 {
		t.Error("cmd too long")
	}
	eqv(t, []any{d["exit"], d["dur_ms"], d["tool"]}, []any{float64(2), float64(1234), "Bash"}, "exit, duration, tool")
	e.hook(obj{"hook_event_name": "PostToolUseFailure", "session_id": "s10", "cwd": "/x", "agent_id": "ag10", "tool_name": "Bash", "tool_input": obj{"command": "false"}, "error": "a\nb\nc"})
	ev = lastEvent(e, "T10", "PostToolUseFailure")
	json.Unmarshal([]byte(ev["data"].(string)), &d)
	eqv(t, d["out"], "a\nb\nc", "short output kept whole")
}

func TestDecisionEndpoint(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "D1", "--title", "decide me", "--status", "review", "--waiting", "me", "--next", "Check http://localhost:3000/x")
	e.post("/api/task/D1/brief", obj{"brief": "## r"})
	p := "/api/task/D1/decision"
	for tok, want := range map[string]int{"": 401, "wrong": 401} {
		if st, _ := e.postTok(p, obj{"action": "approve"}, tok); st != want {
			t.Errorf("tok %q: %d", tok, st)
		}
	}
	if st, _ := e.post("/api/task/NOPE/decision", obj{"action": "approve"}); st != 404 {
		t.Errorf("unknown task: %d", st)
	}
	if st, _ := e.post(p, obj{"action": "merge-it"}); st != 400 {
		t.Errorf("bad action: %d", st)
	}
	if st, _ := e.post(p, obj{"action": "reply", "text": "  "}); st != 400 {
		t.Errorf("empty reply: %d", st)
	}
	eqv(t, e.db.GetTask("D1")["needs_you"], true, "nothing changed by refused calls")
	st, tk := e.post(p, obj{"action": "approve"})
	eqv(t, []any{st, tk["waiting"], tk["status"], tk["needs_you"]}, []any{200, "agent", "building", false}, "approve")
	notes := asList(tk["notes"])
	eqv(t, asObj(notes[len(notes)-1])["text"], "Decision: approve and merge", "note")
	e.cli("task", "D1", "--waiting", "me")
	st, tk = e.post(p, obj{"action": "reply", "text": "Use the new flag, password=hunter2"})
	notes = asList(tk["notes"])
	eqv(t, []any{st, tk["waiting"], len(notes)}, []any{200, "agent", 2}, "reply")
	if !strings.HasPrefix(asObj(notes[len(notes)-1])["text"].(string), "Reply: Use the new flag") {
		t.Error("reply note")
	}
	if b, _ := json.Marshal(tk); strings.Contains(string(b), "hunter2") {
		t.Error("secret in the task")
	}
	n := 0
	var first obj
	for _, ev := range e.events() {
		if ev["task_key"] == "D1" && ev["kind"] == "decision" {
			if n == 0 {
				first = ev
			}
			n++
		}
	}
	var data obj
	json.Unmarshal([]byte(first["data"].(string)), &data)
	eqv(t, []any{n, data["action"], len(data)}, []any{2, "reply", 1}, "decision events")
	evs, _ := e.db.TaskEvents("D1", 200)
	for _, ev := range evs {
		if ev["kind"] == "decision" {
			t.Error("the timeline shows the note, not a second row")
		}
	}
	found := false
	for _, x := range asList(e.state(1)["tasks"]) {
		found = found || asObj(x)["key"] == "D1"
	}
	if !found {
		t.Error("delta misses D1")
	}
}

func TestBriefMovesToReviewAndDecisionBackToBuilding(t *testing.T) {
	e := newEnv(t)
	opts := []obj{{"id": "go", "label": "Go ahead"}}
	e.cli("task", "ST1", "--title", "work", "--status", "building", "--next", "writing code")
	st, tk := e.post("/api/task/ST1/brief", obj{"brief": "## r", "cwd": "/work/st1", "options": opts})
	eqv(t, []any{st, tk["status"], tk["next"], tk["waiting"]}, []any{200, "review", "Review card ready", "me"}, "brief")
	_, out, _ := e.cli("task", "ST1", "--status", "testing")
	if !strings.Contains(out, `"status": "testing"`) {
		t.Error("a later explicit status still wins")
	}
	e.post("/api/task/ST1/brief", obj{"brief": "## again", "cwd": "/work/st1", "options": opts})
	st, tk = e.post("/api/task/ST1/decision", obj{"action": "option", "option": "go"})
	eqv(t, []any{st, tk["status"], tk["next"], tk["waiting"]}, []any{200, "building", "Acting on: Go ahead", "agent"}, "option decision")
	e.post("/api/task/ST1/brief", obj{"brief": "## third", "cwd": "/work/st1", "options": opts})
	_, tk = e.post("/api/task/ST1/decision", obj{"action": "reply", "text": "use the flag"})
	eqv(t, []any{tk["status"], tk["next"]}, []any{"building", "Acting on: your reply"}, "reply decision")
	e.cli("task", "ST2", "--title", "shipped", "--status", "done", "--next", "merged")
	for _, c := range []struct {
		what string
		body obj
	}{{"brief", obj{"brief": "x", "cwd": "/work/st2", "options": opts}}, {"decision", obj{"action": "option", "option": "go"}}} {
		st, tk := e.post("/api/task/ST2/"+c.what, c.body)
		eqv(t, []any{st, tk["status"], tk["next"]}, []any{200, "done", "merged"}, c.what+" on a done task")
	}
}

func (e *env) brief(key, cwd string) (int, obj) {
	e.cli("task", key, "--title", "t "+key, "--status", "review")
	return e.post("/api/task/"+key+"/brief", obj{"brief": "## Review\nAll good", "cwd": cwd,
		"options": []obj{{"id": "merge", "label": "Approve and merge", "hint": "ships", "primary": true}, {"id": "hold", "label": "Hold"}}})
}

func TestBriefEndpointAndCLI(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "BR1", "--title", "t", "--status", "review")
	st, tk := e.post("/api/task/BR1/brief", obj{"brief": "## Plan\npassword=hunter2 ok", "cwd": "/work/br1", "options": []obj{{"id": "merge", "label": "Approve and merge", "primary": true}, {"id": "hold", "label": "Hold"}}})
	opts := asList(tk["brief_options"])
	eqv(t, []any{st, tk["waiting"], tk["brief_cwd"], asObj(opts[0])["id"], asObj(opts[1])["id"], asObj(opts[0])["primary"]}, []any{200, "me", "/work/br1", "merge", "hold", true}, "brief")
	if strings.Contains(tk["brief"].(string), "hunter2") {
		t.Error("secret in brief")
	}
	if lastEvent(e, "BR1", "brief") == nil {
		t.Error("no brief event")
	}
	p := "/api/task/BR1/brief"
	_, tk = e.post(p, obj{"brief": "second", "options": []obj{}, "cwd": "/work/br1"})
	eqv(t, tk["brief"], "second", "replaced")
	var many []obj
	for i := 0; i < 6; i++ {
		many = append(many, obj{"id": fmt.Sprintf("a%d", i), "label": "l"})
	}
	for _, bad := range []obj{{"brief": ""}, {"brief": "x", "options": []obj{{"id": "Bad Id", "label": "x"}}}, {"brief": "x", "options": many}, {"brief": "x", "cwd": "relative"}} {
		if st, _ := e.post(p, bad); st != 400 {
			t.Errorf("%v: %d", bad, st)
		}
	}
	if st, _ := e.post("/api/task/NOPE/brief", obj{"brief": "x"}); st != 404 {
		t.Errorf("unknown task: %d", st)
	}
	if st, _ := e.postTok(p, obj{"brief": "x"}, ""); st != 401 {
		t.Errorf("no token: %d", st)
	}
	f := filepath.Join(e.dir, "b.md")
	os.WriteFile(f, []byte("## From CLI\n- [ ] one"), 0o644)
	c, out, errs := e.cli("brief", "BR1", "--file", f, "--option", "merge|Approve and merge|ships it", "--option", "hold|Hold", "--primary", "merge", "--cwd", "/work/cli")
	if c != 0 {
		t.Fatal(errs)
	}
	json.Unmarshal([]byte(out), &tk)
	o := asList(tk["brief_options"])
	eqv(t, []any{tk["brief_cwd"], asObj(o[0])["hint"], asObj(o[0])["primary"], asObj(o[1])["primary"]}, []any{"/work/cli", "ships it", true, false}, "cli brief")
	if c, _, _ := e.cli("brief", "BR1", "--file", f, "--option", "a|A", "--primary", "zzz"); c == 0 {
		t.Error("--primary not among the options must fail")
	}
	got, _ := cli.ParseOptions([]string{"a|A|h", "b|B"}, "b")
	b, _ := json.Marshal(got)
	eqv(t, string(b), `[{"hint":"h","id":"a","label":"A","primary":false},{"hint":"","id":"b","label":"B","primary":true}]`, "ParseOptions")
}

func TestEnvFallsBackToOldBoardNames(t *testing.T) {
	t.Setenv("SHEEPDOG_TOKEN", "")
	t.Setenv("BOARD_TOKEN", "")
	eqv(t, store.Env("SHEEPDOG_TOKEN", "BOARD_TOKEN", "d"), "d", "default")
	t.Setenv("BOARD_TOKEN", "old")
	eqv(t, store.Env("SHEEPDOG_TOKEN", "BOARD_TOKEN", "d"), "old", "old name")
	t.Setenv("SHEEPDOG_TOKEN", "new")
	eqv(t, store.Env("SHEEPDOG_TOKEN", "BOARD_TOKEN", "d"), "new", "new wins")
	e := newEnv(t)
	t.Setenv("SHEEPDOG_URL", "")
	t.Setenv("SHEEPDOG_TOKEN", "")
	t.Setenv("BOARD_URL", e.ts.URL) // a client with only the old names still reaches the server
	t.Setenv("BOARD_TOKEN", token)
	if c, _, errs := e.cli("task", "ENV1", "--title", "old names"); c != 0 {
		t.Fatal(errs)
	}
}

func TestBrandFilesAndName(t *testing.T) {
	e := newEnv(t)
	st, b, h := e.do("GET", "/brand/icon-64.png", nil, nil)
	if st != 200 || h.Get("Content-Type") != "image/png" || h.Get("Cache-Control") != "max-age=86400" || !bytes.HasPrefix(b, []byte("\x89PNG")) {
		t.Errorf("brand: %d %v", st, h)
	}
	for _, bad := range []string{"/brand/../sheepdog.py", "/brand/brand-sheet.png", "/brand/%2e%2e/sheepdog.py", "/brand/"} {
		if st, _, _ := e.do("GET", bad, nil, nil); st != 404 {
			t.Errorf("%s: %d", bad, st)
		}
	}
	for _, page := range []string{"/", "/architecture"} {
		_, b, _ := e.get(page)
		if !strings.Contains(string(b), "/brand/favicon-32.png") || !strings.Contains(string(b), "sheepdog") {
			t.Errorf("%s misses the brand", page)
		}
	}
}

func TestLongReplyIsStoredWholeOrRefused(t *testing.T) {
	e := newEnv(t)
	e.brief("L1", "/work/l1")
	p := "/api/task/L1/decision"
	st, tk := e.post(p, obj{"action": "reply", "text": strings.Repeat("x", 25000)})
	if st != 400 || !strings.Contains(tk["error"].(string), "Too long: 25000 / 20000") {
		t.Errorf("long reply: %d %v", st, tk)
	}
	eqv(t, e.db.Q("SELECT COUNT(*) n FROM decisions WHERE task_key='L1'")[0]["n"], 0, "no decision row")
	ten := strings.Repeat("ab", 5000)
	st, tk = e.post(p, obj{"action": "reply", "text": ten})
	notes := asList(tk["notes"])
	eqv(t, []any{st, asObj(notes[len(notes)-1])["text"] == "Reply: "+ten}, []any{200, true}, "stored whole")
	eqv(t, e.db.Q("SELECT text FROM decisions WHERE task_key='L1'")[0]["text"] == ten, true, "decision text whole")
}

func (e *env) runHook(p obj) string {
	b, _ := json.Marshal(p)
	return hookViaCLI(e.t, string(b))
}

func TestReplyWithoutBrief(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "N1", "--title", "no card", "--status", "blocked", "--waiting", "me", "--next", "need an answer", "--worktree", "/work/n1")
	eqv(t, e.db.GetTask("N1")["needs_you"], true, "needs you")
	p := "/api/task/N1/decision"
	for _, body := range []obj{{"action": "approve"}, {"action": "option", "option": "go"}} {
		if st, _ := e.post(p, body); st != 400 {
			t.Errorf("%v without a brief: %d", body, st)
		}
	}
	eqv(t, e.db.Q("SELECT COUNT(*) n FROM decisions WHERE task_key='N1'")[0]["n"], 0, "refused calls store nothing")
	st, tk := e.post(p, obj{"action": "reply", "text": "use the flag"})
	eqv(t, []any{st, tk["waiting"], tk["status"], tk["needs_you"]}, []any{200, "agent", "building", false}, "reply")
	_, got := e.apiGet("/api/decisions/wait?cwd=/work/n1&timeout=0")
	g := asList(got)
	if len(g) != 1 || asObj(g[0])["label"] != "Reply" || asObj(g[0])["text"] != "use the flag" {
		t.Fatalf("waiter got %v", got)
	}
	// no worktree: the folder `sheepdog task` ran in carries it, and an empty or relative value never overwrites that
	e.post("/api/task", obj{"key": "N2", "title": "no card", "status": "blocked", "waiting": "me", "cwd": "/work/root"})
	e.post("/api/task", obj{"key": "N2", "cwd": ""})
	e.post("/api/task", obj{"key": "N2", "cwd": "rel"})
	if _, has := e.db.GetTask("N2")["task_cwd"]; has {
		t.Error("task_cwd is not part of the API task")
	}
	if st, _ := e.post("/api/task/N2/decision", obj{"action": "reply", "text": "hi"}); st != 200 {
		t.Errorf("reply: %d", st)
	}
	_, got = e.apiGet("/api/decisions/wait?cwd=/work/other&timeout=0")
	eqv(t, len(asList(got)), 0, "another folder")
	_, got = e.apiGet("/api/decisions/wait?cwd=/work/root&timeout=0")
	eqv(t, len(asList(got)), 1, "the folder the task was set from")
	// the CLI sends its own folder
	e.cli("task", "N3", "--title", "via cli", "--status", "blocked", "--waiting", "me")
	if r := e.db.Q("SELECT task_cwd FROM tasks WHERE key='N3'")[0]["task_cwd"]; r == nil || r == "" {
		t.Error("sheepdog task stored no folder")
	}
}

func TestDecisionExactlyOnceAndScoping(t *testing.T) {
	e := newEnv(t)
	e.brief("W1", "/work/w1/app")
	p := "/api/task/W1/decision"
	if st, _ := e.post(p, obj{"action": "option", "option": "nope"}); st != 400 {
		t.Error("unknown option")
	}
	eqv(t, e.db.Q("SELECT COUNT(*) n FROM decisions WHERE task_key='W1'")[0]["n"], 0, "none yet")
	st, tk := e.post(p, obj{"action": "option", "option": "hold"})
	notes := asList(tk["notes"])
	eqv(t, []any{st, tk["waiting"], asObj(notes[len(notes)-1])["text"], asObj(tk["decision"])["status"], tk["waiter"]}, []any{200, "agent", "Decision: Hold", "pending", false}, "decision")
	for _, other := range []string{"/work/other", "/work/w1/app2", "/work/w1/app/sub"} { // another folder, a sibling prefix, a deeper folder: nothing
		_, got := e.apiGet("/api/decisions/wait?cwd=" + other + "&timeout=0")
		eqv(t, len(asList(got)), 0, other)
	}
	if out := e.runHook(obj{"hook_event_name": "UserPromptSubmit", "session_id": "o", "cwd": "/elsewhere", "prompt": "hi"}); out != "" {
		t.Errorf("elsewhere: %q", out)
	}
	_, got := e.apiGet("/api/decisions/wait?cwd=/work/w1&timeout=0") // a parent folder owns the app folder
	g := asList(got)
	eqv(t, []any{len(g), asObj(g[0])["delivered_to"], asObj(g[0])["status"]}, []any{1, "waiter", "delivered"}, "waiter claim")
	_, got = e.apiGet("/api/decisions/wait?cwd=/work/w1&timeout=0")
	eqv(t, len(asList(got)), 0, "second wait")
	if out := e.runHook(obj{"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/w1/app", "prompt": "hi"}); out != "" {
		t.Errorf("nothing left for the hook: %q", out)
	}
	_, tk = e.post(p, obj{"action": "reply", "text": "use the flag"})
	eqv(t, tk["waiter"], true, "a wait for this folder was seen a moment ago")
	for _, no := range []string{"/work/w1", "/work", "/work/w1/app2"} { // hook is one-way
		if out := e.runHook(obj{"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": no, "prompt": "hi"}); out != "" {
			t.Errorf("%s got %q", no, out)
		}
	}
	out := e.runHook(obj{"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/w1/app/wt", "prompt": "hi"}) // a worktree under the project does
	var hk obj
	if err := json.Unmarshal([]byte(out), &hk); err != nil {
		t.Fatalf("hook output %q: %v", out, err)
	}
	ctx := asObj(hk["hookSpecificOutput"])
	did := e.db.Q("SELECT id FROM decisions WHERE task_key='W1' AND label='Reply'")[0]["id"]
	if ctx["hookEventName"] != "UserPromptSubmit" || !strings.Contains(ctx["additionalContext"].(string), fmt.Sprintf("BOARD DECISION #%d W1: Reply\nuse the flag\n(ack: sheepdog.py done %d)", did, did)) {
		t.Errorf("context: %v", ctx)
	}
	eqv(t, e.db.Q("SELECT delivered_to FROM decisions WHERE id=?", did)[0]["delivered_to"], "hook:s1", "delivered_to")
	_, got = e.apiGet("/api/decisions/wait?cwd=/work/w1&timeout=0")
	eqv(t, len(asList(got)), 0, "claimed by the hook")
	if out := e.runHook(obj{"hook_event_name": "UserPromptSubmit", "session_id": "s1", "cwd": "/work/w1/app", "prompt": "again"}); out != "" {
		t.Errorf("again: %q", out)
	}
	if st, _ := e.postTok(fmt.Sprintf("/api/decision/%d/done", did), obj{}, ""); st != 401 {
		t.Error("done needs a token")
	}
	if st, _ := e.post("/api/decision/999999/done", obj{}); st != 404 {
		t.Error("unknown decision")
	}
	_, d := e.post(fmt.Sprintf("/api/decision/%d/done", did), obj{})
	eqv(t, d["status"], "done", "done")
	eqv(t, e.db.GetTask("W1")["decision"].(store.Row)["status"], "done", "task decision")
	if c, _, errs := e.cli("done", fmt.Sprint(did)); c != 0 {
		t.Error(errs)
	}
}

func TestWaitLongPollDoesNotBlockServer(t *testing.T) {
	e := newEnv(t)
	e.brief("W2", "/work/w2")
	var got any
	var wg sync.WaitGroup
	wg.Add(1)
	go func() { defer wg.Done(); _, got = e.apiGet("/api/decisions/wait?cwd=/work/w2&timeout=10") }()
	time.Sleep(500 * time.Millisecond)
	t0 := time.Now()
	e.state(0)
	if time.Since(t0) > time.Second {
		t.Error("a parked waiter blocked another request")
	}
	t1 := time.Now()
	e.post("/api/task/W2/decision", obj{"action": "option", "option": "merge"})
	wg.Wait()
	if time.Since(t1) > 2*time.Second { // delivered by the change signal, not by a poll interval
		t.Errorf("delivery took %v", time.Since(t1))
	}
	eqv(t, asObj(asList(got)[0])["task_key"], "W2", "delivered")
}

func TestWaitCLIPrintsBlock(t *testing.T) {
	e := newEnv(t)
	e.brief("W3", "/work/w3")
	e.post("/api/task/W3/decision", obj{"action": "reply", "text": "line one"})
	c, out, _ := e.cli("wait", "--cwd", "/work/w3")
	did := e.db.Q("SELECT id FROM decisions WHERE task_key='W3'")[0]["id"]
	eqv(t, []any{c, out}, []any{0, fmt.Sprintf("BOARD DECISION #%d W3: Reply\nline one\n(ack: sheepdog.py done %d)\n", did, did)}, "wait output")
}

func TestResumedAgentRunsAgainAndRelinks(t *testing.T) {
	e := newEnv(t)
	base := func(p obj) obj { p["session_id"], p["cwd"] = "sr", "/work/resume"; return p }
	e.hook(base(obj{"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "tu_r1", "tool_input": obj{"description": "Task 41 Build it", "subagent_type": "general-purpose"}}))
	e.hook(base(obj{"hook_event_name": "SubagentStart", "agent_id": "ra-1", "agent_type": "general-purpose"}))
	e.hook(base(obj{"hook_event_name": "SubagentStop", "agent_id": "ra-1", "agent_type": "general-purpose"}))
	ag := func() obj { return e.agent("ra-1") }
	eqv(t, []any{ag()["status"], ag()["task_key"]}, []any{"done", "T41"}, "stopped")
	if ag()["ended_at"] == nil {
		t.Error("no ended_at")
	}
	e.hook(base(obj{"hook_event_name": "SubagentStart", "agent_id": "ra-1", "agent_type": "general-purpose"})) // resumed
	eqv(t, []any{ag()["status"], ag()["ended_at"]}, []any{"running", nil}, "resumed")
	msg := func(to, text string) {
		e.hook(base(obj{"hook_event_name": "PreToolUse", "tool_name": "SendMessage", "tool_input": obj{"to": to, "message": text}}))
	}
	msg("ra-1", "carry on, no prefix")
	eqv(t, ag()["task_key"], "T41", "no prefix: the link stays")
	msg("ra-1", "Task 42 now review the other thing")
	eqv(t, ag()["task_key"], "T42", "relinked")
	if e.task("T42") == nil {
		t.Error("T42 not created")
	}
	msg("Task 41 Build it", "Task 43 and again") // addressed by name
	eqv(t, ag()["task_key"], "T43", "by name")
	msg("nobody", "Task 44 lost message")
	eqv(t, ag()["task_key"], "T43", "unknown target")
	if e.task("T44") != nil {
		t.Error("T44 must not be created")
	}
	e.hook(base(obj{"hook_event_name": "SubagentStop", "agent_id": "ra-1", "agent_type": "general-purpose"}))
	eqv(t, ag()["status"], "done", "stop again")
}

func TestRawHookPayloadIsTrimmedRedactedAndStored(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "RH1", "--title", "raw hook")
	e.cli("agent", "rh-ag", "--task", "RH1", "--status", "running")
	raw := obj{"hook_event_name": "PostToolUseFailure", "session_id": "rh", "cwd": "/x", "agent_id": "rh-ag", "tool_name": "Bash", "transcript_path": "/secret/transcript.jsonl", "permission_mode": "default", "tool_use_id": "tu",
		"tool_input":    obj{"command": "make", "description": "d", "timeout": 5, "huge": strings.Repeat("y", 100000)},
		"tool_response": obj{"content": strings.Repeat("o", 3_000_000), "stderr": "boom\npassword=hunter2\nlast line", "interrupted": false},
		"error":         "Exit code 3\nboom\npassword=hunter2\nlast line"}
	if st, b := e.rawJSON("/api/hook", raw, token); st != 200 || len(b) != 0 {
		t.Errorf("empty reply expected: %d %q", st, b)
	}
	ev := lastEvent(e, "RH1", "PostToolUseFailure")
	b, _ := json.Marshal(ev)
	if strings.Contains(ev["data"].(string), "hunter2") || strings.Contains(string(b), "transcript") {
		t.Error("secret or transcript stored")
	}
	var d obj
	json.Unmarshal([]byte(ev["data"].(string)), &d)
	out := d["out"].(string)
	if len(out) > 8192 || len(strings.Split(out, "\n")) > 40 || !strings.Contains(out, "last line") || d["exit"] != float64(3) {
		t.Errorf("failure data: %v", d)
	}
	once := store.ReduceHook(raw)
	again, _ := json.Marshal(store.ReduceHook(once))
	first, _ := json.Marshal(once)
	eqv(t, string(again), string(first), "reduce is idempotent")
	if len(first) > 100000 {
		t.Error("reduced payload too big")
	}
	if st, b := e.rawJSON("/api/hook", once, token); st != 200 || len(b) != 0 {
		t.Error("reduced payload")
	}
	if st, _ := e.rawJSON("/api/hook", obj{"hook_event_name": "Stop"}, ""); st != 401 {
		t.Error("hook needs a token")
	}
	if st, _ := e.rawJSON("/api/hook", obj{"tool_name": "Bash"}, token); st != 400 {
		t.Error("no hook_event_name")
	}
}

func TestHookReplyIsEmptyOrExactJSON(t *testing.T) {
	e := newEnv(t)
	e.brief("H1", "/work/h1")
	e.post("/api/task/H1/decision", obj{"action": "option", "option": "hold"})
	prompt := func(cwd string) obj {
		return obj{"hook_event_name": "UserPromptSubmit", "session_id": "sh", "prompt": "hi", "cwd": cwd}
	}
	if st, b := e.rawJSON("/api/hook", prompt("/work/other"), token); st != 200 || len(b) != 0 {
		t.Error("another folder: nothing")
	}
	if st, b := e.rawJSON("/api/hook", obj{"hook_event_name": "Stop", "session_id": "sh", "cwd": "/work/h1"}, token); st != 200 || len(b) != 0 {
		t.Error("only prompts deliver")
	}
	st, b := e.rawJSON("/api/hook", prompt("/work/h1/sub"), token)
	did := e.db.Q("SELECT id FROM decisions WHERE task_key='H1'")[0]["id"]
	want := fmt.Sprintf(`{"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "Board decisions waiting for you:\n\nBOARD DECISION #%d H1: Hold\n(ack: sheepdog.py done %d)"}}`, did, did)
	want = strings.ReplaceAll(want, `\n`, `\n`)
	if st != 200 || string(b) != want {
		t.Errorf("hook JSON:\n got %s\nwant %s", b, want)
	}
	if st, b := e.rawJSON("/api/hook", prompt("/work/h1/sub"), token); st != 200 || len(b) != 0 {
		t.Error("delivered once")
	}
}

func TestBodyCaps(t *testing.T) {
	e := newEnv(t)
	big, _ := json.Marshal(obj{"hook_event_name": "Stop", "session_id": "big", "cwd": "/big", "pad": strings.Repeat("x", 5<<20)})
	if st, b := e.raw("/api/hook", big, token); st != 200 || len(b) != 0 {
		t.Errorf("5 MB on /api/hook: %d", st)
	}
	if st, _ := e.raw("/api/task", big, token); st != 413 {
		t.Errorf("1 MB elsewhere: %d", st)
	}
	if st, _ := e.raw("/api/hook", bytes.Repeat([]byte(" "), 9<<20), token); st != 413 {
		t.Errorf("8 MB ceiling: %d", st)
	}
}

func TestBigFailurePayloadDoesNotStallTheServer(t *testing.T) {
	e := newEnv(t)
	big, _ := json.Marshal(obj{"hook_event_name": "PostToolUseFailure", "session_id": "bf", "cwd": "/x", "tool_name": "Bash", "tool_input": obj{"command": "cat big"}, "tool_response": obj{"stdout": strings.Repeat("x", 5<<20)}, "error": "Exit code 1"})
	var wg sync.WaitGroup
	var st int
	var dt time.Duration
	wg.Add(1)
	go func() {
		defer wg.Done()
		t0 := time.Now()
		st, _ = e.raw("/api/hook", big, token)
		dt = time.Since(t0)
	}()
	time.Sleep(20 * time.Millisecond)
	t0 := time.Now()
	e.rawJSON("/api/hook", obj{"hook_event_name": "Stop", "session_id": "bf2", "cwd": "/y"}, token)
	small := time.Since(t0)
	wg.Wait()
	if st != 200 || dt > 600*time.Millisecond || small > 250*time.Millisecond {
		t.Errorf("big failure: status %d, %v, concurrent small hook %v", st, dt, small)
	}
}

func TestBoardPageSizeBudget(t *testing.T) {
	e := newEnv(t)
	st, b, _ := e.get("/")
	if st != 200 || len(b) >= 90*1000 || regexp.MustCompile(`\{\{(?:THEME_JS|BASE_CSS)\}\}`).Match(b) {
		t.Errorf("board page: %d, %d bytes", st, len(b))
	}
}

func TestArchitecturePageCoversRoutesTablesAndEvents(t *testing.T) {
	e := newEnv(t)
	if st, _, _ := e.do("GET", "/architecture", nil, nil); st != 401 {
		t.Error("needs a token")
	}
	st, b, _ := e.get("/architecture")
	page := string(b)
	if st != 200 || len(b) >= 16*1024 || !strings.Contains(page, "<svg") || !strings.Contains(page, "Parents and sub-tasks") || strings.Index(page, "<svg") > strings.Index(page, "<h2>") {
		t.Fatalf("page: %d, %d bytes", st, len(b))
	}
	if !regexp.MustCompile(`<code>tasks</code></td><td>[^<]*\bparent\b`).MatchString(page) {
		t.Error("tasks.parent missing from the generated table")
	}
	for _, rt := range []string{"/api/task/&lt;key&gt;/events", "/api/decision/&lt;id&gt;/done", "/brand/&lt;name&gt;", "/login", "/api/decisions/wait"} {
		if !strings.Contains(page, rt) {
			t.Errorf("route %s missing", rt)
		}
	}
	for _, tb := range []string{"tasks", "agents", "events", "decisions"} {
		if !strings.Contains(page, "<code>"+tb+"</code>") {
			t.Errorf("table %s missing", tb)
		}
	}
	for _, ev := range []string{"SessionStart", "SessionEnd", "Stop", "Notification", "UserPromptSubmit", "SubagentStart", "SubagentStop", "PreToolUse", "PostToolUse", "PostToolUseFailure"} {
		if !strings.Contains(page, "<code>"+ev+"</code>") {
			t.Errorf("event %s missing", ev)
		}
	}
	if !strings.Contains(page, "board page 89.4 KB of the 90 KB budget") || !strings.Contains(page, "page polls /api/state every 3 s") {
		t.Error("numbers")
	}
}

func TestParentValidationAndRollup(t *testing.T) {
	e := newEnv(t)
	up := func(k string, d obj) (int, obj) { d["key"] = k; return e.post("/api/task", d) }
	up("PV1", obj{"title": "big one"})
	up("PV2", obj{"title": "another big one"})
	for _, c := range []struct {
		k string
		d obj
	}{{"PV1", obj{"parent": "PV1"}}, {"PVX", obj{"parent": "NOPE-PARENT"}}} {
		if st, _ := up(c.k, c.d); st != 400 {
			t.Errorf("%s %v: %d", c.k, c.d, st)
		}
	}
	if e.db.GetTask("PVX") != nil {
		t.Error("the refused create left a task behind")
	}
	_, c := up("PVC", obj{"title": "kid", "parent": "PV1"})
	eqv(t, []any{c["parent"], c["parent_title"]}, []any{"PV1", "big one"}, "child")
	if st, _ := up("PVG", obj{"parent": "PVC"}); st != 400 {
		t.Error("one level only")
	}
	if st, _ := up("PV1", obj{"parent": "PV2"}); st != 400 {
		t.Error("a parent cannot become a sub-task")
	}
	_, c = up("PVC", obj{"parent": ""})
	eqv(t, c["parent"], nil, "cleared")
	p := e.db.GetTask("PV1")
	eqv(t, []any{len(p["children"].([]string)), p["progress"], p["status"]}, []any{0, nil, "planned"}, "no children")

	parent := func(name string, statuses ...string) store.Row {
		up(name, obj{"title": "parent " + name})
		for i, s := range statuses {
			up(fmt.Sprintf("%s-%d", name, i), obj{"title": "kid", "status": s, "parent": name, "waiting": "agent"})
		}
		return e.db.GetTask(name)
	}
	cases := []struct {
		kids []string
		want string
	}{{[]string{"done", "done"}, "done"}, {[]string{"done", "blocked", "building"}, "blocked"}, {[]string{"building", "testing", "planned"}, "review"},
		{[]string{"planned", "review"}, "review"}, {[]string{"analysing", "planned"}, "building"}, {[]string{"planned", "planned"}, "planned"}}
	for n, c := range cases {
		if got := parent(fmt.Sprintf("RU%d", n), c.kids...)["status"]; got != c.want {
			t.Errorf("%v: %v, want %s", c.kids, got, c.want)
		}
	}
	p = e.db.GetTask("RU1")
	b, _ := json.Marshal(p["progress"])
	eqv(t, fmt.Sprint(p["children"]), "[RU1-0 RU1-1 RU1-2]", "children in creation order")
	eqv(t, string(b), `{"by_status":{"blocked":1,"building":1,"done":1},"done":1,"total":3}`, "progress")
	d := e.db.GetTask("RU0")
	var minC, maxE int64 = 1 << 62, 0
	for _, k := range d["children"].([]string) {
		kt := e.db.GetTask(k)
		minC = min(minC, kt["created_at"].(int64))
		maxE = max(maxE, kt["end_at"].(int64))
	}
	eqv(t, []any{d["created_at"], d["end_at"]}, []any{minC, maxE}, "started and ended")
	eqv(t, e.db.GetTask("RU2")["needs_you"], false, "nobody waits on me")
	up("RU2-0", obj{"waiting": "me"})
	eqv(t, e.db.GetTask("RU2")["needs_you"], true, "child needs you")
	up("RU2-0", obj{"waiting": "agent"})
	eqv(t, e.db.GetTask("RU2")["needs_you"], false, "child no longer")
	up("RU2-1", obj{"status": "review", "waiting": ""})
	eqv(t, e.db.GetTask("RU2")["needs_you"], true, "review with no waiting")
	cur := int(e.state(0)["cursor"].(float64))
	up("RU4-0", obj{"status": "done"})
	found := false
	for _, x := range asList(e.state(cur)["tasks"]) {
		found = found || asObj(x)["key"] == "RU4"
	}
	if !found {
		t.Error("a changed child must re-send its parent")
	}
	child := e.task("RU4-0")
	eqv(t, []any{child["parent"], child["parent_title"]}, []any{"RU4", "parent RU4"}, "child chip")
}

func TestCLIParentSetAndClear(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "CP1", "--title", "big")
	_, out, _ := e.cli("task", "CP2", "--title", "piece", "--parent", "CP1")
	var tk obj
	json.Unmarshal([]byte(out), &tk)
	eqv(t, tk["parent"], "CP1", "set")
	_, out, _ = e.cli("task", "CP2", "--note", "x")
	json.Unmarshal([]byte(out), &tk)
	eqv(t, tk["parent"], "CP1", "omitted: unchanged")
	_, out, _ = e.cli("task", "CP2", "--parent", "")
	json.Unmarshal([]byte(out), &tk)
	eqv(t, tk["parent"], nil, "cleared")
	c, _, errs := e.cli("task", "CP3", "--parent", "NOPE-PARENT")
	if c == 0 || !strings.Contains(errs, "no such parent") {
		t.Errorf("%d %s", c, errs)
	}
}

func TestStaleAgent(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "ST1", "--title", "stale")
	e.cli("agent", "old-agent", "--task", "ST1", "--status", "running")
	e.cli("agent", "new-agent", "--task", "ST1", "--status", "running")
	e.db.X("UPDATE agents SET last_event_at=?, rev=rev+100 WHERE id='old-agent'", time.Now().Unix()-3*3600)
	time.Sleep(10 * time.Millisecond)
	e.post("/api/task", obj{"key": "ST1", "note": "wake the cache"}) // a write: the cached state is rebuilt
	eqv(t, []any{e.agent("old-agent")["status"], e.agent("new-agent")["status"]}, []any{"stale", "running"}, "stale")
}

// the cache must not serve a state older than the last write
func TestStateCacheSeesEveryWrite(t *testing.T) {
	e := newEnv(t)
	for i := 0; i < 20; i++ {
		key := fmt.Sprintf("K%d", i)
		e.cli("task", key, "--title", "t")
		if e.task(key) == nil {
			t.Fatalf("task %s missing right after the write", key)
		}
	}
}

func TestEventsEndpointAndStandup(t *testing.T) {
	e := newEnv(t)
	e.cli("task", "EV1", "--title", "events")
	st, v := e.apiGet("/api/task/EV1/events")
	eqv(t, []any{st, len(asList(v)) > 0}, []any{200, true}, "events")
	st, v = e.apiGet("/api/task/NOPE/events")
	eqv(t, []any{st, len(asList(v))}, []any{200, 0}, "unknown task")
	st, v = e.apiGet("/api/standup?off=0")
	if st != 200 || !strings.Contains(asObj(v)["text"].(string), "Blockers:") {
		t.Errorf("standup: %d %v", st, v)
	}
}

func TestUnknownRoutesAndMethods(t *testing.T) {
	e := newEnv(t)
	for _, c := range []struct {
		method, path string
		want         int
	}{{"GET", "/api/nope", 404}, {"GET", "/api/hook", 404}, {"POST", "/api/state", 404}, {"POST", "/", 404}, {"PUT", "/api/task", 501}} {
		if st, _, _ := e.do(c.method, c.path, []byte("{}"), auth(token)); st != c.want {
			t.Errorf("%s %s: %d, want %d", c.method, c.path, st, c.want)
		}
	}
	for _, body := range []string{"{not json", "[1,2]", "5"} {
		if st, _ := e.raw("/api/task", []byte(body), token); st != 400 {
			t.Errorf("body %q: %d", body, st)
		}
	}
}

func TestAllowlistOverHTTP(t *testing.T) {
	e := newEnv(t)
	e.db.SetAllow(store.ParseAllow("/work/in", nil))
	e.cli("task", "A1", "--title", "t", "--worktree", "/work/out/wt") // API and CLI calls are not filtered
	if e.task("A1") == nil {
		t.Fatal("a task from outside the allowlist must be accepted by the API")
	}
	rev := e.state(0)["cursor"]
	if st, b := e.rawJSON("/api/hook", obj{"hook_event_name": "SessionStart", "session_id": "o", "cwd": "/work/out/wt"}, token); st != 200 || len(b) != 0 {
		t.Errorf("outside hook: %d %q (must be the normal empty success)", st, b)
	}
	eqv(t, e.state(0)["cursor"], rev, "an outside hook must not move the revision")
	if st, b := e.rawJSON("/api/hook", obj{"hook_event_name": "SessionStart", "session_id": "i", "cwd": "/work/in/app"}, token); st != 200 || len(b) != 0 {
		t.Errorf("inside hook: %d %q", st, b)
	}
	if e.state(0)["cursor"] == rev {
		t.Error("an inside hook must be stored")
	}
}

// ---------------------------------------------------------------- privacy: nothing that names the person or the machine is kept

func fakeScrubber() *store.Scrubber {
	return store.NewScrubber([]string{"/Users/jdoe/dev/alpha"}, store.ScrubConfig{Home: "/Users/jdoe", User: "jdoe", Host: "jdoes-mbp.local", TmpDir: "/private/var/folders/xy/abc123/T"})
}

func TestNothingIdentifyingIsStoredOrServed(t *testing.T) {
	e := newEnv(t)
	e.db.SetScrub(fakeScrubber())
	home, a, tmp := "/Users/jdoe", "/Users/jdoe/dev/alpha", "/private/var/folders/xy/abc123/T"
	t.Setenv("PWD", a+"/cli") // the folder `sheepdog task` sends: stored rewritten like every other folder
	e.cli("task", "P1", "--title", "fix "+a+"/x.go for jdoe", "--project", "jdoes-mbp", "--branch", "jdoe/fix", "--worktree", a+"/wt",
		"--next", "ask jdoe@example.com about "+home+"/.ssh", "--note", "ran in "+tmp+"/run1 on jdoes-mbp.local")
	e.cli("agent", "ag-1", "--task", "P1", "--name", "jdoe's helper in "+a, "--model", "model-jdoe")
	md := filepath.Join(e.dir, "b.md")
	os.WriteFile(md, []byte("## Review "+a+"/web/a.go\nrun by jdoe on jdoes-mbp, logs /home/jdoe/logs/x.log and "+tmp+"/out.txt\n"), 0o644)
	if c, _, errs := e.cli("brief", "P1", "--file", md, "--option", "go|Go for jdoe|"+home+"/hint", "--cwd", a+"/web"); c != 0 {
		t.Fatal(errs)
	}
	e.post("/api/task/P1/decision", obj{"action": "reply", "text": "use " + a + "/other and ask jdoe"})
	hook := func(p obj) {
		p["session_id"] = "s-1"
		if _, ok := p["cwd"]; !ok {
			p["cwd"] = a + "/web"
		}
		e.hook(p)
	}
	for _, p := range []obj{
		{"hook_event_name": "SessionStart", "source": "startup in /Users/jdoe", "model": "m-jdoe"}, {"hook_event_name": "SessionEnd", "reason": "done at /Users/jdoe/x"}, {"hook_event_name": "Stop"},
		{"hook_event_name": "UserPromptSubmit", "prompt": "look at " + a + "/z.go, jdoe@example.com, token=abc123secret, " + tmp + "/f"},
		{"hook_event_name": "Notification", "message": "jdoe needs " + a + "/perm", "notification_type": "permission_prompt"},
		{"hook_event_name": "SubagentStart", "agent_id": "ag-1", "agent_type": "type-jdoe"}, {"hook_event_name": "SubagentStop", "agent_id": "ag-1", "agent_type": "type-jdoe"},
		{"hook_event_name": "PostToolUse", "tool_name": "Bash", "agent_id": "ag-1", "tool_input": obj{"command": "cd " + a + " && make && cat /home/jdoe/.zshrc " + tmp + "/o", "description": "jdoes-mbp"}},
		{"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "agent_id": "ag-1", "tool_input": obj{"command": "go test " + a + "/..."}, "error": "Exit code 2\n" + a + "/a.go:3: jdoe broke it\nat " + tmp + "/b",
			"tool_response": obj{"stderr": "/home/jdoe/go/pkg/mod/x\n/Users/jdoe/Library/y", "stdout": "host jdoes-mbp.local"}},
		{"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "tu-1", "tool_input": obj{"description": "Task 5 edit " + a + "/q.go as jdoe", "subagent_type": "t-jdoe", "prompt": "x"}},
		{"hook_event_name": "PreCompact", "trigger": "jdoe"}, {"hook_event_name": "PostToolUse", "tool_name": "Edit", "tool_input": obj{"file_path": a + "/web/e.go"}},
		{"hook_event_name": "PostToolUse", "tool_name": "Grep", "tool_input": obj{"pattern": "jdoe", "path": home + "/src"}},
		{"hook_event_name": "PostToolUse", "tool_name": "Agent", "tool_use_id": "tu-1", "tool_input": obj{"description": "Task 5 edit " + a + "/q.go"}, "tool_response": obj{"status": "completed", "agentId": "ag-9", "totalTokens": 5}},
		{"hook_event_name": "PreToolUse", "tool_name": "SendMessage", "tool_input": obj{"to": "ag-1", "message": "Task 6 go to " + a + "/x, jdoe"}},
		{"hook_event_name": "UserPromptSubmit", "prompt": "deliver", "cwd": a + "/web/deep"},
	} {
		hook(p)
	}
	var blobs []string
	for _, tb := range []string{"tasks", "agents", "events", "decisions"} {
		for _, r := range e.db.Q("SELECT * FROM " + tb) {
			b, _ := json.Marshal(r)
			blobs = append(blobs, string(b))
		}
	}
	for _, p := range []string{"/api/state", "/api/standup?off=0", "/api/task/P1/events", "/api/decisions/wait?cwd=" + a + "&timeout=0", "/", "/architecture"} {
		_, b, _ := e.get(p)
		blobs = append(blobs, string(b))
	}
	_, out, _ := e.cli("task", "P1", "--note", "again from "+a)
	blobs = append(blobs, out)
	all := strings.Join(blobs, "\n")
	for _, bad := range []string{"jdoe", "jdoes-mbp", "/Users/", "/home/", "/private/var/folders", "var/folders", "/tmp/", "abc123"} {
		if strings.Contains(all, bad) {
			i := strings.Index(all, bad)
			t.Errorf("%q leaked: ...%s...", bad, all[max(0, i-60):min(len(all), i+60)])
		}
	}
	if _, b, _ := e.get("/api/state"); !strings.Contains(string(b), "alpha:web/a.go") {
		t.Error("the rewritten form must be there, not nothing")
	}
}

func TestDeliveryStillMatchesRewrittenFolders(t *testing.T) {
	e := newEnv(t)
	e.db.SetScrub(fakeScrubber())
	a := "/Users/jdoe/dev/alpha"
	md := filepath.Join(e.dir, "b.md")
	os.WriteFile(md, []byte("## r"), 0o644)
	for _, c := range [][2]string{{"D1", a + "/app"}, {"D2", a}, {"D3", "/Users/jdoe/other"}} {
		e.cli("task", c[0], "--title", c[0], "--status", "review")
		if code, _, errs := e.cli("brief", c[0], "--file", md, "--option", "go|Go", "--cwd", c[1]); code != 0 {
			t.Fatal(errs)
		}
		e.post("/api/task/"+c[0]+"/decision", obj{"action": "option", "option": "go"})
	}
	stored := map[string]string{}
	for _, r := range e.db.Q("SELECT task_key, cwd FROM decisions") {
		stored[r["task_key"].(string)] = r["cwd"].(string)
	}
	eqv(t, fmt.Sprint(stored), "map[D1:alpha:app D2:alpha: D3:~/other]", "stored folders")
	claim := func(cwd string) string {
		got, err := e.db.Ingest(obj{"hook_event_name": "UserPromptSubmit", "session_id": "p", "cwd": cwd, "prompt": "hi"})
		if err != nil {
			t.Fatal(err)
		}
		var keys []string
		for _, d := range got {
			keys = append(keys, d["task_key"].(string))
		}
		return strings.Join(keys, ",")
	}
	eqv(t, claim(a+"/appx"), "D2", "a sibling of app is not under D1; the brief for the root covers everything below it")
	e.cli("brief", "D2", "--file", md, "--option", "go|Go", "--cwd", a)
	eqv(t, claim("/Users/jdoe/dev"), "", "a parent of the root claims nothing")
	eqv(t, claim(a+"/app/src/deep"), "D1", "a sub-folder of the brief's folder")
	eqv(t, claim("/Users/jdoe/other/x"), "D3", "outside any root (the ~ form)")
	e.cli("brief", "D1", "--file", md, "--option", "go|Go", "--cwd", a+"/app")
	e.post("/api/task/D1/decision", obj{"action": "option", "option": "go"})
	_, got := e.apiGet("/api/decisions/wait?cwd=" + a + "&timeout=0") // the waiter's --cwd is rewritten the same way
	eqv(t, asObj(asList(got)[0])["task_key"], "D1", "the waiter claims what is under the root")
	if !e.db.WaiterFor("alpha:app") {
		t.Error("the waiter's folder is recorded in the rewritten form")
	}
}
