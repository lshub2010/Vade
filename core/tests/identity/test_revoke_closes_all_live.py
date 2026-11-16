"""K6 revocation: a key unbind must close EVERY live socket that proved it.

`Registry.revoke_fingerprint` only knows nodes that finished
`module/register`. The gap this file pins: a peer that completed the Noise
handshake and is waiting to register holds an authenticated session but no
`NodeRecord`, so registry-only revocation left it connected — the exact peer an
operator revoking a compromised key wants gone first.
"""

from __future__ import annotations

from typing import Any

from vade.app import App
from vade.identity.whitelist import Whitelist

MODULE_ID = "com.example.fs"
INSTANCE_ID = "fs-01"
DEVICE = f"{MODULE_ID}#{INSTANCE_ID}"
FP = "ab" * 16
OTHER_FP = "cd" * 16


class _Crypto:
    def __init__(self, fingerprint: str | None) -> None:
        self.remote_fingerprint = fingerprint


class _Conn:
    """Duck-typed `Connection`: only what revocation touches."""

    def __init__(self, connection_id: str, fingerprint: str | None, *, browser: bool = False) -> None:
        self.connection_id = connection_id
        self.crypto = None if browser else _Crypto(fingerprint)
        self.closed: list[tuple[int, str]] = []

    def request_close(self, code: int = 1001, reason: str = "closed by core") -> None:
        self.closed.append((code, reason))


class _Registry:
    """Returns the ids it considers registered-closed; records the call."""

    def __init__(self, closed: tuple[str, ...] = ()) -> None:
        self._closed = closed
        self.seen: list[str] = []

    def revoke_fingerprint(self, fingerprint: str) -> tuple[str, ...]:
        self.seen.append(fingerprint)
        return self._closed


def _app(connections: dict[str, Any], registry: Any = None) -> App:
    app = App({"core.heartbeat_interval_s": 10})  # type: ignore[arg-type]
    app.transport = {"connections": connections}
    app.registry = registry
    return app


def test_handshaken_but_unregistered_connection_is_closed():
    unregistered = _Conn("c-unregistered", FP)
    app = _app({unregistered.connection_id: unregistered}, _Registry(closed=()))

    app._revoke_connection(FP, ())

    assert unregistered.closed == [(1001, "fingerprint revoked")], (
        "a handshaken peer without module/register still owns an authenticated session"
    )


def test_every_live_socket_of_the_key_is_closed():
    connections = {
        "c-1": _Conn("c-1", FP),
        "c-2": _Conn("c-2", FP),
        "c-3": _Conn("c-3", OTHER_FP),
        "c-browser": _Conn("c-browser", None, browser=True),
    }
    app = _app(connections, _Registry(closed=()))

    app._revoke_connection(FP, ())

    assert connections["c-1"].closed and connections["c-2"].closed
    assert connections["c-3"].closed == [], "another key keeps its socket"
    assert connections["c-browser"].closed == [], "a browser cookie has no key to revoke"


def test_registered_socket_is_closed_by_the_registry_not_twice():
    registered = _Conn("c-registered", FP)
    registry = _Registry(closed=("c-registered",))
    app = _app({registered.connection_id: registered}, registry)

    app._revoke_connection(FP, ())

    assert registry.seen == [FP]
    assert registered.closed == [], (
        "the registry already scheduled this close; the sweep skips ids it returned"
    )


def test_whitelist_revoke_closes_the_unregistered_socket_end_to_end(tmp_path):
    unregistered = _Conn("c-pending", FP)
    app = _app({unregistered.connection_id: unregistered}, _Registry(closed=()))
    whitelist = Whitelist(tmp_path / "whitelist.json", on_revoke=app._revoke_connection)
    app.whitelist = whitelist
    whitelist.add(MODULE_ID, INSTANCE_ID, FP)

    assert whitelist.revoke(FP) == (DEVICE,)

    assert unregistered.closed == [(1001, "fingerprint revoked")]


def test_revocation_survives_a_connection_that_lost_its_crypto():
    """Half-torn-down sockets must not abort the sweep before the rest."""
    broken = _Conn("c-broken", FP)
    broken.crypto = None  # crypto released during teardown
    healthy = _Conn("c-healthy", FP)
    app = _app({broken.connection_id: broken, healthy.connection_id: healthy}, _Registry(closed=()))

    app._revoke_connection(FP, ())

    assert broken.closed == []
    assert healthy.closed == [(1001, "fingerprint revoked")]


def test_the_admin_route_path_sweeps_pending_sockets_too():
    """`POST /admin/api/nodes/{id}/revoke` calls `registry.revoke_fingerprint`.

    That call knows registered nodes only; once the sweep is installed it also
    closes the handshaken-but-unregistered socket, with the registry's return
    value (what the route counts) unchanged.
    """
    registered = _Conn("c-registered", FP)
    pending = _Conn("c-pending", FP)
    registry = _Registry(closed=("c-registered",))
    app = _app({"c-registered": registered, "c-pending": pending}, registry)
    app._install_revoke_sweep()

    closed = registry.revoke_fingerprint(FP)

    assert closed == ("c-registered",), "the route counts what the registry reported"
    assert pending.closed == [(1001, "fingerprint revoked")]


def test_installing_the_sweep_twice_does_not_chain_or_double_close():
    pending = _Conn("c-pending", FP)
    app = _app({"c-pending": pending}, _Registry(closed=()))
    app._install_revoke_sweep()
    installed = app.registry
    assert installed is not None
    first = installed.revoke_fingerprint
    app._install_revoke_sweep()

    assert installed.revoke_fingerprint is first, "a second install must not wrap the wrapper"
    installed.revoke_fingerprint(FP)
    assert pending.closed == [(1001, "fingerprint revoked")], "one close per socket, not one per wrapper"
