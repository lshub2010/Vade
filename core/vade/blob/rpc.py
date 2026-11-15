"""`ui/blob.token` (RFC-0016 §4): the ONLY minting surface for a device bearer.

Same shape as `session/rpc.py` and `modulepage/rpc.py`: `async handler(connection,
params)` returning a plain dict, `VadeError` for refusals. `install()` (in
`__init__.py`) merges the name into `app.session_handlers` — the one table
`app.make_rpc_handler` reads — and always publishes a provider, because `blob`
sorts before `session` alphabetically and the dict does not exist yet at
install time (the modulepage docstring tells that story in full).

Why the `connection` argument is load-bearing (§5.1): the token binds to the
calling connection's PROVEN `device_id` (Noise static key -> whitelist,
`transport/server.py:354-378`). There is no `device_id` parameter — a hint
from the wire would be a claim, not an identity (§4.2 refuses any unknown key
for exactly this reason), so there is no "mint for another device" shape and
no existence oracle.

Check order (deterministic, documented for the client's §7.2 state machine):
params -> attachments available -> read-only -> cookie session -> device id.
Each earlier failure is the cheaper / more truthful answer; e.g. a disabled
feature answers UNSUPPORTED to everyone because "Disabled" is the stable
per-core state a client maps it to, whatever the caller is.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from ..errors import VadeError

__all__ = ["BlobTokenRPC", "BLOB_TOKEN_ENDPOINTS"]

#: RFC-0016 §4.3: the six routes of the same listener, verbatim aliases of
#: `http_endpoints.endpoints[]` (method + path, decorators included). Not a
#: second contract: `tools/check_contracts.py` compares this table against the
#: `http_endpoints` rows, so a route drift fails the gate instead of silently
#: desyncing the two copies.
BLOB_TOKEN_ENDPOINTS: dict[str, str] = {
    "begin": "POST /ui/blob",
    "part": "PUT /ui/blob/upload/{upload_id}?offset=N",
    "complete": "POST /ui/blob/upload/{upload_id}/complete",
    "abort": "DELETE /ui/blob/upload/{upload_id}",
    "resume": "HEAD /ui/blob/upload/{upload_id}",
    "fetch": "GET|HEAD /ui/blob/{file_id}[?v=thumb]",
}

#: RFC-0016 §5.2 (拍板 Q2: 600 s，沿用 `TOKEN_TTL_*` 常量)。
TTL_DEFAULT = 600
TTL_MIN = 60
TTL_MAX = 900


class BlobTokenRPC:
    """Holds the app (not the service): `app.blob` is None when attachments
    are disabled, and the handler must still exist and answer UNSUPPORTED
    (RFC-0016 §5.7-20) so a client can tell "disabled" from "method absent".
    """

    def __init__(self, app: Any) -> None:
        self._app = app

    def handlers(self) -> dict[str, Callable[[Any, Mapping[str, Any]], Awaitable[Any]]]:
        return {"ui/blob.token": self.mint}

    # ------------------------------------------------------------------ mint
    async def mint(self, connection: Any, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """Mint a device bearer bound to `connection.device_id` (§4/§5).

        Failure modes and their codes (§6.6 — all reuse C-04, none new):
        `ttl_s` bad / unknown key -> INVALID_ARGUMENT; attachments off or the
        component missing -> UNSUPPORTED `attachments_disabled`; explicitly
        read-only connection -> READ_ONLY_SESSION; cookie session ->
        PERMISSION_DENIED `cookie_session`; no proven identity ->
        PERMISSION_DENIED `device_unknown`. Failures write no `blob_tokens` row.
        """
        # 1. Params are strict (§4.2). This method mints a credential: a
        #    silently ignored `device_id`/`read_only` key would let a client
        #    believe it minted something it did not. `None` params == `{}` —
        #    a client may omit params entirely, exactly like an empty object.
        rest = _strict_params(params)
        service = self._service()
        if service is None:
            # §5.7-19/20: `attach.enabled=false` (and dev-mode component
            # absence) is "this core has attachments off", not a malfunction —
            # the same code+reason `ui/submit` answers for the same condition
            # (session/parts.py:486-493). The six HTTP routes 404 in this
            # state; the ws surface says UNSUPPORTED so the client shows
            # "disabled", not "update your core".
            raise VadeError(
                "UNSUPPORTED",
                "attachment storage is not available on this core",
                data={"reason": "attachments_disabled"},
            )
        ttl_s = rest.get("ttl_s", TTL_DEFAULT)
        # RFC-0016 §5.2-6: reject, never clamp. The q&d clamp inside
        # `BlobService.issue_token` stays as an internal fallback for direct
        # callers, but a credential's lifetime is never silently corrected:
        # `int()` accepts "600"/True/1.5 and the clamp would mint 60/600/900
        # for a client that asked for something else (§10 A4 pins each case).
        if isinstance(ttl_s, bool) or not isinstance(ttl_s, int) or not (TTL_MIN <= ttl_s <= TTL_MAX):
            raise VadeError(
                "INVALID_ARGUMENT",
                f"ttl_s must be an integer between {TTL_MIN} and {TTL_MAX}",
                data={"field": "ttl_s"},
            )
        # 3. Explicitly read-only connections never mint (§5.7-18). Same
        #    criterion as `session/rpc.py::_read_only`. Q8 (RFC default, NOT
        #    拍板): the `blob_tokens.read_only` column stays unused — one
        #    method carrying "writable token" and "read-only token" semantics
        #    would make clients guess from a boolean; if a read-only device
        #    ever needs history images, that is a separate RFC.
        if _read_only(connection):
            raise VadeError(
                "READ_ONLY_SESSION",
                "this session is explicitly read-only; minting an upload credential is refused",
            )
        # 4. Cookie sessions are refused (拍板 Q1, §5.3). The browser already
        #    holds a working credential (cookie + Origin + Sec-Fetch-Site +
        #    CSRF double-submit, blob/auth.py:115-156); minting would upgrade
        #    an ambient, four-gate credential into a portable bearer for zero
        #    benefit. Judged by `browser_session` (set by the /ui/session
        #    cookie hit, transport/server.py:311-320), NOT `ui_token` — the
        #    latter is a dynamically attached attribute, not part of
        #    `Connection.__init__` (§5.3-8).
        if bool(getattr(connection, "browser_session", False)):
            raise VadeError(
                "PERMISSION_DENIED",
                "browser sessions use their cookie credential; ui/blob.token is native-only",
                data={"reason": "cookie_session"},
            )
        # 5. Identity comes only from the transport (§5.1-2). An unlisted
        #    Noise peer keeps `device_id=None` (fail-closed) and gets the same
        #    code+reason `ui/submit` uses for that state (parts.py:493-500).
        device_id = getattr(connection, "device_id", None)
        if not device_id:
            raise VadeError(
                "PERMISSION_DENIED",
                "attachments require a known device identity (complete pairing first)",
                data={"reason": "device_unknown"},
            )
        # TTL 内可重放 (拍板 Q3): the row is NOT consumed on use — `get_token`
        # keeps it until expiry, so shard retries reuse the same token. Q5
        # (RFC default, NOT 拍板): bearer semantics, possession is enough —
        # per-request device signing is RFC-0004 §11-B and stays unimplemented.
        issued = service.issue_token(str(device_id), ttl_s=ttl_s)
        return {
            "token": issued["token"],
            "expires_at": issued["expires_at"],
            "max_bytes": issued["max_bytes"],
            "endpoints": dict(BLOB_TOKEN_ENDPOINTS),
        }

    # --------------------------------------------------------------- plumbing
    def _service(self) -> Any:
        blob = getattr(self._app, "blob", None)
        if blob is None:
            return None
        return getattr(blob, "service", None)


def _strict_params(params: Mapping[str, Any] | None) -> dict[str, Any]:
    """`{ttl_s?: int}` and nothing else (§4.2). Unknown key -> the key's name."""
    if params is None:
        return {}
    if not isinstance(params, Mapping):
        raise VadeError(
            "INVALID_ARGUMENT", "params must be an object", data={"field": "params"}
        )
    for key in sorted(params):
        if key != "ttl_s":
            raise VadeError(
                "INVALID_ARGUMENT",
                f"unknown parameter {key!r}: ui/blob.token accepts only ttl_s",
                data={"field": key},
            )
    return dict(params)


def _read_only(connection: Any) -> bool:
    """The `ui/submit` criterion, verbatim (session/rpc.py:518-522)."""
    if bool(getattr(connection, "read_only", False)):
        return True
    principal = getattr(connection, "principal", None)
    return bool(getattr(principal, "read_only", False))
