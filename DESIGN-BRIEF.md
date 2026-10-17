# Design brief: sheepdog

Design the brand and the full UI/UX of **sheepdog**, a tiny self-hosted web app that lets one developer manage, observe and track the work of AI coding agents.
UX comes first; visuals serve fast scanning and fast decisions.

## 1. Product in one paragraph

A developer (the "owner") gives work to an AI orchestrator as numbered tasks ("Task 12: add ingredient search to the recipe page").
The orchestrator splits each task into units and runs several AI sub-agents in parallel (analyse, build, test, docs), each in its own git branch.
sheepdog is the owner's cockpit: it shows every task, what stage it is in, which agents are working on it right now, what failed, what is waiting on the owner, and what was finished today.
It is fed automatically by hooks from the agent tool (every session start/stop, agent start/stop, command failure, notification) and by small CLI calls from the orchestrator's scripts (task status, next step, merge-request link).
It is personal, single-user today; later the owner opens it from a phone while agents run on a remote Linux server.

## 2. Hard constraints (non-negotiable)

- **Very light and fast.** The whole app is ONE Python file (stdlib HTTP server + SQLite) that serves ONE HTML page with inline CSS and vanilla JS. No frameworks, no build step, no CDN, no web fonts, no icon fonts, no images except inline SVG. Page weight today about 60 KB; keep the design implementable under ~80 KB total.
- **System font stack only** (`system-ui, -apple-system, "Segoe UI", Roboto, sans-serif`; a monospace stack for ids/paths). Icons: a small set of inline SVG (max ~15), 16px, 1.5px stroke.
- **Light theme first** (the owner's choice). Provide a dark variant only as optional tokens.
- **Updates by polling every 3 s** (no websockets); the UI must never flicker, jump or lose scroll/focus on refresh.
- **Responsive**: desktop 1440, laptop 1280, tablet 768, phone 400. No horizontal page scroll at any width.
- **Accessible**: WCAG AA contrast for all text and status colors, full keyboard use, visible focus ring, `Esc` closes overlays, reduced-motion respected.
- **Tone**: calm, technical, precise. No marketing language, no emoji.

## 3. Users and jobs to be done (priority order)

1. **"What needs me right now?"** Decisions to answer, reviews to do, merges to approve. Must be answerable in under 2 seconds on page load.
2. **"Is anything stuck or broken?"** Failed agents, failed commands, blocked tasks, agents that went silent (stale).
3. **"What is running and for how long?"** Live agents per task with elapsed time.
4. **"Where is task N?"** Find any task fast (keyboard search), see its stage, next step, branch, MR, history.
5. **"What did we finish today?"** And copy a standup summary.
6. **Light editing**: change a task's status, who it waits on, its next step; add a note; archive.

Not in scope for v1 (do not design controls for them): starting or stopping agents, merging, deploying, chat with agents.
Design so these could be added later as actions in the task panel.

## 4. Data model (what the UI can show)

**Task** (one per "Task N"):
- `key` (T1, T2, ... T44), `title`, `project` (recipe-app, shopping-list, docs-site...), `branch`, `worktree` (absolute path), `mr_url` (pull request, shown as "#101").
- `status`: planned, analysing, building, review, testing, blocked, done (7 states; "running" is an alias of building).
- `waiting`: me (the owner), agent, qc (tester), po (product owner), none. A task "needs you" when waiting = me, or when no waiting is set and status is review/testing. A done task never needs you.
- `next`: one line, the very next step, often with a URL ("Review http://localhost:3000/recipes?q=basil, then say merge").
- `notes`: timestamped list.
- `created_at` (start), `done_at` / `updated_at`; derived total time ("running 1h 23m" or "took 2h 05m").
- `archived`.
- Derived counts: agents running / done / failed / stale on this task; failed commands in the last 24 h.

**Agent** (a sub-agent run, linked to a task by its description "Task 12 ..."):
- `name` (e.g. "Build ingredient search"), `model` (sonnet/opus), `type` (analyst, developer, docs, general), `status` running / done / failed, plus derived **stale** (running but silent for 2 h), `started_at`, `ended_at`, `tool_uses`, `tokens`.

**Event** (the live feed): time, kind (SessionStart, SessionEnd, UserPromptSubmit, Stop, Notification, PreToolUse/PostToolUse on agents, SubagentStart/Stop, PostToolUseFailure, status change, note), one-line summary, linked task and agent. Failures carry the first error line (redacted).

Realistic volumes: 5-25 active tasks, 0-8 agents running at once, 100-2,000 events a day, 3-15 failures a day.

## 5. Current screens (to redesign, keep every capability)

1. **Header**: app name, counts ("2 need you · 3 running · 4 failed today"), live dot + "updated 14:02:31", search box, Alerts toggle (browser notification when a task newly needs you), tab title "(2) sheepdog".
2. **Needs you** strip: rows with key, title, next step, link chips (MR, URLs in next).
3. **Board (kanban)**: 7 status columns, colour-coded; cards show key, title, project · branch, MR chip, agent chips (running pulsing / done / failed / stale with duration), "N failed" chip, next step, "waiting: me" pill, "Started 10:42 · running 1h 23m"; each column scrolls internally after ~5.5 cards; empty columns collapse to a slim header; Done shows only the last 2 days with an "Earlier (n)" expander.
4. **Running now**: list of live agents (pulsing dot, task key, name, elapsed, model), stale ones greyed.
5. **Today**: tasks done today (with MR) + "Also changed today: T1, T3 ..." + **Copy standup** button (Yesterday / Today / Blockers text to clipboard).
6. **Live feed**: last 100 events, filter (all, failures, unlinked, per task), "all kinds" toggle (hides prompt/stop noise by default).
7. **Task side panel** (click a card): header (key, title, status pill, close X), Summary (status, waiting, next, project, branch, worktree, MR, Started, Total), Agents list, Edit (status, waiting, next, note), Timeline grouped by day; Esc and backdrop close; focus returns to the card.
8. **Sign-in page**: single token field ("The token is in SHEEPDOG_TOKEN on the server").

## 6. What I want from you

### A. Brand
- A name treatment for "sheepdog" (wordmark, no image assets: type + one inline SVG mark, works at 16px favicon and 24px header).
- A small, purposeful colour system as CSS custom properties (tokens), light first:
  - neutrals (background, surface, border, text primary/secondary/muted);
  - one accent;
  - **7 status hues** that stay distinguishable for colour-blind users (pair hue with icon or shape), each with tint (background), solid (pill), and text-on-tint values, all AA;
  - semantic: success (done agent), danger (failed), warning (waiting on me / stale), info.
- Type scale (4-5 sizes), spacing scale (4px base), radii, one shadow level, motion tokens (durations, easing; reduced-motion fallbacks).
- Voice for microcopy (labels, empty states, errors), e.g. "Nothing needs you." / "No agents running." / "Board offline, retrying".

### B. Information architecture and layout
- Propose the best page structure for the jobs in section 3 (priority 1 and 2 above the fold at 1280x800).
- Decide what is always visible vs collapsible; how the kanban, Running now, Today and feed share the space at each breakpoint; a phone layout that is genuinely usable one-handed (e.g. Needs you + Running first, board as status tabs or a single list grouped by status).
- Consider a compact "attention bar" summarising needs-you / failing / stale counts that act as filters.

### C. Components (with every state)
For each: anatomy, sizes, states (default, hover, focus, active, disabled, loading, empty, error, stale/offline), and responsive behaviour.
- Header / attention bar / search (with keyboard shortcut `/` and results that jump to a task) / live indicator (live, updating, offline with retry).
- Status pill, waiting pill, MR chip, link chip, agent chip (running/done/failed/stale), failure count chip, duration text.
- Task card (dense and comfortable densities), column header with count, collapsed empty column, "Earlier (n)" expander, in-column scroll affordance.
- Needs-you row, Running-now row, Today row, feed row (with kind badge and failure line), filter controls.
- Task panel (slide-over desktop, full-screen sheet on phone): header, Summary, Agents, Edit form, Timeline (day groups, show-all toggle), notes composer, archive with confirmation.
- Toasts (saved, copied, error), confirmation dialog, sign-in card.

### D. Interaction and UX details
- Keyboard map: `/` search, `j/k` or arrows to move between cards, `Enter` open, `Esc` close, `n` focus Needs you, `?` shortcuts help.
- Live updates without disruption: how a card that changes status animates (or not) between columns; how new feed items appear; never steal focus or scroll.
- Time display rules (relative vs absolute, "running" ticking every second vs minute).
- Empty states for each section, first-run state (no tasks yet, with the CLI one-liner), offline state, auth expired.
- Notifications: what triggers a desktop alert (a task newly needs you, an agent fails) and how the user opts in (no permission prompt on load).
- Copy standup: preview before copy? confirmation.
- Density toggle (comfortable/compact) if it earns its place.

### E. Deliverables
1. Design tokens as a CSS `:root` block (and optional dark block).
2. Annotated layouts at 1440, 1280, 768 and 400 widths, including the open task panel and the phone sheet.
3. A component sheet with all states.
4. The 15 inline SVG icons needed (single path where possible).
5. Microcopy list (every label, empty state, error, tooltip).
6. A short rationale: why this layout serves "what needs me" and "what is broken" first.
7. Implementation notes for a vanilla HTML/CSS/JS single page: class naming, which elements re-render every 3 s, how to keep scroll/focus stable, and a weight budget per part.

## 7. Sample data to design with

Tasks:
- T31 "Add ingredient search", recipe-app, feat/ingredient-search, status testing, waiting me, next "Review http://localhost:3000/recipes?q=basil, then say merge", started 13:05, running 4h 10m, agents 1 done.
- T33 "Fix unit conversion rounding", recipe-app, done, PR #101, took 2h 05m, agents 4 done, 2 failed commands.
- T44 "Cut release 0.9", recipe-app, building, waiting agent, next "Changelog, tag, build, smoke test", running 12m.
- T35 "Dark mode for the recipe card", done, #102, took 48m.
- T27 "Shopping list: group items by aisle", shopping-list, testing, waiting me, next "Review the grouped list view".
- T9 "Write the contributor guide", docs-site, done.
- T19 "Research offline storage options", analysing, waiting agent.
- One blocked task: T36 "Serve recipe photos from an image CDN", blocked, waiting po, next "Maintainer confirms the CDN account".

Running agents: "Task 44 Release 0.9" (opus, 12m), "Task 27 Aisle grouping" (sonnet, 3m), one stale "Task 31 Measure search latency" (sonnet, idle 2h 14m).
Failures today: 4 (e.g. "Bash failed: rg -n ... (exit 1)", "tsc: TS2322 in converter.test.ts").
Feed lines: "14:02 SubagentStop T35 Dark theme tokens done (sonnet, 3m 22s)", "13:58 status T33 building -> done", "13:41 fail T33 tsc TS2322 ...".
