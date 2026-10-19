// Package store is the board's database and everything that changes or reads it: tasks, agents, decisions, events, hook ingest.
// It is a port of the DB class of sheepdog.py; the schema, the rules and the messages are the same.
package store

import (
	"cmp"
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"net/url"
	"os"
	"regexp"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	_ "modernc.org/sqlite" // pure Go SQLite driver, no cgo
)

var (
	Statuses      = []string{"planned", "analysing", "building", "review", "testing", "done", "blocked"}
	AgentStatuses = []string{"running", "done", "failed"}
	Waiting       = []string{"me", "agent", "qc", "po", "none"} // who the next step waits on; "" = unset
)

const (
	StaleSecs = 2 * 3600
	TextMax   = 20000 // briefs, notes and replies; a reply over it is refused, never cut
)

var (
	keyRe   = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,32}$`)
	optIDRe = regexp.MustCompile(`^[a-z0-9-]{1,32}$`)
	taskRe  = regexp.MustCompile(`^Task\s+(\d+)\b[\s:.-]*`) // the dev's rule: every agent description starts with "Task <N> ..."
	mrRe    = regexp.MustCompile(`/merge_requests/(\d+)`)
	httpRe  = regexp.MustCompile(`^https?://`)
)

// pyMatch is re.match with a pattern ending in $: Python's $ also matches before one final newline.
func pyMatch(rx *regexp.Regexp, s string) bool { return rx.MatchString(strings.TrimSuffix(s, "\n")) }

// Schema is created by Open; the statements and their order match SCHEMA in sheepdog.py (sqlite_master keeps the exact text).
const Schema = `
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
`

// Row is one result row; values are int64, float64, string or nil.
type Row map[string]any

func rs(r Row, k string) string { s, _ := r[k].(string); return s }
func ri(r Row, k string) int64  { n, _ := r[k].(int64); return n }

// exe runs a query and returns its rows; the writer and a read transaction each have one.
type exe func(q string, args ...any) []Row

// DB is one board database: a single writer connection (Python's one connection under one lock) and a small read pool.
type DB struct {
	mu    sync.Mutex // the writer lock: every write, and every read that must see uncommitted state
	w     *sql.Conn
	wdb   *sql.DB
	rd    *sql.DB
	stmts map[string]*sql.Stmt
	rev   int64        // guarded by mu: last revision handed out
	pub   atomic.Int64 // last revision whose transaction committed: what readers may use as a cursor

	seenMu   sync.Mutex
	waitSeen map[string]time.Time // cwd -> last time a `sheepdog wait` long-poll for it was alive
	changed  chan struct{}        // closed and replaced when a decision is created: wakes the long-polls

	cacheMu sync.Mutex
	cache   map[int64]cached

	allow *Allowlist // hook allowlist; nil or empty = every folder
	scrub *Scrubber  // rewrites paths and names before anything is stored

	rstmtMu sync.Mutex
	rstmts  map[string]*sql.Stmt // prepared on the read pool; each pooled connection prepares its own copy on first use
}

type cached struct {
	rev  int64
	at   time.Time
	body []byte
}

func dsn(path string, ro bool) string {
	q := url.Values{}
	q.Add("_pragma", "busy_timeout(5000)")
	q.Add("_pragma", "cache_size(-512)") // 512 KB page cache per connection: the board is a few MB, the OS caches the file anyway
	if ro {
		q.Add("mode", "ro")
	} else {
		q.Add("_pragma", "journal_mode(WAL)")
		q.Add("_pragma", "synchronous(NORMAL)")
	}
	return "file:" + (&url.URL{Path: path}).EscapedPath() + "?" + strings.ReplaceAll(q.Encode(), "+", "%20")
}

// Open opens (and creates or migrates) the database at path.
func Open(path string) (*DB, error) {
	wdb, err := sql.Open("sqlite", dsn(path, false))
	if err != nil {
		return nil, err
	}
	wdb.SetMaxOpenConns(1)
	wdb.SetMaxIdleConns(1)
	wdb.SetConnMaxLifetime(0)
	ctx := context.Background()
	w, err := wdb.Conn(ctx)
	if err != nil {
		return nil, err
	}
	d := &DB{w: w, wdb: wdb, stmts: map[string]*sql.Stmt{}, waitSeen: map[string]time.Time{}, changed: make(chan struct{}), cache: map[int64]cached{}, rstmts: map[string]*sql.Stmt{}, scrub: ScrubberFromEnv(nil)}
	if _, err = w.ExecContext(ctx, Schema); err != nil {
		return nil, err
	}
	have := map[string]bool{}
	for _, r := range d.q("PRAGMA table_info(tasks)") {
		have[rs(r, "name")] = true
	}
	for _, c := range [][2]string{{"next", "TEXT NOT NULL DEFAULT ''"}, {"waiting", "TEXT NOT NULL DEFAULT ''"}, {"done_at", "INTEGER"},
		{"brief", "TEXT NOT NULL DEFAULT ''"}, {"brief_options", "TEXT NOT NULL DEFAULT '[]'"}, {"brief_at", "INTEGER"},
		{"brief_cwd", "TEXT NOT NULL DEFAULT ''"}, {"parent", "TEXT"}, {"task_cwd", "TEXT"}} {
		if !have[c[0]] { // old DB: add the column, backfill done_at from the last update
			d.x(fmt.Sprintf("ALTER TABLE tasks ADD COLUMN %s %s", c[0], c[1]))
			if c[0] == "done_at" {
				d.x("UPDATE tasks SET done_at=updated_at WHERE status='done'")
			}
		}
	}
	d.x("CREATE INDEX IF NOT EXISTS tasks_parent ON tasks(parent)") // after the loop: an old DB gets the column first
	for _, t := range []string{"tasks", "agents", "events"} {
		if m := ri(d.q1("SELECT COALESCE(MAX(rev),0) m FROM "+t), "m"); m > d.rev {
			d.rev = m
		}
	}
	d.pub.Store(d.rev)
	rd, err := sql.Open("sqlite", dsn(path, true))
	if err != nil {
		return nil, err
	}
	rd.SetMaxOpenConns(2)
	rd.SetMaxIdleConns(1)
	d.rd = rd
	return d, nil
}

