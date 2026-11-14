"""Synthetic containers for validation tests.

No image codec is installed (and core must not carry one), so the fixtures are
the smallest byte sequences that each parser accepts: a real IHDR/frame header
plus the exact end marker. That is enough to prove the *checks* work — the
parsers never decode pixels.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIG = b"\x89PNG\r\n\x1a\n"


def _chunk(kind: bytes, data: bytes) -> bytes:
    return len(data).to_bytes(4, "big") + kind + data + zlib.crc32(kind + data).to_bytes(4, "big")


def png(
    width: int = 8,
    height: int = 8,
    *,
    animated: bool = False,
    exif: bool = False,
    trailer: bytes = b"",
    pad_idat: int = 0,
) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x00" * (min(width, 64) * 3)
    payload = zlib.compress(row) + (b"\x00" * pad_idat)
    out = PNG_SIG + _chunk(b"IHDR", header)
    if animated:
        out += _chunk(b"acTL", struct.pack(">II", 2, 0))
    if exif:
        out += _chunk(b"eXIf", b"\x00" * 8)
    out += _chunk(b"IDAT", payload)
    out += _chunk(b"IEND", b"")
    return out + trailer


def jpeg(
    width: int = 8,
    height: int = 8,
    *,
    exif: bool = False,
    trailer: bytes = b"",
    scan: bool = True,
) -> bytes:
    out = b"\xff\xd8"
    out += b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    if exif:
        body = b"Exif\x00\x00" + b"II*\x00\x08\x00\x00\x00" + b"\x00" * 6
        out += b"\xff\xe1" + (len(body) + 2).to_bytes(2, "big") + body
    sof = bytes([8]) + height.to_bytes(2, "big") + width.to_bytes(2, "big") + bytes([3])
    sof += b"\x01\x11\x00" + b"\x02\x11\x01" + b"\x03\x11\x01"
    out += b"\xff\xc0" + (len(sof) + 2).to_bytes(2, "big") + sof
    if scan:
        sos = bytes([3]) + b"\x01\x00\x02\x11\x03\x11" + b"\x00\x3f\x00"
        out += b"\xff\xda" + (len(sos) + 2).to_bytes(2, "big") + sos
        out += b"\x00\x11\x22"
    out += b"\xff\xd9"
    return out + trailer


def webp_lossless(width: int = 8, height: int = 8, *, animated: bool = False) -> bytes:
    bits = (width - 1) | ((height - 1) << 14)
    body = b"\x2f" + bits.to_bytes(4, "little") + b"\x00\x00\x00\x00"
    chunks = b""
    if animated:
        chunks += b"ANIM" + (6).to_bytes(4, "little") + b"\x00" * 6
    chunks += b"VP8L" + len(body).to_bytes(4, "little") + body
    if len(body) % 2:
        chunks += b"\x00"
    riff = b"WEBP" + chunks
    return b"RIFF" + (len(riff)).to_bytes(4, "little") + riff


def exe_payload(size: int = 64) -> bytes:
    return b"MZ" + b"\x90" * (size - 2)
