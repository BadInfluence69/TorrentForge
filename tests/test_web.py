"""End-to-end exercise of the web API using Flask's test client.

Drives the exact same routes the browser uses: bootstrap -> browse -> info ->
create -> poll -> download -> history -> delete.  No server is left running.

Run:  python tests/test_web.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as webapp  # noqa: E402
from torrentlib.bencode import decode  # noqa: E402

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


def make_payload(root: str) -> None:
    os.makedirs(os.path.join(root, "part1"), exist_ok=True)
    with open(os.path.join(root, "part1", "a.bin"), "wb") as handle:
        handle.write(os.urandom(400_000))
    with open(os.path.join(root, "b.bin"), "wb") as handle:
        handle.write(os.urandom(150_000))
    with open(os.path.join(root, "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("hello from TorrentForge\n")


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="torrentforge-web-")
    payload_dir = os.path.join(tmp, "WebTest Release")
    os.makedirs(payload_dir)
    make_payload(payload_dir)

    client = webapp.app.test_client()
    created_name = ""

    try:
        print("\n=== bootstrap ===")
        res = client.get("/api/bootstrap")
        check("GET /api/bootstrap is 200", res.status_code == 200, str(res.status_code))
        boot = res.get_json()
        check("bootstrap reports drives", bool(boot.get("drives")))
        check("bootstrap reports an output folder", bool(boot.get("output_dir")))

        print("\n=== index page ===")
        page = client.get("/")
        check("GET / is 200", page.status_code == 200)
        body = page.get_data(as_text=True)
        check("page has the Create Torrent button", "Create Torrent" in body)
        check("page has the download button", "Download Torrent File" in body)

        print("\n=== browsing ===")
        res = client.get("/api/browse", query_string={"path": tmp})
        check("GET /api/browse is 200", res.status_code == 200)
        listing = res.get_json()
        check(
            "browse sees the sample folder",
            any(d["name"] == "WebTest Release" for d in listing["dirs"]),
        )
        check("browse returns breadcrumbs", len(listing["crumbs"]) >= 1)
        bad = client.get("/api/browse", query_string={"path": os.path.join(tmp, "nope")})
        check("browsing a missing folder returns 404", bad.status_code == 404)

        print("\n=== path info ===")
        res = client.get("/api/info", query_string={"path": payload_dir})
        info = res.get_json()
        check("info counts the 3 files", info["file_count"] == 3, str(info))
        expected = sum(
            os.path.getsize(os.path.join(dirpath, f))
            for dirpath, _dirs, names in os.walk(payload_dir)
            for f in names
        )
        check(
            "info totals the bytes on disk",
            info["total_size"] == expected,
            f"{info['total_size']} vs {expected}",
        )
        check("info suggests a piece length", info["suggested_piece_length"] >= 262144)

        print("\n=== create ===")
        res = client.post(
            "/api/create",
            json={
                "source": payload_dir,
                "trackers": "udp://tracker.example.org:1337/announce\nhttp://t2.example/announce",
                "web_seeds": "https://example.com/files/",
                "comment": "made by the web test",
                "source_tag": "SELFTEST",
                "private": True,
                "version": "hybrid",
                "piece_length": 262144,
            },
        )
        check("POST /api/create is 200", res.status_code == 200, res.get_data(as_text=True))
        job_id = res.get_json()["job"]

        state = {}
        deadline = time.time() + 60
        while time.time() < deadline:
            state = client.get(f"/api/job/{job_id}").get_json()
            if state["state"] in ("done", "error", "cancelled"):
                break
            time.sleep(0.1)
        check("job finished", state.get("state") == "done", state.get("error", ""))
        result = state.get("result") or {}
        check("result has a v1 info-hash", len(result.get("infohash_v1") or "") == 40)
        check("result has a v2 info-hash", len(result.get("infohash_v2") or "") == 64)
        check("result lists 3 files", result.get("file_count") == 3)
        check("magnet link built", str(result.get("magnet", "")).startswith("magnet:?"))

        print("\n=== download ===")
        res = client.get(f"/api/download/{job_id}")
        check("GET /api/download is 200", res.status_code == 200)
        check(
            "download is served as an attachment",
            "attachment" in res.headers.get("Content-Disposition", ""),
            res.headers.get("Content-Disposition", ""),
        )
        check(
            "mime type is application/x-bittorrent",
            res.headers.get("Content-Type", "").startswith("application/x-bittorrent"),
        )
        data = res.get_data()
        meta = decode(data)
        check("downloaded file is valid bencode", isinstance(meta, dict))
        check("announce url stored", meta[b"announce"] == b"udp://tracker.example.org:1337/announce")
        check("announce-list has both trackers", len(meta[b"announce-list"]) == 2)
        check("web seed stored", meta[b"url-list"] == b"https://example.com/files/")
        check("comment stored", meta[b"comment"] == b"made by the web test")
        check("private flag stored", meta[b"info"].get(b"private") == 1)
        check("source tag stored", meta[b"info"].get(b"source") == b"SELFTEST")
        check("piece layers present", b"piece layers" in meta)
        created_name = state["filename"]

        print("\n=== history ===")
        hist = client.get("/api/history").get_json()
        check("new torrent shows in history", any(i["name"] == created_name for i in hist["items"]))
        res = client.get(f"/api/history/{created_name}")
        check("history download works", res.status_code == 200 and res.get_data() == data)

        print("\n=== error handling ===")
        res = client.post("/api/create", json={"source": ""})
        check("empty source is rejected", res.status_code == 400)
        res = client.post("/api/create", json={"source": os.path.join(tmp, "ghost")})
        check("missing source is rejected", res.status_code == 404)
        res = client.get("/api/job/deadbeef")
        check("unknown job is 404", res.status_code == 404)
        res = client.get("/api/download/deadbeef")
        check("download of unknown job is 404", res.status_code == 404)

        print("\n=== cleanup ===")
        res = client.delete(f"/api/history/{created_name}")
        check("history delete works", res.status_code == 200)
        hist = client.get("/api/history").get_json()
        check(
            "deleted torrent is gone",
            not any(i["name"] == created_name for i in hist["items"]),
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if created_name:
            leftover = os.path.join(webapp.OUTPUT_DIR, created_name)
            if os.path.exists(leftover):
                os.remove(leftover)

    print(f"\n{PASSED} checks passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
