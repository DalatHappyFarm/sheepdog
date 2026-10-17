package store

import (
	"fmt"
	"os"
	"regexp"
	"strings"
)

var exitRe = regexp.MustCompile(`^\s*Exit code (\d+)`)

// Env is a SHEEPDOG_* setting; the old BOARD_* name still works while the new one is unset.
func Env(newName, oldName, def string) string {
	if v := os.Getenv(newName); v != "" {
		return v
	}
	if v := os.Getenv(oldName); v != "" {
		return v
	}
	return def
}

func str(v any) string { // str(v or "")
	if truthy(v) {
		return pyStr(v)
	}
	return ""
}

func dict(v any) map[string]any {
	m, _ := v.(map[string]any)
	if m == nil {
		return map[string]any{}
	}
	return m
}

// failData is what the failed-command drawer shows: redacted command, last 40 output lines (<= 8 KB, each redacted), exit code, duration.
func failData(ti, p map[string]any) []KV {
	trv := p["tool_response"]
	tr, trIsDict := trv.(map[string]any)
	parts := []any{p["error"]}
	if trIsDict {
		parts = append(parts, tr["stderr"], tr["stdout"], tr["output"])
	} else {
		parts = append(parts, trv)
	}
	var texts []string
	for _, x := range parts {
		if truthy(x) {
			texts = append(texts, pyStr(x))
		}
	}
	text := tail(strings.Join(texts, "\n"), 65536)
	lines := pySplitLines(text)
	if len(lines) > 40 {
		lines = lines[len(lines)-40:]
	}
	for i, ln := range lines {
		lines[i] = Redact(ln)
	}
	out := tail(strings.Join(lines, "\n"), 8192)
	var exit any
	errText := str(p["error"])
	if p["exit_code"] != nil {
		if n, ok := pyInt(p["exit_code"]); ok {
			exit = n
		}
	} else if trIsDict {
		if n, ok := pyInt(tr["exit_code"]); ok {
			exit = n
		}
	}
	if exit == nil {
		if m := exitRe.FindStringSubmatch(errText); m != nil {
			var n int64
			fmt.Sscan(m[1], &n)
			exit = n
		}
	}
	var cmd, o, dur any
	if c := head(Redact(str(ti["command"])), 2048); c != "" {
		cmd = c
	}
	if out != "" {
		o = out
	}
	if n, ok := pyInt(p["duration_ms"]); ok {
		dur = n
	}
	return []KV{{"cmd", cmd}, {"out", o}, {"exit", exit}, {"dur_ms", dur}}
}

