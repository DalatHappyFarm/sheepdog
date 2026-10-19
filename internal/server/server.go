// Package server is the HTTP side of the board: the router, auth, the pages, the long-poll. It is a port of Handler in sheepdog.py;
// the routes, status codes, headers and JSON shapes are the same, and the contract test holds it to that.
package server

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/DalatHappyFarm/sheepdog"
	"github.com/DalatHappyFarm/sheepdog/internal/store"
)

const (
	bodyMax = 1 << 20
	hookMax = 8 << 20
	waitMax = 55 // the longest long-poll, seconds
)

type route struct {
	method, pattern, name, doc string
	rx                         *regexp.Regexp
}

// routes is the router: the /architecture page is generated from this table.
var routes = []route{
	{"GET", `/`, "page", "the board page, one HTML document", nil},
	{"GET", `/architecture`, "arch", "this page", nil},
	{"GET", `/api/state`, "state", "tasks, agents and events changed since ?since=rev; the page polls it every 3 s", nil},
	{"GET", `/api/standup`, "standup", "plain-text standup (Copy standup)", nil},
	{"GET", `/api/task/(?P<key>[^/]+)/events`, "events", "one task's timeline events", nil},
	{"GET", `/api/decisions/wait`, "wait", "long-poll (up to 55 s) for decisions under ?cwd=; claims each exactly once", nil},
	{"POST", `/api/hook`, "hook", "the raw Claude Code hook payload; empty reply, or the hook JSON when decisions wait", nil},
	{"POST", `/api/task`, "task", "create or update a task", nil},
	{"POST", `/api/task/(?P<key>[^/]+)/brief`, "brief", "post a review card (markdown + option buttons)", nil},
	{"POST", `/api/task/(?P<key>[^/]+)/decision`, "decision", "the developer's answer: option, reply or approve (a reply needs no review card; the others do)", nil},
	{"POST", `/api/decision/(?P<id>\d+)/done`, "done", "Claude acknowledges a decision", nil},
	{"POST", `/api/agent`, "agent", "create or update an agent row", nil},
	{"POST", `/api/archive`, "archive", "hide a task from the board", nil},
	{"GET", `/brand/(?P<name>[^/]+)`, "brand", "the four brand PNGs (fixed list, no sign-in: the sign-in page uses them)", nil},
	{"POST", `/login`, "login", "token form, sets the cookie", nil},
}

type hookEvent struct{ name, doc string }

var hookEvents = []hookEvent{ // the hook events Ingest handles
	{"SessionStart", "logged"}, {"SessionEnd", "logged"}, {"Stop", "turn finished, logged"}, {"Notification", "logged"},
	{"UserPromptSubmit", "logged; also hands over waiting decisions for the prompt's folder"},
	{"SubagentStart", "agent starts, or is resumed (status running)"}, {"SubagentStop", "agent done"},
	{"PreToolUse", "Agent spawns an agent row; SendMessage re-links a resumed agent; other tools ignored"},
	{"PostToolUse", "agent finished, or an ordinary tool counted and logged (SHEEPDOG_TOOL_EVENTS=0 skips it)"},
	{"PostToolUseFailure", "failed command: command, last 40 output lines (redacted), exit code, duration"},
}

func init() {
	for i := range routes {
		routes[i].rx = regexp.MustCompile(`^(?:` + routes[i].pattern + `)$`)
	}
}

// Server is the HTTP handler of one board.
type Server struct {
	DB    *store.DB
	Token *string // nil: no auth

	board []byte // the filled pages, built once
	login string // still holds {{ERR}}
	arch  string // template, newlines dropped

	failMu sync.Mutex
	fails  map[string][]time.Time // wrong-token attempts per remote address; in memory, resets on restart
}

// New builds the handler: it fills the page files once.
func New(db *store.DB, token *string) *Server {
	s := &Server{DB: db, Token: token, fails: map[string][]time.Time{}}
	r := strings.NewReplacer("{{THEME_JS}}", sheepdog.ThemeJS, "{{BASE_CSS}}", sheepdog.BaseCSS)
	s.board = []byte(r.Replace(sheepdog.BoardHTML))
	s.login = strings.NewReplacer("{{THEME_JS}}", sheepdog.ThemeJS).Replace(sheepdog.LoginHTML)
	s.arch = strings.ReplaceAll(sheepdog.ArchHTML, "\n", "")
	return s
}

