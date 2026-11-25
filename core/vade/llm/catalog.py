"""`model/list` catalog + session-level selection (RFC-0005 §4, §5).

Two responsibilities, both read-only:

* `ModelCatalog` aggregates what the providers declare — `[llm.capabilities]`
  for the builtin, `capabilities.models[]` for a module, a synthesised
  `<module_id>/*` entry for a module that declares nothing. It never infers a
  capability from a model name and never probes a provider: unknown is `null`.
* `ModelResolver` applies the selection order **session > global default >
  manifest.priority** (§5.2) and reports which layer won (`source`), because a
  UI that has to re-derive that is a second source of truth.

It also owns the optional upstream discovery cache (0.0.3 `model/discover`,
persisted in 0.0.4): `discover()` asks the builtin's `/models` and `catalog()`
appends what it found. The cache is replaced only on success, so a failed
probe keeps the last good list instead of blanking the picker. The last good
list is written to `data_dir/models_discovered.json` (0600) and hydrated on
startup; it is a derived, non-authoritative cache, not user data.

`revision` is the registry's monotonic counter: the list is a pure function of
the registry snapshot and the static `[llm]` config, so every mutation that can
change it bumps the counter and no TTL cache is needed (§4.4). Discovery bumps
it too (a successful refresh is a mutation), even when the upstream adds no new
id — a revision that could fall back would strand `model/list?if_revision`.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .capabilities import ModelSpec, normalise_capabilities

__all__ = ["BUILTIN", "MODEL_TOOL", "ModelEntry", "ModelCatalog", "Selection", "ModelResolver"]

#: Provider name of the builtin OpenAI-compatible path.
BUILTIN = "builtin"

#: The one tool that makes a module a model module (M6).
MODEL_TOOL = "chat"

#: `[llm].provider` values that mean "builtin HTTP provider".
_BUILTIN_MODES = {"", "builtin", "openai", "openai_compatible"}

_SOURCES = ("session", "global", "priority", "none")

logger = logging.getLogger(__name__)

#: Persistent derived cache on disk: survives a core restart so a selected
#: `builtin/<upstream-id>` remains selectable until the next refresh. The
#: data dir is already 0700; the file itself is written 0600.
_DISCOVERY_CACHE_VERSION = 2
_DISCOVERY_CACHE_NAME = "models_discovered.json"


@dataclass(frozen=True, slots=True)
class ModelEntry:
    """One row of `model/list` (§4.2)."""

    id: str
    provider: str
    model: str
    display_name: str
    input_modalities: tuple[str, ...] | None = None
    max_image_bytes: int | None = None
    max_image_px: int | None = None
    max_images_per_turn: int | None = None
    context_window: int | None = None
    supports_tools: bool | None = None
    priority: int = 0
    available: bool = True
    #: Internal tie-breaker for `priority` selection; never on the wire.
    last_frame_at: float = 0.0

    def to_wire(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "display_name": self.display_name,
            "input_modalities": list(self.input_modalities) if self.input_modalities is not None else None,
            "max_image_bytes": self.max_image_bytes,
            "max_image_px": self.max_image_px,
            "max_images_per_turn": self.max_images_per_turn,
            "context_window": self.context_window,
            "supports_tools": self.supports_tools,
            "priority": self.priority,
            "available": self.available,
        }

    def effective(self) -> dict[str, Any]:
        """`session/model.get.effective` — identity + availability only."""
        return {"id": self.id, "provider": self.provider, "model": self.model, "available": self.available}


@dataclass(frozen=True, slots=True)
class Selection:
    """One resolution result (§5.1/§5.2)."""

    model_id: str | None
    source: str
    resolved: bool
    effective: ModelEntry | None = None
    global_default: str | None = None
    reason: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "source": self.source,
            "resolved": self.resolved,
            "effective": self.effective.effective() if self.effective is not None else None,
            "global_default": self.global_default,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ModelCatalog:
    """Snapshot-derived model list. Cheap to rebuild; no TTL cache (§4.4)."""

    entries: tuple[ModelEntry, ...] = ()
    revision: int = 1
    mode: str = "builtin"

    # ------------------------------------------------------------------ reads
    def get(self, model_id: str | None) -> ModelEntry | None:
        if not model_id:
            return None
        for entry in self.entries:
            if entry.id == model_id:
                return entry
        return None

    def available(self) -> tuple[ModelEntry, ...]:
        return tuple(e for e in self.entries if e.available)

    def default_entry(self) -> ModelEntry | None:
        """Highest-priority available entry (ties: freshest, then `id`)."""
        candidates = self.available()
        if not candidates:
            return None
        return sorted(candidates, key=lambda e: (-e.priority, -(e.last_frame_at or 0.0), e.id))[0]

    def to_wire(self) -> list[dict[str, Any]]:
        return [entry.to_wire() for entry in self.entries]


def _utc_now() -> str:
    """Second-resolution ISO-8601 UTC, the `discovered_at` spelling (§2)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _mode_of(config: Any) -> str:
    raw = str(config.get("llm.provider", "") or "").strip().lower()
    if raw == "mock":
        return "mock"
    if raw == "module":
        return "module"
    return BUILTIN


