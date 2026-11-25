"""0.0.3 — `model/discover`: upstream `/models` discovery over the RPC surface.

The ONLY fake is the upstream boundary (`OpenAICompatProvider.list_models`):
`ModelResolver.discover()` builds the provider itself, so there is no transport
to inject. Everything else is the real object a socket would drive — the shipped
`Config` schema defaults, `ModelResolver` and `SessionRPC.dispatch` (which is
also where a `VadeError` becomes a `RpcError` envelope).

What the design record promises is pinned here:

* a successful discovery APPENDS ids to `model/list` without duplicating the
  configured entry, and every discovered row keeps its capability fields
  `null` (nothing is inferred from a model name);
* a mode whose list core does not own (`module`, `mock`, empty `base_url`)
  answers `UNAVAILABLE` and never touches the upstream;
* a failed refresh answers `UNAVAILABLE` but KEEPS the last good list — the
  picker must not blank on a 401/503;
* the 2 s cooldown answers from cache (`cached=true`) instead of turning the
  refresh button into a request amplifier;
* a successful refresh moves `revision` even when the upstream adds no new id
  (a revision that could fall back would strand `model/list?if_revision`).
"""

from __future__ import annotations

import asyncio

import pytest

from vade.llm.catalog import ModelResolver
from vade.session.manager import SessionLog
from vade.session.replay import ReplayBuffer
from vade.session.rpc import WRITE_METHODS, SessionRPC
from vade.session.sqlite_store import SQLiteStore
from vade.transport.connection import RpcError

from .helpers import FakeConnection, make_config

#: Exactly the keys `model/discover` promises (schema/rpc.json, the 1.8.0 append).
RESULT_KEYS = {"revision", "models", "discovered_at", "cached"}


class FakeUpstream:
    """`GET {base_url}/models` stand-in: records calls, can fail on demand."""

    def __init__(self, ids: list[str] | None = None, error: Exception | None = None) -> None:
        self.ids = list(ids or [])
        self.error = error
        self.calls = 0

    async def list_models(self) -> list[str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.ids)


def _patch_upstream(monkeypatch: pytest.MonkeyPatch, upstream: FakeUpstream) -> None:
    """Replace ONLY the HTTP call; the provider object itself stays real."""
    from vade.llm import openai_compat

    async def fake_list_models(self) -> list[str]:  # noqa: ANN001 - bound-method shape
        return await upstream.list_models()

    monkeypatch.setattr(openai_compat.OpenAICompatProvider, "list_models", fake_list_models)


def _builtin_cfg(**overrides):
    return make_config(
        **{
            "llm.provider": "openai_compatible",
            "llm.base_url": "http://llm.test/v1",
            "llm.model": "gpt-4o-mini",
            "llm.api_key_env": "VADE_TEST_API_KEY",
            **overrides,
        }
    )


def _rpc(tmp_path, cfg, data_dir=None):
    log = SessionLog(SQLiteStore(tmp_path / "sessions.db"), replay=ReplayBuffer(maxlen=100))
    resolver = ModelResolver(cfg, None, data_dir=data_dir)
    log.set_model_resolver(resolver)
    return log, SessionRPC(log), resolver


def _discover(rpc: SessionRPC, conn: FakeConnection):
    return asyncio.run(rpc.dispatch(conn, "model/discover", {}))


