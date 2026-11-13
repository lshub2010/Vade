"""Minimal JSON Schema (2020-12 subset) validator.

Why not `jsonschema`: core must start on a bare Python 3.12 install (the
deployment target has no wheels available offline), and C-03 only needs a
small, auditable subset. Supported keywords:

    $ref (local #/$defs/...), type (str or list), required, properties,
    additionalProperties (bool|schema), patternProperties, items, minItems,
    maxItems, uniqueItems, minLength, maxLength, minimum, maximum, enum,
    const, oneOf, anyOf, allOf, default/description/x-vade (ignored)

Deliberately unsupported: $dynamicRef, unevaluatedProperties, if/then/else,
format. C-03 does not use them; if a future contract does, this must grow.

Every error carries a JSON Pointer (`/capabilities/tools/0/name`) because the
acceptance criterion is that the rejection message points at the field.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

__all__ = ["SchemaError", "validate", "resolve_ref"]

_MISSING = object()


class SchemaError(ValueError):
    """First schema violation found. `pointer` is a JSON Pointer."""

    def __init__(self, pointer: str, message: str) -> None:
        self.pointer = pointer
        self.message = message
        super().__init__(f"{pointer or '/'}: {message}")


def resolve_ref(root: Mapping[str, Any], ref: str) -> Mapping[str, Any]:
    """Resolve a local JSON Pointer ref (`#/$defs/tool`). Remote refs are rejected."""
    if not ref.startswith("#/"):
        raise SchemaError("", f"unsupported non-local $ref {ref!r}")
    node: Any = root
    for token in ref[2:].split("/"):
        token = token.replace("~1", "/").replace("~0", "~")
        if not isinstance(node, Mapping) or token not in node:
            raise SchemaError("", f"dangling $ref {ref!r}")
        node = node[token]
    if not isinstance(node, Mapping):
        raise SchemaError("", f"$ref {ref!r} does not point at a schema")
    return node


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, Mapping)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    raise SchemaError("", f"unknown schema type {expected!r}")


def _join(pointer: str, token: Any) -> str:
    return f"{pointer}/{token}"


def validate(instance: Any, schema: Mapping[str, Any], *, root: Mapping[str, Any] | None = None, pointer: str = "") -> None:
    """Validate `instance`; raise SchemaError on the FIRST violation."""
    root = root if root is not None else schema

    if "$ref" in schema:
        validate(instance, resolve_ref(root, schema["$ref"]), root=root, pointer=pointer)
        # A $ref sibling in 2020-12 is legal; C-03 only uses bare refs.

    expected = schema.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(instance, t) for t in allowed):
            raise SchemaError(pointer, f"expected {'/'.join(allowed)}, got {type(instance).__name__}")

    if "const" in schema and instance != schema["const"]:
        raise SchemaError(pointer, f"must equal {schema['const']!r}, got {instance!r}")

    if "enum" in schema and instance not in schema["enum"]:
        raise SchemaError(pointer, f"must be one of {schema['enum']}, got {instance!r}")

    if isinstance(instance, str):
        if (n := schema.get("minLength")) is not None and len(instance) < n:
            raise SchemaError(pointer, f"length {len(instance)} < minLength {n}")
        if (n := schema.get("maxLength")) is not None and len(instance) > n:
            raise SchemaError(pointer, f"length {len(instance)} > maxLength {n}")
        if (pattern := schema.get("pattern")) is not None and re.search(pattern, instance) is None:
            raise SchemaError(pointer, f"value {instance!r} does not match {pattern!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if (n := schema.get("minimum")) is not None and instance < n:
            raise SchemaError(pointer, f"value {instance} < minimum {n}")
        if (n := schema.get("maximum")) is not None and instance > n:
            raise SchemaError(pointer, f"value {instance} > maximum {n}")

    if isinstance(instance, Mapping):
        for key in schema.get("required", ()):
            if key not in instance:
                raise SchemaError(_join(pointer, key), "required property is missing")
        props = schema.get("properties", {})
        pattern_props = schema.get("patternProperties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            child = _join(pointer, key)
            matched = False
            if key in props:
                matched = True
                validate(value, props[key], root=root, pointer=child)
            for pattern, sub in pattern_props.items():
                if re.search(pattern, str(key)):
                    matched = True
                    validate(value, sub, root=root, pointer=child)
            if not matched:
                if extra is False:
                    raise SchemaError(child, "unknown property (additionalProperties: false)")
                if isinstance(extra, Mapping):
                    validate(value, extra, root=root, pointer=child)

    if isinstance(instance, list):
        if (n := schema.get("minItems")) is not None and len(instance) < n:
            raise SchemaError(pointer, f"has {len(instance)} items, minItems {n}")
        if (n := schema.get("maxItems")) is not None and len(instance) > n:
            raise SchemaError(pointer, f"has {len(instance)} items, maxItems {n}")
        if schema.get("uniqueItems"):
            seen: list[Any] = []
            for item in instance:
                if item in seen:
                    raise SchemaError(pointer, f"duplicate item {item!r}")
                seen.append(item)
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, value in enumerate(instance):
                validate(value, items, root=root, pointer=_join(pointer, index))

    for sub in _as_list(schema.get("allOf")):
        validate(instance, sub, root=root, pointer=pointer)

    for keyword in ("anyOf", "oneOf"):
        subs = _as_list(schema.get(keyword))
        if not subs:
            continue
        failures: list[str] = []
        passed = 0
        for sub in subs:
            try:
                validate(instance, sub, root=root, pointer=pointer)
                passed += 1
            except SchemaError as exc:
                failures.append(str(exc))
        if passed == 0:
            raise SchemaError(pointer, f"matches none of {keyword}: " + "; ".join(failures[:3]))
        if keyword == "oneOf" and passed > 1:
            raise SchemaError(pointer, f"matches {passed} branches of oneOf, expected exactly 1")


def _as_list(value: Any) -> Sequence[Mapping[str, Any]]:
    if value is None:
        return ()
    if isinstance(value, Mapping):
        return (value,)
    return tuple(value)