def _builtin_entry(config: Any) -> ModelEntry | None:
    model = str(config.get("llm.model", "") or "gpt-4o-mini")
    cap = normalise_capabilities(config.get("llm.capabilities") or {}, where="llm.capabilities")
    return ModelEntry(
        id=f"{BUILTIN}/{model}",
        provider=BUILTIN,
        model=model,
        display_name=cap["display_name"] or model,
        input_modalities=tuple(cap["input_modalities"]) if cap["input_modalities"] else None,
        max_image_bytes=cap["max_image_bytes"],
        max_image_px=cap["max_image_px"],
        max_images_per_turn=cap["max_images_per_turn"],
        context_window=cap["context_window"],
        supports_tools=cap["supports_tools"],
        priority=0,
        available=True,
    )


def _mock_entry() -> ModelEntry:
    return ModelEntry(
        id=f"{BUILTIN}/mock",
        provider=BUILTIN,
        model="mock",
        display_name="mock",
        priority=0,
        available=True,
    )


def _online(record: Any) -> bool:
    health = getattr(getattr(record, "health", None), "value", str(getattr(record, "health", "")))
    return getattr(record, "connection_id", None) is not None and health not in ("offline", "rejected")


def _module_entries(record: Any, module_id: str) -> list[ModelEntry]:
    manifest = getattr(record, "manifest", None)
    if manifest is None or getattr(manifest, "tool", None) is None or manifest.tool(MODEL_TOOL) is None:
        return []
    priority = int(getattr(manifest, "priority", 0) or 0)
    available = _online(record)
    frame = float(getattr(record, "last_frame_at", 0.0) or 0.0)
    specs: tuple[ModelSpec, ...] = tuple(getattr(manifest, "models", ()) or ())
    if not specs:
        # §4.3: a chat module that declares nothing still appears, with every
        # capability `null`, or FR-8.46's switch list would silently hide it.
        return [
            ModelEntry(
                id=f"{module_id}/*",
                provider=module_id,
                model="*",
                display_name=module_id,
                priority=priority,
                available=available,
                last_frame_at=frame,
            )
        ]
    out: list[ModelEntry] = []
    for spec in specs:
        caps = spec.capabilities()
        out.append(
            ModelEntry(
                id=f"{module_id}/{spec.name}",
                provider=module_id,
                model=spec.name,
                display_name=spec.display_name or spec.name,
                input_modalities=tuple(caps["input_modalities"]) if caps["input_modalities"] else None,
                max_image_bytes=caps["max_image_bytes"],
                max_image_px=caps["max_image_px"],
                max_images_per_turn=caps["max_images_per_turn"],
                context_window=caps["context_window"],
                supports_tools=caps["supports_tools"],
                priority=priority,
                available=available,
                last_frame_at=frame,
            )
        )
    return out


