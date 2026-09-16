# TorrentForge

Make `.torrent` files from a web page on your own machine — **no torrent client
installed, no third-party libraries, nothing uploaded anywhere.**

You run one Python script, a browser tab opens, you point it at a folder (or a
single file) that is already on your disk, press **Create Torrent**, watch the
hashing progress, and then press **Download Torrent File**.

The hashing engine is written from scratch against the specs:

* **BEP 3** — classic v1 metainfo (SHA‑1 over pieces)
* **BEP 52** — v2 metainfo (SHA‑256 merkle trees, 16 KiB blocks, piece layers)
* **BEP 47** — padding files, so hybrid torrents align every file to a piece
* **Hybrid** — one torrent carrying both a v1 and a v2 info‑hash (default)

The generated torrents are **byte‑for‑byte info‑hash identical to libtorrent's**
(the library behind qBittorrent, Deluge, rTorrent) for the same input and piece
size — that is checked automatically by the test suite.

---

## Run it

```
cd TorrentForge
python app.py
```

or just double‑click **`RUN.bat`**.

Then open <http://127.0.0.1:8777/> (it opens by itself).

Useful flags:

```
python app.py --port 9000        listen on another port
python app.py --host 0.0.0.0     reachable from the LAN (see the warning below)
python app.py --no-browser       do not pop a browser open
```

Requirements: Python 3.10+ and Flask (already installed here). Tk is used only
for the optional native “Browse folder…” dialog.

---

## How the page works

1. **Choose what to share**
   * **Browse folder… / Browse file…** — opens the normal Windows file dialog on
     the machine running the script.
   * **Server browser** — an in‑page file browser: drive buttons, breadcrumbs,
     double‑click a folder to pick it, single‑click a file to pick it, or press
     **Use this folder** for the folder you are currently looking at.
   * Or just type/paste a path into the box.

   Nothing is copied or uploaded — the app only reads the bytes to hash them.
   As soon as a path is chosen you get the file count, total size and the piece
   size that will be used.

2. **Torrent options** — trackers (one announce URL per line), web seeds,
   comment, source tag, custom torrent name, piece size (automatic aims for
   ~1500 pieces), format (hybrid / v1 / v2), private flag, include hidden files.
   Your settings are remembered in `settings.json` for the next run.

3. **Build it** — a live progress bar with throughput, current file and ETA, plus
   a **Cancel** button.

4. **Result** — name, both info‑hashes, piece size and count, torrent file size,
   hashing time, the full file list, a copyable **magnet link**, and the big
   green **Download Torrent File** button. Every torrent is also written to
   `output/` and listed under **Previously built**, where it can be downloaded
   again or deleted.

---

## Command line (same engine)

```
python mktorrent.py "D:\Media\My Album" -t udp://tracker.example:1337/announce
python mktorrent.py big.iso -p 4M --version v1 -o big.torrent
python mktorrent.py folder --private --source MYTRACKER -q
```

`-p/--piece-size` accepts `256K`, `1M`, `4M`…; `--version` is `hybrid`, `v1` or
`v2`; `-q` prints only the path of the file it wrote.

---

## Check a torrent

```
python tests\inspect_torrent.py output\MyFolder.torrent
```

Prints what a real client sees: name, trackers, piece length, both info‑hashes,
private flag and the file list (padding files marked).

---

## Tests

`TEST.bat`, or individually:

| command | what it proves |
|---|---|
| `python tests\verify.py` | metainfo structure, merkle roots re‑computed from the piece layers, v1 pieces re‑hashed by a second naive implementation, and info‑hashes matching libtorrent for v1 / v2 / hybrid, folder and single file |
| `python tests\test_web.py` | the whole web API: browse → info → create → poll → download → history → delete, plus error paths |
| `python tests\smoke_server.py` | boots a real HTTP server on a free port, fetches the page and assets, shuts down |

All three exit non‑zero if anything fails. Current state: **85 + 38 + 6 checks
passing**.

`verify.py` also takes a path, to run the same cross‑check against your own
data:

```
python tests\verify.py "D:\Media\My Album"
```

(Verified here on a real 283 MB / 14‑file folder: v1, v2 and hybrid info‑hashes
all identical to libtorrent's.)

> Note on v1 file order: BEP 3 fixes no ordering, so TorrentForge always uses the
> canonical byte‑sorted order that v2 mandates. libtorrent's own scanner happens
> to use a case‑insensitive order for v1‑only torrents, so a v1‑only torrent it
> builds can have a different info‑hash for the same files; the test controls for
> this by feeding libtorrent our order. Hybrid and v2 torrents are unaffected —
> those orders are fixed by the spec and match exactly.

---

## Folder map

```
TorrentForge/
├── app.py                 Flask server: browsing, build jobs, progress, download
├── mktorrent.py           command-line front end for the same engine
├── picker.py              native folder/file dialog (run as a short subprocess)
├── RUN.bat / TEST.bat     double-click launchers
├── settings.json          created on first build: remembers your tracker list
├── output/                every .torrent you build lands here
├── torrentlib/
│   ├── bencode.py         strict bencode encoder/decoder (canonical key order)
│   ├── creator.py         scanning, piece hashing, v2 merkle trees, pad files
│   └── browse.py          drive/folder listing and size previews for the UI
├── templates/index.html   the page
├── static/css/app.css     styling
├── static/js/app.js       browsing, job polling, results, history
└── tests/
    ├── verify.py          format + libtorrent cross-check
    ├── test_web.py        web API end-to-end
    ├── smoke_server.py    live HTTP smoke test
    └── inspect_torrent.py torrent inspector
```

---

## Notes & limits

* **Piece size** must be a power of two; v2/hybrid additionally require a
  multiple of 16 KiB. Automatic mode picks 256 KiB … 16 MiB.
* **Empty files** are kept in the torrent (v2 records them with length 0 and no
  merkle root); a folder containing only empty files is rejected.
* **Symlinks** are skipped, and hidden/system files are skipped unless you tick
  *Include hidden files*.
* **Hybrid torrents** contain `.pad` files (BEP 47). They are part of the v1
  layout only — real clients hide them, and they are why a hybrid torrent's
  reported total size can be slightly larger than the payload.
* **`--host 0.0.0.0` exposes a filesystem browser** for the machine it runs on to
  anyone on your network. There is no authentication; keep it on localhost
  unless you know the network is trusted.
* Creating a torrent does **not** seed it. To share, load the `.torrent` (or the
  magnet link) into whatever client the recipients use, and keep the original
  files where they were when you built it.
