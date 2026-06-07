"""Phase 2 unit tests: _validation.py type detection and validation logic."""

from __future__ import annotations

import dataclasses
from typing import Optional, TypedDict

import pytest


# ---------------------------------------------------------------------------
# Fixtures / sample types
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DCUser:
    name: str
    age: int = 0


class TDUser(TypedDict):
    name: str
    age: int


class TDPartial(TypedDict, total=False):
    tag: str


# ---------------------------------------------------------------------------
# is_dataclass
# ---------------------------------------------------------------------------


def test_is_dataclass_true():
    from lauren._validation import is_dataclass

    assert is_dataclass(DCUser) is True


def test_is_dataclass_false_for_plain_class():
    from lauren._validation import is_dataclass

    class Plain:
        pass

    assert is_dataclass(Plain) is False


def test_is_dataclass_false_for_instance():
    from lauren._validation import is_dataclass

    assert is_dataclass(DCUser(name="x")) is False


def test_is_dataclass_strips_optional():
    from lauren._validation import is_dataclass

    assert is_dataclass(Optional[DCUser]) is True


# ---------------------------------------------------------------------------
# is_typeddict
# ---------------------------------------------------------------------------


def test_is_typeddict_true():
    from lauren._validation import is_typeddict

    assert is_typeddict(TDUser) is True


def test_is_typeddict_false_for_plain_dict():
    from lauren._validation import is_typeddict

    assert is_typeddict(dict) is False


def test_is_typeddict_partial():
    from lauren._validation import is_typeddict

    assert is_typeddict(TDPartial) is True


# ---------------------------------------------------------------------------
# is_msgspec_struct (attribute-probe, no msgspec import needed)
# ---------------------------------------------------------------------------


def test_is_msgspec_struct_false_for_non_struct():
    from lauren._validation import is_msgspec_struct

    assert is_msgspec_struct(DCUser) is False
    assert is_msgspec_struct(TDUser) is False


def test_is_msgspec_struct_true_with_probe():
    """Simulate a msgspec Struct via attribute probing without importing msgspec."""
    from lauren._validation import is_msgspec_struct

    class FakeStruct:
        __struct_fields__ = ("x",)
        __struct_config__ = {}

    assert is_msgspec_struct(FakeStruct) is True


# ---------------------------------------------------------------------------
# is_json_body_type
# ---------------------------------------------------------------------------


def test_is_json_body_type_dataclass():
    from lauren._validation import is_json_body_type

    assert is_json_body_type(DCUser) is True


def test_is_json_body_type_typeddict():
    from lauren._validation import is_json_body_type

    assert is_json_body_type(TDUser) is True


def test_is_json_body_type_plain():
    from lauren._validation import is_json_body_type

    assert is_json_body_type(str) is False
    assert is_json_body_type(int) is False


# ---------------------------------------------------------------------------
# validate_as — dataclass backend
# ---------------------------------------------------------------------------


def test_validate_as_dataclass_happy_path():
    from lauren._validation import validate_as

    result = validate_as(DCUser, {"name": "Alice", "age": 30})
    assert isinstance(result, DCUser)
    assert result.name == "Alice"
    assert result.age == 30


def test_validate_as_dataclass_defaults_applied():
    from lauren._validation import validate_as

    result = validate_as(DCUser, {"name": "Bob"})
    assert result.age == 0


def test_validate_as_dataclass_missing_required_raises():
    from lauren._validation import validate_as
    from lauren.exceptions import ExtractorError

    with pytest.raises(ExtractorError) as exc_info:
        validate_as(DCUser, {})
    assert exc_info.value.detail["field"] == "body"


def test_validate_as_dataclass_custom_field_name():
    from lauren._validation import validate_as
    from lauren.exceptions import ExtractorError

    with pytest.raises(ExtractorError) as exc_info:
        validate_as(DCUser, {}, field="my_field")
    assert exc_info.value.detail["field"] == "my_field"


# ---------------------------------------------------------------------------
# validate_as — typeddict backend
# ---------------------------------------------------------------------------


def test_validate_as_typeddict_happy_path():
    from lauren._validation import validate_as

    result = validate_as(TDUser, {"name": "Carol", "age": 25})
    assert result == {"name": "Carol", "age": 25}


def test_validate_as_typeddict_missing_required_raises():
    from lauren._validation import validate_as
    from lauren.exceptions import ExtractorError

    with pytest.raises(ExtractorError):
        validate_as(TDUser, {"name": "Carol"})


def test_validate_as_typeddict_partial_ok():
    from lauren._validation import validate_as

    result = validate_as(TDPartial, {})
    assert isinstance(result, dict)


# ---------------------------------------------------------------------------
# validate_as — unsupported type
# ---------------------------------------------------------------------------


def test_validate_as_unsupported_type_raises_type_error():
    from lauren._validation import validate_as

    with pytest.raises(TypeError, match="Unsupported"):
        validate_as(str, {"x": 1})


# ---------------------------------------------------------------------------
# json_schema_for — dataclass
# ---------------------------------------------------------------------------


def test_json_schema_for_dataclass():
    from lauren._validation import json_schema_for

    schema = json_schema_for(DCUser)
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "name" in schema.get("required", [])


# ---------------------------------------------------------------------------
# json_schema_for — typeddict
# ---------------------------------------------------------------------------


def test_json_schema_for_typeddict():
    from lauren._validation import json_schema_for

    schema = json_schema_for(TDUser)
    assert schema["type"] == "object"
    assert "name" in schema["properties"]
    assert "age" in schema["properties"]


# ---------------------------------------------------------------------------
# _peel_to_inner edge cases
# ---------------------------------------------------------------------------


def test_peel_strips_optional():
    from lauren._validation import _peel_to_inner

    assert _peel_to_inner(Optional[DCUser]) is DCUser


def test_peel_identity_for_plain_type():
    from lauren._validation import _peel_to_inner

    assert _peel_to_inner(str) is str
