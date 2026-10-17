#!/usr/bin/env python3
"""Load benchmark for a sheepdog server (Python today, Go next). Standard library only.

  python3 contract/bench.py --server "python3 sheepdog.py serve" [--n 400] [--live-db ~/.local/share/sheepdog/board.db]

Measures, one fresh server per database: hook POST latency (p50 / p95 / p99) and throughput at concurrency 1, 16 and 64, GET /api/state
p50 / p95, the board page GET, and the server's resident memory after the run (ps RSS, plus the macOS physical footprint, which leaves out shared library pages). Every request opens a new connection, like the curl
the hooks run. Databases: the fixture, and a COPY of the live board made with sqlite3's backup API from a read-only connection (the
live file is never written, the live server is never touched).
"""
import argparse
import concurrent.futures
import http.client
import json
import os
import sqlite3
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from harness import TOKEN, Server  # noqa: E402
import fixture  # noqa: E402

AUTH = {"Authorization": "Bearer " + TOKEN}
HOOK = json.dumps({"session_id": "bench", "cwd": "/work/alpha/f3", "hook_event_name": "PostToolUse", "agent_id": "a-run", "tool_name": "Bash",
                   "tool_input": {"command": "go test ./... -run TestSomething -count=1"}, "duration_ms": 812}).encode()


def one(port, method, path, body=None):
    t0 = time.perf_counter()
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    try:
        c.request(method, path, body, dict(AUTH, **({"Content-Type": "application/json"} if body else {})))
        r = c.getresponse()
        data = r.read()
        ok = r.status == 200
    except OSError:
        data, ok = b"", False
    finally:
        c.close()
    return time.perf_counter() - t0, ok, len(data)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(len(xs) * p / 100))] * 1000


def load(port, method, path, body, n, conc):
    t0 = time.perf_counter()
    with concurrent.futures.ThreadPoolExecutor(conc) as ex:
        res = list(ex.map(lambda _: one(port, method, path, body), range(n)))
    wall = time.perf_counter() - t0
    return {"n": n, "conc": conc, "p50": pct([r[0] for r in res], 50), "p95": pct([r[0] for r in res], 95), "p99": pct([r[0] for r in res], 99),
            "rps": n / wall, "errors": sum(1 for r in res if not r[1]), "bytes": res[-1][2]}


def row(label, m):
    return "%-34s %5d %5d %9.1f %9.1f %9.1f %9.0f %6d %9d" % (label, m["conc"], m["n"], m["p50"], m["p95"], m["p99"], m["rps"], m["errors"], m["bytes"])


def live_copy(src, dst):
    s = sqlite3.connect("file:%s?mode=ro" % src, uri=True)  # read-only: nothing is written to the live database
    d = sqlite3.connect(dst)
    s.backup(d)
    counts = {t: s.execute("SELECT COUNT(*) FROM " + t).fetchone()[0] for t in ("tasks", "agents", "events", "decisions")}
    s.close()
    d.close()
    return counts


def bench(cmd, db, label, n, lines):
    srv = Server(cmd, db)
    try:
        one(srv.port, "GET", "/api/state")  # warm up
        for conc in (1, 16, 64):
            lines.append(row("%s hook POST" % label, load(srv.port, "POST", "/api/hook", HOOK, n, conc)))
        lines.append(row("%s GET /api/state" % label, load(srv.port, "GET", "/api/state", None, max(50, n // 4), 1)))
        lines.append(row("%s GET /api/state?since=cursor" % label, load(srv.port, "GET", "/api/state?since=%d" % json.loads(http_get(srv.port, "/api/state"))["cursor"], None, max(50, n // 4), 1)))
        lines.append(row("%s GET / (page)" % label, load(srv.port, "GET", "/", None, max(50, n // 4), 1)))
        lines.append("%-34s %d KB" % (label + " server RSS after run", srv.rss_kb() or -1))
        fp = srv.footprint_kb()
        if fp:
            lines.append("%-34s %d KB" % (label + " physical footprint (macOS)", fp))
    finally:
        srv.stop()


def http_get(port, path):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=60)
    c.request("GET", path, headers=AUTH)
    return c.getresponse().read()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--server", default="python3 sheepdog.py serve")
    ap.add_argument("--n", type=int, default=400, help="requests per concurrency level (default 400)")
    ap.add_argument("--live-db", default=os.path.expanduser("~/.local/share/sheepdog/board.db"))
    a = ap.parse_args()
    lines = ["%-34s %5s %5s %9s %9s %9s %9s %6s %9s" % ("measure", "conc", "n", "p50 ms", "p95 ms", "p99 ms", "req/s", "errors", "resp B"), "-" * 100]
    with tempfile.TemporaryDirectory() as tmp:
        fx = os.path.join(tmp, "fixture.db")
        fixture.build(fx)
        bench(a.server, fx, "fixture", a.n, lines)
        if os.path.exists(a.live_db):
            lv = os.path.join(tmp, "live-copy.db")
            counts = live_copy(a.live_db, lv)
            lines.append("live copy: %d KB, rows %s" % (os.path.getsize(lv) // 1024, counts))
            bench(a.server, lv, "live-copy", a.n, lines)
        else:
            lines.append("no live database at %s: skipped" % a.live_db)
    print("server: %s   python: %s   %s" % (a.server, sys.version.split()[0], time.strftime("%Y-%m-%d")))
    print("\n".join(lines))


if __name__ == "__main__":
    main()
