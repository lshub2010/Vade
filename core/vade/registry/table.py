"""Instance table + capability index + conflict/revocation rules (C2 T4).

Invariants:
  * Internal state is immutable (`NodeRecord` is frozen); updates swap records,
    they never mutate a shared dict in place.
  * The capability index is rebuilt and swapped as a whole, so a failed
    `capabilities.update` can never leave a half-applied tool table.
  * `module_id/tool` is globally unique AND the bare tool name is globally
    unique (design §2.3): the later registrant loses, never the earlier one.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping

from .health import HealthState, compute_health
from .manifest import MAX_TOOLS_GLOBAL, Manifest, ManifestError, ToolSpec
from ..errors import VadeError

__all__ = ["Registry", "NodeRecord", "ToolBinding", "Registration", "MAX_TOOLS_GLOBAL"]

_RISK_RANK = {"low": 0, "medium": 1, "high": 2}

#: Tools whose effective risk can never be lowered below `high`, whatever the
#: manifest or [policy.tool_risk] says (design §7.9.1).
_HIGH_FLOOR = {
    "shell",
    "shell_exec",
    "exec",
    "write",
    "write_file",
    "delete",
    "delete_file",
    "forget",
}


def _risk_floor(tool_name: str) -> str:
    tail = tool_name.rsplit(".", 1)[-1].rsplit("_", 1)[-1]
    if tool_name in _HIGH_FLOOR or tail in {"write", "delete", "forget", "exec"}:
        return "high"
    return "low"


@dataclass(frozen=True, slots=True)
class ToolBinding:
    tool_id: str  # "module_id/tool"
    module_id: str
    instance_id: str
    spec: ToolSpec
    effective_risk: str


@dataclass(frozen=True, slots=True)
class NodeRecord:
    module_id: str
    instance_id: str
    fingerprint: str
    kind: str  # "module" | "device"
    auth_mode: str  # "module_key" | "device_key" | "browser_session"
    role: str
    #: `None` only for a rejected node that was never registered (the topology
    #: still draws it, so every accessor must tolerate the empty case).
    manifest: Manifest | None
    enabled: bool = True
    health: HealthState = HealthState.PENDING
    connection_id: str | None = None
    endpoint: str | None = None
    rtt_ms: float | None = None
    connected_at: float | None = None
    last_frame_at: float | None = None
    circuit_open: bool = False
    timeout_rate: float = 0.0
    rejected_reason: str | None = None

    @property
    def node_id(self) -> str:
        return f"{self.module_id}#{self.instance_id}"

    @property
    def tool_count(self) -> int:
        return len(self.manifest.tools) if self.manifest is not None else 0


@dataclass(frozen=True, slots=True)
class Registration:
    accepted: bool
    session_id: str
    tool_names: tuple[str, ...]


class Registry:
    """Thread-safe (single lock) registry. Reads return copies of frozen data."""

    def __init__(
        self,
        *,
        max_global_tools: int = MAX_TOOLS_GLOBAL,
        heartbeat_interval_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        close_connection: Callable[[str], None] | None = None,
    ) -> None:
        self._lock = threading.RLock()
        self._records: dict[tuple[str, str], NodeRecord] = {}
        self._index: dict[str, ToolBinding] = {}
        self._cap_epoch: dict[str, int] = {}
        self._max_global_tools = max_global_tools
        self._heartbeat_interval = heartbeat_interval_s
        self._clock = clock
        self._close_connection = close_connection
        self._revoked: set[str] = set()
        self._risk_overrides: dict[tuple[str, str], str] = {}

    # ------------------------------------------------------------------ register
    def register(
        self,
        manifest: Manifest,
        *,
        fingerprint: str,
        connection_id: str,
        kind: str = "module",
        auth_mode: str = "module_key",
        endpoint: str | None = None,
    ) -> Registration:
        with self._lock:
            if fingerprint in self._revoked:
                raise VadeError("PERMISSION_DENIED", "fingerprint revoked", fatal=True)

            key = (manifest.module_id, manifest.instance_id)
            existing = self._records.get(key)
            if existing is not None and existing.fingerprint != fingerprint:
                # A different key claiming an existing instance slot. Admin must
                # revoke first; otherwise one code could displace a trusted node.
                raise VadeError(
                    "PERMISSION_DENIED",
                    f"instance {manifest.module_id}#{manifest.instance_id} belongs to another fingerprint",
                    fatal=True,
                )

            self._check_concurrency(manifest, existing)
            new_index = self._build_index(manifest, exclude_instance=manifest.instance_id if existing else None)

            now = self._clock()
            record = NodeRecord(
                module_id=manifest.module_id,
                instance_id=manifest.instance_id,
                fingerprint=fingerprint,
                kind=kind,
                auth_mode=auth_mode,
                role=manifest.role,
                manifest=manifest,
                enabled=True,
                health=HealthState.ONLINE,
                connection_id=connection_id,
                endpoint=endpoint,
                connected_at=now,
                last_frame_at=now,
            )
            self._records[key] = record
            self._index = new_index
            self._cap_epoch[manifest.module_id] = self._cap_epoch.get(manifest.module_id, 0) + (
                0 if existing else 1
            )
            return Registration(accepted=True, session_id=f"{manifest.module_id}#{manifest.instance_id}", tool_names=manifest.tool_names)

    def _check_concurrency(self, manifest: Manifest, existing: NodeRecord | None) -> None:
        siblings = [r for r in self._records.values() if r.module_id == manifest.module_id and r.instance_id != manifest.instance_id]
        if manifest.concurrency == "exclusive" and siblings:
            raise VadeError(
                "TOOL_CONFLICT",
                f"{manifest.module_id} is exclusive; instance {manifest.instance_id} cannot be added "
                f"(already {siblings[0].instance_id})",
            )
        if manifest.concurrency in {"pooled", "mirrored"} and existing is None:
            if len(siblings) >= manifest.limits.max_concurrency:
                raise VadeError(
                    "TOOL_CONFLICT",
                    f"{manifest.module_id} already has {len(siblings)} instances, "
                    f"max_concurrency={manifest.limits.max_concurrency}",
                )

    def _build_index(self, manifest: Manifest, *, exclude_instance: str | None) -> dict[str, ToolBinding]:
        """Build the next capability index keyed by `module_id/tool@instance_id`.

        Pooled instances each get their own entry (three instances -> three
        bindings for the same tool), so the router can pick the least-inflight
        one. A tool name held by a DIFFERENT module is a conflict. Disabled
        instances contribute nothing: `set_enabled(False)` must stop routing
        without touching the other instances of the same module.
        """
        index: dict[str, ToolBinding] = {}
        for (module_id, instance_id), record in self._records.items():
            if module_id == manifest.module_id and instance_id == exclude_instance:
                continue
            if not record.enabled or record.manifest is None:
                continue
            for spec in record.manifest.tools:
                index[f"{module_id}/{spec.name}@{instance_id}"] = ToolBinding(
                    tool_id=f"{module_id}/{spec.name}",
                    module_id=record.module_id,
                    instance_id=record.instance_id,
                    spec=spec,
                    effective_risk=self._effective_risk(record.module_id, spec),
                )

        for pos, spec in enumerate(manifest.tools):
            foreign = {b.module_id for b in index.values() if b.spec.name == spec.name} - {manifest.module_id}
            if foreign:
                raise VadeError(
                    "TOOL_CONFLICT",
                    f"/capabilities/tools/{pos}/name: tool {spec.name!r} already held by {sorted(foreign)[0]}",
                )
            index[f"{manifest.module_id}/{spec.name}@{manifest.instance_id}"] = ToolBinding(
                tool_id=f"{manifest.module_id}/{spec.name}",
                module_id=manifest.module_id,
                instance_id=manifest.instance_id,
                spec=spec,
                effective_risk=self._effective_risk(manifest.module_id, spec),
            )

        distinct_ids = {binding.tool_id for binding in index.values()}
        if len(distinct_ids) > self._max_global_tools:
            raise VadeError(
                "TOOL_CONFLICT",
                f"global tool budget exhausted: {len(distinct_ids)} distinct tools > {self._max_global_tools}",
            )
        return index

    def _rebuild_index(self) -> dict[str, ToolBinding]:
        """Index the current table as-is (skips disabled / manifest-less nodes)."""
        index: dict[str, ToolBinding] = {}
        for (module_id, instance_id), record in self._records.items():
            if not record.enabled or record.manifest is None:
                continue
            for spec in record.manifest.tools:
                index[f"{module_id}/{spec.name}@{instance_id}"] = ToolBinding(
                    tool_id=f"{module_id}/{spec.name}",
                    module_id=module_id,
                    instance_id=instance_id,
                    spec=spec,
                    effective_risk=self._effective_risk(module_id, spec),
                )
        return index

    def _effective_risk(self, module_id: str, spec: ToolSpec) -> str:
        # Without a [policy.tool_risk] table injected, undeclared = high is the
        # safe default; when a table is installed it may only raise.
        override = self._risk_overrides.get((module_id, spec.name))
        floor = _risk_floor(spec.name)
        rank = max(_RISK_RANK[spec.risk], _RISK_RANK[floor])
        if override is not None:
            rank = max(rank, _RISK_RANK[override])
        return ("low", "medium", "high")[rank]

    # ------------------------------------------------------------------ update
    def update_capabilities(self, manifest: Manifest, *, fingerprint: str) -> int:
        with self._lock:
            key = (manifest.module_id, manifest.instance_id)
            record = self._records.get(key)
            if record is None:
                raise VadeError("NOT_FOUND", f"{manifest.module_id}#{manifest.instance_id} is not registered")
            if record.fingerprint != fingerprint:
                raise VadeError("PERMISSION_DENIED", "capabilities.update from a different fingerprint", fatal=True)
            if not record.enabled:
                raise VadeError("MODULE_DRAINING", "module is disabled; capabilities.update rejected")

            old = record.manifest
            for pos, spec in enumerate(manifest.tools):
                previous = old.tool(spec.name)
                if previous is None:
                    continue
                binding = self._index.get(f"{manifest.module_id}/{spec.name}@{manifest.instance_id}")
                current = binding.effective_risk if binding else previous.risk
                if _RISK_RANK[spec.risk] < _RISK_RANK[current]:
                    raise VadeError(
                        "RISK_DOWNGRADE_FORBIDDEN",
                        f"/capabilities/tools/{pos}/risk: {spec.name} cannot drop from {current} to {spec.risk}",
                    )

            new_index = self._build_index(manifest, exclude_instance=manifest.instance_id)
            self._records[key] = replace(record, manifest=manifest)
            self._index = new_index
            self._cap_epoch[manifest.module_id] = self._cap_epoch.get(manifest.module_id, 0) + 1
            return self._cap_epoch[manifest.module_id]

    # ------------------------------------------------------------------ queries
    def cap_epoch(self, module_id: str) -> int:
        with self._lock:
            return self._cap_epoch.get(module_id, 0)

    def get(self, module_id: str, instance_id: str) -> NodeRecord | None:
        with self._lock:
            return self._records.get((module_id, instance_id))

    def by_module(self, module_id: str) -> tuple[NodeRecord, ...]:
        with self._lock:
            return tuple(r for r in self._records.values() if r.module_id == module_id)

    def tools(self) -> Mapping[str, ToolBinding]:
        """All live bindings, keyed `module_id/tool@instance_id`."""
        with self._lock:
            return dict(self._index)

    def resolve_tool(self, name: str) -> ToolBinding | None:
        """First enabled binding for a bare tool name (C3 picks by inflight)."""
        with self._lock:
            for key, binding in self._index.items():
                if binding.spec.name == name:
                    record = self._records.get((binding.module_id, binding.instance_id))
                    if record is not None and record.enabled:
                        return binding
            return None

    def bindings_for(self, name: str) -> tuple[ToolBinding, ...]:
        with self._lock:
            return tuple(b for b in self._index.values() if b.spec.name == name)

    def tool_count(self) -> int:
        with self._lock:
            return len({b.tool_id for b in self._index.values()})

    def snapshot(self) -> tuple[NodeRecord, ...]:
        with self._lock:
            return tuple(sorted(self._records.values(), key=lambda r: (r.module_id, r.instance_id)))

    # ------------------------------------------------------------------ health
    def touch(self, module_id: str, instance_id: str, *, rtt_ms: float | None = None) -> None:
        with self._lock:
            key = (module_id, instance_id)
            record = self._records.get(key)
            if record is None:
                return
            self._records[key] = replace(record, last_frame_at=self._clock(), rtt_ms=rtt_ms or record.rtt_ms, health=HealthState.ONLINE)

    def mark_disconnected(self, module_id: str, instance_id: str) -> None:
        with self._lock:
            key = (module_id, instance_id)
            record = self._records.get(key)
            if record is None:
                return
            # Record is retained so the topology can still draw a grey dotted line.
            self._records[key] = replace(record, connection_id=None, health=HealthState.OFFLINE)

    def mark_rejected(self, module_id: str, instance_id: str, reason: str, *, fingerprint: str = "") -> NodeRecord:
        with self._lock:
            key = (module_id, instance_id)
            existing = self._records.get(key)
            manifest = existing.manifest if existing else None
            record = NodeRecord(
                module_id=module_id,
                instance_id=instance_id,
                fingerprint=fingerprint or (existing.fingerprint if existing else ""),
                kind=existing.kind if existing else "module",
                auth_mode=existing.auth_mode if existing else "module_key",
                role=existing.role if existing else "tool",
                manifest=manifest,  # type: ignore[arg-type]
                health=HealthState.REJECTED,
                rejected_reason=reason,
            )
            self._records[key] = record
            return record

    def refresh_health(self) -> None:
        now = self._clock()
        with self._lock:
            for key, record in list(self._records.items()):
                if record.connection_id is None:
                    state = HealthState.OFFLINE
                else:
                    state = compute_health(
                        connected=True,
                        last_frame_age_s=None if record.last_frame_at is None else now - record.last_frame_at,
                        heartbeat_interval_s=self._heartbeat_interval,
                        circuit_open=record.circuit_open,
                        timeout_rate=record.timeout_rate,
                    )
                if state != record.health:
                    self._records[key] = replace(record, health=state)

    def set_circuit(self, module_id: str, instance_id: str, open_: bool, timeout_rate: float = 0.0) -> None:
        with self._lock:
            key = (module_id, instance_id)
            record = self._records.get(key)
            if record is None:
                return
            self._records[key] = replace(record, circuit_open=open_, timeout_rate=timeout_rate)

    # ------------------------------------------------------------------ governance
    def set_enabled(self, module_id: str, instance_id: str, enabled: bool) -> None:
        with self._lock:
            key = (module_id, instance_id)
            record = self._records.get(key)
            if record is None:
                raise VadeError("NOT_FOUND", f"{module_id}#{instance_id} is not registered")
            self._records[key] = replace(record, enabled=enabled)
            if not enabled:
                self._records[key] = replace(self._records[key], health=HealthState.OFFLINE, connection_id=None)
                # Re-index the whole table: filtering by module_id also removed
                # the OTHER instances of a pooled module.
                self._index = self._rebuild_index()
        if not enabled and record.connection_id and self._close_connection:
            self._close_connection(record.connection_id)

    def disconnect(self, module_id: str, instance_id: str) -> None:
        record = self.get(module_id, instance_id)
        self.mark_disconnected(module_id, instance_id)
        if record and record.connection_id and self._close_connection:
            self._close_connection(record.connection_id)

    def revoke_fingerprint(self, fingerprint: str) -> tuple[str, ...]:
        """Revoke a key: closes live connections NOW, not just future handshakes."""
        with self._lock:
            self._revoked.add(fingerprint)
            closed: list[str] = []
            for key, record in list(self._records.items()):
                if record.fingerprint == fingerprint:
                    if record.connection_id:
                        closed.append(record.connection_id)
                    self._records[key] = replace(
                        record, connection_id=None, health=HealthState.REJECTED, rejected_reason="fingerprint revoked"
                    )
        for connection_id in closed:
            if self._close_connection:
                self._close_connection(connection_id)
        return tuple(closed)

    def is_revoked(self, fingerprint: str) -> bool:
        with self._lock:
            return fingerprint in self._revoked

    def set_risk_override(self, module_id: str, tool: str, risk: str) -> None:
        if risk not in _RISK_RANK:
            raise VadeError("INVALID_ARGUMENT", f"risk must be low|medium|high, got {risk!r}")
        with self._lock:
            bindings = [b for b in self._index.values() if b.module_id == module_id and b.spec.name == tool]
            if not bindings:
                raise VadeError("NOT_FOUND", f"{module_id}/{tool} is not registered")
            current = bindings[0].effective_risk
            if _RISK_RANK[risk] < _RISK_RANK[current]:
                raise VadeError(
                    "RISK_DOWNGRADE_FORBIDDEN",
                    f"{module_id}/{tool} effective risk is {current}; override to {risk} would lower it "
                    "(change core.toml and restart instead)",
                )
            self._risk_overrides[(module_id, tool)] = risk
            # Recompute the visible binding so the raised risk takes effect now.
            self._index = {
                name: (
                    ToolBinding(
                        tool_id=b.tool_id,
                        module_id=b.module_id,
                        instance_id=b.instance_id,
                        spec=b.spec,
                        effective_risk=self._effective_risk(b.module_id, b.spec),
                    )
                    if b.module_id == module_id
                    else b
                )
                for name, b in self._index.items()
            }


def install(app: Any) -> None:
    """Component entry point (contract: package root exposes install(app))."""
    if getattr(app, "registry", None) is None:
        app.registry = Registry(heartbeat_interval_s=getattr(app, "heartbeat_interval_s", 10.0))