def test_discover_success_appends_upstream_ids_and_bumps_the_revision(tmp_path, monkeypatch):
    upstream = FakeUpstream(["gpt-4o-mini", "qwen2.5-vl-72b", "llama-3.3-70b"])
    _patch_upstream(monkeypatch, upstream)
    log, rpc, resolver = _rpc(tmp_path, _builtin_cfg())
    conn = FakeConnection()
    before = resolver.catalog()

    result = _discover(rpc, conn)

    assert isinstance(result, dict)
    assert set(result) == RESULT_KEYS
    assert result["cached"] is False
    assert result["discovered_at"].endswith("Z")
    assert result["revision"] > before.revision
    assert upstream.calls == 1

    ids = [row["id"] for row in result["models"]]
    # The configured entry stays single: discovery must not duplicate it.
    assert ids.count("builtin/gpt-4o-mini") == 1
    assert "builtin/qwen2.5-vl-72b" in ids and "builtin/llama-3.3-70b" in ids

    row = next(item for item in result["models"] if item["id"] == "builtin/qwen2.5-vl-72b")
    assert row["provider"] == "builtin"
    assert row["display_name"] == "qwen2.5-vl-72b"
    # No capability is guessed from the name — unknown stays null.
    assert row["context_window"] is None
    assert row["supports_tools"] is None
    assert row["input_modalities"] is None
    assert row["max_image_bytes"] is None
    assert row["available"] is True

    # The client re-pulls model/list and sees the same revision + entries.
    listed = asyncio.run(rpc.dispatch(conn, "model/list", {}))
    assert listed["revision"] == result["revision"]
    assert [item["id"] for item in listed["models"]] == ids

    # A discovered id is a legal session choice, exactly like a configured one.
    session = log.create_session()
    chosen = asyncio.run(
        rpc.dispatch(conn, "session/model.set",
                     {"session_id": session.id, "model_id": "builtin/qwen2.5-vl-72b"})
    )
    assert chosen["model_id"] == "builtin/qwen2.5-vl-72b"
    assert chosen["source"] == "session"


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("module", "http://llm.test/v1"),   # the module declares its own models
        ("mock", "http://llm.test/v1"),     # no upstream at all
        ("openai_compatible", ""),          # nothing to probe
    ],
)
def test_discover_is_unavailable_when_core_does_not_own_the_list(
    tmp_path, monkeypatch, provider, base_url
):
    upstream = FakeUpstream(["never-called"])
    _patch_upstream(monkeypatch, upstream)
    cfg = _builtin_cfg(**{"llm.provider": provider, "llm.base_url": base_url})
    _, rpc, _ = _rpc(tmp_path, cfg)

    result = _discover(rpc, FakeConnection())

    assert isinstance(result, RpcError)
    assert result.code == "UNAVAILABLE"
    assert result.data == {"reason": "not_discoverable"}
    assert upstream.calls == 0, "core must not probe a list it does not own"


