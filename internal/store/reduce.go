package store

import (
	"fmt"
	"strings"
)

var (
	keepKeys   = []string{"session_id", "cwd", "hook_event_name", "tool_name", "tool_use_id", "agent_id", "agent_type", "source", "model", "reason", "message", "notification_type", "duration_ms", "exit_code"}
	inputKeys  = map[string]bool{"command": true, "file_path": true, "description": true, "subagent_type": true, "model": true, "pattern": true, "path": true, "to": true, "message": true}
	agentKeys  = []string{"status", "agentId", "totalTokens", "totalToolUseCount", "resolvedModel"}
	failedKeys = []string{"stderr", "stdout", "output", "exit_code"}
)

// ReduceHook shrinks a hook payload: no tool output, no transcript, only the fields the server uses. The client does it before
// posting, and the server does it again on what it receives.
func ReduceHook(p map[string]any) map[string]any { return reduceHook(p, true) }

// ReduceHookFields keeps the same fields but cuts no string: the server rewrites paths and names first and cuts after, so a path is
// never cut where the rewrite rules cannot recognise it.
func ReduceHookFields(p map[string]any) map[string]any { return reduceHook(p, false) }

func reduceHook(p map[string]any, cut bool) map[string]any {
	out := make(map[string]any, 8)
	for _, k := range keepKeys {
		if v, ok := p[k]; ok {
			out[k] = v
		}
	}
	if v, ok := p["prompt"]; ok {
		out["prompt"] = pyStr(v) // server redacts, then keeps 120
		if cut {
			out["prompt"] = head(pyStr(v), 600)
		}
	}
	if v, ok := p["error"]; ok {
		e := pyStr(v) // keep the first line (summary, "Exit code N") plus the tail; the server keeps the last 40 lines (redacted, <= 8 KB)
		if cut && runeLen(e) > 20000 {
			e = head(strings.SplitN(e, "\n", 2)[0], 300) + "\n" + tail(e, 20000)
		}
		out["error"] = e
	}
	if ti, ok := p["tool_input"].(map[string]any); ok {
		m := make(map[string]any, len(ti))
		for k, v := range ti {
			if !inputKeys[k] {
				continue
			}
			if k == "command" || k == "message" || k == "to" {
				v = pyStr(v)
				if cut {
					v = head(v.(string), 2000)
				}
			}
			m[k] = v
		}
		out["tool_input"] = m
	}
	if tr, ok := p["tool_response"].(map[string]any); ok {
		tn, _ := p["tool_name"].(string)
		if tn == "Agent" || tn == "Task" {
			m := map[string]any{}
			for _, k := range agentKeys {
				if v, ok := tr[k]; ok {
					m[k] = v
				}
			}
			out["tool_response"] = m
		} else if p["hook_event_name"] == "PostToolUseFailure" { // only a failure ships its output
			m := map[string]any{}
			for _, k := range failedKeys {
				if v, ok := tr[k]; ok {
					m[k] = pyStr(v)
					if cut {
						m[k] = tail(pyStr(v), 20000)
					}
				}
			}
			out["tool_response"] = m
		}
	}
	return out
}

// DecisionBlock is the text a Claude session sees for one decision (same for `wait` and the hook).
func DecisionBlock(id int64, taskKey, label, text string) string {
	s := fmt.Sprintf("BOARD DECISION #%d %s: %s", id, taskKey, label)
	if text != "" {
		s += "\n" + text
	}
	return s + fmt.Sprintf("\n(ack: sheepdog.py done %d)", id)
}

// HookJSON is the exact JSON Claude Code reads from a UserPromptSubmit hook (the reply body is printed verbatim).
func HookJSON(decisions []Row) []byte {
	blocks := make([]string, len(decisions))
	for i, d := range decisions {
		blocks[i] = DecisionBlock(ri(d, "id"), rs(d, "task_key"), rs(d, "label"), rs(d, "text"))
	}
	return jsonPy([]KV{{"hookSpecificOutput", []KV{{"hookEventName", "UserPromptSubmit"}, {"additionalContext", "Board decisions waiting for you:\n\n" + strings.Join(blocks, "\n\n")}}}})
}