// Ingest handles one hook payload (the raw Claude Code hook, or the client-reduced one) and returns the decisions that wait for
// the folder of a UserPromptSubmit.
func (d *DB) Ingest(p map[string]any) ([]Row, error) {
	ev := str(p["hook_event_name"])
	if ev == "" {
		return nil, bad("hook_event_name missing")
	}
	rawCwd := str(p["cwd"])
	if !d.allow.Allowed(rawCwd) {
		return nil, nil // outside the allowlist: dropped before anything is stored or changed, the reply is the normal empty one
	}
	absCwd := strings.HasPrefix(rawCwd, "/")
	// from here on nothing holds a raw path or name; the folder is compared and stored in its rewritten form. The strings are cut only
	// after the rewrite, so a path is never cut where the rules cannot recognise it.
	p = ReduceHook(d.scrub.Payload(ReduceHookFields(p)))
	ti := dict(p["tool_input"])
	tr := dict(p["tool_response"])
	tool := str(p["tool_name"])
	isAgentTool := tool == "Agent" || tool == "Task" // the subagent tool was called Task before it was renamed Agent
	if tool != "" && !isAgentTool {
		if ev == "PreToolUse" {
			if tool == "SendMessage" {
				d.resumeRelink(ti, now())
			}
			return nil, nil // ordinary tools are recorded once, on PostToolUse
		}
		if ev == "PostToolUse" && Env("SHEEPDOG_TOOL_EVENTS", "BOARD_TOOL_EVENTS", "1") == "0" {
			return nil, nil
		}
	}
	sid := head(str(p["session_id"]), 100)
	cwd := head(str(p["cwd"]), 500)
	var aid *string
	if s, ok := p["agent_id"].(string); ok {
		aid = &s
	}
	aidS := ""
	if aid != nil {
		aidS = *aid
	}
	err := d.tx(func() error {
		ts := now()
		var data []KV
		task := d.link(cwd, aidS)
		if isAgentTool {
			if t := d.descTask(ti["description"], ts); t != "" {
				task = t
			}
		}
		summary := ev
		switch {
		case ev == "SessionStart":
			summary = "session start (" + orQ(p["source"]) + ")"
			data = []KV{{"source", p["source"]}, {"model", p["model"]}}
		case ev == "SessionEnd":
			summary = "session end (" + orQ(p["reason"]) + ")"
		case ev == "UserPromptSubmit":
			summary = "prompt: " + Clip(str(p["prompt"]), 120)
		case ev == "Stop":
			summary = "turn finished"
		case ev == "Notification":
			summary = "notification: " + Clip(str(p["message"]), 200)
			data = []KV{{"type", p["notification_type"]}}
		case ev == "SubagentStart":
			summary = "subagent start"
			if truthy(p["agent_type"]) {
				summary += " (" + pyStr(p["agent_type"]) + ")"
			}
			d.subagentStart(aid, str(p["agent_type"]), sid, task, ts)
			if t := d.link(cwd, aidS); t != "" { // the adopted "Task N" row wins over the worktree guess
				task = t
			}
		case ev == "SubagentStop":
			summary = "subagent stop"
			if truthy(p["agent_type"]) {
				summary += " (" + pyStr(p["agent_type"]) + ")"
			}
			if aidS != "" {
				var sidv, tk any
				if sid != "" {
					sidv = sid
				}
				if task != "" {
					tk = task
				}
				var typ any
				if p["agent_type"] != nil {
					typ = p["agent_type"]
				}
				d.agentSet(aidS, ts, []fv{{"status", "done"}, {"type", typ}, {"session_id", sidv}, {"task_key", tk}})
			}
		case isAgentTool:
			summary = d.agentToolEvent(ev, ti, tr, p, sid, task, ts)
		case tool != "":
			var first any
			for _, k := range []string{"command", "file_path", "pattern", "path"} {
				if truthy(ti[k]) {
					first = ti[k]
					break
				}
			}
			what := ""
			if first != nil {
				what = Clip(pyStr(first), 200)
			}
			summary = tool
			if what != "" {
				summary = tool + ": " + what
			}
			data = []KV{{"tool", tool}}
			if ev == "PostToolUseFailure" {
				summary = fmt.Sprintf("%s failed: %s (%s)", tool, what, Clip(strings.SplitN(str(p["error"]), "\n", 2)[0], 100))
				data = append(data, failData(ti, p)...)
			} else if aid != nil && aidS != "" {
				d.x("UPDATE agents SET tool_uses=tool_uses+1, last_event_at=?, rev=? WHERE id=?", ts, d.bump(), aidS)
			}
		}
		if aidS != "" && !isAgentTool && ev != "SubagentStart" && ev != "SubagentStop" {
			d.x("UPDATE agents SET last_event_at=?, rev=? WHERE id=?", ts, d.bump(), aidS)
		}
		kept := make([]KV, 0, len(data))
		for _, kv := range data {
			if kv.V != nil {
				kept = append(kept, kv)
			}
		}
		d.event(ev, summary, evArgs{task: task, sid: sid, cwd: cwd, project: d.projectOf(task, cwd), agent: aid, data: kept, ts: ts})
		return nil
	})
	if err != nil {
		return nil, err
	}
	// fallback delivery: a prompt typed in the owning folder picks up decisions no waiter took (only from the project folder or a
	// subfolder/worktree of it, never a parent or sibling)
	if ev == "UserPromptSubmit" && absCwd {
		return d.Claim(cwd, "hook:"+sid, true), nil
	}
	return nil, nil
}

func orQ(v any) string { // (p.get(k) or "?")
	if truthy(v) {
		return pyStr(v)
	}
	return "?"
}