def build_catalog(config: Any, registry: Any | None = None) -> ModelCatalog:
    """The candidate set the current mode can actually route to (§4.1)."""
    mode = _mode_of(config)
    revision = 1 + (int(registry.revision()) if registry is not None and hasattr(registry, "revision") else 0)

    if mode == "mock":
        return ModelCatalog(entries=(_mock_entry(),), revision=revision, mode=mode)

    entries: list[ModelEntry] = []
    base_url = str(config.get("llm.base_url", "") or "")
    builtin = _builtin_entry(config) if base_url else None

    if mode == "module":
        wanted = str(config.get("llm.model_module", "") or "")
        merged: dict[tuple[str, str], ModelEntry] = {}
        order: list[tuple[str, str]] = []
        for record in (registry.snapshot() if registry is not None else ()):
            manifest = getattr(record, "manifest", None)
            if manifest is None:
                continue
            module_id = str(manifest.module_id)
            if wanted and module_id != wanted:
                continue
            for entry in _module_entries(record, module_id):
                key = (entry.provider, entry.model)
                previous = merged.get(key)
                if previous is None:
                    merged[key] = entry
                    order.append(key)
                    continue
                # §4.3: several instances of one module must intersect, not
                # union — the route may land on either instance.
                merged[key] = _merge_entries(previous, entry)
        entries = [merged[key] for key in order]
        if not entries and builtin is not None:
            # No module model right now: still list the builtin when the
            # fallback chain would actually use it.
            if bool(config.get("llm.fallback_to_builtin", True)):
                entries = [builtin]
        elif builtin is not None and bool(config.get("llm.fallback_to_builtin", True)):
            entries.append(builtin)
    elif builtin is not None:
        entries = [builtin]

    return ModelCatalog(entries=tuple(entries), revision=revision, mode=mode)


def _merge_entries(a: ModelEntry, b: ModelEntry) -> ModelEntry:
    spec_a = ModelSpec(
        name=a.model,
        display_name=a.display_name,
        input_modalities=a.input_modalities,
        max_image_bytes=a.max_image_bytes,
        max_image_px=a.max_image_px,
        max_images_per_turn=a.max_images_per_turn,
        context_window=a.context_window,
        supports_tools=a.supports_tools,
    )
    spec_b = ModelSpec(
        name=b.model,
        display_name=b.display_name if b.display_name != b.model else None,
        input_modalities=b.input_modalities,
        max_image_bytes=b.max_image_bytes,
        max_image_px=b.max_image_px,
        max_images_per_turn=b.max_images_per_turn,
        context_window=b.context_window,
        supports_tools=b.supports_tools,
    )
    merged = spec_a.merge(spec_b)
    caps = merged.capabilities()
    return ModelEntry(
        id=a.id,
        provider=a.provider,
        model=a.model,
        display_name=merged.display_name or a.model,
        input_modalities=tuple(caps["input_modalities"]) if caps["input_modalities"] else None,
        max_image_bytes=caps["max_image_bytes"],
        max_image_px=caps["max_image_px"],
        max_images_per_turn=caps["max_images_per_turn"],
        context_window=caps["context_window"],
        supports_tools=caps["supports_tools"],
        priority=max(a.priority, b.priority),
        available=a.available or b.available,
        last_frame_at=max(a.last_frame_at, b.last_frame_at),
    )


def _endpoint_fingerprint(config: Any) -> str:
    """Stable digest of the upstream identity; never persist the URL itself."""
    provider = str(config.get("llm.provider", "") or "").strip().lower()
    base_url = str(config.get("llm.base_url", "") or "").strip().rstrip("/")
    return hashlib.sha256(f"{provider}\x00{base_url}".encode("utf-8")).hexdigest()


