#!/usr/bin/env python3
"""Fetch every HTML page a sheepdog server sends and write the raw bodies to a folder.

  python3 contract/pages.py OUTDIR [--server "python3 sheepdog.py serve"]

Used to prove that moving the page strings into web/ changed no byte, and reusable against the Go server.
"""
import argparse
import http.client
import os
import shlex
import socket
import subprocess
import sys
import tempfile
import time

TOKEN = "contract-token"


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("--server", default="python3 sheepdog.py serve")
    a = ap.parse_args()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    port = free_port()
    with tempfile.TemporaryDirectory() as tmp:
        env = dict(os.environ, SHEEPDOG_TOKEN=TOKEN, SHEEPDOG_DB=os.path.join(tmp, "p.db"))
        p = subprocess.Popen(shlex.split(a.server) + ["--port", str(port)], cwd=here, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(100):
                try:
                    socket.create_connection(("127.0.0.1", port), 0.2).close()
                    break
                except OSError:
                    time.sleep(0.1)

            def req(method, path, body=None, headers=None):
                c = http.client.HTTPConnection("127.0.0.1", port)
                c.request(method, path, body, headers or {})
                return c.getresponse().read()

            auth = {"Authorization": "Bearer " + TOKEN}
            pages = {
                "board.html": req("GET", "/", headers=auth),
                "architecture.html": req("GET", "/architecture", headers=auth),
                "login.html": req("GET", "/", headers={"Accept": "text/html"}),
                "login-error.html": req("POST", "/login", b"token=wrong", {"Content-Type": "application/x-www-form-urlencoded", "Accept": "text/html"}),
            }
            os.makedirs(a.out, exist_ok=True)
            for name, body in pages.items():
                with open(os.path.join(a.out, name), "wb") as f:
                    f.write(body)
                print("%-20s %7d bytes" % (name, len(body)))
        finally:
            p.terminate()
            p.wait()


if __name__ == "__main__":
    sys.exit(main())
