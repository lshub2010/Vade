"""Rust -> C-04 error translation. Never lets an FFI exception escape raw."""

from __future__ import annotations

from typing import Any

from ..errors import VadeError, is_known_code

__all__ = ["from_ffi", "gateway_error"]


def from_ffi(exc: BaseException) -> VadeError:
    """Convert `FfiError.Protocol` (or anything unexpected) into a VadeError."""
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    fatal = bool(getattr(exc, "fatal", False))
    if isinstance(code, str) and is_known_code(code):
        return VadeError(code, message, fatal=fatal)
    # A non-C-04 code from the transport means the binding is out of sync with
    # schema/errors.json. Fail closed rather than leaking an unknown string.
    return VadeError("INTERNAL", f"protocol core returned an unregistered code {code!r}", fatal=True)


def gateway_error(code: str, message: str, *, fatal: bool = False, data: dict[str, Any] | None = None) -> VadeError:
    return VadeError(code, message, fatal=fatal, data=data)