// SetAllow sets the hook allowlist (call before serving).
func (d *DB) SetAllow(a *Allowlist) { d.allow = a }

// SetScrub sets the rewrite of paths and names (call before serving).
func (d *DB) SetScrub(s *Scrubber) { d.scrub = s }

// RewriteCwd is a folder as it is stored and compared (see Scrubber).
func (d *DB) RewriteCwd(cwd string) string { return d.scrub.Cwd(cwd) }

// Close closes the database.
func (d *DB) Close() {
	d.mu.Lock()
	defer d.mu.Unlock()
	for _, s := range d.stmts {
		s.Close()
	}
	d.rd.Close()
	d.w.Close()
	d.wdb.Close()
}

// ---------------------------------------------------------------- query plumbing (panics carry database errors up to the HTTP layer)

type dbError struct{ err error }

func (e dbError) Error() string { return "OperationalError: " + e.err.Error() }

func (d *DB) stmt(q string) *sql.Stmt {
	if s, ok := d.stmts[q]; ok {
		return s
	}
	s, err := d.w.PrepareContext(context.Background(), q)
	if err != nil {
		panic(dbError{err})
	}
	if len(d.stmts) < 512 {
		d.stmts[q] = s
	}
	return s
}

func conv(args []any) []any {
	for i, a := range args {
		if n, ok := a.(json.Number); ok {
			if v, err := n.Int64(); err == nil {
				args[i] = v
			} else if f, err := n.Float64(); err == nil {
				args[i] = f
			}
		}
	}
	return args
}

// q runs a query on the writer (the caller holds mu).
func (d *DB) q(query string, args ...any) []Row {
	s := d.stmt(query)
	rows, err := s.QueryContext(context.Background(), conv(args)...)
	if err != nil {
		panic(dbError{err})
	}
	return scan(rows)
}

func (d *DB) q1(query string, args ...any) Row {
	r := d.q(query, args...)
	if len(r) == 0 {
		return nil
	}
	return r[0]
}

// x runs a statement on the writer and returns the rows it changed.
func (d *DB) x(query string, args ...any) int64 {
	var res sql.Result
	var err error
	if strings.HasPrefix(query, "ALTER") || strings.HasPrefix(query, "CREATE") {
		res, err = d.w.ExecContext(context.Background(), query, conv(args)...)
	} else {
		res, err = d.stmt(query).ExecContext(context.Background(), conv(args)...)
	}
	if err != nil {
		panic(dbError{err})
	}
	n, _ := res.RowsAffected()
	return n
}

func scan(rows *sql.Rows) []Row {
	defer rows.Close()
	cols, _ := rows.Columns()
	var out []Row
	for rows.Next() {
		vals := make([]any, len(cols))
		ptrs := make([]any, len(cols))
		for i := range vals {
			ptrs[i] = &vals[i]
		}
		if err := rows.Scan(ptrs...); err != nil {
			panic(dbError{err})
		}
		r := make(Row, len(cols))
		for i, c := range cols {
			if b, ok := vals[i].([]byte); ok {
				vals[i] = string(b)
			}
			r[c] = vals[i]
		}
		out = append(out, r)
	}
	if err := rows.Err(); err != nil {
		panic(dbError{err})
	}
	return out
}

// tx runs fn inside BEGIN/COMMIT under the writer lock; any error or panic rolls back. The new revision is published after the commit.
func (d *DB) tx(fn func() error) (err error) {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.txL(fn)
}

func (d *DB) txL(fn func() error) (err error) {
	defer func() {
		if r := recover(); r != nil {
			func() {
				defer func() { _ = recover() }()
				d.x("ROLLBACK")
			}()
			switch e := r.(type) {
			case dbError:
				err = e
			case BadRequest:
				err = e
			default:
				panic(r)
			}
		}
	}()
	d.x("BEGIN")
	if err = fn(); err != nil {
		d.x("ROLLBACK")
		return err
	}
	d.x("COMMIT")
	d.pub.Store(d.rev)
	return nil
}

