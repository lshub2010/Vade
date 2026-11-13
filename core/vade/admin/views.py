"""The single exit for node data (`to_public_view`). Nothing else may serialise a node.

Whitelist only (31-C5a §T4). `manifest` bodies, public keys, PSKs, pair codes and
raw headers never pass through here. `endpoint` is reduced to
`scheme://host:port`: a URL with `?token=...` in it must not become an admin
response.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit

__all__ = ["sanitize_endpoint", "to_public_view", "NODE_FIELDS"]

NODE_FIELDS = (
    "id", "kind", "module_id", "instance_id", "label", "capabilities", "state",
    "endpoint", "protocol", "version", "auth_mode", "rtt_ms", "last_seen",
    "x", "y", "revoked", "tool_count", "tool_names",
)


def sanitize_endpoint(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parts = urlsplit(str(value))
    except ValueError:
        return None
    if not parts.scheme or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


def to_public_view(
    record: Any,
    *,
    tool_names: Iterable[str] = (),
    layout: Mapping[str, Any] | None = None,
    revoked: bool = False,
) -> dict[str, Any]:
    names = list(tool_names)
    health = getattr(record, "health", None)
    state = getattr(health, "value", health) or "unknown"
    node_id = getattr(record, "node_id", None) or f"{record.module_id}#{record.instance_id}"
    position = layout or {}
    return {
        "id": node_id,
        "kind": getattr(record, "kind", "module"),
        "module_id": getattr(record, "module_id", None),
        "instance_id": getattr(record, "instance_id", None),
        "label": position.get("label") or getattr(record, "module_id", None),
        "capabilities": names,
        "state": state,
        "endpoint": sanitize_endpoint(getattr(record, "endpoint", None)),
        "protocol": "vade/v1",
        "version": getattr(getattr(record, "manifest", None), "version", None),
        "auth_mode": getattr(record, "auth_mode", None),
        "rtt_ms": getattr(record, "rtt_ms", None),
        "last_seen": getattr(record, "last_frame_at", None),
        "x": position.get("x"),
        "y": position.get("y"),
        "revoked": bool(revoked or state == "rejected"),
        "tool_count": len(names),
        "tool_names": names,
    }
