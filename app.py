"""TorrentForge - create .torrent files from a browser, with no torrent client.

Run it:      python app.py           (then open http://127.0.0.1:8777)
Options:     python app.py --port 9000 --host 0.0.0.0 --no-browser
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
    send_from_directory,
)

from torrentlib import __version__
from torrentlib.browse import home_dir, human_size, list_dir, list_drives, path_info
from torrentlib.creator import (
    TorrentError,
    auto_piece_length,
    create_torrent,
    valid_piece_lengths,
)

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
CONFIG_PATH = os.path.join(BASE_DIR, "settings.json")
PICKER = os.path.join(BASE_DIR, "picker.py")

os.makedirs(OUTPUT_DIR, exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False


# --------------------------------------------------------------------------- #
# Settings (remembers your tracker list between runs)
# --------------------------------------------------------------------------- #
DEFAULT_SETTINGS = {
    "trackers": "",
    "web_seeds": "",
    "comment": "",
    "source": "",
    "private": False,
    "version": "hybrid",
    "piece_length": 0,
    "include_hidden": False,
    "last_dir": "",
}


def load_settings() -> dict:
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        if isinstance(stored, dict):
            data.update({k: v for k, v in stored.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    return data


def save_settings(values: dict) -> None:
    data = load_settings()
    data.update({k: v for k, v in values.items() if k in DEFAULT_SETTINGS})
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Job manager
# --------------------------------------------------------------------------- #
class Job:
    def __init__(self, job_id: str, source: str, options: dict):
        self.id = job_id
        self.source = source
        self.options = options
        self.state = "queued"           # queued | hashing | done | error | cancelled
        self.done_bytes = 0
        self.total_bytes = 0
        self.current = ""
        self.message = "waiting to start"
        self.error = ""
        self.result: dict | None = None
        self.filename = ""
        self.started = time.time()
        self.finished = 0.0
        self.cancel = threading.Event()
        self.lock = threading.Lock()

    def snapshot(self) -> dict:
        with self.lock:
            elapsed = (self.finished or time.time()) - self.started
            percent = (self.done_bytes / self.total_bytes * 100) if self.total_bytes else 0.0
            rate = self.done_bytes / elapsed if elapsed > 0.2 else 0.0
            eta = ((self.total_bytes - self.done_bytes) / rate) if rate > 0 else 0.0
            return {
                "id": self.id,
                "state": self.state,
                "source": self.source,
                "percent": round(percent, 2),
                "done_bytes": self.done_bytes,
                "total_bytes": self.total_bytes,
                "done_human": human_size(self.done_bytes),
                "total_human": human_size(self.total_bytes),
                "rate": rate,
                "rate_human": human_size(rate) + "/s" if rate else "",
                "eta": int(eta) if self.state == "hashing" else 0,
                "current": self.current,
                "message": self.message,
                "error": self.error,
                "elapsed": round(elapsed, 2),
                "filename": self.filename,
                "result": self.result,
            }


JOBS: dict[str, Job] = {}
JOBS_LOCK = threading.Lock()


def _unique_output_path(name: str) -> str:
    safe = "".join(c for c in name if c not in '<>:"/\\|?*').strip() or "torrent"
    candidate = os.path.join(OUTPUT_DIR, safe + ".torrent")
    if not os.path.exists(candidate):
        return candidate
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return os.path.join(OUTPUT_DIR, f"{safe}-{stamp}.torrent")


def run_job(job: Job) -> None:
    opts = job.options
    last_push = [0.0]

    def progress(done: int, total: int, current: str) -> None:
        now = time.time()
        with job.lock:
            job.done_bytes = done
            job.total_bytes = total
            job.current = current
        if now - last_push[0] > 0.1:
            last_push[0] = now

    try:
        with job.lock:
            job.state = "hashing"
            job.message = "reading and hashing files"
        info = path_info(opts["source"], include_hidden=opts["include_hidden"])
        with job.lock:
            job.total_bytes = info["total_size"]

        result = create_torrent(
            opts["source"],
            trackers=opts["trackers"],
            web_seeds=opts["web_seeds"],
            comment=opts["comment"],
            created_by=opts["created_by"],
            source=opts["source_tag"],
            private=opts["private"],
            piece_length=opts["piece_length"] or None,
            version=opts["version"],
            include_hidden=opts["include_hidden"],
            name_override=opts["name_override"],
            progress=progress,
            cancelled=job.cancel.is_set,
        )

        out_path = _unique_output_path(result.name)
        with open(out_path, "wb") as handle:
            handle.write(result.data)

        with job.lock:
            job.state = "done"
            job.message = "torrent ready"
            job.filename = os.path.basename(out_path)
            job.result = result.summary()
            job.result["output_path"] = out_path
            job.result["total_human"] = human_size(result.total_size)
            job.result["piece_human"] = human_size(result.piece_length)
            job.done_bytes = result.total_size
            job.total_bytes = max(result.total_size, 1)
            job.finished = time.time()
    except TorrentError as exc:
        with job.lock:
            cancelled = job.cancel.is_set()
            job.state = "cancelled" if cancelled else "error"
            job.error = "cancelled by user" if cancelled else str(exc)
            job.message = job.error
            job.finished = time.time()
    except Exception as exc:  # noqa: BLE001 - surface anything else in the UI
        with job.lock:
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = job.error
            job.finished = time.time()


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template(
        "index.html",
        version=__version__,
        piece_options=[
            {"value": size, "label": human_size(size)} for size in valid_piece_lengths()
        ],
    )


@app.route("/favicon.ico")
def favicon():
    path = os.path.join(BASE_DIR, "static")
    if os.path.exists(os.path.join(path, "favicon.ico")):
        return send_from_directory(path, "favicon.ico")
    return ("", 204)


# --------------------------------------------------------------------------- #
# Browsing API
# --------------------------------------------------------------------------- #
@app.route("/api/bootstrap")
def api_bootstrap():
    settings = load_settings()
    start = settings.get("last_dir") or home_dir()
    if not os.path.isdir(start):
        start = home_dir()
    return jsonify(
        {
            "home": home_dir(),
            "start": start,
            "drives": list_drives(),
            "settings": settings,
            "native_picker": os.path.exists(PICKER),
            "output_dir": OUTPUT_DIR,
            "version": __version__,
        }
    )


@app.route("/api/browse")
def api_browse():
    path = request.args.get("path", "") or home_dir()
    show_hidden = request.args.get("hidden", "0") in ("1", "true", "True")
    try:
        listing = list_dir(path, show_hidden=show_hidden)
    except (NotADirectoryError, FileNotFoundError) as exc:
        return jsonify({"error": str(exc)}), 404
    except PermissionError as exc:
        return jsonify({"error": str(exc)}), 403
    except OSError as exc:
        return jsonify({"error": str(exc)}), 400
    for entry in listing["files"]:
        entry["human"] = human_size(entry["size"])
    listing["drives"] = list_drives()
    return jsonify(listing)


@app.route("/api/info")
def api_info():
    path = request.args.get("path", "")
    hidden = request.args.get("hidden", "0") in ("1", "true", "True")
    if not path:
        return jsonify({"error": "no path given"}), 400
    try:
        info = path_info(path, include_hidden=hidden)
    except FileNotFoundError:
        return jsonify({"error": f"path not found: {path}"}), 404
    except OSError as exc:
        return jsonify({"error": str(exc)}), 400
    info["suggested_piece_length"] = auto_piece_length(info["total_size"])
    info["suggested_piece_human"] = human_size(info["suggested_piece_length"])
    return jsonify(info)


@app.route("/api/pick", methods=["POST"])
def api_pick():
    payload = request.get_json(silent=True) or {}
    mode = "file" if payload.get("mode") == "file" else "folder"
    start = payload.get("start") or home_dir()
    if not os.path.exists(PICKER):
        return jsonify({"error": "picker helper missing"}), 500
    try:
        proc = subprocess.run(
            [sys.executable, PICKER, mode, start],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return jsonify({"error": "the picker dialog timed out"}), 408
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    chosen = (proc.stdout or "").strip()
    if proc.returncode != 0 and not chosen:
        return jsonify({"error": (proc.stderr or "picker failed").strip()}), 500
    return jsonify({"path": os.path.normpath(chosen) if chosen else ""})


# --------------------------------------------------------------------------- #
# Create / progress / download
# --------------------------------------------------------------------------- #
@app.route("/api/create", methods=["POST"])
def api_create():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source") or "").strip().strip('"')
    if not source:
        return jsonify({"error": "pick a file or folder first"}), 400
    source = os.path.abspath(os.path.expandvars(os.path.expanduser(source)))
    if not os.path.exists(source):
        return jsonify({"error": f"path not found: {source}"}), 404

    def split_lines(value) -> list[str]:
        if isinstance(value, list):
            items = value
        else:
            items = str(value or "").replace(",", "\n").splitlines()
        return [i.strip() for i in items if i and i.strip()]

    try:
        piece_length = int(payload.get("piece_length") or 0)
    except (TypeError, ValueError):
        piece_length = 0

    options = {
        "source": source,
        "trackers": split_lines(payload.get("trackers")),
        "web_seeds": split_lines(payload.get("web_seeds")),
        "comment": (payload.get("comment") or "").strip(),
        "created_by": (payload.get("created_by") or "").strip()
        or f"TorrentForge {__version__}",
        "source_tag": (payload.get("source_tag") or "").strip(),
        "private": bool(payload.get("private")),
        "piece_length": piece_length,
        "version": (payload.get("version") or "hybrid").lower(),
        "include_hidden": bool(payload.get("include_hidden")),
        "name_override": (payload.get("name") or "").strip(),
    }

    save_settings(
        {
            "trackers": "\n".join(options["trackers"]),
            "web_seeds": "\n".join(options["web_seeds"]),
            "comment": options["comment"],
            "source": options["source_tag"],
            "private": options["private"],
            "version": options["version"],
            "piece_length": piece_length,
            "include_hidden": options["include_hidden"],
            "last_dir": os.path.dirname(source) if os.path.isfile(source) else source,
        }
    )

    job = Job(uuid.uuid4().hex[:12], source, options)
    with JOBS_LOCK:
        JOBS[job.id] = job
        for old_id, old in list(JOBS.items()):
            if old.state in ("done", "error", "cancelled") and len(JOBS) > 40:
                JOBS.pop(old_id, None)

    thread = threading.Thread(target=run_job, args=(job,), daemon=True, name=f"job-{job.id}")
    thread.start()
    return jsonify({"job": job.id})


@app.route("/api/job/<job_id>")
def api_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(job.snapshot())


@app.route("/api/job/<job_id>/cancel", methods=["POST"])
def api_cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        return jsonify({"error": "unknown job"}), 404
    job.cancel.set()
    return jsonify({"ok": True})


@app.route("/api/download/<job_id>")
def api_download(job_id: str):
    job = JOBS.get(job_id)
    if not job or job.state != "done" or not job.filename:
        return jsonify({"error": "that torrent is not ready"}), 404
    return send_file(
        os.path.join(OUTPUT_DIR, job.filename),
        as_attachment=True,
        download_name=job.filename,
        mimetype="application/x-bittorrent",
    )


@app.route("/api/history")
def api_history():
    items = []
    for entry in sorted(os.listdir(OUTPUT_DIR)):
        if not entry.lower().endswith(".torrent"):
            continue
        full = os.path.join(OUTPUT_DIR, entry)
        try:
            stat = os.stat(full)
        except OSError:
            continue
        items.append(
            {
                "name": entry,
                "size": stat.st_size,
                "human": human_size(stat.st_size),
                "modified": datetime.fromtimestamp(stat.st_mtime).strftime(
                    "%Y-%m-%d %H:%M:%S"
                ),
                "mtime": stat.st_mtime,
            }
        )
    items.sort(key=lambda i: i["mtime"], reverse=True)
    return jsonify({"items": items, "output_dir": OUTPUT_DIR})


@app.route("/api/history/<path:name>")
def api_history_download(name: str):
    if not name.lower().endswith(".torrent") or os.path.sep in name or "/" in name:
        return jsonify({"error": "bad name"}), 400
    full = os.path.join(OUTPUT_DIR, name)
    if not os.path.isfile(full):
        return jsonify({"error": "not found"}), 404
    return send_file(
        full, as_attachment=True, download_name=name, mimetype="application/x-bittorrent"
    )


@app.route("/api/history/<path:name>", methods=["DELETE"])
def api_history_delete(name: str):
    if not name.lower().endswith(".torrent") or os.path.sep in name or "/" in name:
        return jsonify({"error": "bad name"}), 400
    full = os.path.join(OUTPUT_DIR, name)
    if not os.path.isfile(full):
        return jsonify({"error": "not found"}), 404
    try:
        os.remove(full)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


@app.route("/api/open-output", methods=["POST"])
def api_open_output():
    """Open the output folder in the system file manager."""
    try:
        if os.name == "nt":
            os.startfile(OUTPUT_DIR)  # noqa: S606 - local desktop helper
        elif sys.platform == "darwin":
            subprocess.Popen(["open", OUTPUT_DIR])
        else:
            subprocess.Popen(["xdg-open", OUTPUT_DIR])
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="TorrentForge web torrent creator")
    parser.add_argument("--host", default="127.0.0.1", help="interface to bind")
    parser.add_argument("--port", type=int, default=8777, help="port to listen on")
    parser.add_argument("--no-browser", action="store_true", help="do not auto-open a browser")
    parser.add_argument("--debug", action="store_true", help="Flask debug mode")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print("=" * 62)
    print(f" TorrentForge {__version__} - torrent builder, no client required")
    print(f" Open:   {url}")
    print(f" Output: {OUTPUT_DIR}")
    print(" Stop:   Ctrl+C")
    print("=" * 62)
    if not args.no_browser and not os.environ.get("WERKZEUG_RUN_MAIN"):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