func (d *DB) bump() int64 { d.rev++; return d.rev }

func now() int64 { return time.Now().Unix() }

// ---------------------------------------------------------------- helpers

type evArgs struct {
	task, sid, cwd, project string
	agent                   *string
	data                    any // nil, []KV or anything jsonCompact takes
	ts                      int64
}

func nullStr(s string) any {
	if s == "" {
		return nil
	}
	return s
}

func (d *DB) event(kind, summary string, a evArgs) {
	ts := a.ts
	if ts == 0 {
		ts = now()
	}
	r := d.bump()
	var data []byte
	if a.data == nil {
		data = []byte("{}")
	} else if kv, ok := a.data.([]KV); ok && len(kv) == 0 {
		data = []byte("{}")
	} else {
		data = jsonCompact(a.data)
	}
	var agent any
	if a.agent != nil {
		agent = *a.agent
	}
	d.x("INSERT INTO events(ts,session_id,cwd,project,task_key,agent_id,kind,summary,data,rev) VALUES(?,?,?,?,?,?,?,?,?,?)",
		ts, a.sid, a.cwd, a.project, nullStr(a.task), agent, kind, summary, string(data), r)
	if a.task != "" {
		d.x("UPDATE tasks SET last_event_at=?, rev=? WHERE key=?", ts, r, a.task)
	}
}

func under(path, base string) bool {
	base = strings.TrimRight(base, "/")
	if strings.HasSuffix(base, ":") { // a rewritten root itself ("alpha:"): its children are "alpha:sub"
		return strings.HasPrefix(path, base)
	}
	return path == base || strings.HasPrefix(path, base+"/")
}

// link is the task key for an event: the agent's task, else the longest worktree prefix of cwd (no guessing by project name).
func (d *DB) link(cwd, agentID string) string {
	if agentID != "" {
		if a := d.q1("SELECT task_key FROM agents WHERE id=?", agentID); a != nil && rs(a, "task_key") != "" {
			return rs(a, "task_key")
		}
	}
	if cwd == "" {
		return ""
	}
	best, bestKey := "", ""
	found := false
	for _, t := range d.q("SELECT key,worktree FROM tasks WHERE archived=0") {
		w := strings.TrimRight(rs(t, "worktree"), "/")
		if w != "" && under(cwd, w) && (!found || len(w) > len(best)) {
			best, bestKey, found = w, rs(t, "key"), true
		}
	}
	return bestKey
}

// descTask is the task key from an agent description "Task <N> title"; it creates a minimal task row if it is new.
func (d *DB) descTask(desc any, ts int64) string {
	s := ""
	if truthy(desc) {
		s = pyStr(desc)
	}
	m := taskRe.FindStringSubmatchIndex(s)
	if m == nil {
		return ""
	}
	key := "T" + s[m[2]:m[3]]
	if d.q1("SELECT 1 x FROM tasks WHERE key=?", key) == nil {
		title := Clip(strings.TrimSpace(s[m[1]:]), 200)
		if title == "" {
			title = key
		}
		d.x("INSERT INTO tasks(key,title,status,notes,created_at,updated_at,rev) VALUES(?,?,?,?,?,?,?)", key, title, "building", "[]", ts, ts, d.bump())
	}
	return key
}

func (d *DB) projectOf(task, cwd string) string {
	var t Row
	if task != "" {
		t = d.q1("SELECT project FROM tasks WHERE key=?", task)
	}
	if t == nil && cwd == "" {
		return ""
	}
	if t != nil && rs(t, "project") != "" {
		return rs(t, "project")
	}
	return baseOf(cwd)
}

// baseOf is the project name for a stored folder: its last component ("alpha:" is the root itself, named alpha).
func baseOf(cwd string) string {
	c := strings.TrimRight(cwd, "/")
	if strings.HasSuffix(c, ":") {
		c = c[:len(c)-1]
		return c[strings.LastIndexByte(c, '/')+1:]
	}
	return c[strings.LastIndexAny(c, "/:")+1:]
}

// ---------------------------------------------------------------- tasks

// Note is one timeline note on a task.
type Note struct {
	TS   json.Number `json:"ts"`
	Text string      `json:"text"`
}

// Option is one button of a review card.
type Option struct {
	ID      any    `json:"id"`
	Label   string `json:"label"`
	Hint    string `json:"hint"`
	Primary bool   `json:"primary"`
}

func notesJSON(n []Note) string {
	b := []byte{'['}
	for i, x := range n {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = jenc(b, []KV{{"ts", x.TS}, {"text", x.Text}}, ", ", ": ")
	}
	return string(append(b, ']'))
}

func optionsJSON(o []Option) string {
	b := []byte{'['}
	for i, x := range o {
		if i > 0 {
			b = append(b, ", "...)
		}
		b = jenc(b, []KV{{"id", x.ID}, {"label", x.Label}, {"hint", x.Hint}, {"primary", x.Primary}}, ", ", ": ")
	}
	return string(append(b, ']'))
}