// ---------------------------------------------------------------- small Python-compatible helpers

func eq(a, b string) bool { return subtle.ConstantTimeCompare([]byte(a), []byte(b)) == 1 }

// pyUnquote is urllib.parse.unquote: bad escapes stay as written, bytes decode as UTF-8 with replacement.
func pyUnquote(s string) string {
	if !strings.ContainsRune(s, '%') {
		return s
	}
	var b []byte
	for i := 0; i < len(s); i++ {
		if s[i] == '%' && i+2 < len(s)+0 && isHex(s[i+1]) && isHex(s[i+2]) {
			v, _ := strconv.ParseUint(s[i+1:i+3], 16, 8)
			b = append(b, byte(v))
			i += 2
			continue
		}
		b = append(b, s[i])
	}
	return strings.ToValidUTF8(string(b), "�")
}

func isHex(c byte) bool {
	return c >= '0' && c <= '9' || c >= 'a' && c <= 'f' || c >= 'A' && c <= 'F'
}

// parseQS is urllib.parse.parse_qs: blank values are dropped, '+' is a space.
func parseQS(q string) map[string][]string {
	out := map[string][]string{}
	for _, part := range strings.Split(q, "&") {
		k, v, ok := strings.Cut(part, "=")
		if !ok || v == "" {
			continue
		}
		k = pyUnquote(strings.ReplaceAll(k, "+", " "))
		out[k] = append(out[k], pyUnquote(strings.ReplaceAll(v, "+", " ")))
	}
	return out
}

func first(qs map[string][]string, k, def string) string {
	if v := qs[k]; len(v) > 0 {
		return v[0]
	}
	return def
}

func cookie(tok string) string {
	return "board_token=" + store.Quote(tok) + "; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000"
}

var pyEscaper = strings.NewReplacer("&", "&amp;", "<", "&lt;", ">", "&gt;", `"`, "&quot;", "'", "&#x27;")

// ---------------------------------------------------------------- request context

type req struct {
	s  *Server
	w  http.ResponseWriter
	r  *http.Request
	qs map[string][]string
}

func (q *req) send(code int, body any, ctype string, extra ...[2]string) {
	var b []byte
	switch x := body.(type) {
	case []byte:
		b = x
	case string:
		b = []byte(x)
	default:
		var buf bytes.Buffer
		enc := json.NewEncoder(&buf)
		enc.SetEscapeHTML(false)
		if err := enc.Encode(body); err != nil {
			q.send(500, map[string]string{"error": "TypeError: " + err.Error()}, "application/json")
			return
		}
		b = bytes.TrimSuffix(buf.Bytes(), []byte("\n"))
	}
	h := q.w.Header()
	h["Content-Type"] = []string{ctype + "; charset=utf-8"}
	h["Content-Length"] = []string{strconv.Itoa(len(b))}
	h["Cache-Control"] = []string{"no-store"}
	h["X-Content-Type-Options"] = []string{"nosniff"}
	for _, e := range extra {
		h[e[0]] = []string{e[1]}
	}
	q.w.WriteHeader(code)
	q.w.Write(b)
}

func (q *req) json(code int, body any) { q.send(code, body, "application/json") }

func (q *req) authed() bool {
	if q.s.Token == nil {
		return true
	}
	tok := *q.s.Token
	h := q.r.Header.Get("Authorization")
	if len(h) >= 7 && strings.EqualFold(h[:7], "bearer ") && eq(strings.TrimSpace(h[7:]), tok) {
		return true
	}
	for _, part := range strings.Split(q.r.Header.Get("Cookie"), ";") {
		k, v, _ := strings.Cut(strings.TrimSpace(part), "=")
		if k == "board_token" && eq(pyUnquote(v), tok) {
			return true
		}
	}
	return false
}

func find(method, path string) (*route, map[string]string) {
	for i := range routes {
		rt := &routes[i]
		if rt.method != method {
			continue
		}
		m := rt.rx.FindStringSubmatch(path)
		if m == nil {
			continue
		}
		g := map[string]string{}
		for j, n := range rt.rx.SubexpNames() {
			if n != "" {
				g[n] = pyUnquote(m[j])
			}
		}
		return rt, g
	}
	return nil, nil
}

