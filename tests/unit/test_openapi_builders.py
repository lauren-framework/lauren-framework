"""Unit tests: _ComponentRegistry and _python_type_to_schema."""

import dataclasses
import datetime
import uuid
from typing import Literal, Optional, TypedDict

import pytest


@dataclasses.dataclass
class Address:
    street: str
    city: str


@dataclasses.dataclass
class Person:
    """A person record."""

    name: str
    address: Address


class TestComponentRegistry:
    def test_ensure_returns_ref_path(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        ref = reg.ensure(Address)
        assert ref == "#/components/schemas/Address"

    def test_ensure_idempotent(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        ref1 = reg.ensure(Address)
        ref2 = reg.ensure(Address)
        assert ref1 == ref2

    def test_as_dict_contains_registered_schema(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        reg.ensure(Address)
        d = reg.as_dict()
        assert "Address" in d
        assert d["Address"]["type"] == "object"

    def test_schema_has_title(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        reg.ensure(Address)
        assert reg.as_dict()["Address"].get("title") == "Address"

    def test_schema_has_description_from_docstring(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        reg.ensure(Person)
        assert reg.as_dict()["Person"].get("description") == "A person record."

    def test_cycle_detection_does_not_raise(self):
        from lauren._asgi._openapi import _ComponentRegistry

        @dataclasses.dataclass
        class Node:
            value: int
            next: Optional["Node"] = None  # type: ignore[assignment]

        reg = _ComponentRegistry()
        ref = reg.ensure(Node)
        assert ref == "#/components/schemas/Node"

    def test_pydantic_defs_lifted_to_top_level(self):
        pytest.importorskip("pydantic")
        from pydantic import BaseModel
        from lauren._asgi._openapi import _ComponentRegistry

        class Inner(BaseModel):
            x: int

        class Outer(BaseModel):
            inner: Inner

        reg = _ComponentRegistry()
        reg.ensure(Outer)
        schemas = reg.as_dict()
        assert "Inner" in schemas
        assert "$defs" not in schemas.get("Outer", {})

    def test_contains_supports_in_operator(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        reg.ensure(Address)
        assert "Address" in reg
        assert "Unknown" not in reg

    def test_setitem_and_getitem(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        reg["Foo"] = {"type": "object"}
        assert reg["Foo"] == {"type": "object"}

    def test_setdefault(self):
        from lauren._asgi._openapi import _ComponentRegistry

        reg = _ComponentRegistry()
        reg.setdefault("Bar", {"type": "string"})
        assert reg["Bar"] == {"type": "string"}
        reg.setdefault("Bar", {"type": "integer"})
        assert reg["Bar"] == {"type": "string"}

    def test_typeddict_schema_registered(self):
        from lauren._asgi._openapi import _ComponentRegistry

        class Event(TypedDict):
            name: str
            count: int

        reg = _ComponentRegistry()
        ref = reg.ensure(Event)
        assert ref == "#/components/schemas/Event"
        schema = reg.as_dict()["Event"]
        assert schema["type"] == "object"
        assert "name" in schema["properties"]


class TestPythonTypeToSchema:
    def test_int_maps_to_integer(self):
        from lauren._asgi._openapi import _python_type_to_schema

        assert _python_type_to_schema(int) == {"type": "integer"}

    def test_str_maps_to_string(self):
        from lauren._asgi._openapi import _python_type_to_schema

        assert _python_type_to_schema(str) == {"type": "string"}

    def test_none_maps_to_null(self):
        from lauren._asgi._openapi import _python_type_to_schema

        assert _python_type_to_schema(None) == {"type": "null"}
        assert _python_type_to_schema(type(None)) == {"type": "null"}

    def test_optional_int_is_nullable(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(Optional[int])
        assert schema.get("nullable") is True or "null" in str(schema)

    def test_literal_single_produces_const(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(Literal["cat"])
        assert schema == {"const": "cat"}

    def test_literal_multiple_produces_enum(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(Literal["cat", "dog"])
        assert schema == {"enum": ["cat", "dog"]}

    def test_list_int_produces_array(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(list[int])
        assert schema["type"] == "array"
        assert schema["items"] == {"type": "integer"}

    def test_datetime_has_format(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(datetime.datetime)
        assert schema == {"type": "string", "format": "date-time"}

    def test_date_has_format(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(datetime.date)
        assert schema == {"type": "string", "format": "date"}

    def test_uuid_has_format(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(uuid.UUID)
        assert schema == {"type": "string", "format": "uuid"}

    def test_union_produces_oneof(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(int | str)
        assert "oneOf" in schema

    def test_dict_with_value_type(self):
        from lauren._asgi._openapi import _python_type_to_schema

        schema = _python_type_to_schema(dict[str, int])
        assert schema["type"] == "object"
        assert schema["additionalProperties"] == {"type": "integer"}
