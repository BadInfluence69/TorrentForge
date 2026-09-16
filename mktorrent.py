"""Command-line front end for the same engine the web UI uses.

Examples
    python mktorrent.py "D:\\Media\\My Album"
    python mktorrent.py file.iso -t udp://tracker.example:1337/announce -o out.torrent
    python mktorrent.py folder --version v1 --piece-size 1M --private --source MYTRACKER
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from torrentlib.browse import human_size
from torrentlib.creator import TorrentError, create_torrent, valid_piece_lengths

SUFFIXES = {"k": 1024, "m": 1024**2, "g": 1024**3}


def parse_size(text: str) -> int:
    text = (text or "").strip().lower().replace("ib", "").replace("b", "")
    if not text or text == "auto":
        return 0
    multiplier = 1
    if text[-1] in SUFFIXES:
        multiplier = SUFFIXES[text[-1]]
        text = text[:-1]
    try:
        value = int(float(text) * multiplier)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"bad size: {text!r}") from exc
    if value and value not in valid_piece_lengths():
        raise argparse.ArgumentTypeError(
            "piece size must be a power of two between 16K and 64M"
        )
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mktorrent",
        description="Create a .torrent file from a file or folder - no torrent client needed.",
    )
    parser.add_argument("path", help="file or folder to make a torrent of")
    parser.add_argument("-o", "--output", default="", help="where to write the .torrent")
    parser.add_argument(
        "-t", "--tracker", action="append", default=[], help="announce URL (repeatable)"
    )
    parser.add_argument(
        "-w", "--web-seed", action="append", default=[], help="web seed URL (repeatable)"
    )
    parser.add_argument("-c", "--comment", default="", help="comment stored in the torrent")
    parser.add_argument("-s", "--source", default="", help="source tag (private trackers)")
    parser.add_argument("-n", "--name", default="", help="override the torrent name")
    parser.add_argument(
        "-p", "--piece-size", type=parse_size, default=0, help="e.g. 256K, 1M, 4M (default auto)"
    )
    parser.add_argument(
        "--version",
        choices=("v1", "v2", "hybrid"),
        default="hybrid",
        help="metainfo format (default hybrid)",
    )
    parser.add_argument("--private", action="store_true", help="set the private flag")
    parser.add_argument("--include-hidden", action="store_true", help="include hidden files")
    parser.add_argument("-q", "--quiet", action="store_true", help="only print the output path")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    last = [0.0]

    def progress(done: int, total: int, current: str) -> None:
        if args.quiet:
            return
        now = time.time()
        if now - last[0] < 0.15 and done != total:
            return
        last[0] = now
        percent = (done / total * 100) if total else 0.0
        bar = "#" * int(percent / 2.5)
        sys.stderr.write(
            f"\r  [{bar:<40}] {percent:5.1f}%  {human_size(done)}/{human_size(total)}  "
        )
        sys.stderr.flush()

    try:
        result = create_torrent(
            args.path,
            trackers=args.tracker,
            web_seeds=args.web_seed,
            comment=args.comment,
            source=args.source,
            private=args.private,
            piece_length=args.piece_size or None,
            version=args.version,
            include_hidden=args.include_hidden,
            name_override=args.name,
            progress=progress,
        )
    except TorrentError as exc:
        sys.stderr.write(f"\nerror: {exc}\n")
        return 2

    if not args.quiet:
        sys.stderr.write("\n")

    out = args.output or (result.name + ".torrent")
    if os.path.isdir(out):
        out = os.path.join(out, result.name + ".torrent")
    with open(out, "wb") as handle:
        handle.write(result.data)

    if args.quiet:
        print(os.path.abspath(out))
        return 0

    print(f"  name        : {result.name}")
    print(f"  format      : {result.version}")
    print(f"  payload     : {human_size(result.total_size)} in {result.file_count} file(s)")
    print(f"  piece size  : {human_size(result.piece_length)} ({result.piece_count} pieces)")
    if result.infohash_v1:
        print(f"  info hash v1: {result.infohash_v1}")
    if result.infohash_v2:
        print(f"  info hash v2: {result.infohash_v2}")
    print(f"  hashed in   : {result.elapsed:.2f}s")
    print(f"  written to  : {os.path.abspath(out)}")
    print(f"  magnet      : {result.magnet}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