func parseNotes(s string) []Note {
	n := []Note{}
	if s != "" {
		_ = json.Unmarshal([]byte(s), &n)
	}
	if n == nil {
		n = []Note{}
	}
	return n
}

func parseOptions(s string) []Option {
	o := []Option{}
	if s != "" {
		_ = json.Unmarshal([]byte(s), &o)
	}
	if o == nil {
		o = []Option{}
	}
	return o
}

func isNull(v any) bool { return v == nil }

// taskOut turns a tasks row into what the API sends: parsed notes and options, the latest decision, sub-task roll-up.
func taskOut(ex exe, t Row) Row {
	delete(t, "task_cwd") // delivery plumbing, not part of the task the API shows
	t["notes"] = parseNotes(rs(t, "notes"))
	t["brief_options"] = parseOptions(rs(t, "brief_options"))
	if dec := ex("SELECT id,option_id,label,text,status,created_at,delivered_at,done_at FROM decisions WHERE task_key=? ORDER BY id DESC LIMIT 1", t["key"]); len(dec) > 0 {
		t["decision"] = dec[0]
	} else {
		t["decision"] = nil
	}
	status := rs(t, "status")
	// A done task never needs you, whatever "waiting" was left at.
	if status == "done" {
		if t["done_at"] != nil && ri(t, "done_at") != 0 {
			t["end_at"] = t["done_at"]
		} else {
			t["end_at"] = t["updated_at"]
		}
	} else {
		t["end_at"] = nil
	}
	t["needs_you"] = status != "done" && (rs(t, "waiting") == "me" || (rs(t, "waiting") == "" && (status == "review" || status == "testing")))
	parent, hasParent := t["parent"].(string)
	hasParent = hasParent && parent != ""
	if hasParent {
		if pt := ex("SELECT title FROM tasks WHERE key=?", parent); len(pt) > 0 {
			t["parent_title"] = pt[0]["title"]
		} else {
			t["parent_title"] = ""
		}
	} else if t["parent"] != nil { // an empty-string parent
		t["parent_title"] = ""
	} else {
		t["parent_title"] = nil
	}
	kids := ex("SELECT key,status,waiting,created_at,done_at,updated_at FROM tasks WHERE parent=? AND archived=0 ORDER BY created_at,key", t["key"])
	children := make([]string, len(kids))
	for i, k := range kids {
		children[i] = rs(k, "key")
	}
	t["children"], t["progress"] = children, nil
	if len(kids) > 0 {
		rollup(t, kids)
	}
	return t
}

// rollup: a parent has no status of its own; it is derived from its sub-tasks. It needs you when any child does.
func rollup(t Row, kids []Row) {
	by := map[string]int{}
	for _, k := range kids {
		by[rs(k, "status")]++
	}
	has := func(s string) bool { return by[s] > 0 }
	switch {
	case by["done"] == len(kids):
		t["status"] = "done"
	case has("blocked"):
		t["status"] = "blocked"
	case has("review") || has("testing"):
		t["status"] = "review"
	case has("building") || has("analysing"):
		t["status"] = "building"
	default:
		t["status"] = "planned"
	}
	known := map[string]int{}
	for _, s := range Statuses {
		if by[s] > 0 {
			known[s] = by[s]
		}
	}
	t["progress"] = Row{"total": len(kids), "by_status": known, "done": by["done"]}
	needs := false
	var earliest int64
	var maxEnd int64
	for _, k := range kids {
		w := rs(k, "waiting")
		if rs(k, "status") != "done" && (w == "me" || (w == "" && (rs(k, "status") == "review" || rs(k, "status") == "testing"))) {
			needs = true
		}
		if c := ri(k, "created_at"); c != 0 && (earliest == 0 || c < earliest) {
			earliest = c
		}
		e := ri(k, "done_at")
		if e == 0 {
			e = ri(k, "updated_at")
		}
		if e > maxEnd {
			maxEnd = e
		}
	}
	t["needs_you"] = needs
	if earliest != 0 {
		t["created_at"] = earliest // started = the earliest child
	}
	if t["status"] == "done" {
		t["end_at"] = maxEnd // total runs until the last child is done
	} else {
		t["end_at"] = nil
	}
}

func (d *DB) getTask(key any) Row {
	t := d.q1("SELECT * FROM tasks WHERE key=?", key)
	if t == nil {
		return nil
	}
	return taskOut(d.q, t)
}

// GetTask is the task as the API sends it, or nil.
func (d *DB) GetTask(key string) Row {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.getTask(key)
}

type fv struct {
	k string
	v any
}

func setSQL(table string, f []fv, where string) (string, []any) {
	var sb strings.Builder
	args := make([]any, 0, len(f)+1)
	sb.WriteString("UPDATE " + table + " SET ")
	for i, x := range f {
		if i > 0 {
			sb.WriteByte(',')
		}
		sb.WriteString(x.k + "=?")
		args = append(args, x.v)
	}
	sb.WriteString(" WHERE " + where)
	return sb.String(), args
}

