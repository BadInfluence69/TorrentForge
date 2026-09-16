"""Minimal, strict bencode encoder/decoder (BEP 3).

No third-party dependencies.  Keys are always emitted as raw byte strings and
dictionaries are always written in ascending raw-byte key order, which is what
the BitTorrent spec requires for a canonical info dictionary (the info-hash
depends on it byte for byte).
"""

from __future__ import annotations

__all__ = ["encode", "decode", "BencodeError"]


class BencodeError(ValueError):
    """Raised when data cannot be encoded to or decoded from bencode."""


# --------------------------------------------------------------------------- #
# Encoding
# --------------------------------------------------------------------------- #
def _encode(value, out: bytearray) -> None:
    if isinstance(value, bool):
        # bool is a subclass of int; torrents use 1/0 for flags such as private
        out += b"i1e" if value else b"i0e"
    elif isinstance(value, int):
        out += b"i%de" % value
    elif isinstance(value, bytes):
        out += b"%d:" % len(value)
        out += value
    elif isinstance(value, bytearray):
        data = bytes(value)
        out += b"%d:" % len(data)
        out += data
    elif isinstance(value, str):
        data = value.encode("utf-8")
        out += b"%d:" % len(data)
        out += data
    elif isinstance(value, (list, tuple)):
        out += b"l"
        for item in value:
            _encode(item, out)
        out += b"e"
    elif isinstance(value, dict):
        out += b"d"
        items = []
        for key, val in value.items():
            if isinstance(key, str):
                key = key.encode("utf-8")
            elif isinstance(key, bytearray):
                key = bytes(key)
            if not isinstance(key, bytes):
                raise BencodeError(f"dictionary keys must be strings, got {type(key)!r}")
            items.append((key, val))
        items.sort(key=lambda kv: kv[0])
        seen = None
        for key, val in items:
            if key == seen:
                raise BencodeError(f"duplicate dictionary key {key!r}")
            seen = key
            _encode(key, out)
            _encode(val, out)
        out += b"e"
    else:
        raise BencodeError(f"cannot bencode object of type {type(value)!r}")


def encode(value) -> bytes:
    """Serialise ``value`` (int/bytes/str/list/dict) to a bencoded byte string."""
    out = bytearray()
    _encode(value, out)
    return bytes(out)


# --------------------------------------------------------------------------- #
# Decoding
# --------------------------------------------------------------------------- #
def _decode(data: bytes, pos: int):
    if pos >= len(data):
        raise BencodeError("unexpected end of data")
    char = data[pos : pos + 1]

    if char == b"i":
        end = data.find(b"e", pos)
        if end < 0:
            raise BencodeError("unterminated integer")
        raw = data[pos + 1 : end]
        if raw in (b"", b"-") or (raw.startswith(b"0") and raw != b"0") or raw.startswith(b"-0"):
            raise BencodeError(f"invalid integer {raw!r}")
        return int(raw), end + 1

    if char == b"l":
        pos += 1
        items = []
        while data[pos : pos + 1] != b"e":
            item, pos = _decode(data, pos)
            items.append(item)
        return items, pos + 1

    if char == b"d":
        pos += 1
        result = {}
        while data[pos : pos + 1] != b"e":
            key, pos = _decode(data, pos)
            if not isinstance(key, bytes):
                raise BencodeError("dictionary key is not a byte string")
            value, pos = _decode(data, pos)
            result[key] = value
        return result, pos + 1

    if char.isdigit():
        colon = data.find(b":", pos)
        if colon < 0:
            raise BencodeError("unterminated byte string length")
        length = int(data[pos:colon])
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("byte string runs past end of data")
        return data[start:end], end

    raise BencodeError(f"unexpected byte {char!r} at offset {pos}")


def decode(data: bytes):
    """Parse a bencoded byte string.  Byte strings stay ``bytes``."""
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError("decode() expects bytes")
    value, pos = _decode(bytes(data), 0)
    if pos != len(data):
        raise BencodeError(f"trailing data after value ({len(data) - pos} bytes)")
    return value
