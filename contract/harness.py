"""Shared by run.py and bench.py: start and stop a sheepdog server process, whichever implementation it is.

The server command is the same for every implementation (Python today, Go next): `<--server words> --host 127.0.0.1 --port P --db FILE [--no-auth] [--allow DIR ...]`,
with SHEEPDOG_TOKEN (and optionally SHEEPDOG_TOOL_EVENTS, TZ) in the environment, exactly as `sheepdog.py serve` takes them.
"""
import os
import shlex
import socket
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "contract-token-0123456789"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    def __init__(self, cmd, db, token=TOKEN, no_auth=False, env=None, port=None, args=()):
        self.port = port or free_port()
        self.url = "http://127.0.0.1:%d" % self.port
        self.db = db
        e = dict(os.environ, TZ="UTC", **(env or {}))
        e.pop("SHEEPDOG_TOKEN", None)
        e.pop("BOARD_TOKEN", None)
        if token and not no_auth:
            e["SHEEPDOG_TOKEN"] = token
        args = shlex.split(cmd) + ["--host", "127.0.0.1", "--port", str(self.port), "--db", db] + (["--no-auth"] if no_auth else []) + list(args)
        self.p = subprocess.Popen(args, cwd=ROOT, env=e, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        end = time.time() + 20
        while True:
            if self.p.poll() is not None:
                raise RuntimeError("server exited with %s before it was ready: %s" % (self.p.returncode, args))
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return
            except OSError:
                if time.time() > end:
                    self.stop()
                    raise RuntimeError("server not ready after 20 s: %s" % args)
                time.sleep(0.05)

    def rss_kb(self):
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(self.p.pid)], capture_output=True, text=True).stdout.strip()
        return int(out) if out else None

    def footprint_kb(self):
        """macOS physical footprint (what Activity Monitor shows: the process's own memory, without shared library pages); None elsewhere."""
        try:
            out = subprocess.run(["vmmap", "-summary", str(self.p.pid)], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.TimeoutExpired):
            return None
        for line in out.splitlines():
            if line.startswith("Physical footprint:"):
                v = line.split(":", 1)[1].strip()
                return int(float(v[:-1]) * {"K": 1, "M": 1024, "G": 1024 * 1024}[v[-1]])
        return None

    def stop(self):
        if self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(10)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