func insertSQL(table string, f []fv) (string, []any) {
	cols := make([]string, len(f))
	qs := make([]string, len(f))
	args := make([]any, len(f))
	for i, x := range f {
		cols[i], qs[i], args[i] = x.k, "?", x.v
	}
	return "INSERT INTO " + table + "(" + strings.Join(cols, ",") + ") VALUES(" + strings.Join(qs, ",") + ")", args
}

func fset(f []fv, k string, v any) []fv {
	for i := range f {
		if f[i].k == k {
			f[i].v = v
			return f
		}
	}
	return append(f, fv{k, v})
}

func fget(f []fv, k string) (any, bool) {
	for _, x := range f {
		if x.k == k {
			return x.v, true
		}
	}
	return nil, false
}

func contains(list []string, v any) bool {
	s, ok := v.(string)
	if !ok {
		return false
	}
	for _, x := range list {
		if x == s {
			return true
		}
	}
	return false
}

func isSet(d map[string]any, k string) bool { v, ok := d[k]; return ok && v != nil }

// TaskUpsert creates or updates a task (POST /api/task).
func (d *DB) TaskUpsert(in map[string]any) (out Row, err error) {
	key := ""
	if truthy(in["key"]) {
		key = pyStr(in["key"])
	}
	if !pyMatch(keyRe, key) {
		return nil, bad("bad task key (use letters, digits, _ . -)")
	}
	var f []fv
	for _, k := range []string{"title", "project", "branch", "worktree", "mr_url", "next"} {
		if !isSet(in, k) {
			continue
		}
		if k == "worktree" { // checked raw, then rewritten and cut below
			f = append(f, fv{k, pyStr(in[k])})
		} else { // nothing that names the person or the machine is stored: rewrite first, cut after, so a path is never cut where the rules cannot see it
			f = append(f, fv{k, head(d.scrub.Text(pyStr(in[k])), 500)})
		}
	}
	if v, ok := fget(f, "next"); ok {
		f = fset(f, "next", Redact(v.(string)))
	}
	if v := pyStr(in["cwd"]); truthy(in["cwd"]) && strings.HasPrefix(v, "/") { // where the caller works: never overwritten by an empty or relative value
		f = append(f, fv{"task_cwd", head(d.scrub.Cwd(v), 500)})
	}
	if isSet(in, "waiting") {
		if !contains(append(append([]string{}, Waiting...), ""), in["waiting"]) {
			return nil, bad("waiting must be one of " + strings.Join(Waiting, ", "))
		}
		f = append(f, fv{"waiting", in["waiting"]})
	}
	if v, _ := fget(f, "worktree"); v != nil && v.(string) != "" && !strings.HasPrefix(v.(string), "/") {
		return nil, bad("worktree must be an absolute path")
	}
	if v, _ := fget(f, "mr_url"); v != nil && v.(string) != "" && !httpRe.MatchString(v.(string)) {
		return nil, bad("mr_url must be http(s)")
	}
	if v, _ := fget(f, "worktree"); v != nil && v.(string) != "" {
		f = fset(f, "worktree", head(d.scrub.Cwd(v.(string)), 500))
	}
	var status any = in["status"]
	if status == "running" {
		status = "building" // alias
	}
	if status != nil && !contains(Statuses, status) {
		return nil, bad("status must be one of " + strings.Join(Statuses, ", "))
	}
	note := in["note"]
	ts := now()
	err = d.tx(func() error {
		row := d.getTask(key)
		var newp, oldp string
		var haveNew bool
		if isSet(in, "parent") { // "" clears it
			haveNew = true
			newp = strings.TrimSpace(pyStr(in["parent"]))
			if row != nil && row["parent"] != nil {
				oldp = rs(row, "parent")
			}
			if newp != "" {
				prow := d.q1("SELECT parent FROM tasks WHERE key=? AND archived=0", newp)
				if newp == key {
					return bad("a task cannot be its own parent")
				}
				if prow == nil {
					return bad("no such parent task " + newp)
				}
				if rs(prow, "parent") != "" {
					return bad(fmt.Sprintf("one level only: %s is itself a sub-task", newp))
				}
				if d.q1("SELECT 1 x FROM tasks WHERE parent=? AND archived=0 LIMIT 1", key) != nil {
					return bad("a task with sub-tasks cannot become a sub-task")
				}
				f = fset(f, "parent", newp)
			} else {
				f = fset(f, "parent", nil)
			}
		}
		r := d.bump()
		if row == nil {
			if _, ok := fget(f, "title"); !ok {
				f = append(f, fv{"title", key})
			}
			st := "planned"
			if truthy(status) {
				st = status.(string)
			}
			var doneAt any
			if status == "done" {
				doneAt = ts
			}
			f = fset(f, "status", st)
			f = fset(f, "done_at", doneAt)
			f = fset(f, "notes", "[]")
			f = fset(f, "created_at", ts)
			f = fset(f, "updated_at", ts)
			f = fset(f, "archived", 0)
			f = fset(f, "rev", r)
			f = fset(f, "key", key)
			q, args := insertSQL("tasks", f)
			d.x(q, args...)
			title, _ := fget(f, "title")
			d.event("status", fmt.Sprintf("%s created (%s): %s", key, st, title), evArgs{task: key, ts: ts})
		} else {
			if st, _ := status.(string); st != "" && st != rs(row, "status") {
				f = fset(f, "status", st)
				if st == "done" {
					f = fset(f, "done_at", ts)
				} else {
					f = fset(f, "done_at", nil)
				}
				d.event("status", fmt.Sprintf("%s: %s -> %s", key, rs(row, "status"), st), evArgs{task: key, ts: ts})
			}
			if len(f) > 0 {
				f = fset(f, "updated_at", ts)
				f = fset(f, "rev", r)
				q, args := setSQL("tasks", f, "key=?")
				d.x(q, append(args, key)...)
			}
		}
		if haveNew {
			seen := map[string]bool{"": true}
			for _, pk := range []string{newp, oldp} { // both parents re-send their roll-up
				if !seen[pk] {
					seen[pk] = true
					d.x("UPDATE tasks SET rev=? WHERE key=?", d.bump(), pk)
				}
			}
		}
		if truthy(note) {
			d.addNote(key, pyStr(note), ts)
		}
		out = d.getTask(key) // read inside the transaction: a read outside it costs its own lock round trip
		return nil
	})
	return out, err
}