// rawPath is the request path as sent (urlparse(self.path).path): not decoded, without the query or fragment.
func rawPath(r *http.Request) (path, query string) {
	u := r.RequestURI
	if i := strings.IndexByte(u, '#'); i >= 0 {
		u = u[:i]
	}
	path, query, _ = strings.Cut(u, "?")
	return
}

// ServeHTTP implements http.Handler.
func (s *Server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	q := &req{s: s, w: w, r: r}
	defer func() {
		if rec := recover(); rec != nil {
			if rec == http.ErrAbortHandler {
				panic(rec)
			}
			var msg string
			switch e := rec.(type) {
			case error:
				msg = e.Error()
			default:
				msg = fmt.Sprint(e)
			}
			if !strings.Contains(msg, ": ") {
				msg = "Exception: " + msg
			}
			q.json(500, map[string]string{"error": msg})
		}
	}()
	switch r.Method {
	case "GET":
		q.get()
	case "POST":
		q.post()
	default:
		q.send(501, fmt.Sprintf("Unsupported method (%q)", r.Method), "text/plain")
	}
}

func (q *req) get() {
	path, rawq := rawPath(q.r)
	q.qs = parseQS(rawq)
	s := q.s
	if path == "/" && s.Token != nil && len(q.qs["token"]) > 0 && eq(q.qs["token"][0], *s.Token) {
		q.send(302, []byte{}, "text/plain", [2]string{"Location", "/"}, [2]string{"Set-Cookie", cookie(*s.Token)})
		return
	}
	if strings.HasPrefix(path, "/brand/") {
		q.brand(pyUnquote(path[7:]))
		return
	}
	if !q.authed() {
		if path == "/" && strings.Contains(q.r.Header.Get("Accept"), "text/html") {
			q.send(200, strings.Replace(s.login, "{{ERR}}", "", 1), "text/html")
			return
		}
		if path != "/" {
			q.json(401, map[string]string{"error": "unauthorized"})
		} else {
			q.send(401, "unauthorized: open /?token=<SHEEPDOG_TOKEN> once", "text/plain")
		}
		return
	}
	rt, g := find("GET", path)
	if rt == nil {
		q.json(404, map[string]string{"error": "not found"})
		return
	}
	switch rt.name {
	case "page":
		q.send(200, s.board, "text/html")
	case "arch":
		q.send(200, s.archPage(), "text/html")
	case "state":
		since, err := strconv.ParseInt(strings.TrimSpace(first(q.qs, "since", "0")), 10, 64)
		if err != nil {
			since = 0
		}
		body, err := s.DB.State(since)
		if err != nil {
			panic(err)
		}
		q.send(200, body, "application/json")
	case "standup":
		var off *int64
		if o := first(q.qs, "off", ""); isDigits(strings.TrimLeft(o, "-")) {
			if n, err := strconv.ParseInt(o, 10, 64); err == nil {
				off = &n
			}
		}
		text, err := s.DB.Standup(0, off)
		if err != nil {
			panic(err)
		}
		q.json(200, map[string]string{"text": text})
	case "events":
		ev, err := s.DB.TaskEvents(g["key"], 200)
		if err != nil {
			panic(err)
		}
		q.json(200, ev)
	case "wait":
		q.wait()
	}
}

func isDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

func (q *req) brand(name string) {
	b, ok := sheepdog.Brand(name) // a fixed list, so no path can leave the folder
	if !ok {
		q.json(404, map[string]string{"error": "not found"})
		return
	}
	h := q.w.Header()
	h["Content-Type"] = []string{"image/png"}
	h["Content-Length"] = []string{strconv.Itoa(len(b))}
	h["Cache-Control"] = []string{"max-age=86400"}
	h["X-Content-Type-Options"] = []string{"nosniff"}
	q.w.WriteHeader(200)
	io.WriteString(q.w, b)
}

