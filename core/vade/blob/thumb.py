"""`?v=thumb` derivation (RFC-0004 §4.4).

The variant is an address, not a second `file_id`. Derivation is best-effort by
design: core carries no image codec (RFC §5.4), so when Pillow is not installed
the endpoint answers `415 UNSUPPORTED` and the client falls back to a type icon.
The derived bytes are cached at `blobs/<sha>.thumb` and never participate in
dedupe or message parts.
"""

from __future__ import annotations

import io
from pathlib import Path

from ..errors import VadeError

__all__ = ["derive_thumb", "THUMB_FORMAT", "THUMB_QUALITY"]

THUMB_FORMAT = "WEBP"
THUMB_QUALITY = 80


def derive_thumb(source: Path | str, max_edge: int) -> bytes:
    try:
        from PIL import Image  # type: ignore
    except ImportError:
        raise VadeError(
            "UNSUPPORTED",
            "server-side thumbnails are unavailable (no image codec installed); use a type icon",
            data={"field": "thumb", "reason": "no_codec"},
        ) from None

    path = Path(source)
    try:
        with Image.open(path) as image:
            # `draft` lets the decoder skip pixels we would throw away, which is
            # the cheap half of the decode-bomb guard (the header/canvas checks
            # in validate.py are the other half: this only ever runs on a blob
            # that already passed them).
            image.draft("RGB", (max_edge * 2, max_edge * 2))
            image.thumbnail((max_edge, max_edge))
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format=THUMB_FORMAT, quality=THUMB_QUALITY)
            return buffer.getvalue()
    except VadeError:
        raise
    except Exception:
        raise VadeError(
            "UNSUPPORTED",
            "thumbnail derivation failed",
            data={"field": "thumb", "reason": "encode_failed"},
        ) from None