func (d *DB) addNote(key, text string, ts int64) {
	text = head(Redact(d.scrub.Text(text)), TextMax)
	notes := d.getTask(key)["notes"].([]Note)
	notes = append(notes, Note{json.Number(fmt.Sprint(ts)), text})
	d.x("UPDATE tasks SET notes=?, updated_at=?, rev=? WHERE key=?", notesJSON(notes), ts, d.bump(), key)
	d.event("note", Clip(text, 200), evArgs{task: key, ts: ts})
}

var errNoTask = NotFound("no such task")

// NotFound is an error the HTTP layer answers with 404.
type NotFound string

func (e NotFound) Error() string { return string(e) }

// SetBrief stores a review card: markdown plus up to 5 option buttons, and hands the task to the developer.
func (d *DB) SetBrief(key string, in map[string]any) (Row, error) {
	var out Row
	err := d.tx(func() error {
		if d.getTask(key) == nil {
			return errNoTask
		}
		brief := ""
		if truthy(in["brief"]) {
			brief = pyStr(in["brief"])
		}
		brief = head(strings.TrimSpace(Redact(d.scrub.Text(brief))), TextMax)
		if brief == "" {
			return bad("brief required")
		}
		var opts []any
		if truthy(in["options"]) {
			l, ok := in["options"].([]any)
			if !ok || len(l) > 5 {
				return bad("options must be a list of at most 5")
			}
			opts = l
		}
		clean := []Option{}
		ids := []string{}
		for _, o := range opts {
			m, ok := o.(map[string]any)
			id := ""
			label := ""
			if ok {
				if truthy(m["id"]) {
					id = pyStr(m["id"])
				}
				if truthy(m["label"]) {
					label = pyStr(m["label"])
				}
			}
			if !ok || !pyMatch(optIDRe, id) || strings.TrimSpace(label) == "" {
				return bad("each option needs an id ([a-z0-9-], max 32) and a label")
			}
			for _, c := range clean {
				if c.ID == m["id"] {
					return bad("duplicate option id " + pyStr(m["id"]))
				}
			}
			hint := ""
			if truthy(m["hint"]) {
				hint = pyStr(m["hint"])
			}
			clean = append(clean, Option{m["id"], head(Redact(d.scrub.Text(strings.TrimSpace(label))), 80), head(Redact(d.scrub.Text(strings.TrimSpace(hint))), 200), truthy(m["primary"])})
			ids = append(ids, id)
		}
		cwd := ""
		if truthy(in["cwd"]) {
			cwd = pyStr(in["cwd"])
		}
		if cwd != "" && !strings.HasPrefix(cwd, "/") {
			return bad("cwd must be an absolute path")
		}
		cwd = head(d.scrub.Cwd(cwd), 500) // stored in the same form a hook's folder is compared in
		ts := now()
		// a review card means the work is waiting for review: the column and the next step follow (a done task keeps both)
		d.x("UPDATE tasks SET brief=?, brief_options=?, brief_at=?, brief_cwd=?, waiting='me', updated_at=?, rev=?,"+
			" status=CASE WHEN status='done' THEN status ELSE 'review' END,"+
			" next=CASE WHEN status='done' THEN next ELSE 'Review card ready' END WHERE key=?",
			brief, optionsJSON(clean), ts, cwd, ts, d.bump(), key)
		first := strings.SplitN(strings.TrimLeft(brief, "# "), "\n", 2)[0]
		d.event("brief", "review ready: "+Clip(first, 100), evArgs{task: key, ts: ts, data: []KV{{"options", ids}}})
		out = d.getTask(key)
		return nil
	})
	return out, err
}

