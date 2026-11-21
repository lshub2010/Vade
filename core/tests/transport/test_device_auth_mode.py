import types

from vade.transport.server import CoreApp


class _Conn:
    def __init__(self) -> None:
        self.crypto = types.SimpleNamespace(remote_fingerprint="fp-1")
        self.device_key = None
        self.device_id = None
        self.auth_mode = None


class _Whitelist:
    def __init__(self, name: str) -> None:
        self._name = name

    def device_for_fingerprint(self, fingerprint: str) -> str:
        return self._name


def _bind(name: str) -> _Conn:
    conn = _Conn()
    owner = types.SimpleNamespace(_whitelist=_Whitelist(name))
    CoreApp._bind_device_identity(owner, conn)  # type: ignore[arg-type]
    return conn


def test_a_whitelisted_module_key_is_not_a_device_approver():
    """`module_id#instance_id` must stay module_key: a module cannot approve itself."""
    conn = _bind("mod.echo#inst-1")
    assert conn.auth_mode == "module_key"
    assert conn.device_id is None
    assert conn.device_key == "fp-1"


def test_a_resolved_ui_device_is_a_device_key_approver():
    conn = _bind("ui-abc123")
    assert conn.auth_mode == "device_key"
    assert conn.device_id == "ui-abc123"
