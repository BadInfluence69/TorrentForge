"""Torrent metainfo creation - BitTorrent v1 (BEP 3), v2 (BEP 52) and hybrid.

Pure standard library: the whole thing is SHA-1 / SHA-256 over the file data
plus a bencoded dictionary.  No torrent client and no third-party package is
needed to produce a working .torrent file.

Layout of a hybrid torrent (the default here):

  info = {
      name, piece length,
      length | files      <- v1 payload description (with .pad files)
      pieces              <- v1: concatenated SHA-1 of every piece
      meta version = 2,
      file tree           <- v2: per-file SHA-256 merkle roots
  }
  piece layers            <- v2: outside info, one layer per multi-piece file

Such a torrent has both a v1 and a v2 info-hash and can be used by old and new
clients at the same time.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence
from urllib.parse import quote, urlencode

from .bencode import encode

__all__ = [
    "BLOCK_SIZE",
    "TorrentError",
    "TorrentResult",
    "auto_piece_length",
    "scan_path",
    "create_torrent",
    "valid_piece_lengths",
]

BLOCK_SIZE = 16 * 1024          # v2 merkle leaf size, fixed by BEP 52
ZERO_HASH = bytes(32)           # v2 padding leaf: 32 zero bytes (not a hash)
READ_CHUNK = 1024 * 1024        # how much we pull from disk at a time
MIN_PIECE_LENGTH = 16 * 1024
MAX_PIECE_LENGTH = 64 * 1024 * 1024
DEFAULT_CREATED_BY = "TorrentForge (pure-python)"


class TorrentError(Exception):
    """Anything that stops a torrent from being built."""


# --------------------------------------------------------------------------- #
# Source scanning
# --------------------------------------------------------------------------- #
@dataclass
class SourceFile:
    """One real file on disk that goes into the torrent."""

    abspath: str
    parts: tuple            # path components relative to the torrent name
    size: int

    @property
    def display(self) -> str:
        return "/".join(self.parts)


def _is_hidden(path: str, name: str) -> bool:
    if name.startswith("."):
        return True
    if os.name == "nt":
        try:
            attrs = os.stat(path).st_file_attributes  # type: ignore[attr-defined]
        except (OSError, AttributeError):
            return False
        return bool(attrs & 0x2) or bool(attrs & 0x4)  # HIDDEN | SYSTEM
    return False


def scan_path(root: str, include_hidden: bool = False) -> tuple[str, list[SourceFile], bool]:
    """Return ``(name, files, is_single_file)`` for a file or folder on disk.

    Files are returned in ascending path order, which is the order used for the
    v1 piece stream and the order clients expect.
    """
    root = os.path.abspath(root)
    if not os.path.exists(root):
        raise TorrentError(f"path does not exist: {root}")

    if os.path.isfile(root):
        name = os.path.basename(root)
        size = os.path.getsize(root)
        if size == 0:
            raise TorrentError("cannot create a torrent from an empty (0 byte) file")
        return name, [SourceFile(root, (name,), size)], True

    if not os.path.isdir(root):
        raise TorrentError(f"not a regular file or folder: {root}")

    name = os.path.basename(root.rstrip("\\/")) or root
    files: list[SourceFile] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        if not include_hidden:
            dirnames[:] = [
                d for d in dirnames if not _is_hidden(os.path.join(dirpath, d), d)
            ]
        dirnames.sort()
        for filename in sorted(filenames):
            full = os.path.join(dirpath, filename)
            if not include_hidden and _is_hidden(full, filename):
                continue
            if os.path.islink(full) or not os.path.isfile(full):
                continue
            rel = os.path.relpath(full, root)
            parts = tuple(p for p in rel.replace("\\", "/").split("/") if p not in ("", "."))
            if not parts:
                continue
            try:
                size = os.path.getsize(full)
            except OSError as exc:
                raise TorrentError(f"cannot read {full}: {exc}") from exc
            files.append(SourceFile(full, parts, size))

    files.sort(key=lambda f: f.parts)
    if not files:
        raise TorrentError(f"no files found in {root}")
    if sum(f.size for f in files) == 0:
        raise TorrentError("every file in that folder is empty (0 bytes)")
    return name, files, False


# --------------------------------------------------------------------------- #
# Piece length helpers
# --------------------------------------------------------------------------- #
def valid_piece_lengths() -> list[int]:
    out = []
    size = MIN_PIECE_LENGTH
    while size <= MAX_PIECE_LENGTH:
        out.append(size)
        size *= 2
    return out


def auto_piece_length(total_size: int, target_pieces: int = 1500) -> int:
    """Pick a power-of-two piece size aiming for ~1500 pieces (256 KiB..16 MiB)."""
    if total_size <= 0:
        return 256 * 1024
    size = MIN_PIECE_LENGTH
    while size * target_pieces < total_size and size < 16 * 1024 * 1024:
        size *= 2
    return max(256 * 1024, min(size, 16 * 1024 * 1024))


def _check_piece_length(piece_length: int, need_v2: bool) -> None:
    if piece_length < MIN_PIECE_LENGTH:
        raise TorrentError("piece length must be at least 16 KiB")
    if piece_length > MAX_PIECE_LENGTH:
        raise TorrentError("piece length must be at most 64 MiB")
    if piece_length & (piece_length - 1):
        raise TorrentError("piece length must be a power of two")
    if need_v2 and piece_length % BLOCK_SIZE:
        raise TorrentError("v2 torrents need a piece length that is a multiple of 16 KiB")


# --------------------------------------------------------------------------- #
# Merkle tree (BEP 52)
# --------------------------------------------------------------------------- #
def _merkle_root(layer: Sequence[bytes], pad: bytes = ZERO_HASH) -> bytes:
    """Reduce a layer of hashes to a single root, padding to a power of two."""
    nodes = list(layer)
    if not nodes:
        return pad
    width = 1
    while width < len(nodes):
        width <<= 1
    if len(nodes) < width:
        nodes.extend([pad] * (width - len(nodes)))
    while len(nodes) > 1:
        nodes = [
            hashlib.sha256(nodes[i] + nodes[i + 1]).digest()
            for i in range(0, len(nodes), 2)
        ]
        pad = hashlib.sha256(pad + pad).digest()
    return nodes[0]


class _V2FileHasher:
    """Accumulates 16 KiB block hashes for one file and yields piece roots."""

    def __init__(self, piece_length: int):
        self.blocks_per_piece = piece_length // BLOCK_SIZE
        self._leaves: list[bytes] = []          # leaves of the current piece
        self.piece_roots: list[bytes] = []      # one root per full piece
        self._pad_piece_root: bytes | None = None

    def feed_block(self, block: bytes) -> None:
        self._leaves.append(hashlib.sha256(block).digest())
        if len(self._leaves) == self.blocks_per_piece:
            self.piece_roots.append(_merkle_root(self._leaves))
            self._leaves = []

    def _pad_root(self) -> bytes:
        if self._pad_piece_root is None:
            self._pad_piece_root = _merkle_root([ZERO_HASH] * self.blocks_per_piece)
        return self._pad_piece_root

    def finish(self) -> tuple[bytes, bytes]:
        """Return ``(pieces_root, piece_layer)``.

        ``piece_layer`` is empty for files that fit inside a single piece; such
        files carry no entry in the top level ``piece layers`` dictionary.
        """
        if not self.piece_roots:
            # Small file: merkle over its blocks alone, padded to a power of two.
            return _merkle_root(self._leaves), b""
        if self._leaves:
            # Tail piece: pad the leaves out to a whole piece before rolling up.
            tail = list(self._leaves) + [ZERO_HASH] * (
                self.blocks_per_piece - len(self._leaves)
            )
            self.piece_roots.append(_merkle_root(tail))
            self._leaves = []
        layer = b"".join(self.piece_roots)
        root = _merkle_root(self.piece_roots, self._pad_root())
        return root, layer


# --------------------------------------------------------------------------- #
# Result container
# --------------------------------------------------------------------------- #
@dataclass
class TorrentResult:
    data: bytes
    name: str
    version: str
    piece_length: int
    piece_count: int
    total_size: int
    file_count: int
    infohash_v1: str | None
    infohash_v2: str | None
    files: list[dict] = field(default_factory=list)
    elapsed: float = 0.0
    padding_bytes: int = 0

    trackers: list = field(default_factory=list)

    @property
    def magnet(self) -> str:
        parts: list[str] = []
        if self.infohash_v1:
            parts.append(f"xt=urn:btih:{self.infohash_v1}")
        if self.infohash_v2:
            parts.append(f"xt=urn:btmh:1220{self.infohash_v2}")
        parts.append("dn=" + quote(self.name, safe=""))
        for tracker in self.trackers:
            parts.append("tr=" + quote(tracker, safe=""))
        return "magnet:?" + "&".join(parts)

    def summary(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "piece_length": self.piece_length,
            "piece_count": self.piece_count,
            "total_size": self.total_size,
            "file_count": self.file_count,
            "infohash_v1": self.infohash_v1,
            "infohash_v2": self.infohash_v2,
            "magnet": self.magnet,
            "elapsed": round(self.elapsed, 2),
            "padding_bytes": self.padding_bytes,
            "torrent_size": len(self.data),
            "files": self.files,
        }


# --------------------------------------------------------------------------- #
# The builder
# --------------------------------------------------------------------------- #
def create_torrent(
    path: str,
    *,
    trackers: Iterable[str] = (),
    web_seeds: Iterable[str] = (),
    comment: str = "",
    created_by: str = DEFAULT_CREATED_BY,
    source: str = "",
    private: bool = False,
    piece_length: int | None = None,
    version: str = "hybrid",
    include_hidden: bool = False,
    name_override: str = "",
    creation_date: int | None = None,
    progress: Callable[[int, int, str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> TorrentResult:
    """Build a .torrent for ``path`` (a file or a folder) and return the bytes."""
    version = (version or "hybrid").lower()
    if version not in ("v1", "v2", "hybrid"):
        raise TorrentError(f"unknown torrent version {version!r}")
    want_v1 = version in ("v1", "hybrid")
    want_v2 = version in ("v2", "hybrid")

    started = time.time()
    name, files, single = scan_path(path, include_hidden=include_hidden)
    if name_override.strip():
        name = name_override.strip()
    total_size = sum(f.size for f in files)

    if piece_length in (None, 0, "auto"):
        piece_length = auto_piece_length(total_size)
    piece_length = int(piece_length)
    _check_piece_length(piece_length, want_v2)

    blocks_per_piece = piece_length // BLOCK_SIZE
    pad_files_needed = want_v1 and want_v2 and not single

    # ---------------- hashing pass ---------------- #
    sha1_piece = hashlib.sha1() if want_v1 else None
    piece_fill = 0                      # bytes already in the open v1 piece
    v1_pieces = bytearray()
    piece_count = 0
    padding_bytes = 0

    v1_files: list[dict] = []           # v1 "files" list, includes .pad entries
    file_tree: dict = {}                # v2 "file tree"
    piece_layers: dict[bytes, bytes] = {}
    listing: list[dict] = []
    done_bytes = 0

    def _emit(text: str) -> None:
        if progress:
            progress(done_bytes, total_size, text)

    _emit("starting")

    for index, sf in enumerate(files):
        if cancelled and cancelled():
            raise TorrentError("cancelled")

        v2_hasher = _V2FileHasher(piece_length) if want_v2 and sf.size else None
        remaining = sf.size
        pending = b""                   # partial 16 KiB block carried over

        if sf.size:
            try:
                handle = open(sf.abspath, "rb")
            except OSError as exc:
                raise TorrentError(f"cannot open {sf.abspath}: {exc}") from exc
            with handle:
                while remaining > 0:
                    chunk = handle.read(min(READ_CHUNK, remaining))
                    if not chunk:
                        raise TorrentError(
                            f"{sf.display} shrank while being hashed - aborting"
                        )
                    remaining -= len(chunk)
                    done_bytes += len(chunk)

                    if sha1_piece is not None:
                        view = memoryview(chunk)
                        offset = 0
                        while offset < len(chunk):
                            take = min(piece_length - piece_fill, len(chunk) - offset)
                            sha1_piece.update(view[offset : offset + take])
                            piece_fill += take
                            offset += take
                            if piece_fill == piece_length:
                                v1_pieces += sha1_piece.digest()
                                piece_count += 1
                                sha1_piece = hashlib.sha1()
                                piece_fill = 0
                        view.release()

                    if v2_hasher is not None:
                        data = pending + chunk if pending else chunk
                        full = (len(data) // BLOCK_SIZE) * BLOCK_SIZE
                        for off in range(0, full, BLOCK_SIZE):
                            v2_hasher.feed_block(data[off : off + BLOCK_SIZE])
                        pending = data[full:]

                    if cancelled and cancelled():
                        raise TorrentError("cancelled")
                    _emit(sf.display)

            if v2_hasher is not None and pending:
                v2_hasher.feed_block(pending)
                pending = b""

        # ---- v2 file tree entry ----
        if want_v2:
            node = file_tree
            if single:
                leaf_key = name          # single-file torrents: tree key == info name
            else:
                for part in sf.parts[:-1]:
                    node = node.setdefault(part, {})
                leaf_key = sf.parts[-1]
            if sf.size == 0:
                node[leaf_key] = {"": {"length": 0}}
            else:
                root, layer = v2_hasher.finish()  # type: ignore[union-attr]
                node[leaf_key] = {"": {"length": sf.size, "pieces root": root}}
                if layer:
                    piece_layers[root] = layer

        # ---- v1 file list entry (multi-file mode) ----
        if want_v1 and not single:
            v1_files.append({"length": sf.size, "path": list(sf.parts)})

        listing.append({"path": sf.display, "size": sf.size})

        # ---- hybrid padding so every file starts on a piece boundary ----
        # libtorrent (the BEP 52 reference implementation) also pads the final
        # file, so we do the same and stay info-hash compatible with it.
        if pad_files_needed and piece_fill:
            pad = piece_length - piece_fill
            padding_bytes += pad
            zeros = bytes(min(pad, READ_CHUNK))
            left = pad
            while left > 0:
                block = zeros[: min(left, len(zeros))]
                sha1_piece.update(block)  # type: ignore[union-attr]
                left -= len(block)
            v1_pieces += sha1_piece.digest()  # type: ignore[union-attr]
            piece_count += 1
            sha1_piece = hashlib.sha1()
            piece_fill = 0
            v1_files.append(
                {"attr": "p", "length": pad, "path": [".pad", str(pad)]}
            )

    if want_v1 and piece_fill:
        v1_pieces += sha1_piece.digest()  # type: ignore[union-attr]
        piece_count += 1

    done_bytes = total_size
    _emit("building metainfo")

    # ---------------- metainfo ---------------- #
    info: dict = {"name": name, "piece length": piece_length}

    if want_v1:
        info["pieces"] = bytes(v1_pieces)
        if single:
            info["length"] = files[0].size
        else:
            info["files"] = v1_files
    if want_v2:
        info["meta version"] = 2
        info["file tree"] = file_tree
    if private:
        info["private"] = 1
    if source.strip():
        info["source"] = source.strip()

    torrent: dict = {"info": info}

    tracker_list = [t.strip() for t in trackers if t and t.strip()]
    if tracker_list:
        torrent["announce"] = tracker_list[0]
        torrent["announce-list"] = [[t] for t in tracker_list]
    seed_list = [w.strip() for w in web_seeds if w and w.strip()]
    if seed_list:
        torrent["url-list"] = seed_list if len(seed_list) > 1 else seed_list[0]
    if comment.strip():
        torrent["comment"] = comment.strip()
    if created_by.strip():
        torrent["created by"] = created_by.strip()
    torrent["creation date"] = int(creation_date if creation_date else time.time())
    if want_v2:
        torrent["piece layers"] = piece_layers

    info_bytes = encode(info)
    data = encode(torrent)

    return TorrentResult(
        data=data,
        name=name,
        version=version,
        piece_length=piece_length,
        piece_count=piece_count if want_v1 else _v2_piece_count(files, piece_length),
        total_size=total_size,
        file_count=len(files),
        infohash_v1=hashlib.sha1(info_bytes).hexdigest() if want_v1 else None,
        infohash_v2=hashlib.sha256(info_bytes).hexdigest() if want_v2 else None,
        files=listing,
        elapsed=time.time() - started,
        padding_bytes=padding_bytes,
        trackers=tracker_list,
    )


def _v2_piece_count(files: list[SourceFile], piece_length: int) -> int:
    return sum((f.size + piece_length - 1) // piece_length for f in files)
