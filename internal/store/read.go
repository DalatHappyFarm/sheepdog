package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// readTx runs fn inside one read transaction on the read pool: a consistent snapshot that never waits for the writer.
func (d *DB) readTx(fn func(ex exe)) (err error) {
	defer func() {
		if r := recover(); r != nil {
			if de, ok := r.(dbError); ok {
				err = de
				return
			}
			panic(r)
		}
	}()
	ctx := context.Background()
	tx, err := d.rd.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return err
	}
	defer tx.Rollback()
	fn(func(q string, args ...any) []Row {
		var rows *sql.Rows
		var err error
		if st := d.rstmt(q); st != nil {
			rows, err = tx.StmtContext(ctx, st).QueryContext(ctx, conv(args)...)
		} else { // a query whose text varies: prepared per call by the driver
			rows, err = tx.QueryContext(ctx, q, conv(args)...)
		}
		if err != nil {
			panic(dbError{err})
		}
		return scan(rows)
	})
	return nil
}

// rstmt is the cached prepared statement for q on the read pool, or nil for queries whose text varies (IN lists, built SQL).
func (d *DB) rstmt(q string) *sql.Stmt {
	if strings.Contains(q, " IN (") || strings.Contains(q, "done_at>=") || strings.HasPrefix(q, "PRAGMA") {
		return nil
	}
	d.rstmtMu.Lock()
	defer d.rstmtMu.Unlock()
	if s, ok := d.rstmts[q]; ok {
		return s
	}
	s, err := d.rd.PrepareContext(context.Background(), q)
	if err != nil {
		panic(dbError{err})
	}
	d.rstmts[q] = s
	return s
}

const stateTTL = 2 * time.Second // the stale and failed-in-24h parts depend on the clock, so a cached state is never older than this

// State is the JSON for GET /api/state?since=: tasks, agents and events changed since that revision. The serialized bytes are cached
// per (since, revision) and rebuilt after any write or after stateTTL.
func (d *DB) State(since int64) ([]byte, error) {
	if since < 0 {
		since = 0
	}
	cur := d.pub.Load()
	d.cacheMu.Lock()
	defer d.cacheMu.Unlock()
	if c, ok := d.cache[since]; ok && c.rev == cur && time.Since(c.at) < stateTTL {
		return c.body, nil
	}
	var body []byte
	err := d.readTx(func(ex exe) {
		var tasks, agents, events []Row
		if since <= 0 {
			tasks = ex("SELECT * FROM tasks WHERE archived=0")
			agents = ex("SELECT a.* FROM agents a LEFT JOIN tasks t ON t.key=a.task_key WHERE t.archived=0 OR (a.task_key IS NULL AND a.last_event_at>?)", now()-2*86400)
			events = ex("SELECT * FROM events ORDER BY id DESC LIMIT 100")
		} else {
			tasks = ex("SELECT * FROM tasks WHERE rev>?", since)
			have, par := map[string]bool{}, map[string]bool{}
			for _, t := range tasks {
				have[rs(t, "key")] = true
				if p := rs(t, "parent"); p != "" {
					par[p] = true
				}
			}
			var more []Row
			if len(have) > 0 { // a changed child changes its parent's roll-up; a changed parent changes its children's chip
				keys := make([]any, 0, len(have))
				for k := range have {
					keys = append(keys, k)
				}
				more = append(more, ex("SELECT * FROM tasks WHERE parent IN ("+placeholders(len(keys))+")", keys...)...)
			}
			if len(par) > 0 {
				keys := make([]any, 0, len(par))
				for k := range par {
					keys = append(keys, k)
				}
				more = append(more, ex("SELECT * FROM tasks WHERE key IN ("+placeholders(len(keys))+")", keys...)...)
			}
			for _, t := range more {
				if k := rs(t, "key"); !have[k] {
					have[k] = true
					tasks = append(tasks, t)
				}
			}
			agents = ex("SELECT * FROM agents WHERE rev>?", since)
			events = ex("SELECT * FROM events WHERE rev>? ORDER BY id DESC LIMIT 200", since)
		}
		stale := now() - StaleSecs
		for _, a := range agents { // read-time only: running with no event for 2 h is shown as stale
			if rs(a, "status") == "running" {
				last := ri(a, "last_event_at")
				if last == 0 {
					last = ri(a, "started_at")
				}
				if last < stale {
					a["status"] = "stale"
				}
			}
		}
		// failed commands (ordinary tools only) in the last 24 h per task; "" = unlinked. Sent whole every poll: tiny, and the window slides without any rev bump.
		failed := map[string]int64{}
		for _, r := range ex("SELECT COALESCE(task_key,'') k, COUNT(*) n FROM events WHERE kind='PostToolUseFailure' AND ts>? AND data LIKE '%\"tool\"%' GROUP BY k", now()-86400) {
			failed[rs(r, "k")] = ri(r, "n")
		}
		outs := make([]Row, len(tasks))
		for i, t := range tasks {
			outs[i] = taskOut(ex, t)
		}
		if agents == nil {
			agents = []Row{}
		}
		if events == nil {
			events = []Row{}
		}
		b, jerr := json.Marshal(struct {
			Cursor int64            `json:"cursor"`
			Tasks  []Row            `json:"tasks"`
			Agents []Row            `json:"agents"`
			Events []Row            `json:"events"`
			Failed map[string]int64 `json:"failed"`
		}{cur, outs, agents, events, failed})
		if jerr != nil {
			panic(dbError{jerr})
		}
		body = b
	})
	if err != nil {
		return nil, err
	}
	if len(d.cache) > 16 {
		d.cache = map[int64]cached{}
	}
	d.cache[since] = cached{cur, time.Now(), body}
	return body, nil
}