def test_failed_refresh_keeps_the_last_good_list(tmp_path, monkeypatch):
    upstream = FakeUpstream(["gpt-4o-mini", "qwen2.5-vl-72b"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, resolver = _rpc(tmp_path, _builtin_cfg())
    conn = FakeConnection()

    first = _discover(rpc, conn)
    assert first["cached"] is False
    good_ids = [entry.id for entry in resolver.catalog().entries]
    good_revision = resolver.catalog().revision
    assert "builtin/qwen2.5-vl-72b" in good_ids

    # Let the second call reach the upstream even though the first just ran.
    monkeypatch.setattr(ModelResolver, "DISCOVERY_COOLDOWN_S", 0.0)
    upstream.error = RuntimeError("503 Service Unavailable")

    failed = _discover(rpc, conn)

    assert isinstance(failed, RpcError)
    assert failed.code == "UNAVAILABLE"
    assert failed.data == {"reason": "upstream_error"}
    assert "503" in failed.message and "RuntimeError" in failed.message
    assert upstream.calls == 2

    # The cache is replaced ONLY on success: same ids, same revision.
    after = resolver.catalog()
    assert [entry.id for entry in after.entries] == good_ids
    assert after.revision == good_revision
    listed = asyncio.run(rpc.dispatch(conn, "model/list", {}))
    assert [item["id"] for item in listed["models"]] == good_ids


def test_refresh_inside_the_cooldown_is_a_cache_hit(tmp_path, monkeypatch):
    upstream = FakeUpstream(["gpt-4o-mini", "qwen2.5-vl-72b"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, _ = _rpc(tmp_path, _builtin_cfg())
    conn = FakeConnection()

    first = _discover(rpc, conn)
    second = _discover(rpc, conn)

    assert first["cached"] is False
    assert second["cached"] is True
    assert second["revision"] == first["revision"]
    assert second["discovered_at"] == first["discovered_at"]
    assert [item["id"] for item in second["models"]] == [item["id"] for item in first["models"]]
    assert upstream.calls == 1, "a refresh inside 2 s must not reach the upstream"


def test_success_that_adds_no_new_id_still_moves_the_revision(tmp_path, monkeypatch):
    """`revision` is the client's invalidation edge — it may not go backwards.

    A later discovery can legitimately return only the configured model (the
    gateway shrank its list). The entries are then unchanged, but the revision
    must still move: `model/list?if_revision` would otherwise answer
    `not_modified` forever after having shown the bigger list.
    """
    upstream = FakeUpstream(["gpt-4o-mini", "qwen2.5-vl-72b"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, _ = _rpc(tmp_path, _builtin_cfg())
    conn = FakeConnection()

    first = _discover(rpc, conn)
    assert "builtin/qwen2.5-vl-72b" in [row["id"] for row in first["models"]]

    monkeypatch.setattr(ModelResolver, "DISCOVERY_COOLDOWN_S", 0.0)
    upstream.ids = ["gpt-4o-mini"]
    second = _discover(rpc, conn)

    assert second["cached"] is False
    assert second["revision"] > first["revision"]
    assert [row["id"] for row in second["models"]] == ["builtin/gpt-4o-mini"]
    assert upstream.calls == 2


def test_discover_is_registered_and_read_only_viewers_may_refresh(tmp_path, monkeypatch):
    # Registration + the read-only decision are both contract facts: the method
    # declares no READ_ONLY_SESSION, so it must not be a WRITE_METHODS verb.
    assert "model/discover" in SessionRPC(None).handlers()
    assert "model/discover" not in WRITE_METHODS

    upstream = FakeUpstream(["gpt-4o-mini"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, _ = _rpc(tmp_path, _builtin_cfg())
    conn = FakeConnection()
    conn.read_only = True

    result = _discover(rpc, conn)

    assert isinstance(result, dict)
    assert result["cached"] is False
    assert upstream.calls == 1


def test_discovery_never_changes_the_configured_default(tmp_path, monkeypatch):
    """Refreshing the list is a read; it must not silently re-route turns.

    With equal priority the resolver's last tie-breaker is lexical id, so an
    upstream id like `claude-*` would win over the configured `gpt-*`.
    """
    upstream = FakeUpstream(["claude-3-haiku", "gpt-4o-mini", "zzz-70b"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, resolver = _rpc(tmp_path, _builtin_cfg())

    before = resolver.resolve(None).model_id
    assert before == "builtin/gpt-4o-mini"

    result = _discover(rpc, FakeConnection())

    assert result["revision"] > 0
    assert resolver.resolve(None).model_id == before


def test_failed_refresh_arms_the_cooldown(tmp_path, monkeypatch):
    upstream = FakeUpstream([], error=RuntimeError("503 Service Unavailable"))
    _patch_upstream(monkeypatch, upstream)
    _, rpc, _ = _rpc(tmp_path, _builtin_cfg())

    first = _discover(rpc, FakeConnection())
    assert isinstance(first, RpcError)
    assert upstream.calls == 1

    # A second click inside the cooldown must not hit the upstream again.
    second = _discover(rpc, FakeConnection())
    assert isinstance(second, dict)
    assert second["cached"] is True
    assert upstream.calls == 1


def test_discovery_survives_a_core_restart(tmp_path, monkeypatch):
    """The selected upstream id must remain resolvable after the process restarts."""
    upstream = FakeUpstream(["qwen2.5-vl-72b", "llama-3.3-70b"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, _ = _rpc(tmp_path, _builtin_cfg(), data_dir=tmp_path)

    result = _discover(rpc, FakeConnection())
    assert isinstance(result, dict)

    cache = tmp_path / "models_discovered.json"
    assert cache.is_file()
    assert (cache.stat().st_mode & 0o777) == 0o600

    restarted = ModelResolver(_builtin_cfg(), None, data_dir=tmp_path)
    assert restarted.catalog().get("builtin/qwen2.5-vl-72b") is not None
    assert restarted.discovered_at == result["discovered_at"]
    assert restarted.resolve("builtin/qwen2.5-vl-72b").resolved is True


def test_a_failed_refresh_does_not_overwrite_the_persisted_list(tmp_path, monkeypatch):
    upstream = FakeUpstream(["keep-me"])
    _patch_upstream(monkeypatch, upstream)
    _, rpc, resolver = _rpc(tmp_path, _builtin_cfg(), data_dir=tmp_path)
    _discover(rpc, FakeConnection())
    before = (tmp_path / "models_discovered.json").read_text()

    resolver._last_discovery_at -= 10  # step past the 2 s refresh cooldown
    upstream.error = RuntimeError("503 Service Unavailable")
    failed = _discover(rpc, FakeConnection())
    assert isinstance(failed, RpcError)
    assert (tmp_path / "models_discovered.json").read_text() == before


def test_corrupt_cache_is_ignored_at_startup(tmp_path):
    (tmp_path / "models_discovered.json").write_text("{not json", encoding="utf-8")
    resolver = ModelResolver(_builtin_cfg(), None, data_dir=tmp_path)
    assert resolver.catalog().get("builtin/qwen2.5-vl-72b") is None
    assert resolver.discovered_at is None
