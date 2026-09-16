"""Inspect a .torrent file: prints what a real client sees inside it.

Uses libtorrent when it is installed (an entirely independent parser) and
falls back to the built-in bencode decoder otherwise.

Run:  python tests/inspect_torrent.py path\\to\\file.torrent
"""

from __future__ import annotations

import hashlib
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from torrentlib.bencode import decode, encode  # noqa: E402
from torrentlib.browse import human_size  # noqa: E402


def with_libtorrent(path: str) -> bool:
    try:
        import libtorrent as lt
    except Exception:
        return False

    info = lt.torrent_info(path)
    hashes = info.info_hashes()
    print("parsed by libtorrent:")
    print(f"  name        : {info.name()}")
    print(f"  comment     : {info.comment()}")
    print(f"  created by  : {info.creator()}")
    print(f"  piece length: {human_size(info.piece_length())} ({info.num_pieces()} pieces)")
    print(f"  info hash v1: {hashes.v1 if hashes.has_v1() else '-'}")
    print(f"  info hash v2: {hashes.v2 if hashes.has_v2() else '-'}")
    print(f"  private     : {info.priv()}")
    trackers = [t.url for t in info.trackers()]
    print(f"  trackers    : {trackers or '-'}")
    files = info.files()
    print(f"  files       : {files.num_files()} entries")
    for index in range(files.num_files()):
        pad = files.file_flags(index) & lt.file_storage.flag_pad_file
        mark = "  [padding]" if pad else ""
        print(f"    {files.file_path(index)}  {human_size(files.file_size(index))}{mark}")
    return True


def with_bencode(path: str) -> None:
    with open(path, "rb") as handle:
        meta = decode(handle.read())
    info = meta[b"info"]
    print("parsed by the built-in decoder:")
    print(f"  name        : {info[b'name'].decode('utf-8', 'replace')}")
    print(f"  piece length: {human_size(info[b'piece length'])}")
    if b"pieces" in info:
        print(f"  v1 pieces   : {len(info[b'pieces']) // 20}")
        print(f"  info hash v1: {hashlib.sha1(encode(info)).hexdigest()}")
    if info.get(b"meta version") == 2:
        print(f"  info hash v2: {hashlib.sha256(encode(info)).hexdigest()}")
        print(f"  piece layers: {len(meta.get(b'piece layers', {}))} file(s)")
    if b"announce" in meta:
        print(f"  announce    : {meta[b'announce'].decode()}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = sys.argv[1]
    if not os.path.isfile(path):
        print(f"no such file: {path}")
        return 1
    print(f"\n{path}  ({human_size(os.path.getsize(path))})\n")
    if not with_libtorrent(path):
        print("(libtorrent not installed)")
    print()
    with_bencode(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
