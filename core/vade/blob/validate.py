"""Server-side revalidation of uploaded bytes (RFC-0004 §5.3, §8.3).

Nothing a client *says* is trusted once the bytes are on disk: at `complete`
the store re-measures the file, re-hashes it and parses the container headers.
There is no decoder in core (RFC §5.4 forbids transcoding), so "decode" here
means *header* parsing plus hard pixel ceilings — enough to reject a
decompression bomb before anything would ever try to decode it, and enough to
reject polyglots via the trailer/frame checks.

All failures are field-level (`data.field`) and use existing C-04 codes only.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..errors import VadeError

__all__ = [
    "ImageInfo",
    "sniff",
    "inspect_image",
    "validate_image",
    "validate_file",
    "IMAGE_MIMES",
    "EXECUTABLE_MAGIC",
]

IMAGE_MIMES = ("image/jpeg", "image/png", "image/webp")

#: Content that must never be accepted as an "image" or a "file".
EXECUTABLE_MAGIC: tuple[tuple[bytes, str], ...] = (
    (b"MZ", "pe"),
    (b"\x7fELF", "elf"),
    (b"\xcf\xfa\xed\xfe", "macho64"),
    (b"\xce\xfa\xed\xfe", "macho32"),
    (b"\xca\xfe\xba\xbe", "macho-fat"),
    (b"#!", "script"),
)

#: JPEG SOFn markers (C4/C8/CC are not frame headers).
_SOF = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
_PNG_SIG = b"\x89PNG\r\n\x1a\n"


class _Malformed(Exception):
    """Internal: the container does not parse. Mapped to 501 by the caller."""


@dataclass(frozen=True, slots=True)
class ImageInfo:
    mime: str
    width: int
    height: int
    exif: bool
    frames: int
    trailer_ok: bool


# --------------------------------------------------------------------- sniffing
def sniff(head: bytes) -> str | None:
    """Magic-byte identification. Extension and `Content-Type` are hints only."""
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith(_PNG_SIG):
        return "image/png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return "application/pdf"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06"):
        return "application/zip"
    for magic, _kind in EXECUTABLE_MAGIC:
        if head.startswith(magic):
            return "application/x-executable"
    if head and b"\x00" not in head:
        try:
            head.decode("utf-8")
        except UnicodeDecodeError:
            return None
        return "text/plain"
    return None


def _fail(code: str, message: str, **data: object) -> VadeError:
    return VadeError(code, message, data=data)


# ------------------------------------------------------------------------ JPEG
def _jpeg(data: bytes) -> ImageInfo:
    i = 2
    width = height = 0
    exif = False
    saw_sos = False
    while i + 2 <= len(data):
        if data[i] != 0xFF:
            raise _Malformed("expected a JPEG marker")
        marker = data[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        if marker == 0xD9:  # EOI
            i += 2
            break
        if i + 4 > len(data):
            raise _Malformed("truncated JPEG segment")
        length = int.from_bytes(data[i + 2 : i + 4], "big")
        if length < 2 or i + 2 + length > len(data):
            raise _Malformed("bad JPEG segment length")
        segment = data[i + 4 : i + 2 + length]
        if marker == 0xE1 and segment.startswith(b"Exif\x00\x00"):
            exif = True
        elif marker in _SOF and not width:
            if len(segment) < 6:
                raise _Malformed("short SOF segment")
            height = int.from_bytes(segment[1:3], "big")
            width = int.from_bytes(segment[3:5], "big")
        if marker == 0xDA:  # SOS: entropy-coded data follows, stop walking
            saw_sos = True
            break
        i += 2 + length
    if not width or not height:
        raise _Malformed("no JPEG frame header")
    if not saw_sos:
        raise _Malformed("no JPEG scan")
    return ImageInfo("image/jpeg", width, height, exif, 1, data.endswith(b"\xff\xd9"))


# ------------------------------------------------------------------------- PNG
def _png(data: bytes) -> ImageInfo:
    i = 8
    width = height = 0
    exif = False
    frames = 1
    end_at: int | None = None
    while i + 12 <= len(data):
        size = int.from_bytes(data[i : i + 4], "big")
        kind = data[i + 4 : i + 8]
        chunk_end = i + 12 + size
        if chunk_end > len(data):
            raise _Malformed("truncated PNG chunk")
        if kind == b"IHDR":
            if size != 13:
                raise _Malformed("bad IHDR")
            width = int.from_bytes(data[i + 8 : i + 12], "big")
            height = int.from_bytes(data[i + 12 : i + 16], "big")
        elif kind == b"eXIf":
            exif = True
        elif kind == b"acTL":
            declared = int.from_bytes(data[i + 8 : i + 12], "big")
            frames = max(frames, declared, 2)
        if kind == b"IEND":
            end_at = chunk_end
            break
        i = chunk_end
    if not width or not height:
        raise _Malformed("no PNG IHDR")
    return ImageInfo("image/png", width, height, exif, frames, end_at == len(data))


# ------------------------------------------------------------------------ WebP
def _webp(data: bytes) -> ImageInfo:
    if len(data) < 12:
        raise _Malformed("short RIFF header")
    riff_size = int.from_bytes(data[4:8], "little")
    size_ok = riff_size + 8 == len(data)
    i = 12
    width = height = 0
    exif = False
    frames = 1
    while i + 8 <= len(data):
        fourcc = data[i : i + 4]
        size = int.from_bytes(data[i + 4 : i + 8], "little")
        body = data[i + 8 : i + 8 + size]
        if len(body) < size:
            raise _Malformed("truncated WebP chunk")
        if fourcc == b"VP8X" and size >= 10:
            width = 1 + int.from_bytes(body[4:7], "little")
            height = 1 + int.from_bytes(body[7:10], "little")
        elif fourcc == b"VP8 " and size >= 10 and body[3:6] == b"\x9d\x01\x2a":
            width = int.from_bytes(body[6:8], "little") & 0x3FFF
            height = int.from_bytes(body[8:10], "little") & 0x3FFF
        elif fourcc == b"VP8L" and size >= 5 and body[0] == 0x2F:
            bits = int.from_bytes(body[1:5], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
        elif fourcc in (b"ANIM", b"ANMF"):
            frames = 2
        elif fourcc == b"EXIF":
            exif = True
        i += 8 + size + (size & 1)  # chunks are even-padded
    if not width or not height:
        raise _Malformed("no WebP frame header")
    return ImageInfo("image/webp", width, height, exif, frames, size_ok)


_PARSERS = {"image/jpeg": _jpeg, "image/png": _png, "image/webp": _webp}


def inspect_image(data: bytes, mime: str) -> ImageInfo:
    """Parse container headers. Raises 501 `data.field=mime` when malformed."""
    parser = _PARSERS.get(mime)
    if parser is None:
        raise _fail("UNSUPPORTED", "unsupported image type", field="mime")
    try:
        return parser(data)
    except _Malformed as exc:
        raise VadeError("UNSUPPORTED", f"malformed or unsupported image: {exc}", data={"field": "mime"}) from None


# -------------------------------------------------------------------- validation
def validate_image(
    data: bytes,
    *,
    declared_mime: str,
    declared_width: int | None,
    declared_height: int | None,
    max_bytes: int,
    max_edge: int,
    max_pixels: int,
) -> ImageInfo:
    """§5.3 table, in order. Any failure means no `file_id` is ever produced."""
    actual = len(data)
    if actual > max_bytes:
        raise _fail("PAYLOAD_TOO_LARGE", "image exceeds the configured byte limit", field="size", limit=max_bytes, actual=actual)

    sniffed = sniff(data[:64])
    if sniffed is None:
        raise _fail("UNSUPPORTED", "content type could not be confirmed from the file's magic bytes", field="mime")
    if sniffed != declared_mime:
        raise _fail("INVALID_ARGUMENT", "declared mime does not match the actual bytes", field="mime", sniffed=sniffed)
    for magic, kind in EXECUTABLE_MAGIC:
        if data.startswith(magic):
            raise _fail("UNSUPPORTED", f"executable content is not accepted as an image ({kind})", field="mime")

    info = inspect_image(data, sniffed)

    if declared_width is not None and info.width != declared_width:
        raise _fail("INVALID_ARGUMENT", "declared width does not match the image", field="width", expected=declared_width, actual=info.width)
    if declared_height is not None and info.height != declared_height:
        raise _fail("INVALID_ARGUMENT", "declared height does not match the image", field="height", expected=declared_height, actual=info.height)

    long_edge = max(info.width, info.height)
    if long_edge > max_edge:
        raise _fail("PAYLOAD_TOO_LARGE", "image exceeds the configured edge limit", field="pixels", limit=max_edge, actual=long_edge)
    pixels = info.width * info.height
    if pixels > max_pixels:
        raise _fail("PAYLOAD_TOO_LARGE", "image exceeds the configured pixel budget", field="pixels", limit=max_pixels, actual=pixels)

    if info.exif:
        raise _fail("UNSUPPORTED", "image still carries EXIF metadata; strip it on the client", field="exif")
    if info.frames > 1:
        raise _fail("UNSUPPORTED", "animated images are not accepted; send a single frame", field="frames")
    if not info.trailer_ok:
        raise _fail("UNSUPPORTED", "file has bytes after its end marker (polyglot guard)", field="trailer")
    return info


_FILE_CONTAINERS = {
    "application/pdf": "application/pdf",
    "application/zip": "application/zip",
    "text/plain": "text/plain",
}


def validate_file(data_head: bytes, *, declared_mime: str) -> str:
    """Confirm a `file` part from its content (FR-8.38 whitelist by content).

    v1 accepts containers core can actually identify: PDF, ZIP/Office and
    UTF-8 text (plus images uploaded as files). Anything else is refused
    instead of being stored as opaque bytes.
    """
    for magic, kind in EXECUTABLE_MAGIC:
        if data_head.startswith(magic):
            raise _fail("UNSUPPORTED", f"executable content is not accepted ({kind})", field="mime")

    sniffed = sniff(data_head[:64])
    if sniffed is None:
        raise _fail("UNSUPPORTED", "file type could not be confirmed from content", field="mime")
    if sniffed in IMAGE_MIMES or sniffed == "application/x-executable":
        # An image declared as a file is allowed only if it really is one; the
        # executable case was already refused above.
        if sniffed != declared_mime:
            raise _fail("INVALID_ARGUMENT", "declared mime does not match the actual bytes", field="mime", sniffed=sniffed)
        return sniffed

    expected = _FILE_CONTAINERS.get(sniffed)
    declared = declared_mime.lower()
    if expected == "application/pdf" and declared != "application/pdf":
        raise _fail("INVALID_ARGUMENT", "declared mime does not match the actual bytes", field="mime", sniffed=sniffed)
    if expected == "application/zip" and not (
        declared in {"application/zip", "application/x-zip-compressed"}
        or "openxmlformats" in declared
        or "officedocument" in declared
    ):
        raise _fail("INVALID_ARGUMENT", "declared mime does not match the actual bytes", field="mime", sniffed=sniffed)
    if expected == "text/plain" and not declared.startswith("text/"):
        raise _fail("INVALID_ARGUMENT", "declared mime does not match the actual bytes", field="mime", sniffed=sniffed)
    if expected is None:
        raise _fail("UNSUPPORTED", "file type is not on the v1 whitelist", field="mime")
    return expected
