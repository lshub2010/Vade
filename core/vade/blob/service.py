"""Upload session state machine, quotas, TTL/GC and `complete` revalidation.

The protocol is three states, not one POST (RFC-0004 §2.5):

    begin ──► PUT part* ──► complete ──► blob (unpinned ─► pinned)
      │                        └─► 400 leaves the session intact
      └─► abort / idle expiry ─► gone

Design rules that are easy to get wrong and expensive to discover later:

* **Ordering at complete**: fsync the temp file, rename it into the
  content-addressed path, then write the row. A crash in between leaves an
  orphan file the sweeper collects; the reverse order would leave a row
  pointing at nothing.
* **Dedupe is per device** (§4.2): global dedupe turns "does this core already
  have sha X" into an existence oracle.
* **Quota counts unpinned blobs too** (§4.6): "upload and never submit" must
  not be free storage.
* **Nothing declared by the client survives `complete`**: size comes from
  `stat`, the digest is recomputed, dimensions come from the container header.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from ..errors import VadeError
from . import chunks
from .ids import (
    blob_path,
    new_blob_id,
    new_upload_id,
    normalize_sha256,
    orphan_dir,
    require_upload_id,
    sanitize_name,
    thumb_path,
    tmp_path,
)
from .settings import AttachSettings
from .store import BlobRecord, BlobStore, TokenRecord, UploadRecord
from .thumb import derive_thumb
from .validate import IMAGE_MIMES, inspect_image, validate_file, validate_image

__all__ = ["BlobService", "Audit"]

Audit = Callable[[Mapping[str, Any]], Any]

#: Default TTL of a device bearer token (§4.5).
TOKEN_TTL_DEFAULT = 600
TOKEN_TTL_MIN = 60
TOKEN_TTL_MAX = 900

_CHUNK = 1024 * 1024


def _ms(seconds: float | None) -> int | None:
    """Wire convention is unix **milliseconds** (`schema/events.json`).

    Internally everything is seconds (`time.time()`); converting once at the
    JSON boundary keeps `X-Vade-Expires-At` and the bodies in one unit.
    """
    return None if seconds is None else int(seconds * 1000)


class BlobService:
    def __init__(
        self,
        data_dir: Path | str,
        settings: AttachSettings | None = None,
        *,
        wall: Callable[[], float] = time.time,
        audit: Audit | None = None,
    ) -> None:
        self.data_dir = Path(data_dir).resolve()
        self.root = self.data_dir / "blobs"
        self.settings = settings or AttachSettings()
        self._wall = wall
        self._audit = audit
        self.store = BlobStore(self.data_dir / "blob.sqlite3")
        self._inflight: dict[str, int] = {}
        self._gc_task: asyncio.Task[None] | None = None
        self._closed = False
        self._prepare()

    # ------------------------------------------------------------------ plumbing
    def _prepare(self) -> None:
        for directory in (self.root, self.root / "tmp", orphan_dir(self.root)):
            directory.mkdir(parents=True, exist_ok=True)

    def now(self) -> float:
        return float(self._wall())

    def close(self) -> None:
        """Sync teardown: stop the sweeper best-effort, then release sqlite."""
        task = self._gc_task
        self._gc_task = None
        if task is not None and not task.done():
            # `aclose()` awaits the cancellation; this path is the sync last
            # resort (a fixture teardown, a crash-handler). Swallow the result
            # so an un-awaited task never logs "exception was never retrieved".
            task.add_done_callback(lambda done: done.cancelled() or done.exception())
            task.cancel()
        if not self._closed:
            self._closed = True
            self.store.close()

    async def aclose(self) -> None:
        await self.stop_sweeper()
        self.close()

    # ------------------------------------------------------------------ sweeper
    def ensure_sweeper(self) -> None:
        """Create the periodic GC task on the *current* running loop.

        Sync on purpose: `install()` must be able to start it without awaiting
        an extra hop (a pending `start_sweeper()` coroutine would be destroyed
        with the loop).
        """
        if self._gc_task is not None and not self._gc_task.done():
            return
        self._gc_task = asyncio.create_task(self._sweep_forever(), name="vade.blob.gc")

    async def start_sweeper(self) -> None:
        """Start the periodic GC task on the running loop (idempotent)."""
        self.ensure_sweeper()

    async def stop_sweeper(self) -> None:
        """Cancel and *await* the GC task: no pending task may outlive shutdown."""
        task, self._gc_task = self._gc_task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - a sweep must never kill shutdown
            pass

    async def _sweep_forever(self) -> None:
        interval = max(0.01, float(self.settings.gc_interval_s))
        while True:
            try:
                # sqlite + unlink in a worker thread: a quota-driven sweep on a
                # busy store must not stall `/ws`.
                await asyncio.to_thread(self.sweep)
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - keep collecting next tick
                pass
            await asyncio.sleep(interval)


    def _record(self, kind: str, *, status: str = "ok", code: str | None = None, **extra: Any) -> None:
        if self._audit is None:
            return
        payload = {"kind": kind, "status": status, "extra": extra}
        if code:
            payload["code"] = code
        try:
            self._audit(payload)
        except Exception:  # pragma: no cover - audit must never break uploads
            pass

    # -------------------------------------------------------------------- tokens
    def issue_token(
        self,
        device_id: str,
        *,
        ttl_s: int = TOKEN_TTL_DEFAULT,
        read_only: bool = False,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Mint a device bearer token; `ui/blob.token` (`blob/rpc.py`) calls this.

        The clamp below is an INTERNAL fallback for direct callers only: the
        RPC surface validates `ttl_s` strictly first (RFC-0016 §5.2-6 — bool /
        float / str / out-of-range are INVALID_ARGUMENT there) so a credential
        lifetime is never silently corrected on the wire.
        """
        if not device_id:
            raise VadeError("INVALID_ARGUMENT", "device_id is required", data={"field": "device_id"})
        ttl = max(TOKEN_TTL_MIN, min(int(ttl_s), TOKEN_TTL_MAX))
        from .auth import hash_token, new_token

        token = new_token()
        expires_at = self.now() + ttl
        self.store.add_token(
            hash_token(token),
            TokenRecord(
                device_id=device_id,
                expires_at=expires_at,
                read_only=read_only,
                max_bytes=max_bytes if max_bytes is not None else self.settings.max_bytes,
            ),
        )
        # RFC-0016 §5.8-21: one audit row per successful mint — kind/status/
        # device_id/expires_at only. The token itself and any prefix of its
        # sha256 NEVER enter the audit (A10 greps the audit for the literal).
        self._record("blob.token", device_id=device_id, expires_at=_ms(expires_at))
        return {
            "token": token,
            "expires_at": _ms(expires_at),
            "max_bytes": max_bytes if max_bytes is not None else self.settings.max_bytes,
        }

    def get_token(self, token_hash: str, now: float) -> TokenRecord | None:
        return self.store.get_token(token_hash, now)

    def revoke_device(self, device_id: str) -> int:
        return self.store.revoke_device_tokens(device_id)

    # --------------------------------------------------------------------- begin
    def begin(self, principal: Any, payload: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        principal.require_write()
        params = self._begin_params(payload)
        device = principal.device_id
        now = self.now()

        if principal.max_bytes is not None and params["size"] > int(principal.max_bytes):
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                "file exceeds the limit attached to this token",
                data={"field": "size", "limit": int(principal.max_bytes), "actual": params["size"]},
            )

        existing = self.store.find_blob_by_content(device, params["sha256"], params["size"])
        if (
            existing is not None
            and not existing.pinned
            and existing.expires_at is not None
            and existing.expires_at <= now
        ):
            # Lazy TTL: the sweeper runs every gc_interval_s, and handing out a
            # row that is already past its expiry would produce a 404 right
            # after a successful begin.
            self._release(existing)
            existing = None
        if existing is not None and existing.state == "ok" and existing.scan != "infected":
            self._record(
                "blob.begin", status="dedupe", device_id=device, file_id=existing.file_id,
                sha256=existing.sha256, mime=existing.mime, bytes=existing.size,
            )
            return 200, {
                "file_id": existing.file_id,
                "upload_id": None,
                "dedupe": True,
                "size": existing.size,
                "mime": existing.mime,
                "pinned": existing.pinned,
                "expires_at": _ms(existing.expires_at),
                "part_size": params["part_size"],
            }

        resumed = self.store.find_open_upload(device, params["sha256"], params["size"])
        if resumed is not None and self._expire_if_idle(resumed, now):
            resumed = None
        if resumed is not None:
            self._record(
                "blob.begin", status="resume", device_id=device, upload_id=resumed.upload_id,
                sha256=resumed.sha256, mime=resumed.mime, bytes=resumed.size,
            )
            return 200, self._upload_payload(resumed, now, dedupe=False)

        self._check_quota(device, params["size"], now)

        upload_id = new_upload_id()
        target = tmp_path(self.root, upload_id)
        with open(target, "wb") as handle:
            handle.truncate(params["size"])  # sparse reserve: parts seek+write

        record = UploadRecord(
            upload_id=upload_id,
            device_id=device,
            sha256=params["sha256"],
            size=params["size"],
            mime=params["mime"],
            name=params["name"],
            kind=params["kind"],
            width=params["width"],
            height=params["height"],
            part_size=params["part_size"],
            ranges=[],
            created_at=now,
            updated_at=now,
        )
        self.store.add_upload(record)
        self._record(
            "blob.begin", status="created", device_id=device, upload_id=upload_id,
            sha256=record.sha256, mime=record.mime, bytes=record.size,
        )
        return 201, self._upload_payload(record, now, dedupe=False)

    def _begin_params(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, Mapping):
            raise VadeError("INVALID_ARGUMENT", "body must be a JSON object", data={"field": "body"})

        kind = payload.get("kind", "image")
        if kind == "audio":
            raise VadeError(
                "UNSUPPORTED",
                "audio attachments are disabled in v1 (transcribe on the device)",
                data={"field": "kind", "reason": "audio_disabled"},
            )
        if kind not in {"image", "file"}:
            raise VadeError("INVALID_ARGUMENT", "kind must be image or file", data={"field": "kind"})

        sha256 = normalize_sha256(payload.get("sha256"))

        mime = str(payload.get("mime") or "").strip().lower()
        if kind == "image" and mime not in IMAGE_MIMES:
            raise VadeError(
                "UNSUPPORTED",
                "image mime must be image/jpeg, image/png or image/webp",
                data={"field": "mime"},
            )
        if kind == "file" and not mime:
            mime = "application/octet-stream"
        # §5.1: `kind` is a hint, the *mime* decides which gate runs. Sending
        # `kind:"file"` with `image/png` must not skip the image ceilings or
        # the metadata strip at `complete`.
        image = kind == "image" or mime in IMAGE_MIMES

        size_raw = payload.get("size")
        if isinstance(size_raw, bool) or not isinstance(size_raw, int) or size_raw <= 0:
            raise VadeError("INVALID_ARGUMENT", "size must be a positive integer", data={"field": "size"})
        size = int(size_raw)
        if size > self.settings.max_bytes:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                "file exceeds the configured hard limit",
                data={"field": "size", "limit": self.settings.max_bytes, "actual": size},
            )
        if image and size > self.settings.image_max_bytes:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                "image exceeds the configured limit (compress before uploading)",
                data={"field": "size", "limit": self.settings.image_max_bytes, "actual": size, "kind": "image"},
            )

        width = payload.get("width")
        height = payload.get("height")
        if image:
            width = self._positive_int(width, "width")
            height = self._positive_int(height, "height")
            long_edge = max(width, height)
            if long_edge > self.settings.image_max_edge:
                raise VadeError(
                    "PAYLOAD_TOO_LARGE",
                    "image exceeds the configured edge limit",
                    data={"field": "pixels", "limit": self.settings.image_max_edge, "actual": long_edge},
                )
            if width * height > self.settings.image_max_pixels:
                raise VadeError(
                    "PAYLOAD_TOO_LARGE",
                    "image exceeds the configured pixel budget",
                    data={"field": "pixels", "limit": self.settings.image_max_pixels, "actual": width * height},
                )
        else:
            width = height = None

        return {
            "kind": kind,
            "sha256": sha256,
            "size": size,
            "mime": mime,
            "name": sanitize_name(payload.get("name")),
            "width": width,
            "height": height,
            "part_size": self.settings.clamp_part_size(payload.get("part_size")),
        }

    @staticmethod
    def _positive_int(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise VadeError("INVALID_ARGUMENT", f"{field} must be a positive integer", data={"field": field})
        return int(value)

    def _check_quota(self, device_id: str, size: int, now: float) -> None:
        # Must stay *before* the temp file is created: a refusal here leaves no
        # orphan `.part` behind that no sweep would ever reclaim (§4.6).
        idle = self.settings.idle_seconds
        used = self.store.usage(device_id, now, idle_s=idle)
        if used + size > self.settings.quota_bytes:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                "device storage quota would be exceeded",
                data={"field": "size", "reason": "quota", "limit": self.settings.quota_bytes, "used": used},
            )
        tmp_used = self.store.tmp_usage(device_id, now, idle_s=idle)
        if tmp_used + size > self.settings.tmp_quota_bytes:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                "temporary upload area quota would be exceeded",
                data={
                    "field": "size",
                    "reason": "tmp_quota",
                    "limit": self.settings.tmp_quota_bytes,
                    "used": tmp_used,
                },
            )

    def _upload_payload(self, upload: UploadRecord, now: float, *, dedupe: bool) -> dict[str, Any]:
        summary = chunks.summary(upload.ranges, upload.size)
        return {
            "file_id": None,
            "upload_id": upload.upload_id,
            "dedupe": dedupe,
            "size": upload.size,
            "mime": upload.mime,
            "pinned": False,
            "expires_at": _ms(now + self.settings.idle_seconds),
            "part_size": upload.part_size,
            "received": summary["received"],
            "offset_next": summary["offset_next"],
            "confirmed_bytes": summary["confirmed_bytes"],
            "max_bytes": self.settings.max_bytes,
        }

    # ---------------------------------------------------------------------- part
    def put_part(
        self,
        principal: Any,
        upload_id: str,
        offset: int,
        body: bytes,
        *,
        chunk_sha256: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        principal.require_write()
        upload = self._require_upload(principal, require_upload_id(upload_id))
        now = self.now()
        if self._expire_if_idle(upload, now):
            raise VadeError("NOT_FOUND", "upload session expired", data={"field": "upload_id"})

        if not self._enter_part(upload.upload_id):
            raise VadeError(
                "OVERLOADED",
                "too many concurrent parts for this upload",
                data={"retry_after": 1},
            )
        try:
            return self._put_part_locked(upload, offset, body, chunk_sha256, now)
        finally:
            self._leave_part(upload.upload_id)

    def _put_part_locked(
        self,
        upload: UploadRecord,
        offset: int,
        body: bytes,
        chunk_sha256: str | None,
        now: float,
    ) -> tuple[int, dict[str, Any]]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise VadeError("INVALID_ARGUMENT", "offset must be a non-negative integer", data={"field": "offset"})
        if upload.part_size and offset % upload.part_size:
            raise VadeError(
                "INVALID_ARGUMENT",
                "offset must fall on a part boundary",
                data={"field": "offset", "expected": self._expected_offsets(upload)},
            )

        length = len(body)
        if length == 0:
            raise VadeError("INVALID_ARGUMENT", "part body is empty", data={"field": "body"})
        if length > upload.part_size:
            raise VadeError(
                "PAYLOAD_TOO_LARGE",
                "part exceeds part_size",
                data={"field": "size", "limit": upload.part_size, "actual": length},
            )
        if offset >= upload.size or offset + length > upload.size:
            raise VadeError(
                "INVALID_ARGUMENT",
                "part runs past the declared size",
                data={"field": "offset", "reason": "out_of_bounds", "expected": self._expected_offsets(upload)},
            )

        digest = hashlib.sha256(body).hexdigest()
        if chunk_sha256:
            try:
                declared = normalize_sha256(chunk_sha256)
            except VadeError:
                raise VadeError(
                    "INVALID_ARGUMENT", "x-vade-chunk-sha256 must be 64 hex characters",
                    data={"field": "part_sha256"},
                ) from None
            if declared != digest:
                raise VadeError(
                    "INVALID_ARGUMENT",
                    "part digest does not match the bytes",
                    data={"field": "part_sha256", "expected": declared, "actual": digest},
                )

        if chunks.overlaps(upload.ranges, offset, offset + length):
            previous = self.store.part_digest(upload.upload_id, offset)
            if previous == digest:
                # Idempotent retry: "timed out but the server had it" is normal
                # on mobile, so a repeat must not fail and must not allocate.
                self.store.touch_upload(upload.upload_id, now)
                payload = self._upload_payload(upload, now, dedupe=False)
                return 200, {
                    "received": payload["received"],
                    "offset_next": payload["offset_next"],
                    "confirmed_bytes": payload["confirmed_bytes"],
                    "expires_at": payload["expires_at"],
                }
            raise VadeError(
                "INVALID_ARGUMENT",
                "part overlaps bytes already accepted for this upload",
                data={"field": "offset", "reason": "chunk_conflict", "expected": self._expected_offsets(upload)},
            )

        target = tmp_path(self.root, upload.upload_id)
        if not target.exists():
            raise VadeError("NOT_FOUND", "upload session has no temporary file", data={"field": "upload_id"})
        try:
            with open(target, "r+b") as handle:
                handle.seek(offset)
                handle.write(body)
        except OSError:
            # Gone or unwritable between the check above and the open: an abort
            # or an idle sweep can drop the file, and on Windows an external
            # handle (AV, backup, indexer) turns the open into `PermissionError`.
            # Both mean "this session cannot take the part", not a 500.
            raise VadeError("NOT_FOUND", "upload session expired", data={"field": "upload_id"}) from None

        ranges = chunks.add(upload.ranges, offset, offset + length)
        # File first, row second: a crash between them only loses the record of
        # bytes that are on disk (the client retries the same part idempotently).
        self.store.commit_part(
            upload.upload_id, offset=offset, length=length, digest=digest, ranges=ranges, now=now
        )
        summary = chunks.summary(ranges, upload.size)
        return 200, {
            "received": summary["received"],
            "offset_next": summary["offset_next"],
            "confirmed_bytes": summary["confirmed_bytes"],
            "expires_at": _ms(now + self.settings.idle_seconds),
        }

    def _expected_offsets(self, upload: UploadRecord) -> list[int]:
        gaps = chunks.gaps(upload.ranges, upload.size)
        offsets = [start for start, _end in gaps][:8]
        return offsets or [upload.size]

    def _enter_part(self, upload_id: str) -> bool:
        current = self._inflight.get(upload_id, 0)
        if current >= self.settings.part_max_concurrent:
            return False
        self._inflight[upload_id] = current + 1
        return True

    def _leave_part(self, upload_id: str) -> None:
        current = self._inflight.get(upload_id, 0) - 1
        if current <= 0:
            self._inflight.pop(upload_id, None)
        else:
            self._inflight[upload_id] = current

    # ---------------------------------------------------------------- head/abort
    def head_upload(self, principal: Any, upload_id: str) -> dict[str, Any]:
        upload = self._require_upload(principal, require_upload_id(upload_id))
        now = self.now()
        if self._expire_if_idle(upload, now):
            raise VadeError("NOT_FOUND", "upload session expired", data={"field": "upload_id"})
        summary = chunks.summary(upload.ranges, upload.size)
        return {
            "upload_id": upload.upload_id,
            "size": upload.size,
            "part_size": upload.part_size,
            "received": summary["received"],
            "offset_next": summary["offset_next"],
            "confirmed_bytes": summary["confirmed_bytes"],
            "expires_at": _ms(now + self.settings.idle_seconds),
            "state": upload.state,
        }

    def abort(self, principal: Any, upload_id: str) -> None:
        """Idempotent: 204 for known, unknown and malformed ids alike.

        Abort only ever touches an *upload session*. A committed blob is owned
        by the message lifecycle and is never deleted from here (§4.4).
        """
        principal.require_write()
        from .ids import is_id

        if not is_id(upload_id) or not str(upload_id).startswith("upl_"):
            return
        upload = self.store.get_upload(str(upload_id))
        if upload is None:
            return
        if upload.device_id != principal.device_id:
            return
        target = tmp_path(self.root, upload.upload_id)
        try:
            target.unlink()
        except OSError:
            # Windows refuses to unlink a file a part-writer still holds open;
            # POSIX unlinks it under the writer's feet. Either way the row goes,
            # and `reconcile` collects a temp file whose upload id no longer
            # exists (§4.4: abort is always 204).
            pass
        self.store.delete_upload(upload.upload_id)
        self._record(
            "blob.abort", device_id=upload.device_id, upload_id=upload.upload_id,
            bytes=upload.size, sha256=upload.sha256,
        )

    # ------------------------------------------------------------------ complete
    async def acomplete(
        self, principal: Any, upload_id: str, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        """`complete` on a worker thread: re-hashing and header parsing are the
        only CPU-bound work on this channel and they must not stall `/ws`.

        The store is opened with `check_same_thread=False` and every mutation
        takes its own `RLock`, so running the sync path off-loop is safe.
        """
        return await asyncio.to_thread(self.complete, principal, upload_id, payload)

    def complete(
        self, principal: Any, upload_id: str, payload: Mapping[str, Any]
    ) -> tuple[int, dict[str, Any]]:
        principal.require_write()
        upload = self._require_upload(principal, require_upload_id(upload_id))
        now = self.now()
        if self._expire_if_idle(upload, now):
            raise VadeError("NOT_FOUND", "upload session expired", data={"field": "upload_id"})
        if not isinstance(payload, Mapping):
            raise VadeError("INVALID_ARGUMENT", "body must be a JSON object", data={"field": "body"})

        declared = normalize_sha256(payload.get("sha256"))
        if declared != upload.sha256:
            raise VadeError(
                "INVALID_ARGUMENT",
                "complete sha256 does not match the one declared at begin",
                data={"field": "sha256", "expected": upload.sha256, "actual": declared},
            )
        body_size = payload.get("size")
        if isinstance(body_size, bool) or not isinstance(body_size, int) or body_size != upload.size:
            raise VadeError(
                "INVALID_ARGUMENT",
                "complete size does not match the upload session",
                data={"field": "size", "expected": upload.size, "actual": body_size},
            )

        if not chunks.covers(upload.ranges, upload.size):
            raise VadeError(
                "INVALID_ARGUMENT",
                "upload is not complete yet",
                data={"field": "received", "missing": [[a, b] for a, b in chunks.gaps(upload.ranges, upload.size)]},
            )

        source = tmp_path(self.root, upload.upload_id)
        if not source.exists():
            raise VadeError("NOT_FOUND", "upload session has no temporary file", data={"field": "upload_id"})

        actual_size = source.stat().st_size
        if actual_size != upload.size:
            # The declared number is a hint; `stat` is the measurement (§5.3).
            if actual_size > self.settings.max_bytes:
                raise VadeError(
                    "PAYLOAD_TOO_LARGE",
                    "file exceeds the configured hard limit",
                    data={"field": "size", "limit": self.settings.max_bytes, "actual": actual_size},
                )
            raise VadeError(
                "INVALID_ARGUMENT",
                "stored bytes do not match the declared size",
                data={"field": "size", "expected": upload.size, "actual": actual_size},
            )

        head = self._read_head(source, 64)
        # `kind` is a client hint; an image mime is revalidated as an image even
        # when it arrived as `kind:"file"` (no metadata bypass, §5.1).
        if upload.mime in IMAGE_MIMES:
            data = source.read_bytes()
            info = validate_image(
                data,
                declared_mime=upload.mime,
                declared_width=upload.width,
                declared_height=upload.height,
                max_bytes=self.settings.image_max_bytes,
                max_edge=self.settings.image_max_edge,
                max_pixels=self.settings.image_max_pixels,
            )
            width, height = info.width, info.height
        else:
            validate_file(head, declared_mime=upload.mime)
            width, height = None, None

        actual_sha = self._hash_file(source)
        if actual_sha != upload.sha256:
            raise VadeError(
                "INVALID_ARGUMENT",
                "recomputed sha256 does not match the declared digest",
                data={"field": "sha256", "expected": upload.sha256, "actual": actual_sha},
            )

        final = blob_path(self.root, actual_sha)
        final.parent.mkdir(parents=True, exist_ok=True)
        self._fsync(source)
        os.replace(source, final)

        scan = "pending" if self.settings.scan_enabled else "clean"
        record = BlobRecord(
            file_id=new_blob_id(),
            device_id=upload.device_id,
            sha256=actual_sha,
            size=upload.size,
            mime=upload.mime if upload.kind == "image" else (upload.mime or "application/octet-stream"),
            name=upload.name,
            kind=upload.kind,
            width=width,
            height=height,
            pinned=False,
            expires_at=now + self.settings.blob_ttl_seconds,
            created_at=now,
            scan=scan,
            state="ok",
        )
        stored = self.store.add_blob(record)
        if stored.file_id != record.file_id:
            # Dedupe hit: `add_blob` handed back the row already on file for
            # this (device, digest, size). That row is not necessarily usable —
            # `begin` only reuses rows in state `ok` with a clean scan, so a
            # `missing` row (bytes were lost, `reconcile` marked it) or an
            # `infected` one (scanner removed the content) lands here too.
            #
            # Returning such a row with a 201 hands the client a `file_id` that
            # `GET` must refuse: the "re-upload to recover" path would silently
            # fail. The bytes are on disk again (we just moved them), so either
            # re-admit the row or refuse the upload outright.
            if stored.scan == "infected":
                # Quarantined content must not come back through a re-upload:
                # drop the bytes we just put back and the session with them.
                self._remove_blob_files(stored)
                self.store.delete_upload(upload.upload_id)
                self._record(
                    "blob.complete", status="infected", device_id=upload.device_id,
                    upload_id=upload.upload_id, sha256=actual_sha, mime=upload.mime,
                )
                raise VadeError(
                    "PERMISSION_DENIED",
                    "file contents were removed by the scanner",
                    data={"field": "sha256", "reason": "infected"},
                )
            if stored.state != "ok":
                # Revive: the re-upload re-validated the bytes (mime, edges,
                # metadata strip) and the digest matched, so the row may take
                # the fresh metadata and a new deadline.
                stored = self.store.revive_blob(stored.file_id, record)
        self.store.delete_upload(upload.upload_id)
        self._record(
            "blob.complete", device_id=upload.device_id, upload_id=upload.upload_id,
            file_id=stored.file_id, bytes=stored.size, sha256=stored.sha256, mime=stored.mime,
        )
        return 201, {
            "file_id": stored.file_id,
            "sha256": stored.sha256,
            "size": stored.size,
            "mime": stored.mime,
            "name": stored.name,
            "width": stored.width,
            "height": stored.height,
            "pinned": stored.pinned,
            "expires_at": _ms(stored.expires_at),
            "thumb": f"/ui/blob/{stored.file_id}?v=thumb" if stored.kind == "image" else None,
            "status": stored.scan,
        }

    @staticmethod
    def _read_head(path: Path, size: int) -> bytes:
        with open(path, "rb") as handle:
            return handle.read(size)

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(_CHUNK)
                if not block:
                    break
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _fsync(path: Path) -> None:
        # "rb+", not "rb": Windows refuses FlushFileBuffers on a handle opened
        # for reading only ("Bad file descriptor"), and this runs on every
        # complete.
        with open(path, "rb+") as handle:
            os.fsync(handle.fileno())

    # ------------------------------------------------------------------ read side
    def stat(self, principal: Any, file_id: str) -> BlobRecord:
        from .ids import require_file_id

        blob = self.store.get_blob(require_file_id(file_id))
        if blob is None:
            raise VadeError("NOT_FOUND", "file not found", data={"field": "file_id"})
        if blob.state == "missing":
            # 410, not 404: the row exists and the bytes are gone. The UI must
            # be able to tell this apart from "not yours".
            raise VadeError("NOT_FOUND", "file contents are no longer available", data={"field": "file_id", "gone": True})
        if blob.scan == "infected":
            raise VadeError("NOT_FOUND", "file was removed by the scanner", data={"field": "file_id"})
        if not blob.pinned and blob.expires_at is not None and blob.expires_at <= self.now():
            raise VadeError("NOT_FOUND", "file expired", data={"field": "file_id"})
        # RFC-0016 §11.1 Q7 (拍板 2026-11-15): a device bearer may only read
        # blobs its own device uploaded. Deliberately INDEPENDENT of
        # `read_scope`: `read_scope="device"` is the operator switch
        # (RFC-0004 §8.5, default "core"), while `kind=="device"` is the token
        # rule — tightening it only under the operator switch would silently
        # reopen the cross-device read the 拍板 closed. Known cost (written
        # down in §11.1, do not "fix" it here): a phone-uploaded image in a
        # shared session will NOT render on a desktop that fetches it with its
        # own token; if the product wants cross-device visibility, the correct
        # fix is session-scoped authorization (a separate change), never a
        # quiet widening here. Cookie principals keep the read_scope semantics
        # (they are how history images render in the browser today).
        # Fail-closed: the comparison below denies unless both sides agree.
        if getattr(principal, "kind", None) == "device" or self.settings.read_scope == "device":
            if blob.device_id != principal.device_id:
                raise VadeError(
                    "PERMISSION_DENIED",
                    "file belongs to another device",
                    data={"field": "file_id"},
                )
        return blob

    def metadata(self, blob: BlobRecord) -> dict[str, Any]:
        return {
            "file_id": blob.file_id,
            "sha256": blob.sha256,
            "size": blob.size,
            "mime": blob.mime,
            "name": blob.name,
            "kind": blob.kind,
            "width": blob.width,
            "height": blob.height,
            "pinned": blob.pinned,
            "expires_at": _ms(blob.expires_at),
            "scan": blob.scan,
            "state": blob.state,
            "thumb": f"/ui/blob/{blob.file_id}?v=thumb" if blob.kind == "image" else None,
        }

    def content_path(self, blob: BlobRecord, variant: str | None = None) -> tuple[Path, str]:
        """Return (path, content-type) for a read, deriving `?v=thumb` lazily."""
        if blob.kind != "image":
            if variant:
                raise VadeError("UNSUPPORTED", "only images have a thumbnail variant", data={"field": "v"})
            path = blob_path(self.root, blob.sha256)
            return path, blob.mime

        if variant == "thumb":
            cached = thumb_path(self.root, blob.sha256)
            if cached.exists():
                return cached, f"image/{self._thumb_ext()}"
            source = blob_path(self.root, blob.sha256)
            if not source.exists():
                self._mark_missing(blob)
                raise VadeError("NOT_FOUND", "file contents are no longer available", data={"field": "file_id", "gone": True})
            payload = derive_thumb(source, self.settings.thumb_px)
            tmp = cached.with_name(cached.name + ".tmp")
            tmp.write_bytes(payload)
            os.replace(tmp, cached)
            return cached, "image/webp"

        path = blob_path(self.root, blob.sha256)
        if not path.exists():
            self._mark_missing(blob)
            raise VadeError("NOT_FOUND", "file contents are no longer available", data={"field": "file_id", "gone": True})
        return path, blob.mime

    @staticmethod
    def _thumb_ext() -> str:
        return "webp"

    # ---------------------------------------------------------------------- pin
    def pin(self, file_ids: Iterable[str], *, device_id: str | None = None) -> list[str]:
        """§4.7 pin: called by `ui/submit` in the same transaction as the message."""
        pinned: list[str] = []
        for file_id in file_ids:
            blob = self.store.get_blob(str(file_id))
            if blob is None:
                continue
            if device_id is not None and blob.device_id != device_id:
                continue
            self.store.set_pinned(blob.file_id, True)
            pinned.append(blob.file_id)
        return pinned

    def unpin(self, file_ids: Iterable[str]) -> None:
        for file_id in file_ids:
            blob = self.store.get_blob(str(file_id))
            if blob is not None:
                # Unpinning restarts the clock: `expires_at=NULL` would make the
                # row immortal and the bytes uncollectable in practice.
                self.store.set_pinned(
                    blob.file_id, False, expires_at=self.now() + self.settings.blob_ttl_seconds
                )

    # ----------------------------------------------------------------------- gc
    def sweep(self, now: float | None = None) -> dict[str, int]:
        """One sweeper pass. Single writer via the `blob_gc` lease row."""
        moment = self.now() if now is None else float(now)
        if not self.store.acquire_gc(moment, max(30.0, self.settings.gc_interval_s * 2)):
            return {"scanned": 0, "deleted": 0, "bytes": 0, "orphans": 0, "missing": 0, "skipped": 1}
        try:
            deleted = 0
            freed = 0
            scanned = 0

            for upload in self.store.open_uploads():
                scanned += 1
                if upload.upload_id in self._inflight:
                    # A part is being written right now: unlinking under it is a
                    # 500 on Windows and silent byte loss on POSIX.
                    continue
                if moment - upload.updated_at >= self.settings.idle_seconds:
                    try:
                        tmp_path(self.root, upload.upload_id).unlink()
                    except OSError:
                        pass
                    self.store.delete_upload(upload.upload_id)
                    deleted += 1
                    freed += upload.size
                    self._record(
                        "blob.gc", status="expired_upload", device_id=upload.device_id,
                        upload_id=upload.upload_id, bytes=upload.size,
                    )

            for blob in self.store.expired_blobs(moment):
                scanned += 1
                # Only the last row for a digest may take the bytes with it.
                if self._release(blob):
                    freed += blob.size
                deleted += 1
                self._record(
                    "blob.gc", status="expired_blob", device_id=blob.device_id,
                    file_id=blob.file_id, bytes=blob.size,
                )

            orphans, missing = self.reconcile(now=moment)
            self.store.prune_tokens(moment)
            if self.settings.scan_enabled:
                self.run_pending_scans()
            summary: dict[str, Any] = {
                "scanned": scanned,
                "deleted": deleted,
                "bytes": freed,
                "orphans": orphans,
                "missing": missing,
                "skipped": 0,
            }
            self._record("blob.gc", status="summary", **summary)
            return summary
        finally:
            self.store.release_gc()

    def reconcile(self, now: float | None = None) -> tuple[int, int]:
        """Startup/periodic reconciliation: files without rows, rows without files."""
        moment = self.now() if now is None else float(now)
        known = {blob.sha256: blob for blob in self.store.all_blobs()}
        orphans = 0
        for path in self._iter_blob_files():
            sha = path.name.split(".", 1)[0]
            if sha in known:
                continue
            destination = orphan_dir(self.root) / f"{path.name}.{int(moment)}"
            try:
                shutil.move(str(path), str(destination))
                orphans += 1
            except OSError:  # pragma: no cover - racing sweeper
                pass

        missing = 0
        for sha, blob in known.items():
            if blob.state != "ok":
                continue
            if not blob_path(self.root, sha).exists():
                self.store.set_state(blob.file_id, "missing")
                missing += 1

        self._collect_stray_temp_files()

        # Orphans are kept 24 h for postmortems, then dropped.
        for leftover in orphan_dir(self.root).glob("*"):
            try:
                if moment - leftover.stat().st_mtime >= 86400:
                    leftover.unlink()
            except OSError:  # pragma: no cover
                pass
        return orphans, missing

    def _collect_stray_temp_files(self) -> int:
        """Delete `.part` files whose upload row is gone.

        An abort or an idle expiry that could not unlink (Windows, handle held
        by the writer) leaves a temp file nothing else looks at: `sweep` walks
        rows and the orphan pass skips `tmp/`. Age-gating on the *real* mtime
        keeps a `begin` that has created its file but not yet its row safe.
        """
        known = self.store.upload_ids()
        cutoff = time.time() - self.settings.idle_seconds
        removed = 0
        for path in (self.root / "tmp").glob("*.part"):
            if path.name[: -len(".part")] in known:
                continue
            try:
                if path.stat().st_mtime > cutoff:
                    continue
                path.unlink()
                removed += 1
            except OSError:
                continue
        return removed

    def _iter_blob_files(self):
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            if path.parent.name in {"tmp", "orphan"} or "orphan" in path.parts:
                continue
            yield path

    def run_pending_scans(self) -> int:
        """Run `attach.scan.command` on pending blobs; exit 0 = clean."""
        import subprocess

        command = self.settings.scan_command.strip()
        done = 0
        for blob in self.store.pending_scans():
            path = blob_path(self.root, blob.sha256)
            if not path.exists():
                self.store.set_state(blob.file_id, "missing")
                continue
            if not command:
                self.store.set_scan(blob.file_id, "pending")
                continue
            try:
                result = subprocess.run(  # noqa: S603 - operator-configured command
                    [*command.split(), str(path)], capture_output=True, timeout=30, check=False
                )
            except (OSError, subprocess.SubprocessError):
                continue
            if result.returncode == 0:
                self.store.set_scan(blob.file_id, "clean")
            elif result.returncode == 1:
                self.store.set_scan(blob.file_id, "infected")
                self._remove_blob_files(blob)
                self._record(
                    "blob.scan", status="infected", device_id=blob.device_id, file_id=blob.file_id
                )
            done += 1
        return done

    def _release(self, blob: BlobRecord) -> bool:
        """Drop one row reference; delete the bytes only when the last one goes.

        Returns True when the file was removed (the row was the last owner).
        """
        if not self.store.release_blob(blob.file_id):
            return False
        self._remove_blob_files(blob)
        return True

    def _remove_blob_files(self, blob: BlobRecord) -> None:
        for path in (blob_path(self.root, blob.sha256), thumb_path(self.root, blob.sha256)):
            try:
                path.unlink()
            except OSError:
                # Windows: a reader still holds a handle. POSIX: already gone.
                # Neither may turn a sweep or a scan into a 500.
                pass

    def _mark_missing(self, blob: BlobRecord) -> None:
        self.store.set_state(blob.file_id, "missing")

    # ------------------------------------------------------------------ internals
    def _require_upload(self, principal: Any, upload_id: str) -> UploadRecord:
        upload = self.store.get_upload(upload_id)
        if upload is None or upload.device_id != principal.device_id:
            # Cross-device and unknown are the same answer: no existence oracle.
            raise VadeError("NOT_FOUND", "upload session not found", data={"field": "upload_id"})
        return upload

    def _expire_if_idle(self, upload: UploadRecord, now: float) -> bool:
        if upload.state != "open":
            return True
        if now - upload.updated_at < self.settings.idle_seconds:
            return False
        if upload.upload_id in self._inflight:
            # Another part is mid-write on this very session: not expired yet.
            return False
        try:
            tmp_path(self.root, upload.upload_id).unlink()
        except OSError:
            pass
        self.store.delete_upload(upload.upload_id)
        self._record(
            "blob.gc", status="expired_upload", device_id=upload.device_id,
            upload_id=upload.upload_id, bytes=upload.size,
        )
        return True
