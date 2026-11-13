"""Signature -> JSON Schema: the mapping table in PYTHON_SDK.md §3."""

from __future__ import annotations

import pytest

from vade_sdk.spec import SchemaError, ToolSpec, schema_for_function, validate_args


def test_scalar_and_container_mapping():
    def fn(a: str, b: int, c: float, d: bool, e: list, f: dict) -> None:
        ...

    schema = schema_for_function(fn)
    assert schema["type"] == "object"
    assert schema["properties"] == {
        "a": {"type": "string"},
        "b": {"type": "integer"},
        "c": {"type": "number"},
        "d": {"type": "boolean"},
        "e": {"type": "array"},
        "f": {"type": "object"},
    }
    assert schema["required"] == ["a", "b", "c", "d", "e", "f"]
    assert schema["additionalProperties"] is False


def test_optional_and_defaults_are_not_required():
    def fn(text: str, limit: int = 10, note: str | None = None) -> None:
        ...

    schema = schema_for_function(fn)
    assert schema["required"] == ["text"]
    assert schema["properties"]["limit"] == {"type": "integer", "default": 10}
    assert schema["properties"]["note"] == {"type": "string"}


def test_generic_containers():
    def fn(names: list[str], weights: dict[str, float]) -> None:
        ...

    schema = schema_for_function(fn)
    assert schema["properties"]["names"]["items"] == {"type": "string"}
    assert schema["properties"]["weights"]["additionalProperties"] == {"type": "number"}


@pytest.mark.parametrize(
    "annotation",
    [bytes, object, tuple, set, list[int] | list[str]],
)
def test_unsupported_types_fail_at_construction(annotation):
    namespace = {"__annotations__": {}}

    def fn(value):  # noqa: ANN001
        ...

    fn.__annotations__ = {"value": annotation}
    with pytest.raises(SchemaError):
        schema_for_function(fn)


def test_missing_annotation_is_an_error_not_an_empty_schema():
    def fn(value):
        ...

    with pytest.raises(SchemaError, match="missing type annotation"):
        schema_for_function(fn)


def test_varargs_rejected():
    def fn(*args: str) -> None:
        ...

    with pytest.raises(SchemaError, match=r"\*args"):
        schema_for_function(fn)


def test_toolspec_validates_name_risk_timeout():
    def fn(text: str) -> dict:
        return {"text": text}

    with pytest.raises(ValueError, match="tool name"):
        ToolSpec(name="Echo", fn=fn, description="x")
    with pytest.raises(ValueError, match="risk"):
        ToolSpec(name="echo", fn=fn, description="x", risk="critical")
    with pytest.raises(ValueError, match="timeout_ms"):
        ToolSpec(name="echo", fn=fn, description="x", timeout_ms=10)
    with pytest.raises(ValueError, match="description"):
        ToolSpec(name="echo", fn=fn, description="   ")


def test_validate_args_reports_unknown_missing_and_type():
    def fn(text: str, count: int = 1) -> None:
        ...

    schema = schema_for_function(fn)
    assert validate_args(schema, {"text": "hi"}) == []
    problems = validate_args(schema, {"count": 2})
    assert any("missing required argument 'text'" in p for p in problems)
    problems = validate_args(schema, {"text": "hi", "path": "/etc/passwd"})
    assert any("unknown argument 'path'" in p for p in problems)
    problems = validate_args(schema, {"text": 5})
    assert any("must be string" in p for p in problems)
    # bool is not an integer argument
    problems = validate_args(schema, {"text": "hi", "count": True})
    assert any("must be integer" in p for p in problems)
