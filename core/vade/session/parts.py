"""`ui/submit` content parts: shape (§6.1), projection (§6.2), ladder (§6.4).

The **order** of the checks is part of the contract, not an implementation
detail: a client that sends an unknown part type *and* a stale `file_id` must be
told about the type first, otherwise "fix the UI" requires reading core source.
`SessionLog.submit` therefore calls exactly the four entry points below, in this
order:

    require_shape  → normalize  → resolve_attachments  → check_limits

with the idempotency lookup wedged between `require_shape` and `normalize`
(§6.4 step 2 — a retried `client_msg_id` must not start failing because a model
capability or a TTL changed in between).

Nothing here writes: the caller persists the message, then pins the blobs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from ..blob.ids import is_id
from ..errors import VadeError
from ..logging import get_logger

__all__ = [
    "TEXT",
    "IMAGE",
    "FILE",
    "AUDIO",
    "PART_TYPES",
    "HARD_MAX_PARTS",
    "HARD_TEXT_CHARS",
    "HARD_IMAGE_BYTES",
    "HARD_IMAGE_EDGE",
    "HARD_IMAGE_PIXELS",
    "IMAGE_MIMES",
    "PartLimits",
    "require_shape",
    "normalize",
    "parts_text",
    "has_renderable",
    "check_limits",
    "resolve_attachments",
    "apply_attachment_metadata",
    "attachment_ids",
]

TEXT = "text"
IMAGE = "image"
FILE = "file"
AUDIO = "audio"
PART_TYPES = (TEXT, IMAGE, FILE, AUDIO)

#: C-05 1.1.0 `ui/submit.content.maxItems` (§7.2). `attach.max_per_turn` is the soft
#: ceiling (10) the config owns; this one is the frozen schema's hard cap.
HARD_MAX_PARTS = 20

#: C-05 / D96 / §7.2 frozen ceilings. C-18's `[attach]` may *narrow* these but
#: never widen them: a part above them is one the frozen schema (or the D96
#: sizing rule behind it) rejects on the wire, so `PartLimits` clamps and warns
#: instead of letting the same core emit history its own schema calls illegal.
HARD_TEXT_CHARS = 32000
HARD_IMAGE_BYTES = 5242880
HARD_IMAGE_EDGE = 2048
HARD_IMAGE_PIXELS = 4194304

#: §6.1: the only three image types that may be attached (FR-8.38).
IMAGE_MIMES = frozenset({"image/jpeg", "image/png", "image/webp"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}\Z")

_log = get_logger("vade.session.parts")


# --------------------------------------------------------------------- limits
@dataclass(frozen=True, slots=True)
class PartLimits:
    """The subset of `[attach]` (C-18 §7.4) that `ui/submit` judges against.

    A copy, not a reference: the session component must not depend on the blob
    package's settings object to answer "is this submit too big". `max_bytes`
    doubles as the per-turn total ceiling — a turn of 10 files each at the
    100 MiB hard cap is a quota event, not a legitimate message.
    """

    enabled: bool = True
    max_per_turn: int = 10
    text_max_chars: int = 32000
    image_max_bytes: int = 5242880
    image_max_edge: int = 2048
    image_max_pixels: int = 4194304
    max_bytes: int = 104857600

    def __post_init__(self) -> None:
        # C-18 owns the dial, C-05 owns the ceiling. A configured number above a
        # frozen one is clamped (loudly) rather than honoured: otherwise the same
        # core, given a legal `[attach]`, produces messages its own schema calls
        # illegal — and every step-5 check built on these numbers becomes a
        # check against a client-chosen ceiling again.
        for name, ceiling, floor in (
            ("max_per_turn", HARD_MAX_PARTS, 1),
            ("text_max_chars", HARD_TEXT_CHARS, 1),
            ("image_max_bytes", HARD_IMAGE_BYTES, 1),
            ("image_max_edge", HARD_IMAGE_EDGE, 1),
            ("image_max_pixels", HARD_IMAGE_PIXELS, 1),
        ):
            value = int(getattr(self, name))
            bounded = min(max(value, floor), ceiling)
            if bounded != value:
                _log.warning(
                    "attach.%s=%s exceeds the frozen C-05 ceiling %s; clamped",
                    name,
                    value,
                    ceiling,
                )
                object.__setattr__(self, name, bounded)

    @classmethod
    def from_config(cls, config: Any) -> "PartLimits":
        base = cls()
        if config is None:
            return base

        def get(key: str, fallback: Any) -> Any:
            value = config.get(f"attach.{key}", None)
            return fallback if value is None else value

        try:
            return cls(
                enabled=bool(get("enabled", base.enabled)),
                max_per_turn=int(get("max_per_turn", base.max_per_turn)),
                text_max_chars=int(get("text_max_chars", base.text_max_chars)),
                image_max_bytes=int(get("image.max_bytes", base.image_max_bytes)),
                image_max_edge=int(get("image.max_edge", base.image_max_edge)),
                image_max_pixels=int(get("image.max_pixels", base.image_max_pixels)),
                max_bytes=int(get("max_bytes", base.max_bytes)),
            )
        except (TypeError, ValueError):
            # A malformed value is a config error the schema validator already
            # rejected at startup; a hand-built Config falls back to defaults.
            return base


# ---------------------------------------------------------------------- shape
def _invalid(field: str, message: str, **data: Any) -> VadeError:
    payload: dict[str, Any] = {"field": field}
    payload.update(data)
    return VadeError("INVALID_ARGUMENT", message, data=payload)


def _too_large(field: str, message: str, **data: Any) -> VadeError:
    payload: dict[str, Any] = {"field": field}
    payload.update(data)
    return VadeError("PAYLOAD_TOO_LARGE", message, data=payload)


def require_shape(content: Any) -> tuple[Mapping[str, Any], ...]:
    """§6.4 step 1: `content` is a non-empty array of at most 20 objects."""
    if isinstance(content, (str, bytes, bytearray)) or not isinstance(content, Sequence):
        raise _invalid("content", "content must be an array of parts")
    blocks = tuple(content)
    if not blocks:
        raise _invalid("content", "content must not be empty", min=1, actual=0)
    if len(blocks) > HARD_MAX_PARTS:
        raise _invalid(
            "content",
            f"content has more than {HARD_MAX_PARTS} parts",
            limit=HARD_MAX_PARTS,
            actual=len(blocks),
        )
    return blocks


# ------------------------------------------------------------------ part kinds
def _require_str(part: Mapping[str, Any], field: str, key: str, *, max_len: int) -> str:
    value = part.get(key)
    if not isinstance(value, str) or not value:
        raise _invalid(f"{field}.{key}", f"{key} is required")
    if len(value) > max_len:
        raise _invalid(f"{field}.{key}", f"{key} is longer than {max_len} characters", limit=max_len)
    return value


def _require_int(
    part: Mapping[str, Any], field: str, key: str, *, minimum: int, maximum: int | None = None
) -> int:
    value = part.get(key)
    # `True` is an int in Python; a boolean dimension is a client bug, not a
    # 1-pixel image.
    if not isinstance(value, int) or isinstance(value, bool):
        raise _invalid(f"{field}.{key}", f"{key} must be an integer")
    if value < minimum:
        raise _invalid(f"{field}.{key}", f"{key} must be >= {minimum}", minimum=minimum)
    if maximum is not None and value > maximum:
        raise _invalid(
            f"{field}.{key}", f"{key} must be <= {maximum}", limit=maximum, actual=value
        )
    return int(value)


def _require_mime(part: Mapping[str, Any], field: str, *, allowed: frozenset[str] | None) -> str:
    mime = _require_str(part, field, "mime", max_len=255)
    if allowed is not None:
        if mime not in allowed:
            raise VadeError(
                "UNSUPPORTED",
                f"mime {mime!r} is not an accepted image type",
                data={"field": f"{field}.mime", "allowed": sorted(allowed)},
            )
    elif "/" not in mime or mime.startswith("/") or mime.endswith("/"):
        raise _invalid(f"{field}.mime", "mime must look like type/subtype")
    return mime


def _require_file_id(part: Mapping[str, Any], field: str) -> str:
    """Shape only (§6.4 step 3). Existence/ownership is step 4."""
    file_id = part.get("file_id")
    if not isinstance(file_id, str) or not file_id:
        raise _invalid(f"{field}.file_id", "file_id is required")
    return file_id


def _measured_dimension(value: Any) -> int | None:
    """A `BlobRecord` dimension as a positive int, or `None` if unusable.

    The record's numbers come from the decoder, not the client, so there is no
    message to write: a non-positive or missing edge is a blob the session
    layer must refuse rather than serialise into a schema-illegal part.
    """
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return int(value)


def _normalize_image(part: Mapping[str, Any], field: str, limits: PartLimits) -> dict[str, Any]:
    out = {
        "type": IMAGE,
        "file_id": _require_file_id(part, field),
        "mime": _require_mime(part, field, allowed=IMAGE_MIMES),
        "name": _require_str(part, field, "name", max_len=255),
        "size": _require_int(part, field, "size", minimum=1),
        "width": _require_int(part, field, "width", minimum=1, maximum=int(limits.image_max_edge)),
        "height": _require_int(part, field, "height", minimum=1, maximum=int(limits.image_max_edge)),
    }
    sha = part.get("sha256")
    if not isinstance(sha, str) or not _SHA256_RE.match(sha):
        raise _invalid(f"{field}.sha256", "sha256 must be 64 lowercase hex characters")
    out["sha256"] = sha
    return out


def _normalize_file(part: Mapping[str, Any], field: str) -> dict[str, Any]:
    return {
        "type": FILE,
        "file_id": _require_file_id(part, field),
        "mime": _require_mime(part, field, allowed=None),
        "name": _require_str(part, field, "name", max_len=255),
        "size": _require_int(part, field, "size", minimum=1),
    }


def normalize(
    blocks: Sequence[Mapping[str, Any]], limits: PartLimits
) -> tuple[dict[str, Any], ...]:
    """§6.4 step 3: part types, required fields, per-turn text budget.

    Returns a *new* tuple of plain dicts with only the frozen fields, in the
    caller's order (§6.1: the server must never reorder parts). Unknown extra
    keys are dropped rather than stored, so the `messages.parts` payload cannot
    become a smuggling channel for client state.
    """
    if len(blocks) > int(limits.max_per_turn):
        raise _invalid(
            "content",
            f"a turn may carry at most {limits.max_per_turn} parts",
            limit=int(limits.max_per_turn),
            actual=len(blocks),
        )

    out: list[dict[str, Any]] = []
    text_chars = 0
    for index, block in enumerate(blocks):
        field = f"content[{index}]"
        if not isinstance(block, Mapping):
            raise _invalid(field, "part must be an object")
        kind = block.get("type")
        if not isinstance(kind, str) or not kind:
            raise _invalid(f"{field}.type", "part.type is required")

        if kind == TEXT:
            text = block.get("text")
            if not isinstance(text, str) or not text:
                raise _invalid(f"{field}.text", "text part requires a non-empty string")
            if len(text) > int(limits.text_max_chars):
                raise _too_large(
                    f"{field}.text",
                    f"text part exceeds {limits.text_max_chars} characters",
                    limit=int(limits.text_max_chars),
                    actual=len(text),
                )
            text_chars += len(text)
            out.append({"type": TEXT, "text": text})
            continue

        if kind == AUDIO:
            # D97: the type stays in C-05 so the four FR-8.42 classes are not
            # reduced; the behaviour is not shipped. Turning it on is a core
            # predicate change, not a schema change (§7.2).
            raise VadeError(
                "UNSUPPORTED",
                "audio parts are not accepted in v1",
                data={"field": f"{field}.type", "reason": "audio_disabled"},
            )

        if kind not in (IMAGE, FILE):
            raise _invalid(
                f"{field}.type",
                f"unknown part type {kind!r}",
                allowed=list(PART_TYPES),
            )

        if not limits.enabled:
            raise VadeError(
                "UNSUPPORTED",
                "attachments are disabled on this core",
                data={"field": f"{field}.type", "reason": "attachments_disabled"},
            )

        if kind == IMAGE:
            out.append(_normalize_image(block, field, limits))
        else:
            out.append(_normalize_file(block, field))

    if text_chars > int(limits.text_max_chars):
        raise _too_large(
            "content",
            f"text parts exceed {limits.text_max_chars} characters in total",
            limit=int(limits.text_max_chars),
            actual=text_chars,
        )
    return tuple(out)


# ------------------------------------------------------------------ projection
def parts_text(parts: Sequence[Mapping[str, Any]]) -> str:
    """§6.2 text projection. Frozen: compaction, `user_text` and tests read it.

    text → as-is; image → `[image: {name}]`; file → `[file: {name}]`; joined by
    newlines. A pure-image turn therefore gets a non-empty `user_text`, which is
    what keeps "empty text" from becoming a second rejection point elsewhere.
    """
    pieces: list[str] = []
    for part in parts:
        kind = part.get("type")
        if kind == TEXT:
            text = str(part.get("text") or "")
            if text:
                pieces.append(text)
        elif kind == IMAGE:
            pieces.append(f"[image: {part.get('name') or 'image'}]")
        elif kind == FILE:
            pieces.append(f"[file: {part.get('name') or 'file'}]")
        elif kind == AUDIO:
            pieces.append(f"[audio: {part.get('name') or 'audio'}]")
    return "\n".join(pieces)


def has_renderable(parts: Sequence[Mapping[str, Any]]) -> bool:
    """§6.4 step 6: at least one part a message renderer can draw."""
    for part in parts:
        kind = part.get("type")
        if kind in (IMAGE, FILE):
            return True
        if kind == TEXT and str(part.get("text") or "").strip():
            return True
    return False


# ---------------------------------------------------------------------- limits
def check_limits(parts: Sequence[Mapping[str, Any]], limits: PartLimits) -> None:
    """§6.4 step 5: bytes, pixels and edge length against `[attach]`, then the total.

    Judge the *authoritative* numbers: `manager.submit` runs
    `apply_attachment_metadata` before this, so `size/width/height` are the ones
    the blob store measured, not the ones the client typed.

    Only `attach.*` static ceilings exist here: per-model caps need RFC-0005
    (§9), which is not landed, so the documented degradation is "static limits
    only, no vision gate".
    """
    total = 0
    for index, part in enumerate(parts):
        field = f"content[{index}]"
        kind = part.get("type")
        size = int(part.get("size") or 0)
        total += size
        if kind == IMAGE:
            if size > int(limits.image_max_bytes):
                raise _too_large(
                    f"{field}.size",
                    f"image exceeds {limits.image_max_bytes} bytes",
                    limit=int(limits.image_max_bytes),
                    actual=size,
                )
            width = int(part.get("width") or 0)
            height = int(part.get("height") or 0)
            edge = int(limits.image_max_edge)
            # C-05 froze `width/height maximum: 2048`; a declared 1×1 over a
            # 4000 px blob used to slip past both this and the schema.
            if width > edge:
                raise _too_large(
                    f"{field}.width",
                    f"image is wider than {edge} pixels",
                    limit=edge,
                    actual=width,
                )
            if height > edge:
                raise _too_large(
                    f"{field}.height",
                    f"image is taller than {edge} pixels",
                    limit=edge,
                    actual=height,
                )
            if width * height > int(limits.image_max_pixels):
                raise _too_large(
                    f"{field}.pixels",
                    f"image exceeds {limits.image_max_pixels} pixels",
                    limit=int(limits.image_max_pixels),
                    actual=width * height,
                )
        elif kind == FILE:
            if size > int(limits.max_bytes):
                raise _too_large(
                    f"{field}.size",
                    f"file exceeds {limits.max_bytes} bytes",
                    limit=int(limits.max_bytes),
                    actual=size,
                )
    if total > int(limits.max_bytes):
        raise _too_large(
            "content",
            f"attachments exceed {limits.max_bytes} bytes in total",
            limit=int(limits.max_bytes),
            actual=total,
        )


# ----------------------------------------------------------------- attachments
@dataclass(frozen=True, slots=True)
class _Principal:
    """Minimal stand-in for `blob.auth.Principal` (only `device_id` is read)."""

    device_id: str


def resolve_attachments(
    service: Any,
    parts: Sequence[Mapping[str, Any]],
    *,
    device_id: str | None = None,
) -> list[Any]:
    """§6.4 step 4: every `file_id` exists, is visible, unexpired and ours.

    `service` is the live `BlobService` (only `stat()` is called), so the checks
    are the upload channel's own checks — a second, weaker copy of "does this
    file exist" is how the two halves drift apart. Malformed ids get the same
    NOT_FOUND as missing ones (§8.4: no oracle), but the field pointer names the
    part, so the UI can highlight the chip.

    An attachment with no device identity is **fail-closed**: under the default
    `read_scope="core"`, `stat()` deliberately hands back another device's blob
    (multi-device history, §8.5), so "is it ours" has to be answered here — and
    with no identity there is nothing to compare. A transport that does not put
    `device_id` on the connection simply cannot submit parts carrying a
    `file_id`; text-only turns are unaffected.
    """
    wanted = [
        (index, part)
        for index, part in enumerate(parts)
        if part.get("type") in (IMAGE, FILE)
    ]
    if not wanted:
        return []
    if service is None:
        index = wanted[0][0]
        raise VadeError(
            "UNSUPPORTED",
            "attachment storage is not available",
            data={"field": f"content[{index}].type", "reason": "attachments_disabled"},
        )
    if device_id is None:
        index = wanted[0][0]
        raise VadeError(
            "PERMISSION_DENIED",
            "attachments require a known device identity",
            data={"field": f"content[{index}].file_id", "reason": "device_unknown"},
        )

    principal = _Principal(device_id=str(device_id))
    records: list[Any] = []
    for index, part in wanted:
        field = f"content[{index}].file_id"
        file_id = str(part.get("file_id") or "")
        if not is_id(file_id) or not file_id.startswith("blb_"):
            raise VadeError("NOT_FOUND", "file not found", data={"field": field})
        try:
            blob = service.stat(principal, file_id)
        except VadeError as exc:
            # Re-anchor the pointer: `stat` reports `field: file_id`, which is
            # useless to a client looking at an array of parts.
            raise VadeError(
                exc.code, exc.message, data={**(exc.data or {}), "field": field}
            ) from exc
        # `stat()` only enforces ownership when `read_scope="device"`; a submit
        # may never reference another device's blob, whatever the read scope.
        if getattr(blob, "device_id", None) != device_id:
            raise VadeError(
                "PERMISSION_DENIED", "file belongs to another device", data={"field": field}
            )
        if getattr(blob, "state", "ok") != "ok":
            raise VadeError(
                "NOT_FOUND", "file contents are no longer available", data={"field": field}
            )
        scan = getattr(blob, "scan", "clean")
        if scan == "pending":
            # §8.5: an unfinished scan is a "not yet", not a "missing".
            raise VadeError(
                "UNSUPPORTED",
                "file is still being scanned",
                data={"field": field, "reason": "scan_pending"},
            )
        if scan not in ("clean", "scanning"):
            raise VadeError("NOT_FOUND", "file was removed by the scanner", data={"field": field})
        records.append(blob)
    return records


def apply_attachment_metadata(
    parts: Sequence[Mapping[str, Any]], records: Sequence[Any]
) -> tuple[dict[str, Any], ...]:
    """§6.4 step 4, second half: the blob store owns the metadata.

    `normalize` only *parses* what the client claims. `BlobRecord` is what the
    upload channel actually measured: real byte count, decoded dimensions,
    sniffed mime, recomputed sha256, stored name. Keeping the claim let a
    3 MiB / 4000 px image travel as `{"size":1,"width":1,"height":1}` and turned
    step 5 into a check of a number the client picked — so every attachment
    field is replaced here, *before* `check_limits` runs.

    The part type is checked against the record as well: an `image` part may
    only name a blob whose authoritative mime is one of `IMAGE_MIMES` (the
    upload side re-derives image-ness from the mime — `kind` is a client hint
    there — so mime, not the hint, is the honest predicate), and a `file` part
    may name anything. A mismatch is the client lying about the chip it draws.
    """
    by_id: dict[str, Any] = {}
    for record in records:
        file_id = str(getattr(record, "file_id", "") or "")
        if file_id:
            by_id[file_id] = record

    out: list[dict[str, Any]] = [dict(part) for part in parts]
    for index, part in enumerate(parts):
        kind = part.get("type")
        if kind not in (IMAGE, FILE):
            continue
        field = f"content[{index}]"
        file_id = str(part.get("file_id") or "")
        record = by_id.get(file_id)
        if record is None:
            # `resolve_attachments` just proved it exists; a miss is a wiring bug.
            raise VadeError("NOT_FOUND", "file not found", data={"field": f"{field}.file_id"})

        mime = str(getattr(record, "mime", "") or "")
        name = str(getattr(record, "name", "") or "")
        size = int(getattr(record, "size", 0) or 0)

        if kind == FILE:
            out[index] = {
                "type": FILE,
                "file_id": file_id,
                "mime": mime,
                "name": name,
                "size": size,
            }
            continue

        if mime not in IMAGE_MIMES:
            raise _invalid(
                f"{field}.kind",
                f"part type 'image' but the stored file is {mime or 'untyped'!r}",
                reason="kind_mismatch",
                expected=sorted(IMAGE_MIMES),
                actual=mime or None,
            )
        width = _measured_dimension(getattr(record, "width", None))
        height = _measured_dimension(getattr(record, "height", None))
        if width is None or height is None:
            # C-05 requires integer dimensions; a record without them cannot be
            # serialised into a legal image part.
            raise VadeError(
                "UNSUPPORTED",
                "stored image has no decoded dimensions",
                data={"field": f"{field}.width", "reason": "image_dimensions_unknown"},
            )
        out[index] = {
            "type": IMAGE,
            "file_id": file_id,
            "mime": mime,
            "name": name,
            "size": size,
            "width": width,
            "height": height,
            "sha256": str(getattr(record, "sha256", "") or ""),
        }
    return tuple(out)


def attachment_ids(records: Sequence[Any]) -> list[str]:
    ids: list[str] = []
    for record in records:
        file_id = str(getattr(record, "file_id", "") or "")
        if file_id and file_id not in ids:
            ids.append(file_id)
    return ids
