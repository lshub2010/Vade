"""Manifest validation — the ONE implementation (C-03).

Order is fixed and stops at the first error, mirroring 21-C2 T4:

    1. JSON Schema (schema/manifest.schema.json)      -> pointer to the field
    2. protocol major == 1                            -> both versions in message
    3. tool name regex/length, description non-empty  -> value + rule
    4. input_schema.type == "object"                  -> actual type
    5. risk enum, timeout_ms in [100, 600000]         -> actual value
    6. tool counts (<=64 here; global 128 in table.py) -> conflicting id/owner

Cross-module uniqueness lives in registry/table.py because only the table
knows the other holders. This module never mutates its input: the result is a
frozen dataclass (no shared mutable dicts as internal state).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .._jsonschema import SchemaError, validate as js_validate
from ..errors import VadeError

__all__ = [
    "Manifest",
    "ManifestError",
    "ToolSpec",
    "Limits",
    "load_schema",
    "validate_manifest",
    "TOOL_NAME_RE",
    "MAX_TOOLS_PER_MODULE",
    "MAX_TOOLS_GLOBAL",
    "MAX_FRAME_PLAINTEXT",
]

SCHEMA_PATH = Path(__file__).resolve().parents[3] / "schema" / "manifest.schema.json"

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
MAX_TOOLS_PER_MODULE = 64
MAX_TOOLS_GLOBAL = 128
MAX_DESCRIPTION_LEN = 512
MAX_FRAME_PLAINTEXT = 65519  # C-01 §7.5: Noise MAXMSGLEN 65535 - 16B tag
RISKS = ("low", "medium", "high")
TIMEOUT_MS_MIN = 100
TIMEOUT_MS_MAX = 600_000

_SCHEMA_CACHE: dict[str, Any] | None = None


class ManifestError(VadeError):
    """MANIFEST_INVALID with a JSON Pointer at the offending field."""

    def __init__(self, pointer: str, message: str, *, code: str = "MANIFEST_INVALID") -> None:
        self.pointer = pointer
        super().__init__(code, f"{pointer or '/'}: {message}")


def load_schema(path: Path | str | None = None) -> dict[str, Any]:
    global _SCHEMA_CACHE
    if path is None and _SCHEMA_CACHE is not None:
        return _SCHEMA_CACHE
    target = Path(path) if path is not None else SCHEMA_PATH
    data = json.loads(target.read_text(encoding="utf-8"))
    if path is None:
        _SCHEMA_CACHE = data
    return data


@dataclass(frozen=True, slots=True)
class ToolSpec:
    name: str
    description: str
    input_schema: Mapping[str, Any]
    risk: str
    timeout_ms: int
    idempotent: bool

    @property
    def declared_risk(self) -> str:
        return self.risk


@dataclass(frozen=True, slots=True)
class Limits:
    max_concurrency: int
    max_payload: int


@dataclass(frozen=True, slots=True)
class Manifest:
    module_id: str
    instance_id: str
    version: str
    protocol: str
    role: str
    concurrency: str
    priority: int
    tools: tuple[ToolSpec, ...]
    limits: Limits
    prompts: tuple[Mapping[str, Any], ...]
    events: tuple[str, ...]
    injects: tuple[str, ...]
    digest: str
    #: C-14 append (RFC-0001). Trailing default so every existing construction
    #: still type-checks and a manifest that predates the field keeps working.
    subscriptions: tuple[str, ...] = ()

    @property
    def protocol_major(self) -> int:
        match = re.match(r"^vade/v([0-9]+)", self.protocol)
        return int(match.group(1)) if match else -1

    @property
    def tool_names(self) -> tuple[str, ...]:
        return tuple(t.name for t in self.tools)

    def tool(self, name: str) -> ToolSpec | None:
        for spec in self.tools:
            if spec.name == name:
                return spec
        return None


def validate_manifest(raw: Any, *, schema: Mapping[str, Any] | None = None) -> Manifest:
    """Validate one manifest. Raises ManifestError on the first violation."""
    if not isinstance(raw, Mapping):
        raise ManifestError("", f"manifest must be an object, got {type(raw).__name__}")

    # 1. JSON Schema (structure, required fields, types, per-list caps).
    try:
        js_validate(raw, schema if schema is not None else load_schema())
    except SchemaError as exc:
        raise ManifestError(exc.pointer, exc.message) from None

    # 2. Protocol major version.
    protocol = str(raw["protocol"])
    major = re.match(r"^vade/v([0-9]+)", protocol)
    if major is None or int(major.group(1)) != 1:
        raise ManifestError(
            "/protocol",
            f"unsupported protocol {protocol!r}; core supports vade/v1 (major 1 only, no downgrade)",
            code="MANIFEST_INVALID",
        )
    if protocol != "vade/v1":
        # Minor/patch mismatches are accepted with a warning (01-contracts §六).
        pass

    # 3-5. Per-tool rules that JSON Schema either cannot express or must report
    # with the actual value (so the module author can fix it without guessing).
    tools_raw = raw["capabilities"]["tools"]
    tools: list[ToolSpec] = []
    for index, item in enumerate(tools_raw):
        base = f"/capabilities/tools/{index}"

        name = item["name"]
        if TOOL_NAME_RE.match(name) is None:
            raise ManifestError(
                f"{base}/name",
                f"tool name {name!r} violates ^[a-z][a-z0-9_]{{0,63}}$ (lowercase snake_case, <=64 chars)",
            )

        description = item["description"]
        if not description.strip():
            raise ManifestError(f"{base}/description", "description must not be blank (the model cannot pick an undescribed tool)")
        if len(description) > MAX_DESCRIPTION_LEN:
            raise ManifestError(
                f"{base}/description",
                f"description length {len(description)} > {MAX_DESCRIPTION_LEN}",
            )

        input_schema = item["input_schema"]
        if not isinstance(input_schema, Mapping):
            raise ManifestError(f"{base}/input_schema", f"must be an object, got {type(input_schema).__name__}")
        actual_type = input_schema.get("type")
        if actual_type != "object":
            raise ManifestError(
                f"{base}/input_schema/type",
                f"input_schema.type must be \"object\", got {actual_type!r}",
            )
        # Structural sanity of the nested schema: unknown keywords are fine
        # (forward compatible), but properties/required must have sane types.
        props = input_schema.get("properties", {})
        if not isinstance(props, Mapping):
            raise ManifestError(f"{base}/input_schema/properties", f"must be an object, got {type(props).__name__}")
        required = input_schema.get("required", [])
        if not isinstance(required, list) or any(not isinstance(x, str) for x in required):
            raise ManifestError(f"{base}/input_schema/required", "must be an array of strings")
        unknown_required = [x for x in required if x not in props]
        if unknown_required:
            raise ManifestError(
                f"{base}/input_schema/required",
                f"required names not declared in properties: {unknown_required}",
            )

        risk = item["risk"]
        if risk not in RISKS:
            raise ManifestError(f"{base}/risk", f"risk must be one of {list(RISKS)}, got {risk!r}")

        timeout_ms = item["timeout_ms"]
        if not (TIMEOUT_MS_MIN <= timeout_ms <= TIMEOUT_MS_MAX):
            raise ManifestError(
                f"{base}/timeout_ms",
                f"timeout_ms must be within [{TIMEOUT_MS_MIN}, {TIMEOUT_MS_MAX}], got {timeout_ms}",
            )

        tools.append(
            ToolSpec(
                name=name,
                description=description,
                input_schema=dict(input_schema),
                risk=risk,
                timeout_ms=int(timeout_ms),
                idempotent=bool(item["idempotent"]),
            )
        )

    # 6a. Per-module tool budget (schema's maxItems already catches >64, but a
    # duplicate name inside one manifest is a separate error).
    if len(tools) > MAX_TOOLS_PER_MODULE:
        raise ManifestError(
            "/capabilities/tools",
            f"{len(tools)} tools > per-module limit {MAX_TOOLS_PER_MODULE}",
        )
    seen: dict[str, int] = {}
    for index, spec in enumerate(tools):
        if spec.name in seen:
            raise ManifestError(
                f"/capabilities/tools/{index}/name",
                f"duplicate tool name {spec.name!r} in this manifest (first at index {seen[spec.name]})",
            )
        seen[spec.name] = index

    limits_raw = raw["limits"]
    limits = Limits(
        max_concurrency=int(limits_raw["max_concurrency"]),
        max_payload=int(limits_raw["max_payload"]),
    )
    if limits.max_payload > MAX_FRAME_PLAINTEXT:
        # The schema caps it too; keeping the explicit branch makes the reason
        # readable instead of "value > maximum 65519".
        raise ManifestError(
            "/limits/max_payload",
            f"max_payload {limits.max_payload} exceeds the single-frame plaintext ceiling {MAX_FRAME_PLAINTEXT}",
        )

    concurrency = raw.get("concurrency", "exclusive")
    if concurrency == "exclusive" and limits.max_concurrency != 1:
        # Not fatal: exclusive simply means one instance. Warn only.
        pass

    prompts_raw = raw["capabilities"].get("prompts", [])
    events_raw = raw["capabilities"].get("events", [])
    injects_raw = raw.get("injects", [])
    # C-14 append (RFC-0001): shape only. Topic names are validated at
    # `events/subscribe`, where an unknown or ungranted topic must become a
    # per-topic `rejected` row, not a manifest error that kills the module.
    subscriptions_raw = raw.get("subscriptions") or []
    if not isinstance(subscriptions_raw, list):
        raise ManifestError(
            "/subscriptions", f"must be an array of strings, got {type(subscriptions_raw).__name__}"
        )
    subscriptions: list[str] = []
    for index, topic in enumerate(subscriptions_raw):
        if not isinstance(topic, str):
            raise ManifestError(f"/subscriptions/{index}", f"must be a string, got {type(topic).__name__}")
        if topic not in subscriptions:  # preserve order, drop repeats
            subscriptions.append(topic)
    for index, inject in enumerate(injects_raw):
        if inject != "session_id":
            raise ManifestError(
                f"/injects/{index}",
                f"unknown injection source {inject!r}; v1 implements only 'session_id'",
            )

    digest = hashlib.sha256(
        json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()

    return Manifest(
        module_id=str(raw["module_id"]),
        instance_id=str(raw["instance_id"]),
        version=str(raw["version"]),
        protocol=protocol,
        role=str(raw.get("role", "tool")),
        concurrency=str(concurrency),
        priority=int(raw.get("priority", 0)),
        tools=tuple(tools),
        limits=limits,
        prompts=tuple(dict(p) for p in prompts_raw),
        events=tuple(str(e) for e in events_raw),
        injects=tuple(str(i) for i in injects_raw),
        digest=digest,
        subscriptions=tuple(subscriptions),
    )
