"""C-04 error codes: no invented strings, no drift from schema/errors.json."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vade_sdk.errors import ERROR_CODES, ToolError, schema_codes

REPO = Path(__file__).resolve().parents[3]


def test_table_matches_c04_file():
    table = schema_codes(REPO / "schema" / "errors.json")
    assert table is not None, "schema/errors.json must be readable from the repo"
    assert table == ERROR_CODES


def test_forbidden_is_not_a_code():
    with pytest.raises(ValueError, match="not a C-04 error code"):
        ToolError("FORBIDDEN", "nope")


def test_retriable_defaults_to_the_code_property():
    assert ToolError("TIMEOUT", "slow").retriable is True
    assert ToolError("INVALID_ARGUMENT", "bad").retriable is False
    assert ToolError("INVALID_ARGUMENT", "bad", retriable=True).retriable is True


def test_message_is_truncated_to_300_chars():
    error = ToolError("INTERNAL", "x" * 1000)
    assert len(error.message) == 300


def test_to_dict_shape_matches_the_contract():
    payload = ToolError("PERMISSION_DENIED", "no").to_dict()
    assert payload == {"code": "PERMISSION_DENIED", "message": "no", "retriable": False}


def test_schema_file_has_every_code_we_emit():
    raw = json.loads((REPO / "schema" / "errors.json").read_text(encoding="utf-8"))
    codes = raw.get("codes", raw)
    assert set(ERROR_CODES) == set(codes)
