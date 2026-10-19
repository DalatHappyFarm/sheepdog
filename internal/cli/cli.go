// Package cli is the client side of sheepdog: task, agent, archive, brief, wait, done and the Claude Code hook command.
package cli

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/DalatHappyFarm/sheepdog/internal/store"
)

var (
	statuses      = []string{"planned", "analysing", "building", "review", "testing", "done", "blocked"}
	agentStatuses = []string{"running", "done", "failed"}
	waiting       = []string{"me", "agent", "qc", "po", "none"}
)

// exit is how a command ends: the text goes to stderr (empty: nothing) and the process exits with code.
type exit struct {
	msg  string
	code int
}

func (e exit) Error() string { return e.msg }

func fail(msg string) exit { return exit{msg, 1} }

func baseURL() string {
	return strings.TrimRight(store.Env("SHEEPDOG_URL", "BOARD_URL", "http://127.0.0.1:8787"), "/")
}

func token() string { return store.Env("SHEEPDOG_TOKEN", "BOARD_TOKEN", "") }

// call posts JSON to the server and returns the reply body; an HTTP error or an unreachable server ends the command.
func call(path string, payload any, timeout time.Duration) ([]byte, error) {
	b, _ := json.Marshal(payload)
	u := baseURL() + path
	req, err := http.NewRequest("POST", u, bytes.NewReader(b))
	if err != nil {
		return nil, fail(fmt.Sprintf("cannot reach %s: %v", u, err))
	}
	req.Header.Set("Content-Type", "application/json")
	if t := token(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t)
	}
	resp, err := (&http.Client{Timeout: timeout}).Do(req)
	if err != nil {
		return nil, fail(fmt.Sprintf("cannot reach %s: %v", u, err))
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode >= 400 {
		var e struct {
			Error string `json:"error"`
		}
		_ = json.Unmarshal(body, &e)
		msg := e.Error
		if msg == "" {
			msg = http.StatusText(resp.StatusCode)
		}
		return nil, fail(fmt.Sprintf("error %d: %s", resp.StatusCode, msg))
	}
	return body, nil
}

func printJSON(raw []byte) {
	var out bytes.Buffer
	if json.Indent(&out, raw, "", "  ") != nil {
		out.Reset()
		out.Write(raw)
	}
	fmt.Println(out.String())
}

// ---------------------------------------------------------------- arguments (argparse-like: flags may come after the positionals)

type spec struct {
	flags   map[string]bool // flag name -> takes a value
	choices map[string][]string
}

type parsed struct {
	pos  []string
	opts map[string][]string
}

func (p parsed) get(name string) (string, bool) {
	v, ok := p.opts[name]
	if !ok {
		return "", false
	}
	return v[len(v)-1], true
}

func usage(msg string) error { return exit{"sheepdog: error: " + msg, 2} }

func parseArgs(args []string, sp spec) (parsed, error) {
	p := parsed{opts: map[string][]string{}}
	for i := 0; i < len(args); i++ {
		a := args[i]
		if a == "--" {
			p.pos = append(p.pos, args[i+1:]...)
			break
		}
		if !strings.HasPrefix(a, "--") {
			p.pos = append(p.pos, a)
			continue
		}
		name, val, hasVal := strings.Cut(a[2:], "=")
		if _, known := sp.flags[name]; !known {
			return p, usage("unrecognized arguments: " + a)
		}
		if !hasVal {
			if i+1 >= len(args) {
				return p, usage("argument --" + name + ": expected one argument")
			}
			i++
			val = args[i]
		}
		if ch, ok := sp.choices[name]; ok && !in(ch, val) {
			return p, usage(fmt.Sprintf("argument --%s: invalid choice: '%s' (choose from '%s')", name, val, strings.Join(ch, "', '")))
		}
		p.opts[name] = append(p.opts[name], val)
	}
	return p, nil
}

func in(l []string, v string) bool {
	for _, x := range l {
		if x == v {
			return true
		}
	}
	return false
}

func need(p parsed, n int, names string) error {
	if len(p.pos) < n {
		return usage("the following arguments are required: " + names)
	}
	if len(p.pos) > n {
		return usage("unrecognized arguments: " + strings.Join(p.pos[n:], " "))
	}
	return nil
}

