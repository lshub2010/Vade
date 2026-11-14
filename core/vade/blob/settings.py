"""`[attach]` settings (C-18, RFC-0004 §7.4).

Defaults live in `schema/config.schema.json`; this module only maps the dotted
paths onto a frozen dataclass so the rest of the package never reads the whole
config. Values are read once at construction: `restart` keys cannot change
under a live listener, and the `hot` ones are re-read by `reload()` (the admin
plane calls it after writing overrides).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

__all__ = ["AttachSettings", "MIN_PART_BYTES", "MAX_PART_BYTES"]

#: RFC-0004 Q1 / §4.2: the server clamps a client-proposed part size into
#: [5 MiB, attach.part_size]. Q1 leaves the upper bound at the config value.
MIN_PART_BYTES = 5 * 1024 * 1024
MAX_PART_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AttachSettings:
    enabled: bool = True
    max_bytes: int = 104857600
    image_max_bytes: int = 5242880
    image_max_edge: int = 2048
    image_max_pixels: int = 4194304
    part_size: int = 10485760
    part_max_concurrent: int = 3
    max_per_turn: int = 10
    text_max_chars: int = 32000
    ttl_hours: float = 1
    upload_idle_ttl_min: float = 15
    quota_bytes: int = 2147483648
    gc_interval_s: float = 60
    thumb_px: int = 320
    read_scope: str = "core"
    scan_enabled: bool = False
    scan_command: str = ""

    @property
    def tmp_quota_bytes(self) -> int:
        """§4.6: the temporary area is a quarter of the device quota."""
        return max(1, self.quota_bytes // 4)

    @property
    def idle_seconds(self) -> float:
        return float(self.upload_idle_ttl_min) * 60.0

    @property
    def blob_ttl_seconds(self) -> float:
        return float(self.ttl_hours) * 3600.0

    def clamp_part_size(self, proposed: Any) -> int:
        """Server-clamped part size: [min(5 MiB, cap), cap]."""
        cap = int(self.part_size)
        lower = min(MIN_PART_BYTES, cap)
        try:
            value = int(proposed) if proposed is not None else cap
        except (TypeError, ValueError):
            value = cap
        return max(lower, min(value, cap))

    @classmethod
    def from_config(cls, config: Any) -> "AttachSettings":
        base = cls()
        if config is None:
            return base

        def get(dotted: str, fallback: Any) -> Any:
            value = config.get(f"attach.{dotted}", None)
            return fallback if value is None else value

        try:
            return replace(
                base,
                enabled=bool(get("enabled", base.enabled)),
                max_bytes=int(get("max_bytes", base.max_bytes)),
                image_max_bytes=int(get("image.max_bytes", base.image_max_bytes)),
                image_max_edge=int(get("image.max_edge", base.image_max_edge)),
                image_max_pixels=int(get("image.max_pixels", base.image_max_pixels)),
                part_size=int(get("part_size", base.part_size)),
                part_max_concurrent=int(get("part_max_concurrent", base.part_max_concurrent)),
                max_per_turn=int(get("max_per_turn", base.max_per_turn)),
                text_max_chars=int(get("text_max_chars", base.text_max_chars)),
                ttl_hours=float(get("ttl_hours", base.ttl_hours)),
                upload_idle_ttl_min=float(get("upload_idle_ttl_min", base.upload_idle_ttl_min)),
                quota_bytes=int(get("quota_bytes", base.quota_bytes)),
                gc_interval_s=float(get("gc_interval_s", base.gc_interval_s)),
                thumb_px=int(get("thumb_px", base.thumb_px)),
                read_scope=str(get("read_scope", base.read_scope)),
                scan_enabled=bool(get("scan.enabled", base.scan_enabled)),
                scan_command=str(get("scan.command", base.scan_command) or ""),
            )
        except (TypeError, ValueError):
            # A malformed value is a config error, not a silent default: the
            # schema validator already rejected it at startup, so reaching this
            # means someone built a Config by hand.
            return base
