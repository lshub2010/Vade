"""ToolSpec and the signature -> JSON Schema mapping (C-07).

The function signature IS the schema. Supported annotations:

    str -> string        int -> integer      float -> number
    bool -> boolean      list -> array       dict -> object
    list[T] / dict[str, T] -> typed items/additionalProperties
    Optional[T] / T | None -> T, not required

Anything else (`bytes`, `Any`, `tuple`, `set`, `Union[A, B]`, a missing
annotation) raises at construction time. Silently emitting `{}` would produce a
manifest that looks fine and gives the model no argument names — the exact
failure mode this check exists to prevent.
"""

from __future__ import annotations

import inspect
import re
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

__all__ = ["ToolSpec", "schema_for_function", "validate_args", "TOOL_NAME_RE", "RISKS"]

TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RISKS = ("low", "medium", "high")
MAX_DESCRIPTION_LEN = 512
TIMEOUT_MS_MIN = 100
TIMEOUT_MS_MAX = 600_000
DEFAULT_TIMEOUT_MS = 30_000
MAX_TOOLS = 64

_NONE_TYPE = type(None)
_UNION_ORIGINS = {typing.Union, types.UnionType}


class SchemaError(ValueError):
    """A signature that cannot be expressed as a JSON Schema."""


def _scalar(annotation: Any) -> dict[str, Any] | None:
    if annotation is str:
        return {"type": "string"}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation is dict or annotation is typing.Dict:
        return {"type": "object"}
    if annotation is list or annotation is typing.List:
        return {"type": "array"}
    return None


def _schema_for(annotation: Any, *, where: str) -> tuple[dict[str, Any], bool]:
    """Return (schema, optional). Raises SchemaError for unsupported types."""
    if annotation is inspect.Parameter.empty:
        raise SchemaError(f"{where}: missing type annotation — the signature is the schema, so every parameter must be annotated")

    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)

    if origin in _UNION_ORIGINS:
        non_none = [a for a in args if a is not _NONE_TYPE]
        if len(non_none) != 1 or len(non_none) == len(args):
            raise SchemaError(
                f"{where}: only Optional[T] / T | None is supported, got {annotation!r}"
            )
        inner, _ = _schema_for(non_none[0], where=where)
        return inner, True

    scalar = _scalar(annotation)
    if scalar is not None:
        return scalar, False

    if origin is list:
        if not args:
            return {"type": "array"}, False
        if len(args) != 1:
            raise SchemaError(f"{where}: list[...] takes exactly one item type, got {annotation!r}")
        item, _ = _schema_for(args[0], where=f"{where}[]")
        return {"type": "array", "items": item}, False

    if origin is dict:
        if not args:
            return {"type": "object"}, False
        if len(args) != 2 or args[0] is not str:
            raise SchemaError(f"{where}: only dict[str, T] is supported, got {annotation!r}")
        value, _ = _schema_for(args[1], where=f"{where}{{}}")
        return {"type": "object", "additionalProperties": value}, False

    raise SchemaError(
        f"{where}: unsupported annotation {annotation!r}. "
        "Supported: str, int, float, bool, list, dict, list[T], dict[str, T], Optional[T]"
    )


def schema_for_function(fn: Callable[..., Any]) -> dict[str, Any]:
    """Build the `input_schema` for one tool function."""
    if not callable(fn):
        raise SchemaError(f"fn must be callable, got {type(fn).__name__}")
    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError) as exc:
        raise SchemaError(f"cannot inspect signature of {fn!r}: {exc}") from None
    try:
        hints = typing.get_type_hints(fn)
    except Exception as exc:  # NameError for unresolved forward refs
        raise SchemaError(f"{getattr(fn, '__name__', fn)!r}: cannot resolve type hints ({exc})") from None

    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        where = f"{getattr(fn, '__name__', 'fn')}({name})"
        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
            raise SchemaError(f"{where}: *args/**kwargs cannot be described by a JSON Schema object")
        annotation = hints.get(name, param.annotation)
        schema, optional = _schema_for(annotation, where=where)
        if param.default is not inspect.Parameter.empty:
            if param.default is not None:
                schema = dict(schema)
                schema["default"] = param.default
            if optional:
                pass
        elif not optional:
            required.append(name)
        properties[name] = schema

    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def _type_ok(value: Any, schema: Mapping[str, Any]) -> bool:
    expected = schema.get("type")
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "array":
        return isinstance(value, list)
    if expected == "object":
        return isinstance(value, dict)
    return True


def validate_args(schema: Mapping[str, Any], args: Mapping[str, Any]) -> list[str]:
    """Return human-readable violations; empty means the args are acceptable."""
    problems: list[str] = []
    properties = schema.get("properties", {}) or {}
    if schema.get("additionalProperties") is False:
        for key in args:
            if key not in properties:
                problems.append(f"unknown argument {key!r}")
    for name in schema.get("required", []) or []:
        if name not in args:
            problems.append(f"missing required argument {name!r}")
    for name, value in args.items():
        spec = properties.get(name)
        if not isinstance(spec, Mapping):
            continue
        if value is None:
            continue
        # Union is not produced by this module, but a hand-written schema could
        # contain one; only the concrete types above are enforced.
        if not _type_ok(value, spec):
            problems.append(
                f"argument {name!r} must be {spec.get('type')}, got {type(value).__name__}"
            )
        elif spec.get("type") == "array" and isinstance(spec.get("items"), Mapping):
            for index, item in enumerate(value):
                if item is not None and not _type_ok(item, spec["items"]):
                    problems.append(
                        f"argument {name!r}[{index}] must be {spec['items'].get('type')}, got {type(item).__name__}"
                    )
    return problems


@dataclass(frozen=True)
class ToolSpec:
    """One tool. `fn` is a plain function — sync or async, no SDK types inside."""

    name: str
    fn: Callable[..., Any]
    description: str
    risk: str = "low"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    idempotent: bool = True
    #: Filled from `fn`'s signature; passing it explicitly is not supported in
    #: 0.1 (the signature is the single source of truth).
    input_schema: Mapping[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if TOOL_NAME_RE.match(self.name) is None:
            raise ValueError(f"tool name {self.name!r} violates ^[a-z][a-z0-9_]{{0,63}}$")
        if not self.description or not str(self.description).strip():
            raise ValueError(f"tool {self.name!r}: description must not be blank")
        if len(self.description) > MAX_DESCRIPTION_LEN:
            raise ValueError(f"tool {self.name!r}: description {len(self.description)} > {MAX_DESCRIPTION_LEN}")
        if self.risk not in RISKS:
            raise ValueError(f"tool {self.name!r}: risk must be one of {list(RISKS)}, got {self.risk!r}")
        if not (TIMEOUT_MS_MIN <= int(self.timeout_ms) <= TIMEOUT_MS_MAX):
            raise ValueError(
                f"tool {self.name!r}: timeout_ms must be within [{TIMEOUT_MS_MIN}, {TIMEOUT_MS_MAX}], got {self.timeout_ms}"
            )
        if not callable(self.fn):
            raise ValueError(f"tool {self.name!r}: fn must be callable")
        schema = schema_for_function(self.fn)
        object.__setattr__(self, "input_schema", schema)

    @property
    def is_async(self) -> bool:
        return inspect.iscoroutinefunction(self.fn)

    def manifest_entry(self) -> dict[str, Any]:
        """Exactly what goes into `capabilities.tools[i]`."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "risk": self.risk,
            "timeout_ms": int(self.timeout_ms),
            "idempotent": bool(self.idempotent),
        }
