"""TorrentForge library: bencode, filesystem browsing and torrent creation."""

from .bencode import decode, encode
from .creator import (
    BLOCK_SIZE,
    TorrentError,
    TorrentResult,
    auto_piece_length,
    create_torrent,
    scan_path,
    valid_piece_lengths,
)

__all__ = [
    "encode",
    "decode",
    "BLOCK_SIZE",
    "TorrentError",
    "TorrentResult",
    "auto_piece_length",
    "create_torrent",
    "scan_path",
    "valid_piece_lengths",
]

__version__ = "1.0.0"