class ModelResolver:
    """Selection order §5.2, with the layer that won kept explicit."""

    #: Explicit refresh calls closer than this reuse the last answer instead of
    #: hitting the upstream again. A UI "refresh" button should not be a way to
    #: turn one core into a request amplifier against the LLM gateway.
    DISCOVERY_COOLDOWN_S = 2.0

    def __init__(self, config: Any, registry: Any | None = None, data_dir: str | Path | None = None) -> None:
        self.config = config
        self.registry = registry
        self._cache_path = Path(data_dir) / _DISCOVERY_CACHE_NAME if data_dir is not None else None
        self._cache_endpoint = _endpoint_fingerprint(config)
        self._discovered: tuple[ModelEntry, ...] = ()
        self._discovered_revision = 0
        self._last_discovery_at = 0.0
        self._discovered_at: str | None = None
        self._discovery_generation = 0
        self._discovery_lock = asyncio.Lock()
        self._last_discovery_error: str | None = None
        self._load_discovered()

    def _load_discovered(self) -> None:
        """Hydrate the last successful discovery from the data dir.

        Best-effort: a corrupt or unreadable cache must not stop the core from
        starting. The configured model remains the default (priority 0); the
        loaded rows keep priority -1 so a refresh never changes that default.
        """
        path = self._cache_path
        if path is None or not path.is_file():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - startup must survive a bad cache
            logger.warning("ignoring unreadable model discovery cache: %s", exc)
            return
        if not isinstance(payload, dict) or payload.get("version") != _DISCOVERY_CACHE_VERSION:
            logger.info("ignoring model discovery cache with an unknown version")
            return
        if payload.get("endpoint_sha256") != self._cache_endpoint:
            logger.info("ignoring model discovery cache for a different upstream endpoint")
            return
        raw_ids = payload.get("ids")
        if not isinstance(raw_ids, list):
            return
        ids: list[str] = []
        for raw in raw_ids:
            if isinstance(raw, str) and raw.strip() and raw.strip() not in ids:
                ids.append(raw.strip())
        self._discovered = self._entries_for(ids)
        self._discovered_revision = 1
        self._discovery_generation = 1
        discovered_at = payload.get("discovered_at")
        self._discovered_at = discovered_at if isinstance(discovered_at, str) else None

    @staticmethod
    def _entries_for(ids: list[str]) -> tuple[ModelEntry, ...]:
        return tuple(
            ModelEntry(
                id=f"{BUILTIN}/{model_id}",
                provider=BUILTIN,
                model=model_id,
                display_name=model_id,
                # A refresh must never silently change the default: the
                # configured `builtin/<llm.model>` entry stays ahead of every
                # discovered one. With equal priority, selection falls back to
                # lexical id and picks e.g. `claude-*` merely because it sorts
                # before `gpt-*`.
                priority=-1,
                available=True,
            )
            for model_id in ids
        )

    def _persist_discovered(self) -> None:
        """Atomically write the last good list; discovery is still a success if
        the write fails, but the restart gap then remains (logged, not silent)."""
        path = self._cache_path
        if path is None:
            return
        payload = {
            "version": _DISCOVERY_CACHE_VERSION,
            "endpoint_sha256": self._cache_endpoint,
            "discovered_at": self._discovered_at,
            "ids": [entry.model for entry in self._discovered],
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
            os.chmod(path, 0o600)
        except Exception as exc:  # noqa: BLE001 - cache persistence is best effort
            logger.warning("could not persist model discovery cache: %s", exc)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    def catalog(self) -> ModelCatalog:
        base = build_catalog(self.config, self.registry)
        if not self._discovery_generation or not self.can_discover():
            return base
        configured = {entry.id for entry in base.entries}
        extra = tuple(entry for entry in self._discovered if entry.id not in configured)
        # The revision is bumped even when every discovered id is already
        # configured (or the upstream returned none): `model/discover` promises
        # "a successful refresh moves `revision`", and keying the merge on
        # `extra` instead of on "did a discovery ever succeed" would make the
        # counter fall back to `base.revision` — i.e. go BACKWARDS.
        return ModelCatalog(
            entries=base.entries + extra,
            revision=base.revision + self._discovered_revision,
            mode=base.mode,
        )

    # ------------------------------------------------------------------ discovery
    def can_discover(self) -> bool:
        """Only the builtin HTTP path has a `/models` endpoint we own."""
        return (
            str(self.config.get("llm.provider", "") or "").strip().lower() in _BUILTIN_MODES
            and bool(str(self.config.get("llm.base_url", "") or "").strip())
        )

    async def discover(self) -> ModelCatalog:
        """Ask the configured upstream for `/models` and merge the ids.

        The cache is persisted to `data_dir/models_discovered.json` (0600) and
        replaced only on success: a timed-out or 401 upstream keeps the last
        good list visible instead of blanking the picker. Nothing is inferred about capabilities — every discovered entry
        keeps `None` for context window / modalities / tool support.
        """
        if not self.can_discover():
            raise RuntimeError("model discovery needs a builtin provider with llm.base_url")
        async with self._discovery_lock:
            now = time.monotonic()
            if self._in_cooldown(now):
                return self.catalog()
            from .openai_compat import OpenAICompatProvider

            provider = OpenAICompatProvider(
                base_url=str(self.config.get("llm.base_url", "")),
                model=str(self.config.get("llm.model", "") or "gpt-4o-mini"),
                api_key_env=str(self.config.get("llm.api_key_env", "") or "VADE_LLM_API_KEY"),
                timeout_s=min(float(self.config.get("llm.timeout_s", 120) or 120), 10.0),
            )
            try:
                ids = await provider.list_models()
            except Exception as exc:  # noqa: BLE001 - keep the last good list, report the cause
                # Arm the cooldown on failure too: the refresh button is most
                # likely to be hammered exactly when the upstream is down.
                self._last_discovery_at = now
                self._last_discovery_error = f"{type(exc).__name__}: {exc}"[:300]
                raise
            self._last_discovery_error = None
            self._discovered = self._entries_for(ids)
            self._discovered_revision += 1
            self._discovery_generation += 1
            self._last_discovery_at = now
            self._discovered_at = _utc_now()
            self._persist_discovered()
            return self.catalog()

    def _in_cooldown(self, now: float | None = None) -> bool:
        """True when a refresh would reuse the cache instead of the upstream.

        The timestamp is armed by failures too, so a down gateway cannot be
        hammered by repeated refresh clicks.
        """
        return self._last_discovery_at > 0 and (
            (time.monotonic() if now is None else now) - self._last_discovery_at
        ) < self.DISCOVERY_COOLDOWN_S

    @property
    def discovery_generation(self) -> int:
        """How many times the cache was actually REPLACED (successes only).

        `model/discover` compares it before/after the call to report `cached`:
        a wait behind the lock that then finds the cache fresh is still a cache
        hit, and only this counter can tell the two apart.
        """
        return self._discovery_generation

    @property
    def discovered_at(self) -> str | None:
        """ISO-8601 UTC of the last SUCCESSFUL discovery, or None."""
        return self._discovered_at

    @property
    def last_discovery_error(self) -> str | None:
        return self._last_discovery_error

    # ------------------------------------------------------------------ resolve
    def resolve(self, session_model_id: str | None, catalog: ModelCatalog | None = None) -> Selection:
        catalog = catalog or self.catalog()
        global_default = str(self.config.get("llm.default_model", "") or "").strip() or None

        # 1. Explicit session choice: never silently replaced (§5.2 row 1).
        if session_model_id:
            entry = catalog.get(session_model_id)
            if entry is None or not entry.available:
                return Selection(
                    model_id=session_model_id,
                    source="session",
                    resolved=False,
                    effective=None,
                    global_default=global_default,
                    reason="model_unavailable",
                )
            return Selection(session_model_id, "session", True, entry, global_default, None)

        # 2. Operator default: a module target may fall back to the builtin.
        if global_default:
            entry = catalog.get(global_default)
            if entry is not None and entry.available:
                return Selection(global_default, "global", True, entry, global_default, None)
            if bool(self.config.get("llm.fallback_to_builtin", True)):
                builtin = self._builtin(catalog)
                if builtin is not None:
                    return Selection(builtin.id, "global", True, builtin, global_default, None)
            return Selection(
                model_id=global_default,
                source="global",
                resolved=False,
                effective=None,
                global_default=global_default,
                reason="model_unavailable",
            )

        # 3. Highest priority, freshest heartbeat, then `id` (§5.2 row 3).
        entry = catalog.default_entry()
        if entry is not None:
            return Selection(entry.id, "priority", True, entry, None, None)
        return Selection(None, "none", False, None, None, "no_model_configured")

    @staticmethod
    def _builtin(catalog: ModelCatalog) -> ModelEntry | None:
        for entry in catalog.entries:
            if entry.provider == BUILTIN and entry.available:
                return entry
        return None
