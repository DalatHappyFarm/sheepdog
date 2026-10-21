// sheepdog: a tiny self-hosted task board for AI agent workflows. One static binary.
//
//	sheepdog serve                       run the server (needs SHEEPDOG_TOKEN, or --no-auth)
//	sheepdog task T2 --title ... --status testing --note "..."
//	sheepdog agent <id> --task T2 --status running|done|failed
//	sheepdog archive T2
//	sheepdog brief T2 --file review.md --option 'merge|Approve and merge|ships it' --primary merge [--cwd DIR]
//	sheepdog wait [--cwd DIR]            block until the developer decides on the board, print it, exit 0
//	sheepdog done <id>                   acknowledge a board decision
//	sheepdog purge --outside-allow [--allow DIR ...] [--db PATH] [--dry-run]   drop the stored events and agents of folders outside the allowlist
//	sheepdog scrub [--db PATH] [--dry-run] [--allow DIR ...]   rewrite the paths and names already stored (stop the server first)
//	sheepdog hook                        Claude Code hook: JSON on stdin, always exits 0
//	sheepdog version                     the release this binary is, or the commit it was built from
package main

import (
	"context"
	"errors"
	"fmt"
	"net"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"runtime/debug"
	"strconv"
	"strings"
	"syscall"
	"time"

	"github.com/DalatHappyFarm/sheepdog/internal/cli"
	"github.com/DalatHappyFarm/sheepdog/internal/server"
	"github.com/DalatHappyFarm/sheepdog/internal/store"
)

func main() {
	if len(os.Args) > 1 && os.Args[1] == "hook" { // the hook runs on every Claude event: no flag parsing, always exit 0
		cli.Hook(os.Stdin)
		return
	}
	if len(os.Args) > 1 && os.Args[1] == "serve" {
		os.Exit(serve(os.Args[2:]))
	}
	if len(os.Args) > 1 && os.Args[1] == "purge" {
		os.Exit(purge(os.Args[2:]))
	}
	if len(os.Args) > 1 && os.Args[1] == "scrub" {
		os.Exit(scrub(os.Args[2:]))
	}
	if len(os.Args) > 1 && (os.Args[1] == "version" || os.Args[1] == "--version") {
		info, _ := debug.ReadBuildInfo()
		fmt.Println("sheepdog", versionOf(info))
		return
	}
	if len(os.Args) > 1 && (os.Args[1] == "-h" || os.Args[1] == "--help") {
		fmt.Println("usage: sheepdog {serve,task,agent,archive,hook,brief,wait,done,version} ...\nEnv: SHEEPDOG_TOKEN, SHEEPDOG_URL (client, default http://127.0.0.1:8787), SHEEPDOG_DB, SHEEPDOG_TOOL_EVENTS=0 (the old BOARD_* names still work)")
		return
	}
	os.Exit(cli.Run(os.Args[1:]))
}

// versionOf is the module version `go install ...@v0.1.0` stamps in, else the commit a checkout was built from.
func versionOf(info *debug.BuildInfo) string {
	if info == nil {
		return "unknown"
	}
	if v := info.Main.Version; v != "" && v != "(devel)" {
		return v
	}
	rev, dirty := "", false
	for _, s := range info.Settings {
		switch s.Key {
		case "vcs.revision":
			rev = s.Value
		case "vcs.modified":
			dirty = s.Value == "true"
		}
	}
	if rev == "" {
		return "devel"
	}
	if len(rev) > 12 {
		rev = rev[:12]
	}
	if dirty {
		rev += "-dirty"
	}
	return "devel " + rev
}

// dataDB is the default database: ~/.local/share/sheepdog/board.db, or the old agent-board folder's file when only that exists.
func dataDB() string {
	home, _ := os.UserHomeDir()
	n := filepath.Join(home, ".local/share/sheepdog/board.db")
	o := filepath.Join(home, ".local/share/agent-board/board.db")
	if _, err := os.Stat(n); err != nil {
		if _, err := os.Stat(o); err == nil {
			fmt.Printf("sheepdog: using the old database %s; move it to %s\n", o, n)
			return o
		}
	}
	_ = os.MkdirAll(filepath.Dir(n), 0o777)
	return n
}