// Decide records the developer's answer to a needs-you task: a timeline note, the ball goes back to the agent, and (when the task has
// a review brief with a cwd, or a worktree; a reply needs neither a brief) a pending decision row for the Claude session that owns it. Runs nothing.
func (d *DB) Decide(key string, in map[string]any) (Row, error) {
	var out Row
	var created bool
	err := d.tx(func() error {
		t := d.getTask(key)
		if t == nil {
			return errNoTask
		}
		act := in["action"]
		text := ""
		if truthy(in["text"]) {
			text = pyStr(in["text"])
		}
		text = Redact(d.scrub.Text(strings.TrimSpace(text)))
		if n := runeLen(text); n > TextMax {
			return bad(fmt.Sprintf("Too long: %d / %d characters", n, TextMax))
		}
		if act != "approve" && act != "reply" && act != "option" {
			return bad("action must be approve, reply or option")
		}
		if act == "reply" && text == "" {
			return bad("reply needs text")
		}
		if act != "reply" && rs(t, "brief") == "" {
			return bad("brief required") // options and approve answer a review card; only a reply works without one
		}
		ts := now()
		oid, label, note := "", "", ""
		switch act {
		case "option":
			var opt *Option
			for _, o := range t["brief_options"].([]Option) {
				if o.ID == in["option"] {
					o := o
					opt = &o
					break
				}
			}
			if opt == nil {
				return bad("unknown option")
			}
			oid, label = pyStr(opt.ID), opt.Label
			note = "Decision: " + opt.Label
		case "reply":
			label, note = "Reply", "Reply: "+text
		default:
			note = "Decision: approve and merge"
		}
		what := "approve and merge"
		if act == "option" {
			what = label
		} else if act == "reply" {
			what = "your reply"
		}
		// the ball goes back to the agent, so the column follows the work (a done task keeps its status and next)
		notes := append(t["notes"].([]Note), Note{json.Number(fmt.Sprint(ts)), note})
		d.x("UPDATE tasks SET notes=?, waiting='agent', updated_at=?, rev=?,"+
			" status=CASE WHEN status='done' THEN status ELSE 'building' END,"+
			" next=CASE WHEN status='done' THEN next ELSE ? END WHERE key=?",
			notesJSON(notes), ts, d.bump(), "Acting on: "+what, key)
		cwd := decisionCwd(rs(t, "brief_cwd"), rs(t, "worktree"), rs(d.q1("SELECT task_cwd FROM tasks WHERE key=?", key), "task_cwd"))
		if act != "approve" && cwd != "" {
			txt := ""
			if act == "reply" {
				txt = text
			}
			d.x("INSERT INTO decisions(task_key,option_id,label,text,cwd,created_at,rev) VALUES(?,?,?,?,?,?,?)", key, oid, label, txt, cwd, ts, d.bump())
			created = true
		}
		data := []KV{{"action", act}}
		if oid != "" {
			data = append(data, KV{"option", oid})
		}
		d.event("decision", Clip(note, 200), evArgs{task: key, ts: ts, data: data})
		out = d.getTask(key)
		return nil
	})
	if err != nil {
		return nil, err
	}
	out["waiter"] = d.WaiterFor(decisionCwd(rs(out, "brief_cwd"), rs(out, "worktree"), rs(d.q1("SELECT task_cwd FROM tasks WHERE key=?", key), "task_cwd")))
	if created {
		d.wake()
	}
	return out, nil
}

// decisionCwd is the folder a decision is delivered to: the review card's, else the task's worktree, else where `sheepdog task` last ran.
func decisionCwd(brief, worktree, task string) string { return cmp.Or(brief, worktree, task) }

func (d *DB) wake() {
	d.seenMu.Lock()
	close(d.changed)
	d.changed = make(chan struct{})
	d.seenMu.Unlock()
}

// Changed returns a channel that is closed the next time a decision is created.
func (d *DB) Changed() <-chan struct{} {
	d.seenMu.Lock()
	defer d.seenMu.Unlock()
	return d.changed
}

// Seen records that a `wait` long-poll for cwd is alive.
func (d *DB) Seen(cwd string) {
	d.seenMu.Lock()
	d.waitSeen[cwd] = time.Now()
	d.seenMu.Unlock()
}

// WaiterFor says whether a `sheepdog wait` for this folder is alive (a long-poll runs up to 55 s; 70 s of silence = gone).
func (d *DB) WaiterFor(cwd string) bool {
	if cwd == "" {
		return false
	}
	d.seenMu.Lock()
	defer d.seenMu.Unlock()
	for w, ts := range d.waitSeen {
		if time.Since(ts) < 70*time.Second && under(cwd, w) {
			return true
		}
	}
	return false
}

// Claim marks pending decisions for this folder delivered, each exactly once (one transaction, status guard in the UPDATE).
func (d *DB) Claim(cwd, to string, hook bool) []Row {
	out := []Row{}
	_ = d.tx(func() error {
		ts := now()
		for _, dec := range d.q("SELECT * FROM decisions WHERE status='pending' ORDER BY id") {
			dc := rs(dec, "cwd")
			if hook && !under(cwd, dc) || !hook && !under(dc, cwd) { // hook: prompt folder is the project or inside it; waiter: decision is inside the waiter's folder
				continue
			}
			if d.x("UPDATE decisions SET status='delivered', delivered_at=?, delivered_to=?, rev=? WHERE id=? AND status='pending'", ts, to, d.bump(), dec["id"]) > 0 {
				d.x("UPDATE tasks SET rev=? WHERE key=?", d.bump(), dec["task_key"])
				dec["status"], dec["delivered_at"], dec["delivered_to"] = "delivered", ts, to
				out = append(out, dec)
			}
		}
		return nil
	})
	return out
}

