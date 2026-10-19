package store

// ScrubCount is how many rows of a table changed (or would change in a dry run).
type ScrubCount struct {
	Table string
	Rows  int
}

// scrubCols lists, per table, its key column, the free-text columns, the folder columns, and the JSON text columns (rewritten as text).
var scrubCols = []struct {
	table, key string
	text       []string
	cwd        []string
}{
	{"tasks", "key", []string{"title", "project", "branch", "mr_url", "next", "brief", "notes", "brief_options"}, []string{"worktree", "brief_cwd", "task_cwd"}},
	{"agents", "id", []string{"name", "model", "type"}, nil},
	{"events", "id", []string{"project", "summary", "data"}, []string{"cwd"}},
	{"decisions", "id", []string{"label", "text"}, []string{"cwd"}},
}

// ScrubRows applies the Scrubber to every stored string that can carry a path or a name (rows written before the rewrite existed).
// One transaction; it is idempotent. Stop the server first: a running server keeps its state cache and would not see the change.
func (d *DB) ScrubRows(s *Scrubber, dry bool) ([]ScrubCount, error) {
	var out []ScrubCount
	err := d.tx(func() error {
		for _, t := range scrubCols {
			cols := append(append([]string{}, t.text...), t.cwd...)
			sel := "SELECT " + t.key
			for _, c := range cols {
				sel += ", " + c
			}
			n := 0
			for _, r := range d.q(sel + " FROM " + t.table) {
				var sets []fv
				for _, c := range t.text {
					if v, ok := r[c].(string); ok {
						if nv := s.Text(v); nv != v {
							sets = append(sets, fv{c, nv})
						}
					}
				}
				for _, c := range t.cwd {
					if v, ok := r[c].(string); ok && v != "" {
						if nv := s.Cwd(v); nv != v {
							sets = append(sets, fv{c, nv})
						}
					}
				}
				if len(sets) == 0 {
					continue
				}
				n++
				if !dry {
					q, args := setSQL(t.table, sets, t.key+"=?")
					d.x(q, append(args, r[t.key])...)
				}
			}
			out = append(out, ScrubCount{t.table, n})
		}
		return nil
	})
	return out, err
}