func (d *DB) subagentStart(aid *string, atype, sid, task string, ts int64) {
	if aid == nil || *aid == "" {
		return
	}
	// best guess: adopt the oldest running placeholder row made by PreToolUse(Agent) in this session
	ph := d.q1("SELECT id FROM agents WHERE status='running' AND id LIKE 'tu:%' AND session_id=? AND (type=? OR type='') ORDER BY started_at LIMIT 1", sid, atype)
	if ph != nil && d.q1("SELECT 1 x FROM agents WHERE id=?", *aid) == nil {
		d.x("UPDATE agents SET id=?, rev=? WHERE id=?", *aid, d.bump(), ph["id"])
	}
	row := d.q1("SELECT task_key FROM agents WHERE id=?", *aid)
	var tk any = nilIfEmpty(task)
	if row != nil && rs(row, "task_key") != "" {
		tk = nil
	}
	// status=running also revives a finished agent that was resumed with SendMessage (SubagentStop sets it back to done)
	d.agentSet(*aid, ts, []fv{{"type", atype}, {"session_id", sid}, {"task_key", tk}, {"status", "running"}})
}

func nilIfEmpty(s string) any {
	if s == "" {
		return nil
	}
	return s
}

// resumeRelink: a SendMessage {to, message} whose message starts with "Task N" moves the addressed agent (by id or name) to task TN.
func (d *DB) resumeRelink(ti map[string]any, ts int64) {
	to := str(ti["to"])
	if to == "" || !taskRe.MatchString(str(ti["message"])) {
		return
	}
	_ = d.tx(func() error {
		a := d.q1("SELECT id FROM agents WHERE id=? OR name=? ORDER BY COALESCE(last_event_at,0) DESC LIMIT 1", to, to)
		if a == nil {
			return nil
		}
		if key := d.descTask(pyStr(ti["message"]), ts); key != "" {
			d.x("UPDATE agents SET task_key=?, rev=? WHERE id=?", key, d.bump(), a["id"])
		}
		return nil
	})
}

func (d *DB) agentToolEvent(ev string, ti, tr, p map[string]any, sid, task string, ts int64) string {
	desc := Clip(str(ti["description"]), 120)
	var kind any = ti["model"]
	if !truthy(kind) {
		kind = ti["subagent_type"]
	}
	what := taskRe.ReplaceAllString(desc, "")
	if truthy(kind) {
		what += " (" + pyStr(kind) + ")"
	}
	tuid := str(p["tool_use_id"])
	var row Row
	if tuid != "" {
		row = d.q1("SELECT id FROM agents WHERE tool_use_id=?", tuid)
	}
	placeholder := func() string {
		if tuid != "" {
			return "tu:" + tuid
		}
		return fmt.Sprintf("tu:%s:%d", sid, ts)
	}
	if ev == "PreToolUse" {
		if row == nil {
			aid := placeholder()
			d.agentSet(aid, ts, []fv{{"name", desc}, {"model", ti["model"]}, {"type", ti["subagent_type"]}, {"session_id", sid}, {"task_key", nilIfEmpty(task)}, {"status", "running"}})
			if tuid != "" {
				d.x("UPDATE agents SET tool_use_id=? WHERE id=?", tuid, aid)
			}
		}
		return "agent start: " + what
	}
	failed := ev == "PostToolUseFailure"
	if ev == "PostToolUse" || failed {
		var id string
		if row == nil { // no Pre seen (hook installed mid-flight): create it already finished
			id = placeholder()
			if truthy(tr["agentId"]) {
				id = pyStr(tr["agentId"])
			}
			d.agentSet(id, ts, []fv{{"name", desc}, {"model", ti["model"]}, {"type", ti["subagent_type"]}, {"session_id", sid}, {"task_key", nilIfEmpty(task)}})
		} else {
			id = rs(row, "id")
		}
		if tr["status"] == "async_launched" {
			return "agent launched in background: " + what
		}
		st := "done"
		if failed {
			st = "failed"
		}
		var model any
		if truthy(tr["resolvedModel"]) {
			model = tr["resolvedModel"]
		} else if truthy(ti["model"]) {
			model = ti["model"]
		}
		d.agentSet(id, ts, []fv{{"status", st}, {"tokens", tr["totalTokens"]}, {"model", model}})
		if tr["totalToolUseCount"] != nil {
			n, ok := pyInt(tr["totalToolUseCount"])
			if !ok {
				panic(BadRequest(fmt.Sprintf("invalid literal for int() with base 10: %s", pyRepr(pyStr(tr["totalToolUseCount"])))))
			}
			d.x("UPDATE agents SET tool_uses=? WHERE id=?", n, id)
		}
		return fmt.Sprintf("agent %s: %s", st, taskRe.ReplaceAllString(desc, ""))
	}
	return "agent: " + what
}
