#!/usr/bin/env python3
"""Build the fixture database with the Python server's own code (no hand SQL).

  python3 contract/fixture.py OUT.db

Every timestamp is relative to the build moment (the server judges "stale" and "last 24 h" against the real clock), driven
through a patched clock, so the data is the same shape on every build; run.py normalizes the timestamps themselves.

Contents: tasks in every status (planned analysing building review testing done blocked, one archived), a parent with three
sub-tasks, agents running / stale / done / failed / unlinked, hook events of every kind, decisions pending / delivered / done.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import sheepdog  # noqa: E402

BRIEF = "# Review T-F4\n\nThe change is ready.\n\n- [x] tests\n- [ ] docs\n\n| a | b |\n|---|---|\n| 1 | 2 |\n"
OPTS = [{"id": "merge", "label": "Approve and merge", "hint": "ships it", "primary": True}, {"id": "hold", "label": "Hold", "hint": ""},
        {"id": "rework", "label": "Send back", "hint": "needs changes"}]


def prev_workday_noon(now):
    p = now // 86400 - 1
    while (p + 3) % 7 >= 5:  # same rule as DB.standup: epoch day 0 was a Thursday, skip Sat/Sun
        p -= 1
    return p * 86400 + 43200


def build(path, now=None):
    now = int(now or time.time())
    clock = [now]
    real = time.time
    time.time = lambda: clock[0]
    at = lambda secs_ago: clock.__setitem__(0, now - secs_ago)
    db = sheepdog.DB(path)
    try:
        clock[0] = prev_workday_noon(now)
        db.task_upsert({"key": "F6", "title": "Finished last working day", "status": "done", "project": "alpha", "mr_url": "https://gitlab.example.com/g/p/-/merge_requests/7"})
        at(4 * 3600)
        db.task_upsert({"key": "F1", "title": "Planned work", "project": "alpha", "next": "Pick it up", "waiting": "none", "note": "first planning note"})
        db.task_upsert({"key": "F2", "title": "Analysing the problem", "status": "analysing", "project": "alpha"})
        db.task_upsert({"key": "F3", "title": "Building the thing", "status": "building", "project": "alpha", "branch": "feat/f3", "worktree": "/work/alpha/f3",
                        "mr_url": "https://gitlab.example.com/g/p/-/merge_requests/42", "waiting": "agent", "next": "Write tests"})
        db.task_upsert({"key": "F4", "title": "Review me", "status": "review", "project": "alpha", "worktree": "/work/alpha/f4"})
        db.task_upsert({"key": "F5", "title": "Testing, nobody waits", "status": "testing", "project": "beta"})
        db.task_upsert({"key": "F7", "title": "Done a moment ago", "project": "beta"})
        db.task_upsert({"key": "F8", "title": "Blocked on a vendor", "status": "blocked", "project": "beta", "waiting": "po", "note": "waiting for the vendor key token=hunter2hunter2"})
        db.task_upsert({"key": "F9", "title": "Archived work", "status": "done", "project": "beta"})
        db.archive("F9")
        db.task_upsert({"key": "F10", "title": "Reply delivered, not acknowledged", "status": "review", "project": "beta"})
        db.task_upsert({"key": "F11", "title": "Decision acknowledged", "status": "review", "project": "beta"})
        db.task_upsert({"key": "F12", "title": "Approved without a session", "status": "review", "project": "beta"})
        db.task_upsert({"key": "P1", "title": "A parent task", "project": "alpha"})
        db.task_upsert({"key": "C1", "title": "Sub-task building", "status": "building", "parent": "P1"})
        db.task_upsert({"key": "C2", "title": "Sub-task done", "status": "done", "parent": "P1"})
        db.task_upsert({"key": "C3", "title": "Sub-task in review", "status": "review", "waiting": "me", "parent": "P1"})

        at(3 * 3600 + 1800)
        db.agent_upsert({"id": "a-stale", "task": "F3", "name": "Task 3 stale worker", "model": "sonnet", "status": "running"})  # silent for 3.5 h: shown stale
        at(3 * 3600)
        db.agent_upsert({"id": "a-done", "task": "F2", "name": "Task 2 analyst", "model": "opus", "status": "done", "tokens": 12345})
        db.agent_upsert({"id": "a-fail", "task": "F5", "name": "Task 5 tester", "model": "sonnet", "status": "failed"})

        at(2 * 3600)
        db.set_brief("F4", {"brief": BRIEF, "options": OPTS, "cwd": "/work/alpha/f4"})
        at(2 * 3600 - 600)
        db.decide("F4", {"action": "option", "option": "merge"})  # pending
        at(1800 * 3)
        db.set_brief("F10", {"brief": "## F10\nplease look", "options": OPTS[:2], "cwd": "/work/beta/f10"})
        db.decide("F10", {"action": "reply", "text": "please rebase first"})
        db.claim("/work/beta", "waiter")  # delivered, not acknowledged (F4 is under /work/alpha: stays pending)
        db.set_brief("F11", {"brief": "## F11\nship?", "options": OPTS[:1], "cwd": "/work/beta/f11"})
        db.decide("F11", {"action": "option", "option": "merge"})
        got = db.claim("/work/beta/f11", "hook:s-old", hook=True)
        db.finish_decision(got[0]["id"])  # done
        db.set_brief("F12", {"brief": "## F12\nok?", "options": []})
        db.decide("F12", {"action": "approve"})  # no cwd on the brief: no decision row
        db.task_upsert({"key": "F13", "title": "Waiting for the developer", "project": "alpha"})
        db.set_brief("F13", {"brief": "## F13\nyour call", "options": OPTS, "cwd": "/work/alpha/f13"})  # a review card nobody answered yet

        at(1800)
        db.task_upsert({"key": "F7", "title": "Done a moment ago", "status": "done"})
        sid, base = "sess-fixture", {"session_id": "sess-fixture", "cwd": "/work/alpha/f3"}
        for p in ({"hook_event_name": "SessionStart", "source": "startup", "model": "claude-opus"},
                  {"hook_event_name": "UserPromptSubmit", "prompt": "please continue with the build token=abc12345 now"},
                  {"hook_event_name": "Notification", "message": "Claude needs your permission", "notification_type": "permission_prompt"},
                  {"hook_event_name": "PreToolUse", "tool_name": "Agent", "tool_use_id": "tu-fx1", "tool_input": {"description": "Task 3 build helper", "subagent_type": "general-purpose", "model": "sonnet"}},
                  {"hook_event_name": "SubagentStart", "agent_id": "a-run", "agent_type": "general-purpose"},
                  {"hook_event_name": "PostToolUse", "agent_id": "a-run", "tool_name": "Bash", "tool_input": {"command": "make test"}},
                  {"hook_event_name": "PostToolUse", "agent_id": "a-run", "tool_name": "Read", "tool_input": {"file_path": "/work/alpha/f3/main.go"}},
                  {"hook_event_name": "PostToolUseFailure", "agent_id": "a-run", "tool_name": "Bash", "tool_input": {"command": "make lint"},
                   "error": "Exit code 2\nlint failed: password=swordfish1", "tool_response": {"stderr": "line1\nline2 secret=s3cr3tvalue", "exit_code": 2}, "duration_ms": 1530},
                  {"hook_event_name": "PostToolUseFailure", "cwd": "/elsewhere", "tool_name": "Bash", "tool_input": {"command": "false"}, "error": "Exit code 1"}):
            db.ingest(dict(base, **p))
        at(600)
        db.agent_upsert({"id": "a-free", "name": "unlinked helper", "model": "haiku", "status": "running"})
        db.ingest({"hook_event_name": "SubagentStop", "session_id": sid, "cwd": "/work/alpha/f3", "agent_id": "a-done", "agent_type": "general-purpose"})
        db.ingest({"hook_event_name": "Stop", "session_id": sid, "cwd": "/work/alpha/f3"})
        db.c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        time.time = real
        db.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    if os.path.exists(sys.argv[1]):
        sys.exit("%s exists" % sys.argv[1])
    build(sys.argv[1])
