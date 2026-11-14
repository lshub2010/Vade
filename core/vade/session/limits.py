"""RFC-0006 §8: the single-message text ceiling.

32,000 Unicode code points is a protocol constant, not a config key (C-18 is
unchanged by RFC-0006). RFC-0004's `attach.text_max_chars` must carry the same
number; `tools/check_contracts.py` asserts the two agree.

Counting unit is the code point (`len(str)`), deliberately not UTF-16 units:
16,500 non-BMP emoji are 33,000 in JavaScript's `.length` and would be refused
by a client that counted the wrong way, while the core accepts them (A14).
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from ..errors import VadeError

__all__ = ["TEXT_LIMIT", "content_text", "check_content_limits"]

#: Max code points across all text parts of one message (RFC-0006 §8).
TEXT_LIMIT = 32_000


def content_text(blocks: Sequence[Mapping[str, Any]]) -> str:
    """Flatten `content` to the text the core stores (unchanged `_text_of`)."""
    parts = [str(b.get("text", "")) for b in blocks if b.get("type", "text") == "text"]
    return "\n".join(p for p in parts if p)


def _too_large(field: str, actual: int) -> VadeError:
    return VadeError(
        "PAYLOAD_TOO_LARGE",
        f"{field} exceeds {TEXT_LIMIT} code points",
        data={"field": field, "limit": TEXT_LIMIT, "actual": actual, "unit": "codepoints"},
    )


def check_content_limits(blocks: Sequence[Mapping[str, Any]]) -> None:
    """Validate `content` lengths in the RFC-0006 §8 order.

    The caller has already checked the shape (list of objects) and the session
    write permission, so this is steps ③ and ④: per-part length, then the sum.
    Raises `PAYLOAD_TOO_LARGE` with a field-level `data` payload; nothing is
    persisted when it raises.
    """
    total = 0
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping) or block.get("type", "text") != "text":
            continue
        text = block.get("text") or ""
        length = len(str(text))
        if length > TEXT_LIMIT:
            raise _too_large(f"content[{index}].text", length)
        total += length
    if total > TEXT_LIMIT:
        raise _too_large("content", total)