func serve(args []string) int {
	host, port, dbPath, noAuth := "127.0.0.1", 8787, "", false
	var allow []string
	for i := 0; i < len(args); i++ {
		a := args[i]
		name, val, hasVal := strings.Cut(a, "=")
		next := func() string {
			if hasVal {
				return val
			}
			i++
			if i >= len(args) {
				fmt.Fprintf(os.Stderr, "sheepdog serve: error: argument %s: expected one argument\n", name)
				os.Exit(2)
			}
			return args[i]
		}
		switch name {
		case "--db":
			dbPath = next()
		case "--host":
			host = next()
		case "--port":
			n, err := strconv.Atoi(next())
			if err != nil {
				fmt.Fprintln(os.Stderr, "sheepdog serve: error: argument --port: invalid int value")
				return 2
			}
			port = n
		case "--allow":
			allow = append(allow, next())
		case "--no-auth":
			noAuth = true
		default:
			fmt.Fprintf(os.Stderr, "sheepdog serve: error: unrecognized arguments: %s\n", a)
			return 2
		}
	}
	tok := store.Env("SHEEPDOG_TOKEN", "BOARD_TOKEN", "")
	if tok == "" && !noAuth {
		fmt.Fprintln(os.Stderr, "SHEEPDOG_TOKEN is required (or pass --no-auth for local dev)")
		return 1
	}
	if dbPath == "" {
		dbPath = store.Env("SHEEPDOG_DB", "BOARD_DB", "")
	}
	if dbPath == "" {
		dbPath = dataDB()
	}
	db, err := store.Open(dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog:", err)
		return 1
	}
	al := store.ParseAllow(os.Getenv("SHEEPDOG_ALLOW"), allow)
	db.SetAllow(al)
	db.SetScrub(store.ScrubberFromEnv(al.Roots()))
	var token *string
	if !noAuth {
		token = &tok
	}
	if err := db.Prune(30, 200000); err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog: prune:", err)
	}
	ln, err := net.Listen("tcp", net.JoinHostPort(host, strconv.Itoa(port)))
	if err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog:", err)
		return 1
	}
	debug.SetGCPercent(25) // a small steady heap: the board is a few MB of live data
	go func() {
		for range time.Tick(time.Hour) {
			_ = db.Prune(30, 200000)
		}
	}()
	srv := &http.Server{Handler: server.New(db, token), ReadHeaderTimeout: 10 * time.Second, IdleTimeout: 60 * time.Second}
	auth := "token"
	if noAuth {
		auth = "off"
	}
	fmt.Printf("sheepdog on http://%s:%d (db %s, auth %s)\n", host, ln.Addr().(*net.TCPAddr).Port, dbPath, auth)
	if al.Active() {
		fmt.Printf("sheepdog: hooks accepted only from %s\n", strings.Join(al.Roots(), ", "))
	}
	stop := make(chan os.Signal, 1)
	signal.Notify(stop, os.Interrupt, syscall.SIGTERM)
	done := make(chan struct{})
	go func() {
		<-stop
		ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
		defer cancel()
		_ = srv.Shutdown(ctx)
		_ = srv.Close()
		close(done)
	}()
	if err := srv.Serve(ln); err != nil && !errors.Is(err, http.ErrServerClosed) {
		fmt.Fprintln(os.Stderr, "sheepdog:", err)
		return 1
	}
	<-done
	db.Close()
	return 0
}

