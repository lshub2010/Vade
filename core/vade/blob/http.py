"""The `/ui/blob` ASGI surface (RFC-0004 §4.1).

Six routes on the **data plane** listener (same process as `/ws` and
`/ui/session`; `/admin*` stays 404 here):

    POST   /ui/blob                                  begin
    PUT    /ui/blob/upload/{upload_id}?offset=N      write one part
    HEAD   /ui/blob/upload/{upload_id}               progress / resume
    POST   /ui/blob/upload/{upload_id}/complete      revalidate + file_id
    DELETE /ui/blob/upload/{upload_id}               abort (idempotent)
    GET|HEAD /ui/blob/{file_id}[?v=thumb]            fetch / metadata

Hard rules enforced here, before any domain logic runs:

* **No `multipart/form-data`** (or `text/plain`, or form-urlencoded). Those are
  CORS *simple* requests and would skip the preflight that protects the cookie
  path; they get `415` with `data.field="content-type"` (§8.2).
* Body ceilings: 64 KiB for the JSON endpoints, `part_size + 1 KiB` for a part.
* CORS is the existing exact-match allowlist; a disallowed `Origin` is
  `403 ORIGIN_MISMATCH` even before authentication.

Errors always carry `data.field` so the UI can point at the offending control
(§4.8 / G7), and only C-04 codes are ever returned.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import parse_qs, quote

from ..errors import VadeError, http_status
from .auth import Authenticator
from .ids import sanitize_name
from .service import BlobService

__all__ = ["BlobApp", "ROUTES", "BEGIN_BODY_MAX", "PART_BODY_SLACK", "build_from_config"]

BEGIN_BODY_MAX = 64 * 1024
PART_BODY_SLACK = 1024
_STREAM = 256 * 1024

#: Method + path table, asserted by `tools/check_contracts.py` against the RFC.
ROUTES: tuple[tuple[str, str], ...] = (
    ("POST", "/ui/blob"),
    ("PUT", "/ui/blob/upload/{upload_id}"),
    ("HEAD", "/ui/blob/upload/{upload_id}"),
    ("POST", "/ui/blob/upload/{upload_id}/complete"),
    ("DELETE", "/ui/blob/upload/{upload_id}"),
    ("GET|HEAD", "/ui/blob/{file_id}"),
)

_JSON_TYPES = {"application/json"}
_PART_TYPES = {"application/octet-stream"}
_FORBIDDEN_TYPES = {
    "multipart/form-data",
    "application/x-www-form-urlencoded",
    "text/plain",
}

Send = Callable[[dict[str, Any]], Awaitable[None]]
Receive = Callable[[], Awaitable[dict[str, Any]]]


def _content_type(headers: Mapping[str, str]) -> str:
    raw = headers.get("content-type", "")
    return raw.split(";", 1)[0].strip().lower()


class BlobApp:
    """ASGI app. Construct once per core; `bind_sessions` wires the live cookie table."""

    def __init__(
        self,
        service: BlobService,
        *,
        sessions: Mapping[str, Any] | None = None,
        origins: Sequence[str] = (),
    ) -> None:
        self.service = service
        self.settings = service.settings
        self._sessions: Mapping[str, Any] = sessions if sessions is not None else {}
        self._origins = tuple(origins)
        self._auth = Authenticator(
            tokens=service,
            sessions=lambda: self._sessions,
            origins=self._origins,
            wall=service.now,
        )

    # ------------------------------------------------------------------ wiring
    def bind_sessions(self, sessions: Mapping[str, Any]) -> None:
        self._sessions = sessions

    def bind_origins(self, origins: Sequence[str]) -> None:
        self._origins = tuple(origins)
        self._auth = Authenticator(
            tokens=self.service,
            sessions=lambda: self._sessions,
            origins=self._origins,
            wall=self.service.now,
        )

    def close(self) -> None:
        self.service.close()

    # ---------------------------------------------------------------- lifecycle
    async def start(self) -> None:
        """Start the periodic GC task (called from the ASGI lifespan)."""
        await self.service.start_sweeper()

    async def stop(self) -> None:
        """Cancel the GC task and *await* it, then release the store."""
        await self.service.aclose()

    # --------------------------------------------------------------------- ASGI
    async def __call__(self, scope: Mapping[str, Any], receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            return
        path = str(scope.get("path") or "/")
        method = str(scope.get("method") or "GET").upper()
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        cors = self._cors(origin)

        if not self.settings.enabled:
            await self._json(send, 404, {"code": "NOT_FOUND", "message": "not found"}, cors)
            return
        if origin and cors is None:
            await self._json(
                send, 403,
                {"code": "ORIGIN_MISMATCH", "message": "origin not allowed", "retriable": False},
                None,
            )
            return
        if method == "OPTIONS":
            await self._preflight(send, headers, cors)
            return

        try:
            await self._dispatch(scope, path, method, headers, cors, receive, send)
        except VadeError as exc:
            await self._error(send, exc, cors)

    async def _dispatch(
        self,
        scope: Mapping[str, Any],
        path: str,
        method: str,
        headers: Mapping[str, str],
        cors: Mapping[str, str] | None,
        receive: Receive,
        send: Send,
    ) -> None:
        if path == "/ui/blob":
            if method != "POST":
                await self._method_not_allowed(send, cors, "POST")
                return
            await self._begin(headers, cors, receive, send)
            return

        if path.startswith("/ui/blob/upload/"):
            rest = path[len("/ui/blob/upload/") :]
            if rest.endswith("/complete"):
                if method != "POST":
                    await self._method_not_allowed(send, cors, "POST")
                    return
                await self._complete(rest[: -len("/complete")], headers, cors, receive, send)
                return
            if method == "PUT":
                await self._put_part(rest, scope, headers, cors, receive, send)
                return
            if method == "HEAD":
                await self._head_upload(rest, headers, cors, send)
                return
            if method == "DELETE":
                await self._abort(rest, headers, cors, send)
                return
            await self._method_not_allowed(send, cors, "PUT, HEAD, DELETE")
            return

        if path.startswith("/ui/blob/"):
            file_id = path[len("/ui/blob/") :]
            if method not in {"GET", "HEAD"}:
                await self._method_not_allowed(send, cors, "GET, HEAD")
                return
            await self._get_blob(file_id, method, scope, headers, cors, send)
            return

        await self._json(send, 404, {"code": "NOT_FOUND", "message": "not found", "retriable": False}, cors)

    # ------------------------------------------------------------------ handlers
    async def _begin(self, headers, cors, receive, send) -> None:
        self._require_content_type(headers, _JSON_TYPES)
        principal = self._auth.authenticate(headers)
        body = await self._read_body(receive, BEGIN_BODY_MAX)
        payload = self._json_body(body)
        status, result = self.service.begin(principal, payload)
        await self._json(send, status, result, cors)

    async def _put_part(self, upload_id, scope, headers, cors, receive, send) -> None:
        self._require_content_type(headers, _PART_TYPES)
        principal = self._auth.authenticate(headers)
        offset = self._offset_param(scope)
        limit = self.settings.part_size + PART_BODY_SLACK
        body = await self._read_body(receive, limit)
        status, result = self.service.put_part(
            principal, upload_id, offset, body, chunk_sha256=headers.get("x-vade-chunk-sha256")
        )
        await self._json(send, status, result, cors)

    async def _head_upload(self, upload_id, headers, cors, send) -> None:
        principal = self._auth.authenticate(headers)
        result = self.service.head_upload(principal, upload_id)
        await self._json(send, 200, result, cors)

    async def _complete(self, upload_id, headers, cors, receive, send) -> None:
        self._require_content_type(headers, _JSON_TYPES)
        principal = self._auth.authenticate(headers)
        body = await self._read_body(receive, BEGIN_BODY_MAX)
        payload = self._json_body(body)
        status, result = await self.service.acomplete(principal, upload_id, payload)
        await self._json(send, status, result, cors)

    async def _abort(self, upload_id, headers, cors, send) -> None:
        principal = self._auth.authenticate(headers)
        self.service.abort(principal, upload_id)
        await self._empty(send, 204, cors)

    async def _get_blob(self, file_id, method, scope, headers, cors, send) -> None:
        principal = self._auth.authenticate(headers)
        variant = self._query_param(scope, "v")
        if variant is not None and variant != "thumb":
            raise VadeError("INVALID_ARGUMENT", "v must be 'thumb'", data={"field": "v"})
        blob = self.service.stat(principal, file_id)
        if method == "HEAD":
            await self._blob_metadata(send, blob, cors)
            return
        path, content_type = self.service.content_path(blob, variant)

        etag = f'"sha256:{blob.sha256}"' if variant != "thumb" else f'"sha256:{blob.sha256}-thumb"'
        base_headers = {
            **cors,
            "ETag": etag,
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        }
        if headers.get("if-none-match") == etag:
            await self._empty(send, 304, base_headers)
            return

        size = path.stat().st_size
        window = self._parse_range(headers.get("range"), size)
        if window is None:
            start, end, status = 0, size - 1, 200
        else:
            start, end, status = window[0], window[1], 206
        length = end - start + 1
        extra = dict(base_headers)
        extra["Content-Type"] = content_type
        extra["Content-Length"] = str(length)
        extra["Content-Disposition"] = self._disposition(blob.name, blob.file_id)
        extra["Accept-Ranges"] = "bytes"
        if status == 206:
            extra["Content-Range"] = f"bytes {start}-{end}/{size}"

        await send({"type": "http.response.start", "status": status, "headers": self._encode(extra)})
        remaining = length
        with open(path, "rb") as handle:
            handle.seek(start)
            while remaining > 0:
                block = handle.read(min(_STREAM, remaining))
                if not block:
                    break
                remaining -= len(block)
                await send(
                    {"type": "http.response.body", "body": block, "more_body": remaining > 0}
                )
        if remaining > 0:  # pragma: no cover - file shrank under us
            await send({"type": "http.response.body", "body": b"", "more_body": False})

    async def _blob_metadata(self, send, blob, cors) -> None:
        """HEAD on a file: metadata as headers, no body (HTTP-correct).

        `x-vade-scan` is the field `ui/submit` needs; the rest mirrors the JSON
        metadata so a client can cache without a second GET.
        """
        self.service.content_path(blob)  # 410 when the bytes are gone
        headers = {
            **cors,
            "Content-Type": "application/json; charset=utf-8",
            "Content-Length": "0",
            "ETag": f'"sha256:{blob.sha256}"',
            "Cache-Control": "private, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "X-Vade-File-Id": blob.file_id,
            "X-Vade-Sha256": blob.sha256,
            "X-Vade-Size": str(blob.size),
            "X-Vade-Scan": blob.scan,
            "X-Vade-Pinned": "true" if blob.pinned else "false",
            "X-Vade-Expires-At": "" if blob.expires_at is None else str(int(blob.expires_at * 1000)),
            "Accept-Ranges": "bytes",
        }
        await send({"type": "http.response.start", "status": 200, "headers": self._encode(headers)})
        await send({"type": "http.response.body", "body": b""})

    # -------------------------------------------------------------------- gates
    def _require_content_type(self, headers: Mapping[str, str], allowed: set[str]) -> None:
        actual = _content_type(headers)
        if actual in _FORBIDDEN_TYPES:
            # 415 is the whole point: these are CORS simple requests and would
            # bypass the preflight that guards the cookie credential (§8.2).
            raise VadeError(
                "UNSUPPORTED",
                "multipart and form content types are refused on the upload channel",
                data={"field": "content-type", "reason": "simple_request_forbidden", "actual": actual},
            )
        if actual not in allowed:
            raise VadeError(
                "UNSUPPORTED",
                "unsupported content type for this endpoint",
                data={"field": "content-type", "expected": sorted(allowed), "actual": actual},
            )

    @staticmethod
    def _offset_param(scope: Mapping[str, Any]) -> int:
        raw = BlobApp._query_param(scope, "offset")
        if raw is None:
            raise VadeError("INVALID_ARGUMENT", "offset query parameter is required", data={"field": "offset"})
        try:
            value = int(raw)
        except (TypeError, ValueError):
            raise VadeError(
                "INVALID_ARGUMENT", "offset must be an integer", data={"field": "offset"}
            ) from None
        if value < 0:
            raise VadeError("INVALID_ARGUMENT", "offset must be non-negative", data={"field": "offset"})
        return value

    @staticmethod
    def _query_param(scope: Mapping[str, Any], name: str) -> str | None:
        query = scope.get("query_string") or b""
        if isinstance(query, bytes):
            query = query.decode("latin-1")
        values = parse_qs(str(query), keep_blank_values=True).get(name)
        return values[0] if values else None

    @staticmethod
    def _json_body(body: bytes) -> dict[str, Any]:
        if not body:
            raise VadeError("INVALID_ARGUMENT", "request body is empty", data={"field": "body"})
        try:
            payload = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise VadeError("INVALID_ARGUMENT", "body must be valid JSON", data={"field": "body"}) from None
        if not isinstance(payload, dict):
            raise VadeError("INVALID_ARGUMENT", "body must be a JSON object", data={"field": "body"})
        return payload

    @staticmethod
    async def _read_body(receive: Receive, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                break
            chunk = message.get("body") or b""
            total += len(chunk)
            if total > limit:
                raise VadeError(
                    "PAYLOAD_TOO_LARGE",
                    "request body exceeds the limit for this endpoint",
                    data={"field": "body", "limit": limit},
                )
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        return b"".join(chunks)

    # -------------------------------------------------------------------- CORS
    def _cors(self, origin: str | None) -> dict[str, str] | None:
        if not origin:
            return {}
        if origin in self._origins:
            return {
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Credentials": "true",
                "Vary": "Origin",
            }
        return None

    async def _preflight(self, send: Send, headers: Mapping[str, str], cors: Mapping[str, str] | None) -> None:
        if cors is None:
            await self._json(
                send, 403,
                {"code": "ORIGIN_MISMATCH", "message": "origin not allowed", "retriable": False},
                None,
            )
            return
        allow = {
            **cors,
            "Access-Control-Allow-Methods": "GET, HEAD, POST, PUT, DELETE, OPTIONS",
            "Access-Control-Allow-Headers": (
                "authorization, content-type, x-vade-csrf, x-vade-chunk-sha256, range, if-none-match"
            ),
            "Access-Control-Max-Age": "600",
            "Content-Length": "0",
        }
        await send({"type": "http.response.start", "status": 204, "headers": self._encode(allow)})
        await send({"type": "http.response.body", "body": b""})

    # ------------------------------------------------------------------ responses
    async def _method_not_allowed(self, send: Send, cors: Mapping[str, str] | None, allow: str) -> None:
        await self._json(
            send, 405,
            {"code": "INVALID_ARGUMENT", "message": "method not allowed", "retriable": False},
            cors or {},
            {"Allow": allow},
        )

    async def _json(
        self,
        send: Send,
        status: int,
        payload: Mapping[str, Any],
        cors: Mapping[str, str] | None,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"}
        headers.update(cors or {})
        headers.update(extra or {})
        await send({"type": "http.response.start", "status": status, "headers": self._encode(headers)})
        await send({"type": "http.response.body", "body": body})

    async def _empty(self, send: Send, status: int, cors: Mapping[str, str] | None) -> None:
        headers = {"Content-Length": "0"}
        headers.update(cors or {})
        await send({"type": "http.response.start", "status": status, "headers": self._encode(headers)})
        await send({"type": "http.response.body", "body": b""})

    async def _error(self, send: Send, exc: VadeError, cors: Mapping[str, str] | None) -> None:
        status = http_status(exc.code)
        data = dict(exc.data or {})
        if data.pop("gone", False):
            status = 410  # row exists, bytes are gone: not the same as "not yours"
        if exc.code == "UNSUPPORTED" and data.get("field") in {"content-type", "thumb", "v"}:
            status = 415  # §4.4 / §8.2 say 415 for these two, code stays C-04
        payload: dict[str, Any] = {"code": exc.code, "message": exc.message, "retriable": exc.retriable}
        if data:
            payload["data"] = data
        extra: dict[str, str] = {}
        if exc.code == "OVERLOADED" and "retry_after" in data:
            extra["Retry-After"] = str(int(data["retry_after"]))
        await self._json(send, status, payload, cors, extra)

    @staticmethod
    def _encode(headers: Mapping[str, str]) -> list[tuple[bytes, bytes]]:
        return [(str(k).lower().encode("latin-1"), str(v).encode("latin-1")) for k, v in headers.items()]

    @staticmethod
    def _parse_range(value: str | None, size: int) -> tuple[int, int] | None:
        if not value:
            return None
        if not value.lower().startswith("bytes="):
            raise VadeError("INVALID_ARGUMENT", "only single-range bytes requests are supported", data={"field": "range"})
        spec = value[6:].strip()
        if "," in spec:
            raise VadeError("INVALID_ARGUMENT", "multiple ranges are not supported", data={"field": "range"})
        start_text, _, end_text = spec.partition("-")
        try:
            if not start_text:
                if not end_text:
                    raise ValueError
                suffix = int(end_text)
                if suffix <= 0:
                    raise ValueError
                start = max(0, size - suffix)
                end = size - 1
            else:
                start = int(start_text)
                end = int(end_text) if end_text else size - 1
        except ValueError:
            raise VadeError("INVALID_ARGUMENT", "malformed Range header", data={"field": "range"}) from None
        if start < 0 or start >= size or start > end:
            raise VadeError(
                "INVALID_ARGUMENT", "Range is outside the file", data={"field": "range", "size": size}
            )
        return start, min(end, size - 1)

    @staticmethod
    def _disposition(name: str, file_id: str) -> str:
        safe = sanitize_name(name, default=file_id)
        ascii_name = "".join(ch if 32 <= ord(ch) < 127 and ch not in '"\\' else "_" for ch in safe)
        encoded = quote(safe, safe="")
        return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{encoded}"


def build_from_config(config: Any, data_dir: Any, *, sessions: Mapping[str, Any] | None = None) -> BlobApp:
    """Component hook used by `vade.blob.install`."""
    from .settings import AttachSettings

    settings = AttachSettings.from_config(config)
    service = BlobService(data_dir, settings)
    origins = tuple(config.get("server.ui_origins", []) or []) if config is not None else ()
    return BlobApp(service, sessions=sessions, origins=origins)