func defaultCwd() string {
	if v := os.Getenv("PWD"); v != "" {
		return v
	}
	d, _ := os.Getwd()
	return d
}

// payload copies the options the user gave into a request body; options not given are sent as null.
func payload(p parsed, pairs [][2]string) map[string]any {
	pl := map[string]any{}
	for _, k := range pairs {
		if v, ok := p.get(k[0]); ok {
			pl[k[1]] = v
		} else {
			pl[k[1]] = nil
		}
	}
	return pl
}

// ---------------------------------------------------------------- commands

// Run runs one client command (args[0] is the subcommand) and returns the exit code.
func Run(args []string) int {
	err := run(args)
	if err == nil {
		return 0
	}
	if e, ok := err.(exit); ok {
		if e.msg != "" {
			fmt.Fprintln(os.Stderr, e.msg)
		}
		return e.code
	}
	fmt.Fprintln(os.Stderr, err)
	return 1
}

func postAndPrint(path string, pl map[string]any) error {
	out, err := call(path, pl, 10*time.Second)
	if err != nil {
		return err
	}
	printJSON(out)
	return nil
}

func run(args []string) error {
	if len(args) == 0 {
		return usage("the following arguments are required: cmd")
	}
	rest := args[1:]
	switch args[0] {
	case "hook":
		Hook(os.Stdin)
	case "task":
		p, err := parseArgs(rest, spec{flags: map[string]bool{"title": true, "status": true, "project": true, "branch": true, "worktree": true, "mr": true, "note": true, "next": true, "parent": true, "waiting": true},
			choices: map[string][]string{"status": append(append([]string{}, statuses...), "running"), "waiting": waiting}})
		if err != nil {
			return err
		}
		if err := need(p, 1, "key"); err != nil {
			return err
		}
		pl := payload(p, [][2]string{{"title", "title"}, {"status", "status"}, {"project", "project"}, {"branch", "branch"}, {"worktree", "worktree"}, {"mr", "mr_url"}, {"note", "note"}, {"next", "next"}, {"waiting", "waiting"}, {"parent", "parent"}})
		pl["key"] = p.pos[0]
		pl["cwd"] = defaultCwd() // lets a reply on a task with no review card find this folder's `sheepdog wait`
		return postAndPrint("/api/task", pl)
	case "agent":
		p, err := parseArgs(rest, spec{flags: map[string]bool{"task": true, "name": true, "model": true, "status": true}, choices: map[string][]string{"status": agentStatuses}})
		if err != nil {
			return err
		}
		if err := need(p, 1, "id"); err != nil {
			return err
		}
		pl := payload(p, [][2]string{{"task", "task"}, {"name", "name"}, {"model", "model"}, {"status", "status"}})
		pl["id"] = p.pos[0]
		return postAndPrint("/api/agent", pl)
	case "archive":
		p, err := parseArgs(rest, spec{})
		if err != nil {
			return err
		}
		if err := need(p, 1, "key"); err != nil {
			return err
		}
		return postAndPrint("/api/archive", map[string]any{"key": p.pos[0]})
	case "brief":
		return brief(rest)
	case "wait":
		p, err := parseArgs(rest, spec{flags: map[string]bool{"cwd": true}})
		if err != nil {
			return err
		}
		if err := need(p, 0, ""); err != nil {
			return err
		}
		cwd, ok := p.get("cwd")
		if !ok {
			cwd = defaultCwd()
		}
		return Wait(cwd)
	case "done":
		p, err := parseArgs(rest, spec{})
		if err != nil {
			return err
		}
		if err := need(p, 1, "id"); err != nil {
			return err
		}
		id, perr := strconv.Atoi(p.pos[0])
		if perr != nil {
			return usage(fmt.Sprintf("argument id: invalid int value: '%s'", p.pos[0]))
		}
		return postAndPrint(fmt.Sprintf("/api/decision/%d/done", id), map[string]any{})
	default:
		return usage(fmt.Sprintf("argument cmd: invalid choice: '%s' (choose from 'serve', 'task', 'agent', 'archive', 'hook', 'brief', 'wait', 'done')", args[0]))
	}
	return nil
}

