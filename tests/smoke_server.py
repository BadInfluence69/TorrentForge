"""Boot the real HTTP server on a spare port, fetch the pages, shut it down.

This proves the app serves over a socket (not just through the test client)
and that the static assets resolve.  It exits by itself.

Run:  python tests/smoke_server.py
"""

from __future__ import annotations

import os
import socket
import sys
import threading
import time
import urllib.request

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from werkzeug.serving import make_server  # noqa: E402

import app as webapp  # noqa: E402

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [ok]   {label}")
    else:
        FAILED += 1
        print(f"  [FAIL] {label}" + (f" -> {detail}" if detail else ""))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def fetch(url: str) -> tuple[int, bytes, str]:
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.status, response.read(), response.headers.get("Content-Type", "")


def main() -> int:
    port = free_port()
    server = make_server("127.0.0.1", port, webapp.app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.4)
    base = f"http://127.0.0.1:{port}"
    print(f"\n=== live server on {base} ===")

    try:
        status, body, ctype = fetch(base + "/")
        check("index page served", status == 200 and b"TorrentForge" in body)
        check("index is html", ctype.startswith("text/html"), ctype)

        status, body, ctype = fetch(base + "/static/css/app.css")
        check("stylesheet served", status == 200 and b":root" in body)

        status, body, ctype = fetch(base + "/static/js/app.js")
        check("script served", status == 200 and b"createTorrent" in body)

        status, body, _ = fetch(base + "/api/bootstrap")
        check("bootstrap api served", status == 200 and b"drives" in body)

        status, body, _ = fetch(base + "/api/history")
        check("history api served", status == 200 and b"output_dir" in body)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        print("  server shut down cleanly")

    print(f"\n{PASSED} checks passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
