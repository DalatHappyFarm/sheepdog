# contract/

A safety net for porting the server: one scripted scenario, run against any implementation, compared with golden files recorded from the Python server.

| File | What |
| --- | --- |
| `run.py` | the contract test |
| `fixture.py` | builds the fixture database with the server's own code |
| `bench.py` | the load benchmark |
| `harness.py` | starts and stops a server process, shared by the other scripts |
| `pages.py` | writes the four HTML pages a server sends, to a folder |
| `golden/` | recorded results: `http.json`, `cli.json`, `db.json`, `alt-server.json`, `migrate.json`, `allow.json`, `privacy.json` |
| `rewrite-cases.json` | inputs and answers of the path and name rewrite, checked by both the Go and the Python tests |
| `baseline-pages/` | the four pages as the Python server sent them before `web/` existed |
| `bench-python.txt` | benchmark numbers for the Python server |

## Running

```
python3 contract/run.py                                       # Python server, compare with golden/, exit 1 on any difference
python3 contract/run.py --record                              # rewrite golden/ (only ever against the Python server)
python3 contract/run.py --server "bin/sheepdog serve" --cli bin/sheepdog   # the Go binary (go build -o bin/sheepdog ./cmd/sheepdog)
python3 contract/bench.py --server "bin/sheepdog serve"                  # numbers: bench-python.txt and bench-go.txt
```

The server command is the interface: the runner appends `--host 127.0.0.1 --port P --db FILE` (and `--no-auth` in one stage) and puts `SHEEPDOG_TOKEN`, `SHEEPDOG_TOOL_EVENTS` and `TZ=UTC` in the environment, the way `sheepdog.py serve` takes them.
Wait for readiness is a TCP connect.
The CLI (`task`, `agent`, `hook`, `brief`, `wait`, `done`, `archive`) is always a client of the server under test; `--cli` picks it, default `python3 sheepdog.py`.
The runner starts five servers, one after the other, each with its own database in a temp folder: the fixture (main scenario), an empty database with auth off and `SHEEPDOG_TOOL_EVENTS=0` (`alt-server.json`), an old-schema database (`migrate.json`), a server with an allowlist (`allow.json`) and one with a fake user, home and host (`privacy.json`).
Nothing touches port 8787 or the live database.

## What is recorded

Per HTTP step: request line, status, `Content-Type`, the headers `Cache-Control`, `X-Content-Type-Options`, `Location`, `Set-Cookie`, `Retry-After` (when present), and the body.
Per CLI step: arguments, stdin (first 80 characters), exit code, stdout, stderr.
JSON bodies are compared as parsed JSON, so key order and whitespace do not matter.
HTML pages and PNGs are recorded as `{sha256, bytes}`, and the four HTML pages are also compared with `baseline-pages/` byte for byte, with the first differing offset printed.
Other bodies (plain text, the empty hook reply) are recorded as text.
`Date`, `Server`, `Content-Length` and the other headers are not recorded.
After the scenario the server is stopped and the database is dumped: every `sqlite_master` entry with its SQL (whitespace collapsed to single spaces) and every table, rows in rowid order, JSON text columns (`notes`, `data`, `brief_options`) parsed.

## Normalization rules

Nothing else is changed.

1. Timestamps: any value under the keys `ts`, `created_at`, `updated_at`, `started_at`, `ended_at`, `last_event_at`, `done_at`, `brief_at`, `delivered_at`, `end_at` that is an integer becomes `"<ts>"`.
   `null` stays `null`, so "set" versus "not set" still compares.
   Applies to every JSON body, CLI output, and database row, at any depth (task notes carry `ts` too).
2. Revisions: an integer under `rev` or `cursor` becomes `"<rev>"`.
   Row ids (events, decisions) are not normalized: the fixture and the scenario are deterministic, so the Go server must produce the same ids.
3. Event `data`: the events API returns `data` as a JSON-encoded string.
   When it parses to an object, it is compared as that object (key order and spacing inside the string are not part of the contract).
   Same for the `data` column in the database dump.
4. JSON syntax errors: the text of the 400 for a request body that is not valid JSON (`bad-json`, `bad-json-hook`) is Python's own parser message, so `error` is replaced by `"<json syntax error>"` in those two steps.
   Status and shape still compare.
   Every other error text is the server's own and compares exactly.
5. Temp paths: the temp folder in CLI arguments is written `<tmp>`.

The fixture is built relative to the build moment (the server judges stale agents and the 24 hour failure window against the real clock), through a patched clock, so the shape is the same on every run and only the timestamps differ, which rule 1 hides.
The standup steps use `?off=0` or the server's default zone with `TZ=UTC`, and the fixture's "last working day" task is dated with the same rule the server uses, so the answers do not depend on the weekday.
The runner aborts if the UTC date changes while it runs.

## Scenario coverage

Every route of the `ROUTES` table (the `/architecture` page lists the same), plus the CLI, the auth cases and the error cases:

