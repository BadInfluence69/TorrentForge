"""Self-check for TorrentForge.

Builds real torrents from generated sample data and verifies them three ways:

  1. structural checks on the bencoded metainfo (BEP 3 / BEP 52 rules);
  2. independent re-computation of every v2 merkle root from the piece layers;
  3. cross-check of the v1 and v2 info-hashes against libtorrent, if installed.

Run:  python tests/verify.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from torrentlib.bencode import decode, encode  # noqa: E402
from torrentlib.creator import (  # noqa: E402
    BLOCK_SIZE,
    ZERO_HASH,
    _merkle_root,
    create_torrent,
)

try:
    import libtorrent as lt
except Exception:  # pragma: no cover - optional
    lt = None

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


# --------------------------------------------------------------------------- #
# Sample data
# --------------------------------------------------------------------------- #
def build_sample(root: str) -> None:
    """A tree that exercises every interesting code path."""
    os.makedirs(os.path.join(root, "video"), exist_ok=True)
    os.makedirs(os.path.join(root, "docs", "nested"), exist_ok=True)

    def write(path: str, size: int, seed: int) -> None:
        rnd = bytearray()
        value = seed
        while len(rnd) < size:
            value = (value * 1103515245 + 12345) & 0xFFFFFFFF
            rnd += value.to_bytes(4, "little")
        with open(path, "wb") as handle:
            handle.write(bytes(rnd[:size]))

    write(os.path.join(root, "video", "clip.bin"), 700_000, 1)      # > 1 piece
    write(os.path.join(root, "video", "tiny.bin"), 100, 2)          # < 1 block
    write(os.path.join(root, "docs", "notes.txt"), 40_000, 3)       # few blocks
    write(os.path.join(root, "docs", "nested", "deep.bin"), 300_000, 4)
    write(os.path.join(root, "readme.md"), 17, 5)
    open(os.path.join(root, "docs", "empty.dat"), "wb").close()     # 0 bytes


# --------------------------------------------------------------------------- #
# Structural checks
# --------------------------------------------------------------------------- #
def walk_tree(tree: dict, prefix=()):
    for key, value in tree.items():
        if key == b"":
            yield prefix, value
        else:
            yield from walk_tree(value, prefix + (key,))


def structural_checks(result, version: str) -> None:
    meta = decode(result.data)
    info = meta[b"info"]

    check(f"{version}: bencode round-trips byte for byte", encode(meta) == result.data)
    check(f"{version}: info has a name", bool(info.get(b"name")))
    check(
        f"{version}: piece length is a power of two",
        info[b"piece length"] & (info[b"piece length"] - 1) == 0,
    )

    if version in ("v1", "hybrid"):
        pieces = info[b"pieces"]
        check(f"{version}: pieces is a multiple of 20 bytes", len(pieces) % 20 == 0)
        check(
            f"{version}: piece count matches the payload size",
            len(pieces) // 20 == result.piece_count,
            f"{len(pieces) // 20} vs {result.piece_count}",
        )
        computed = hashlib.sha1(encode(info)).hexdigest()
        check(f"{version}: v1 info-hash reproducible", computed == result.infohash_v1)
    else:
        check("v2: no v1 pieces key", b"pieces" not in info)

    if version in ("v2", "hybrid"):
        check(f"{version}: meta version is 2", info.get(b"meta version") == 2)
        check(f"{version}: file tree present", bool(info.get(b"file tree")))
        layers = meta[b"piece layers"]
        blocks_per_piece = info[b"piece length"] // BLOCK_SIZE
        pad_root = _merkle_root([ZERO_HASH] * blocks_per_piece)

        ok_roots = True
        detail = ""
        for path, leaf in walk_tree(info[b"file tree"]):
            length = leaf[b"length"]
            if length == 0:
                if b"pieces root" in leaf:
                    ok_roots, detail = False, f"empty file {path} has a pieces root"
                continue
            root = leaf[b"pieces root"]
            if len(root) != 32:
                ok_roots, detail = False, f"{path} root is not 32 bytes"
                break
            if length > info[b"piece length"]:
                layer = layers.get(root)
                if not layer or len(layer) % 32:
                    ok_roots, detail = False, f"{path} has no valid piece layer"
                    break
                hashes = [layer[i : i + 32] for i in range(0, len(layer), 32)]
                expected_pieces = -(-length // info[b"piece length"])
                if len(hashes) != expected_pieces:
                    ok_roots, detail = False, f"{path}: {len(hashes)} != {expected_pieces} pieces"
                    break
                if _merkle_root(hashes, pad_root) != root:
                    ok_roots, detail = False, f"{path}: merkle root mismatch"
                    break
            elif root in layers:
                ok_roots, detail = False, f"{path}: small file should not have a piece layer"
                break
        check(f"{version}: every v2 merkle root re-computes from its piece layer", ok_roots, detail)
        computed = hashlib.sha256(encode(info)).hexdigest()
        check(f"{version}: v2 info-hash reproducible", computed == result.infohash_v2)

    if version == "hybrid" and b"files" in info:
        pads = [f for f in info[b"files"] if f.get(b"attr") == b"p"]
        offset = 0
        aligned = True
        for entry in info[b"files"]:
            if entry.get(b"attr") != b"p":
                if offset % info[b"piece length"] and offset != 0:
                    aligned = False
                    break
            offset += entry[b"length"]
        check("hybrid: every real file starts on a piece boundary", aligned)
        check("hybrid: padding files are described in the v1 file list", len(pads) >= 1)
        check(
            "hybrid: padding bytes reported match the pad entries",
            sum(f[b"length"] for f in pads) == result.padding_bytes,
        )


# --------------------------------------------------------------------------- #
# libtorrent cross-check
# --------------------------------------------------------------------------- #
def naive_v1_check(path: str, result, version: str) -> None:
    """Re-hash the v1 piece stream with a deliberately dumb implementation.

    The creator hashes incrementally while streaming 1 MiB chunks; this reads
    whole files and slices the concatenated stream, so a boundary bug in one
    would not appear in the other.  BEP 3 does not fix a file order, so we use
    the order recorded in the torrent itself.
    """
    if version == "v2":
        return
    meta = decode(result.data)
    info = meta[b"info"]
    piece_length = info[b"piece length"]
    root = os.path.dirname(path) if os.path.isfile(path) else path

    stream = bytearray()
    digests = bytearray()

    def flush(final: bool) -> None:
        while len(stream) >= piece_length:
            digests.extend(hashlib.sha1(bytes(stream[:piece_length])).digest())
            del stream[:piece_length]
        if final and stream:
            digests.extend(hashlib.sha1(bytes(stream)).digest())
            stream.clear()

    if b"files" in info:
        for entry in info[b"files"]:
            if entry.get(b"attr") == b"p":
                stream.extend(bytes(entry[b"length"]))
            else:
                parts = [p.decode("utf-8") for p in entry[b"path"]]
                with open(os.path.join(root, *parts), "rb") as handle:
                    stream.extend(handle.read())
            flush(False)
    else:
        with open(path, "rb") as handle:
            stream.extend(handle.read())
        flush(False)
    flush(True)

    check(
        f"{version}: v1 pieces match an independent naive hasher",
        bytes(digests) == info[b"pieces"],
        f"{len(digests) // 20} vs {len(info[b'pieces']) // 20} pieces",
    )


def libtorrent_check(path: str, result, version: str) -> None:
    if lt is None:
        print("  [skip] libtorrent not installed - cross-check skipped")
        return

    info = lt.torrent_info(lt.bdecode(result.data))
    check(f"{version}: libtorrent parses the torrent", info.name() == result.name)
    storage_view = info.files()
    payload = sum(
        storage_view.file_size(i)
        for i in range(storage_view.num_files())
        if not (storage_view.file_flags(i) & lt.file_storage.flag_pad_file)
    )
    check(
        f"{version}: libtorrent sees the same payload size",
        payload == result.total_size,
        f"{payload} vs {result.total_size}",
    )

    # Build the reference with *our* file order.  v1 has no mandated ordering
    # (libtorrent's own scan uses a different one), so fixing the order is the
    # only way to compare the hashing itself rather than a naming convention.
    storage = lt.file_storage()
    if version == "v1":
        if len(result.files) == 1 and os.path.isfile(path):
            storage.add_file(result.name, result.files[0]["size"])
        else:
            for item in result.files:
                storage.add_file(result.name + "/" + item["path"], item["size"])
    else:
        # v2 and hybrid have a mandated canonical order; let libtorrent scan.
        lt.add_files(storage, path)
    flags = 0
    if version == "v1":
        flags = lt.create_torrent.v1_only
    elif version == "v2":
        flags = lt.create_torrent.v2_only
    maker = lt.create_torrent(storage, result.piece_length, flags=flags)
    lt.set_piece_hashes(maker, os.path.dirname(path))
    reference = lt.torrent_info(maker.generate())

    hashes = info.info_hashes()
    ref_hashes = reference.info_hashes()
    if result.infohash_v1:
        check(
            f"{version}: v1 info-hash equals libtorrent's",
            str(hashes.v1) == str(ref_hashes.v1),
            f"{hashes.v1} vs {ref_hashes.v1}",
        )
        check(
            f"{version}: v1 info-hash equals our own hex",
            str(hashes.v1) == result.infohash_v1,
        )
    if result.infohash_v2:
        check(
            f"{version}: v2 info-hash equals libtorrent's",
            str(hashes.v2) == str(ref_hashes.v2),
            f"{hashes.v2} vs {ref_hashes.v2}",
        )
        check(
            f"{version}: v2 info-hash equals our own hex",
            str(hashes.v2) == result.infohash_v2,
        )


# --------------------------------------------------------------------------- #
def check_real_path(path: str) -> int:
    """Cross-check a real file/folder the user points us at."""
    path = os.path.abspath(path)
    print(f"\n=== real path: {path} ===")
    for version in ("v1", "v2", "hybrid"):
        result = create_torrent(path, version=version, piece_length=256 * 1024)
        print(f"-- {version}: {result.file_count} file(s), "
              f"{result.total_size} bytes, {result.elapsed:.1f}s")
        structural_checks(result, version)
        naive_v1_check(path, result, version)
        libtorrent_check(path, result, version)
    print(f"\n{PASSED} checks passed, {FAILED} failed")
    return 1 if FAILED else 0


def main() -> int:
    if len(sys.argv) > 1:
        return check_real_path(sys.argv[1])

    tmp = tempfile.mkdtemp(prefix="torrentforge-test-")
    sample = os.path.join(tmp, "SampleRelease")
    os.makedirs(sample, exist_ok=True)
    build_sample(sample)

    single = os.path.join(tmp, "single-file.bin")
    with open(single, "wb") as handle:
        handle.write(bytes(range(256)) * 4000)  # ~1 MB

    try:
        for version in ("v1", "v2", "hybrid"):
            print(f"\n=== folder torrent, {version} ===")
            result = create_torrent(
                sample,
                trackers=["udp://tracker.example.org:1337/announce"],
                comment="TorrentForge self-test",
                version=version,
                piece_length=256 * 1024,
            )
            structural_checks(result, version)
            naive_v1_check(sample, result, version)
            libtorrent_check(sample, result, version)

        for version in ("v1", "v2", "hybrid"):
            print(f"\n=== single-file torrent, {version} ===")
            result = create_torrent(single, version=version, piece_length=256 * 1024)
            structural_checks(result, version)
            naive_v1_check(single, result, version)
            libtorrent_check(single, result, version)

        print("\n=== automatic piece length ===")
        result = create_torrent(sample, version="hybrid")
        check("auto piece size is at least 256 KiB", result.piece_length >= 256 * 1024)
        check("magnet link carries both hashes", "btih" in result.magnet and "btmh" in result.magnet)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{PASSED} checks passed, {FAILED} failed")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main())