// ParseOptions turns the CLI --option 'id|Label|hint' (hint optional) into the brief_options list.
func ParseOptions(opts []string, primary string) ([]map[string]any, error) {
	out := []map[string]any{}
	found := false
	for _, o := range opts {
		parts := strings.SplitN(o, "|", 3)
		for len(parts) < 3 {
			parts = append(parts, "")
		}
		id := strings.TrimSpace(parts[0])
		isP := id == primary
		found = found || isP
		out = append(out, map[string]any{"id": id, "label": strings.TrimSpace(parts[1]), "hint": strings.TrimSpace(parts[2]), "primary": isP})
	}
	if primary != "" && !found {
		return nil, fmt.Errorf("--primary %s is not one of the options", primary)
	}
	return out, nil
}

func brief(args []string) error {
	p, err := parseArgs(args, spec{flags: map[string]bool{"file": true, "option": true, "primary": true, "cwd": true}})
	if err != nil {
		return err
	}
	if err := need(p, 1, "key"); err != nil {
		return err
	}
	file, ok := p.get("file")
	if !ok {
		return usage("the following arguments are required: --file")
	}
	primary, _ := p.get("primary")
	opts, err := ParseOptions(p.opts["option"], primary)
	if err != nil {
		return fail(err.Error())
	}
	var md []byte
	if file == "-" {
		md, err = io.ReadAll(os.Stdin)
	} else {
		md, err = os.ReadFile(file)
	}
	if err != nil {
		return fail(err.Error())
	}
	cwd, ok := p.get("cwd")
	if !ok {
		cwd = defaultCwd()
	}
	return postAndPrint("/api/task/"+store.Quote(p.pos[0])+"/brief", map[string]any{"brief": string(md), "options": opts, "cwd": cwd})
}

// Wait long-polls for this folder's decisions; it prints them and returns on the first answer. Errors only slow it down (backoff up to 30 s).
func Wait(cwd string) error {
	delay := time.Second
	for {
		got, err := fetchDecisions(cwd)
		if err != nil {
			time.Sleep(delay)
			delay = min(30*time.Second, delay*2)
			continue
		}
		delay = time.Second
		if len(got) > 0 {
			blocks := make([]string, len(got))
			for i, d := range got {
				id, _ := d["id"].(float64)
				blocks[i] = store.DecisionBlock(int64(id), str(d["task_key"]), str(d["label"]), str(d["text"]))
			}
			fmt.Println(strings.Join(blocks, "\n\n"))
			return nil
		}
		time.Sleep(200 * time.Millisecond)
	}
}

func str(v any) string { s, _ := v.(string); return s }

func fetchDecisions(cwd string) ([]map[string]any, error) {
	u := baseURL() + "/api/decisions/wait?" + url.Values{"cwd": {cwd}, "timeout": {"50"}}.Encode()
	req, err := http.NewRequest("GET", u, nil)
	if err != nil {
		return nil, err
	}
	if t := token(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t)
	}
	resp, err := (&http.Client{Timeout: 65 * time.Second}).Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("status %d", resp.StatusCode)
	}
	var out []map[string]any
	if err := json.NewDecoder(resp.Body).Decode(&out); err != nil {
		return nil, err
	}
	return out, nil
}

// Hook is the Claude Code hook command: JSON on stdin. It never fails and never blocks Claude for more than about a second; it prints
// the server's reply (valid hook JSON) only when the server hands back board decisions for this folder on a UserPromptSubmit.
func Hook(stdin io.Reader) {
	defer func() { _ = recover() }()
	raw, _ := io.ReadAll(io.LimitReader(stdin, 4_000_000))
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	var v any
	if dec.Decode(&v) != nil {
		return
	}
	m, ok := v.(map[string]any)
	if !ok {
		return
	}
	b, _ := json.Marshal(store.ReduceHookFields(m))
	req, err := http.NewRequest("POST", baseURL()+"/api/hook", bytes.NewReader(b))
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/json")
	if t := token(); t != "" {
		req.Header.Set("Authorization", "Bearer "+t)
	}
	resp, err := (&http.Client{Timeout: time.Second}).Do(req)
	if err != nil {
		return
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(resp.Body)
	if resp.StatusCode < 400 && len(body) > 0 { // the server's reply is empty, or exactly the hook JSON
		os.Stdout.Write(body)
	}
}