// FinishDecision acknowledges a decision; nil when there is no such decision.
func (d *DB) FinishDecision(id int64) (Row, error) {
	var out Row
	err := d.tx(func() error {
		dec := d.q1("SELECT task_key FROM decisions WHERE id=?", id)
		if dec == nil {
			return nil
		}
		d.x("UPDATE decisions SET status='done', done_at=?, rev=? WHERE id=?", now(), d.bump(), id)
		d.x("UPDATE tasks SET rev=? WHERE key=?", d.bump(), dec["task_key"])
		out = d.q1("SELECT * FROM decisions WHERE id=?", id)
		return nil
	})
	return out, err
}

// Archive hides a task from the board.
func (d *DB) Archive(key string) (Row, error) {
	var out Row
	err := d.tx(func() error {
		if d.getTask(key) == nil {
			return bad("no such task")
		}
		d.x("UPDATE tasks SET archived=1, updated_at=?, rev=? WHERE key=?", now(), d.bump(), key)
		d.event("status", key+" archived", evArgs{task: key})
		out = d.getTask(key)
		return nil
	})
	return out, err
}

// ---------------------------------------------------------------- agents

// AgentUpsert creates or updates an agent row (POST /api/agent).
func (d *DB) AgentUpsert(in map[string]any) (Row, error) {
	aid := ""
	if truthy(in["id"]) {
		aid = head(pyStr(in["id"]), 100)
	}
	if aid == "" {
		return nil, bad("agent id required")
	}
	st := in["status"]
	if st != nil && !contains(AgentStatuses, st) {
		return nil, bad("status must be one of " + strings.Join(AgentStatuses, ", "))
	}
	var task any = in["task"]
	if !truthy(task) {
		task = in["task_key"]
	}
	var out Row
	err := d.tx(func() error {
		if truthy(task) && d.getTask(task) == nil {
			return bad("no such task " + pyStr(task))
		}
		ts := now()
		d.agentSet(aid, ts, []fv{{"task_key", task}, {"name", in["name"]}, {"model", in["model"]}, {"status", st}, {"tokens", in["tokens"]}})
		a := d.q1("SELECT * FROM agents WHERE id=?", aid)
		label := ""
		if rs(a, "name") != "" {
			label = " (" + taskRe.ReplaceAllString(rs(a, "name"), "") + ")"
		}
		ag := aid
		d.event("status", fmt.Sprintf("agent %s %s%s", head(aid, 12), rs(a, "status"), label), evArgs{task: rs(a, "task_key"), agent: &ag, ts: ts})
		out = a
		return nil
	})
	return out, err
}

// agentSet creates or updates an agent row; only non-nil fields are written.
func (d *DB) agentSet(aid string, ts int64, in []fv) {
	var f []fv
	for _, x := range in {
		if x.v != nil {
			f = append(f, x)
		}
	}
	for i, x := range f {
		if x.k == "name" || x.k == "model" || x.k == "type" {
			f[i].v = head(d.scrub.Text(pyStr(x.v)), 200)
		}
	}
	if st, _ := fget(f, "status"); st == "done" || st == "failed" {
		f = fset(f, "ended_at", ts)
	} else if st == "running" {
		f = fset(f, "ended_at", nil)
	}
	f = fset(f, "last_event_at", ts)
	f = fset(f, "rev", d.bump())
	if d.q1("SELECT 1 x FROM agents WHERE id=?", aid) != nil {
		q, args := setSQL("agents", f, "id=?")
		d.x(q, append(args, aid)...)
	} else {
		f = fset(f, "id", aid)
		f = fset(f, "started_at", ts)
		if _, ok := fget(f, "status"); !ok {
			f = append(f, fv{"status", "running"})
		}
		q, args := insertSQL("agents", f)
		d.x(q, args...)
	}
}

// ---------------------------------------------------------------- prune

// Prune drops events older than days and keeps at most cap of them.
func (d *DB) Prune(days int64, cap int64) error {
	return d.tx(func() error {
		d.x("DELETE FROM events WHERE ts<?", now()-days*86400)
		if mx := ri(d.q1("SELECT MAX(id) m FROM events"), "m"); mx != 0 {
			d.x("DELETE FROM events WHERE id<=?", mx-cap)
		}
		return nil
	})
}

// ---------------------------------------------------------------- helpers for tests and the fixture

// Q runs a read on the writer connection (for tests).
func (d *DB) Q(query string, args ...any) []Row {
	d.mu.Lock()
	defer d.mu.Unlock()
	return d.q(query, args...)
}

// X runs a statement on the writer connection (for tests).
func (d *DB) X(query string, args ...any) { d.mu.Lock(); defer d.mu.Unlock(); d.x(query, args...) }

var _ = os.Getenv