// wait is the long-poll: it claims decisions under ?cwd= and sleeps on the store's change signal instead of polling the database.
func (q *req) wait() {
	db, cwd := q.s.DB, first(q.qs, "cwd", "")
	if !strings.HasPrefix(cwd, "/") {
		q.json(400, map[string]string{"error": "cwd must be an absolute path"})
		return
	}
	cwd = db.RewriteCwd(cwd) // the form briefs and hooks are stored and compared in
	secs := int64(25)
	if n, err := strconv.ParseInt(strings.TrimSpace(first(q.qs, "timeout", "25")), 10, 64); err == nil {
		secs = n
	}
	secs = max(0, min(waitMax, secs))
	end := time.Now().Add(time.Duration(secs) * time.Second)
	ctx := q.r.Context()
	for {
		db.Seen(cwd)
		changed := db.Changed() // taken before the claim, so a decision made in between is not missed
		got := db.Claim(cwd, "waiter", false)
		if len(got) > 0 || !time.Now().Before(end) {
			db.Seen(cwd)
			q.json(200, got)
			return
		}
		wake := time.NewTimer(min(time.Until(end), 20*time.Second)) // the waiter stays "alive" for 70 s; the timer renews it
		select {
		case <-ctx.Done(): // the waiter hung up: claiming now would lose the decision
			wake.Stop()
			return
		case <-changed:
		case <-wake.C:
		}
		wake.Stop()
	}
}

// ---------------------------------------------------------------- POST

func (q *req) login() {
	s := q.s
	host, _, err := net.SplitHostPort(q.r.RemoteAddr)
	if err != nil {
		host = q.r.RemoteAddr
	}
	now := time.Now()
	s.failMu.Lock()
	kept := s.fails[host][:0:0]
	for _, t := range s.fails[host] {
		if now.Sub(t) < 60*time.Second {
			kept = append(kept, t)
		}
	}
	s.fails[host] = kept
	throttled := len(kept) >= 5
	s.failMu.Unlock()
	if throttled {
		q.send(429, "too many attempts, wait a minute", "text/plain", [2]string{"Retry-After", "60"})
		return
	}
	n := int64(0)
	if q.r.ContentLength > 0 {
		n = min(q.r.ContentLength, 4096)
	}
	http.NewResponseController(q.w).SetReadDeadline(time.Now().Add(30 * time.Second))
	body, _ := io.ReadAll(io.LimitReader(q.r.Body, n))
	tok := first(parseQS(strings.ToValidUTF8(string(body), "�")), "token", "")
	if s.Token != nil && eq(tok, *s.Token) {
		q.send(303, []byte{}, "text/plain", [2]string{"Location", "/"}, [2]string{"Set-Cookie", cookie(*s.Token)})
		return
	}
	s.failMu.Lock()
	s.fails[host] = append(s.fails[host], now)
	if len(s.fails) > 1024 {
		for k, v := range s.fails {
			if len(v) == 0 || now.Sub(v[len(v)-1]) > 60*time.Second {
				delete(s.fails, k)
			}
		}
	}
	s.failMu.Unlock()
	q.send(401, strings.Replace(s.login, "{{ERR}}", "<p class=err>Wrong token</p>", 1), "text/html")
}

func (q *req) post() {
	path, _ := rawPath(q.r)
	rt, g := find("POST", path)
	if rt != nil && rt.name == "login" {
		q.login()
		return
	}
	if !q.authed() {
		q.json(401, map[string]string{"error": "unauthorized"})
		return
	}
	if rt == nil {
		q.json(404, map[string]string{"error": "not found"})
		return
	}
	n := q.r.ContentLength
	if n < 0 {
		n = 0
	}
	limit := int64(bodyMax)
	if rt.name == "hook" {
		limit = hookMax
	}
	if n > limit {
		io.CopyN(io.Discard, q.r.Body, min(n, 2*hookMax)) // drain what the client is sending so it can read the 413 instead of a reset
		q.json(413, map[string]string{"error": "too large"})
		return
	}
	http.NewResponseController(q.w).SetReadDeadline(time.Now().Add(30 * time.Second))
	raw, _ := io.ReadAll(io.LimitReader(q.r.Body, n))
	var d map[string]any
	if len(bytes.TrimSpace(raw)) == 0 && len(raw) == 0 {
		d = map[string]any{}
	} else {
		dec := json.NewDecoder(bytes.NewReader(raw))
		dec.UseNumber()
		var v any
		if err := dec.Decode(&v); err != nil {
			q.json(400, map[string]string{"error": err.Error()})
			return
		}
		if _, err := dec.Token(); err != io.EOF {
			q.json(400, map[string]string{"error": "Extra data"})
			return
		}
		m, ok := v.(map[string]any)
		if !ok {
			q.json(400, map[string]string{"error": "JSON object expected"})
			return
		}
		d = m
	}
	q.handle(rt.name, d, g)
}