func placeholders(n int) string { return strings.TrimSuffix(strings.Repeat("?,", n), ",") }

// TaskEvents is one task's timeline events (notes and decisions live in the task itself).
func (d *DB) TaskEvents(key string, limit int) ([]Row, error) {
	var out []Row
	err := d.readTx(func(ex exe) {
		out = ex("SELECT * FROM events WHERE task_key=? AND kind NOT IN ('note','decision') ORDER BY id DESC LIMIT ?", key, limit)
	})
	if out == nil {
		out = []Row{}
	}
	return out, err
}

// TableColumns lists the tables of the open database (without sqlite_*) with their column names, for the architecture page.
func (d *DB) TableColumns() (names []string, cols map[string][]string, err error) {
	cols = map[string][]string{}
	err = d.readTx(func(ex exe) {
		for _, r := range ex("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name") {
			n := rs(r, "name")
			names = append(names, n)
			for _, c := range ex("PRAGMA table_info(" + n + ")") {
				cols[n] = append(cols[n], rs(c, "name"))
			}
		}
	})
	return
}

// Standup is the plain-text standup; off = seconds east of UTC for the day boundaries (nil: server local time).
func (d *DB) Standup(nowTS int64, off *int64) (string, error) {
	if nowTS == 0 {
		nowTS = now()
	}
	var o int64
	if off == nil {
		_, z := time.Unix(nowTS, 0).Zone()
		o = int64(z)
	} else {
		o = *off
	}
	day := floorDiv(nowTS+o, 86400)
	p := day - 1
	for ((p+3)%7+7)%7 >= 5 { // epoch day 0 was a Thursday; skip Sat/Sun
		p--
	}
	span := func(a, b int64) string {
		return fmt.Sprintf("SELECT * FROM tasks WHERE status='done' AND done_at>=%d AND done_at<%d ORDER BY done_at", a*86400-o, b*86400-o)
	}
	ref := func(t Row) string {
		s := rs(t, "key") + " " + rs(t, "title")
		if m := mrRe.FindStringSubmatch(rs(t, "mr_url")); m != nil {
			s += " (!" + m[1] + ")"
		}
		return s
	}
	var out string
	err := d.readTx(func(ex exe) {
		live := map[string]bool{}
		for _, r := range ex("SELECT task_key FROM agents WHERE status='running' AND COALESCE(last_event_at,started_at)>=?", nowTS-StaleSecs) {
			live[rs(r, "task_key")] = true
		}
		var yesterday, today, blocked []string
		for _, t := range ex(span(p, p+1)) {
			yesterday = append(yesterday, ref(t))
		}
		for _, t := range ex(span(day, day+1)) {
			today = append(today, ref(t))
		}
		for _, t := range ex("SELECT * FROM tasks WHERE archived=0 AND status!='done'") {
			switch st := rs(t, "status"); {
			case st == "analysing" || st == "building" || st == "review" || st == "testing" || live[rs(t, "key")]:
				today = append(today, ref(t))
			}
		}
		for _, t := range ex("SELECT * FROM tasks WHERE archived=0 AND status='blocked'") {
			blocked = append(blocked, ref(t))
		}
		line := func(name string, xs []string) string {
			if len(xs) == 0 {
				return name + ": None"
			}
			return name + ": " + strings.Join(xs, "; ")
		}
		out = strings.Join([]string{line("Yesterday", yesterday), line("Today", today), line("Blockers", blocked)}, "\n")
	})
	return out, err
}

func floorDiv(a, b int64) int64 {
	q := a / b
	if a%b != 0 && (a < 0) != (b < 0) {
		q--
	}
	return q
}