// purge deletes what hooks from outside the allowlist left in the database (see store.Purge). It never touches tasks or decisions.
func purge(args []string) int {
	var allow []string
	dbPath, outside, dry := "", false, false
	for i := 0; i < len(args); i++ {
		name, val, hasVal := strings.Cut(args[i], "=")
		next := func() string {
			if hasVal {
				return val
			}
			i++
			if i >= len(args) {
				fmt.Fprintf(os.Stderr, "sheepdog purge: error: argument %s: expected one argument\n", name)
				os.Exit(2)
			}
			return args[i]
		}
		switch name {
		case "--outside-allow":
			outside = true
		case "--allow":
			allow = append(allow, next())
		case "--db":
			dbPath = next()
		case "--dry-run":
			dry = true
		default:
			fmt.Fprintf(os.Stderr, "sheepdog purge: error: unrecognized arguments: %s\n", args[i])
			return 2
		}
	}
	if !outside {
		fmt.Fprintln(os.Stderr, "sheepdog purge: error: say what to purge: --outside-allow")
		return 2
	}
	al := store.ParseAllow(os.Getenv("SHEEPDOG_ALLOW"), allow) // --allow adds to SHEEPDOG_ALLOW, like serve
	if !al.Active() {
		fmt.Fprintln(os.Stderr, "sheepdog purge: refusing to run with an empty allowlist (pass --allow DIR or set SHEEPDOG_ALLOW)")
		return 1
	}
	if dbPath == "" {
		dbPath = store.Env("SHEEPDOG_DB", "BOARD_DB", "")
	}
	if dbPath == "" {
		dbPath = dataDB()
	}
	if _, err := os.Stat(dbPath); err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog purge:", err)
		return 1
	}
	db, err := store.Open(dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog purge:", err)
		return 1
	}
	defer db.Close()
	rep, err := db.Purge(al, dry)
	if err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog purge:", err)
		return 1
	}
	verb := "deleted"
	if dry {
		verb = "would delete (dry run, nothing changed)"
	}
	fmt.Printf("db %s\nallowlist: %s\n%s:\n%-28s %8s %8s\n", dbPath, strings.Join(al.Roots(), ", "), verb, "project", "events", "agents")
	for _, r := range rep.Rows {
		fmt.Printf("%-28s %8d %8d\n", r.Project, r.Events, r.Agents)
	}
	fmt.Printf("%-28s %8d %8d\n", "total", rep.Events, rep.Agents)
	return 0
}

// scrub applies the rewrite of paths and names to the rows already stored (see store.Scrubber). Stop the server first.
func scrub(args []string) int {
	var allow []string
	dbPath, dry := "", false
	for i := 0; i < len(args); i++ {
		name, val, hasVal := strings.Cut(args[i], "=")
		next := func() string {
			if hasVal {
				return val
			}
			i++
			if i >= len(args) {
				fmt.Fprintf(os.Stderr, "sheepdog scrub: error: argument %s: expected one argument\n", name)
				os.Exit(2)
			}
			return args[i]
		}
		switch name {
		case "--allow":
			allow = append(allow, next())
		case "--db":
			dbPath = next()
		case "--dry-run":
			dry = true
		case "-h", "--help":
			fmt.Println("usage: sheepdog scrub [--db PATH] [--dry-run] [--allow DIR ...]\n\nRewrites the paths and names already stored (home directory, temp dirs, user and host name, folders under the allowed roots)\nthe way the server rewrites them on the way in. Idempotent, one transaction.\nSTOP THE SERVER FIRST: a running server keeps cached state and would not see the change.\nThe roots come from SHEEPDOG_ALLOW plus --allow; the home, user and host come from this process (HOME, USER, SHEEPDOG_HOSTNAME).")
			return 0
		default:
			fmt.Fprintf(os.Stderr, "sheepdog scrub: error: unrecognized arguments: %s\n", args[i])
			return 2
		}
	}
	if dbPath == "" {
		dbPath = store.Env("SHEEPDOG_DB", "BOARD_DB", "")
	}
	if dbPath == "" {
		dbPath = dataDB()
	}
	if _, err := os.Stat(dbPath); err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog scrub:", err)
		return 1
	}
	db, err := store.Open(dbPath)
	if err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog scrub:", err)
		return 1
	}
	defer db.Close()
	al := store.ParseAllow(os.Getenv("SHEEPDOG_ALLOW"), allow)
	counts, err := db.ScrubRows(store.ScrubberFromEnv(al.Roots()), dry)
	if err != nil {
		fmt.Fprintln(os.Stderr, "sheepdog scrub:", err)
		return 1
	}
	verb := "rewrote"
	if dry {
		verb = "would rewrite (dry run, nothing changed)"
	}
	fmt.Printf("db %s\n%s rows:\n", dbPath, verb)
	for _, c := range counts {
		fmt.Printf("%-12s %8d\n", c.Table, c.Rows)
	}
	return 0
}