func (q *req) handle(name string, d map[string]any, g map[string]string) {
	db := q.s.DB
	var out any
	var err error
	switch name {
	case "hook":
		var got []store.Row
		got, err = db.Ingest(d)
		if err == nil {
			if len(got) > 0 {
				q.send(200, store.HookJSON(got), "application/json")
			} else {
				q.send(200, []byte{}, "application/json")
			}
			return
		}
	case "task":
		out, err = db.TaskUpsert(d)
	case "agent":
		out, err = db.AgentUpsert(d)
	case "archive":
		key := ""
		if v, ok := d["key"]; ok && v != nil && v != "" {
			key = fmt.Sprint(v)
		}
		out, err = db.Archive(key)
	case "decision":
		out, err = db.Decide(g["key"], d)
	case "brief":
		out, err = db.SetBrief(g["key"], d)
	case "done":
		id, perr := strconv.ParseInt(g["id"], 10, 64)
		if perr != nil {
			q.json(404, map[string]string{"error": "no such decision"})
			return
		}
		var row store.Row
		row, err = db.FinishDecision(id)
		if err == nil && row == nil {
			q.json(404, map[string]string{"error": "no such decision"})
			return
		}
		out = row
	}
	if err != nil {
		var br store.BadRequest
		var nf store.NotFound
		switch {
		case errors.As(err, &br):
			q.json(400, map[string]string{"error": br.Error()})
		case errors.As(err, &nf):
			q.json(404, map[string]string{"error": nf.Error()})
		default:
			q.json(500, map[string]string{"error": err.Error()})
		}
		return
	}
	q.json(200, out)
}

// ---------------------------------------------------------------- the architecture page

var pollRe = regexp.MustCompile(`setTimeout\(loop,(\d+)\)`)

func (s *Server) archPage() string {
	esc := pyEscaper.Replace
	show := regexp.MustCompile(`\(\?P<(\w+)>[^)]*\)`)
	var routesHTML, tablesHTML, eventsHTML, numsHTML strings.Builder
	for _, rt := range routes {
		fmt.Fprintf(&routesHTML, "<tr><td>%s</td><td><code>%s</code></td><td>%s</td></tr>", rt.method, esc(show.ReplaceAllString(rt.pattern, "<$1>")), esc(rt.doc))
	}
	names, cols, err := s.DB.TableColumns()
	if err != nil {
		panic(err)
	}
	for _, t := range names {
		fmt.Fprintf(&tablesHTML, "<tr><td><code>%s</code></td><td>%s</td></tr>", t, esc(strings.Join(cols[t], ", ")))
	}
	for _, e := range hookEvents {
		fmt.Fprintf(&eventsHTML, "<tr><td><code>%s</code></td><td>%s</td></tr>", e.name, esc(e.doc))
	}
	poll := int64(0)
	if m := pollRe.FindSubmatch(s.board); m != nil {
		poll, _ = strconv.ParseInt(string(m[1]), 10, 64)
	}
	nums := []string{
		fmt.Sprintf("board page %.1f KB of the 90 KB budget", float64(len(s.board))/1000),
		fmt.Sprintf("page polls /api/state every %s s", strconv.FormatFloat(float64(poll)/1000, 'g', -1, 64)),
		"board state: cached per revision, rebuilt after a write or after 2 s",
		"hook: curl gives up after 2 s (sheepdog hook: 1 s); the reply is empty unless decisions wait",
		fmt.Sprintf("request bodies: %d MB on /api/hook, %d MB elsewhere", hookMax>>20, bodyMax>>20),
		fmt.Sprintf("long-poll: at most %d s per call", waitMax),
		fmt.Sprintf("an agent silent for %d h shows as stale; events are pruned after 30 days", store.StaleSecs/3600),
	}
	for _, n := range nums {
		fmt.Fprintf(&numsHTML, "<li>%s</li>", esc(n))
	}
	return strings.NewReplacer("{{THEME_JS}}", sheepdog.ThemeJS, "{{BASE_CSS}}", sheepdog.BaseCSS, "{{DIAGRAM}}", sheepdog.ArchDiagramHTML,
		"{{NUMBERS}}", numsHTML.String(), "{{ROUTES}}", routesHTML.String(), "{{TABLES}}", tablesHTML.String(), "{{EVENTS}}", eventsHTML.String()).Replace(s.arch)
}

var _ = os.Getenv