- Pages and auth: board page, sign-in page (with and without `Accept: text/html`), no token, bad token, lowercase `bearer`, `/?token=` sign-in (302 and cookie), wrong `?token=`, valid and invalid cookie, POST without token, `/login` (wrong, JSON body, right, throttled after five failures, right token while throttled), `/architecture` with and without token.
- `/brand/`: the four files without a token, with a token, an unknown name, a real file that is not on the list, three traversal forms, an empty name.
- Reads: `/api/state` (full, empty delta, garbage and negative `since`, delta after changes), `/api/standup` (`off=0`, default zone, garbage `off`), `/api/task/<key>/events` (fixture, hook-created, unknown, escaped key).
- Wrong method or route: GET on a POST route, POST on a GET route, POST on `/`, unknown paths.
- `/api/task`: create, update, `running` alias, same status, done, reopen, redaction of `next` and notes, clipping (title 500, note 20000), clearing `waiting`, every validation error, parent set, self, missing, one level only, parent with children, clear, empty body.
- `/api/agent`: create, update, `task_key` alias, no task, failed, long id and name clipped, bad status, missing id, unknown task.
- `/api/archive`: ok, unknown, missing key.
- `/api/task/<key>/brief`: ok, on a done task (status kept), empty, too many options, options not a list, bad option id, option without label, duplicate, relative cwd, unknown task, clipping.
- `/api/task/<key>/decision`: option, reply, approve (no decision row), unknown option, empty reply, bad or missing action, reply over 20000 characters (400), reply of exactly 20000, unknown task, task without a brief folder.
- `/api/decisions/wait`: relative or missing cwd, claim under a folder, nothing left, negative timeout, the `waiter` flag before and after a waiter was seen, and a real long-poll that is started first and delivered to by a decision made afterwards.
- `/api/decision/<id>/done`: ok, again, unknown, a fixture decision that was delivered.
- `/api/hook`, raw payloads: SessionStart (with and without source), SessionEnd, UserPromptSubmit (no decisions, no cwd, from the parent folder, from a sibling folder, from a subfolder that receives the decision, then empty), Notification, Stop, PreToolUse (Agent, repeated Agent, ordinary tool ignored, SendMessage with and without a `Task N` prefix), SubagentStart (adopting the placeholder, without an id), PostToolUse (Bash with token redaction and tool count, Edit, Grep, Glob, a tool with no detail, Agent async launch, Agent completed, Agent without a Pre), PostToolUseFailure (Bash with 60 lines of output and secrets, exit code from the error text, `exit_code` at top level, string tool response, huge output, the Agent tool as `Task`), SubagentStop, an unknown event, malformed shapes (tool input not an object, agent id not a string), missing event name, empty body.
- Limits: a body over 1 MB (413), a 1 MB hook body (accepted), a hook body over 8 MB (413), bad JSON, JSON array, JSON scalar.
- CLI: `task` (create, `--status running`, `--parent`, clear parent, server error), `agent` (create, done, server error), `brief` (three options and `--primary`, `--file -` from stdin, bad `--primary`, too many options, unknown task), `wait` (delivering a decision made while it waits), `done` (ok, unknown), `archive` (ok, unknown), `hook` (UserPromptSubmit delivering a decision then silent, SessionStart, PostToolUse, PostToolUseFailure with a huge error, Agent Pre and Post, ordinary PreToolUse ignored, SubagentStop, garbage stdin, empty stdin: always exit 0).
- Alternate config (`alt-server.json`): no auth (state, board, `?token=`, `/login`, task, all open), `SHEEPDOG_TOOL_EVENTS=0` (ordinary PostToolUse skipped, ordinary failure and the Agent tool still logged), and the schema of a brand new database.
- Allowlist (`allow.json`): a server started with `SHEEPDOG_ALLOW` and `--allow`; outside hooks are dropped with the normal empty reply and change nothing, inside hooks are stored, a prompt from outside never delivers, API and CLI calls are not filtered.
- Privacy (`privacy.json`): a server started with a fake user (`jdoe`), home, host name, temp dir and allowed root; every write path (tasks, agents, briefs, decisions, every hook event type) gets raw paths and names, and the run fails if anything identifying shows up in an answer or in the database. Delivery by folder (a prompt from a sub-folder, the waiter) is checked in the same stage.
- Migration (`migrate.json`): an old `tasks` table gets its added columns, `done_at` is backfilled from `updated_at`, and the final schema and rows are compared.

Not covered, and why:

- `PUT`, `DELETE`, `HEAD`, `OPTIONS`: Python's `http.server` answers 501 with its own HTML error page; nothing depends on it.
- Malformed HTTP (bad `Content-Length`, truncated body, slow clients): the Python server relies on the standard library's behaviour and a 10 second socket timeout, which the scenario cannot pin without being slow or flaky.
- `GET /api/decisions/wait` with a non-numeric `timeout`: it waits the default 25 seconds, too slow for the suite.
- The `wait` hang-up race (a waiter that disconnects before the claim) and the hourly prune: not reachable from a short scripted run.
- Server startup errors (`SHEEPDOG_TOKEN` missing, port in use) and the old `BOARD_*` environment names.
- A CLI that cannot reach the server (the message carries an operating system error text).
- `standup` with a non-zero `off`: the answer depends on the time of day the test runs, so only the unit tests in `test_sheepdog.py` cover it (they fix the clock).
