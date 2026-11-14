"""Who is calling `ui/*` (RFC-0009 §10.2).

`modulepage` used to serve any connection: module A's own Noise socket could ask
`ui/page.get com.b` and receive B's page, `ui/state.get com.b` and receive B's
state, and `ui/tool.call com.b/refresh` and execute B's tool. The page→tool
"ownership" rule (§6.2) only ever answered *"is this tool declared by the module
that owns the page"*; it never asked who was asking.

This module answers that from the **connection** and nothing else — never from a
`params` field, because a parameter is exactly what the attacker controls:

* a Noise peer is named by the whitelist (`module_id#instance_id`), the same
  string `transport/server.py::_bind_device_identity` writes into
  `connection.device_id` after the handshake proved the static key. The peer
  cannot type it.
* a browser session is `ui-<hex>`, minted by `/ui/session` and read back from
  the `__Host-ac_ui` cookie. It is the human looking at a module card, so it may
  read the pages of **enabled** modules — that is the whole point of C-19 — but
  it may never turn a page into a tool call without an approval dialog.
* anything else (`device_id` empty, unpaired, or a shape we do not recognise) is
  `unknown` and is refused. Fail closed, never "probably fine".

The one thing this module must NOT do is look only at `module_id`: that is the
bug, not the fix.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import VadeError

__all__ = [
    "BROWSER_PREFIX",
    "Caller",
    "authorize_target",
    "caller_of",
    "device_of",
    "is_read_only",
    "require_caller",
    "require_visible",
]

#: `/ui/session` mints `ui-<16 hex>` (`transport/server.py`). A module id can
#: never start with this: `module_id` is matched by `^[a-z][a-z0-9_.-]*$` and a
#: trailing `-` after `ui` would make it `ui-…`, which the registry rejects for
#: the same reason the pairing screen shows the id.
BROWSER_PREFIX = "ui-"

#: The separator `Registry` uses for a node id (`NodeRecord.node_id`).
IDENTITY_SEPARATOR = "#"


@dataclass(frozen=True, slots=True)
class Caller:
    """A resolved caller: `module`, `browser` or `unknown`."""

    kind: str
    device_id: str
    module_id: str | None = None
    instance_id: str | None = None

    @property
    def is_browser(self) -> bool:
        return self.kind == "browser"

    @property
    def is_module(self) -> bool:
        return self.kind == "module"


def device_of(connection: Any) -> str:
    """`connection.device_id` as a string; `""` when it is absent or not a str.

    Never falls back to `connection_id`: a socket id is not an identity, and
    treating it as one would make every unpaired peer uniquely "authenticated".
    """
    value = getattr(connection, "device_id", None)
    return value if isinstance(value, str) else ""


def caller_of(connection: Any) -> Caller:
    """Resolve the caller from the connection, without trusting any parameter."""
    device_id = device_of(connection)
    if bool(getattr(connection, "browser_session", False)) or device_id.startswith(BROWSER_PREFIX):
        return Caller(kind="browser", device_id=device_id)
    module_id, separator, instance_id = device_id.partition(IDENTITY_SEPARATOR)
    if separator and module_id and instance_id:
        return Caller(kind="module", device_id=device_id, module_id=module_id, instance_id=instance_id)
    return Caller(kind="unknown", device_id=device_id)


def require_caller(connection: Any) -> Caller:
    """The caller, or `PERMISSION_DENIED` when the socket carries no identity."""
    caller = caller_of(connection)
    if caller.kind == "unknown":
        raise VadeError(
            "PERMISSION_DENIED",
            "this connection has no verified identity; ui/* requires a paired module or a browser session",
            data={"field": "module_id"},
        )
    return caller


def authorize_target(connection: Any, module_id: str, instance_id: str) -> Caller:
    """Refuse a target that is not the caller's own module.

    Runs **before** the registry lookup on purpose: a module probing
    `com.b` must not be able to tell "exists" from "does not exist" by the
    difference between `NOT_FOUND` and `PERMISSION_DENIED`.
    """
    caller = require_caller(connection)
    if caller.is_module and (caller.module_id, caller.instance_id) != (module_id, instance_id):
        raise VadeError(
            "PERMISSION_DENIED",
            f"{caller.device_id} may not act on {module_id}#{instance_id}; "
            "a page belongs to the module that declared it",
            data={"field": "module_id"},
        )
    return caller


def require_visible(caller: Caller, record: Any) -> None:
    """A browser may only read an ENABLED module's page (§8.2's entry rules)."""
    if getattr(record, "enabled", True):
        return
    raise VadeError(
        "PERMISSION_DENIED",
        f"{record.module_id}#{record.instance_id} is disabled; its pages are not served",
        data={"field": "module_id"},
    )


def is_read_only(connection: Any) -> bool:
    """`read_only` on the connection or on its principal (AMEND-22 #14).

    Only an EXPLICITLY read-only session is refused; a plain cookie session is
    writable and behaves like any other (D100).
    """
    if bool(getattr(connection, "read_only", False)):
        return True
    principal = getattr(connection, "principal", None)
    return bool(getattr(principal, "read_only", False))
