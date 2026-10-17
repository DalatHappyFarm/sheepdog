package store

import (
	"sort"
	"strings"
)

// PurgeRow is the count for one project (the base name of the folder, for display only).
type PurgeRow struct {
	Project string
	Events  int
	Agents  int
}

// PurgeReport says what a purge deleted, or would delete in a dry run.
type PurgeReport struct {
	Rows   []PurgeRow
	Events int
	Agents int
}

func baseName(cwd string) string {
	c := strings.TrimRight(cwd, "/")
	if b := c[strings.LastIndexByte(c, '/')+1:]; b != "" {
		return b
	}
	return "(root)"
}

// Purge deletes the events whose folder is set and outside the allowlist, and the agents that only ever appear in such events (an
// agent counts by its own events; one with none counts by the events of its session). Tasks and decisions are never touched. It is one
// transaction and bumps the revision once, so open pages see a change. dry only counts.
func (d *DB) Purge(al *Allowlist, dry bool) (*PurgeReport, error) {
	rep := &PurgeReport{}
	err := d.tx(func() error {
		type ev struct {
			id            int64
			cwd, sid, aid string
		}
		cwdOutside := map[string]bool{}
		outside := func(cwd string) bool {
			o, ok := cwdOutside[cwd]
			if !ok {
				o = !al.AllowedStored(cwd)
				cwdOutside[cwd] = o
			}
			return o
		}
		var gone []ev
		outByAgent, outBySession := map[string]int{}, map[string]int{}
		agentProject, sessionProject := map[string]string{}, map[string]string{}
		byProject := map[string]*PurgeRow{}
		row := func(p string) *PurgeRow {
			if byProject[p] == nil {
				byProject[p] = &PurgeRow{Project: p}
			}
			return byProject[p]
		}
		for _, r := range d.q("SELECT id, cwd, session_id, agent_id FROM events WHERE cwd IS NOT NULL AND cwd != ''") {
			cwd := rs(r, "cwd")
			if !outside(cwd) {
				continue
			}
			e := ev{ri(r, "id"), cwd, rs(r, "session_id"), rs(r, "agent_id")}
			gone = append(gone, e)
			row(baseName(cwd)).Events++
			if e.aid != "" {
				outByAgent[e.aid]++
				if agentProject[e.aid] == "" {
					agentProject[e.aid] = baseName(cwd)
				}
			}
			if e.sid != "" {
				outBySession[e.sid]++
				if sessionProject[e.sid] == "" {
					sessionProject[e.sid] = baseName(cwd)
				}
			}
		}
		allByAgent, allBySession := map[string]int{}, map[string]int{}
		for _, r := range d.q("SELECT agent_id, COUNT(*) n FROM events WHERE agent_id IS NOT NULL AND agent_id != '' GROUP BY agent_id") {
			allByAgent[rs(r, "agent_id")] = int(ri(r, "n"))
		}
		for _, r := range d.q("SELECT session_id, COUNT(*) n FROM events WHERE session_id IS NOT NULL AND session_id != '' GROUP BY session_id") {
			allBySession[rs(r, "session_id")] = int(ri(r, "n"))
		}
		var deadAgents []string
		for _, a := range d.q("SELECT id, session_id FROM agents") {
			id, sid := rs(a, "id"), rs(a, "session_id")
			switch {
			case allByAgent[id] > 0:
				if outByAgent[id] != allByAgent[id] {
					continue
				}
				row(agentProject[id]).Agents++
			case sid != "" && allBySession[sid] > 0 && outBySession[sid] == allBySession[sid]:
				row(sessionProject[sid]).Agents++
			default:
				continue
			}
			deadAgents = append(deadAgents, id)
		}
		for _, r := range byProject {
			rep.Rows = append(rep.Rows, *r)
			rep.Events += r.Events
			rep.Agents += r.Agents
		}
		sort.Slice(rep.Rows, func(i, j int) bool {
			if rep.Rows[i].Events != rep.Rows[j].Events {
				return rep.Rows[i].Events > rep.Rows[j].Events
			}
			return rep.Rows[i].Project < rep.Rows[j].Project
		})
		if dry || (len(gone) == 0 && len(deadAgents) == 0) {
			return nil
		}
		for i := 0; i < len(gone); i += 500 {
			part := gone[i:min(i+500, len(gone))]
			args := make([]any, len(part))
			for j, e := range part {
				args[j] = e.id
			}
			d.x("DELETE FROM events WHERE id IN ("+placeholders(len(part))+")", args...)
		}
		for i := 0; i < len(deadAgents); i += 500 {
			part := deadAgents[i:min(i+500, len(deadAgents))]
			args := make([]any, len(part))
			for j, id := range part {
				args[j] = id
			}
			d.x("DELETE FROM agents WHERE id IN ("+placeholders(len(part))+")", args...)
		}
		d.x("UPDATE tasks SET rev=? WHERE archived=0", d.bump()) // one new revision: a page polling by cursor sees a change
		return nil
	})
	return rep, err
}
