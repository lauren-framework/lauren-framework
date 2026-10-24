"""Unit tests for the projectors (response filtering, F01) and the JSON Schema emitter (F06)."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import pathlib
import uuid
from typing import Annotated, Any, Literal, NamedTuple, TypedDict

import pytest

from lauren import Discriminated
from lauren.validation import Alias, Ge, MaxLen, MinLen, MultipleOf, Pattern, TypeCompiler, validation_config


class Role(enum.Enum):
    ADMIN = "admin"
    USER = "user"


@dataclasses.dataclass
class Address:
    city: str


@dataclasses.dataclass
class Account:
    id: uuid.UUID
    role: Role
    created: datetime.datetime
    balance: decimal.Decimal
    ttl: datetime.timedelta
    address: Address | None = None
    tags: set[str] = dataclasses.field(default_factory=set)


ACCOUNT = Account(
    id=uuid.UUID("a2345678-1234-5678-1234-56781234abcd"),
    role=Role.ADMIN,
    created=datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    balance=decimal.Decimal("12.50"),
    ttl=datetime.timedelta(hours=1, minutes=30),
    address=Address("Paris"),
    tags={"a"},
)


def _compile(tp: Any) -> Any:
    return TypeCompiler().compile(tp)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


def test_projection_returns_json_builtins() -> None:
    assert _compile(Account).project(ACCOUNT) == {
        "id": "a2345678-1234-5678-1234-56781234abcd",
        "role": "admin",
        "created": "2024-01-02T03:04:05+00:00",
        "balance": "12.50",
        "ttl": "PT1H30M",
        "address": {"city": "Paris"},
        "tags": ["a"],
    }


def test_projection_round_trips_through_validation() -> None:
    compiled = _compile(Account)

    assert compiled.validate(compiled.project(ACCOUNT)) == ACCOUNT


@dataclasses.dataclass
class Public:
    name: str


@dataclasses.dataclass
class Private(Public):
    password_hash: str = ""


def test_projection_drops_undeclared_attributes() -> None:
    assert _compile(Public).project(Private(name="ada", password_hash="secret")) == {"name": "ada"}


def test_projection_reads_mappings_by_python_or_wire_name() -> None:
    @validation_config(rename="camel")
    @dataclasses.dataclass
    class Out:
        first_name: str
        last_name: str = ""

    compiled = _compile(Out)

    assert compiled.project({"first_name": "Ada", "lastName": "L", "secret": 1}) == {
        "firstName": "Ada",
        "lastName": "L",
    }


def test_projection_includes_init_false_fields() -> None:
    @dataclasses.dataclass
    class Order:
        qty: int
        total: int = dataclasses.field(init=False, default=0)

        def __post_init__(self) -> None:
            self.total = self.qty * 2

    compiled = _compile(Order)
    order = compiled.validate({"qty": 2})

    assert compiled.project(order) == {"qty": 2, "total": 4}


def test_projection_of_typeddicts_and_namedtuples() -> None:
    class Movie(TypedDict):
        title: str

    class Point(NamedTuple):
        x: int
        y: int

    assert _compile(Movie).project({"title": "x", "rating": 5}) == {"title": "x"}
    assert _compile(Point).project(Point(1, 2)) == {"x": 1, "y": 2}


def test_projection_of_unions_picks_the_matching_member() -> None:
    compiled = _compile(int | Address | None)

    assert compiled.project(Address("x")) == {"city": "x"}
    assert compiled.project(3) == 3
    assert compiled.project(None) is None


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"]
    lives: int = 9


@dataclasses.dataclass
class Dog:
    kind = "dog"  # a plain class attribute: the tag is not a dataclass field
    good: bool = True


def test_projection_of_discriminated_unions_restores_a_stripped_tag() -> None:
    compiled = _compile(Discriminated[Cat | Dog, "kind"])

    assert compiled.project(Cat(kind="cat")) == {"kind": "cat", "lives": 9}
    dog = compiled.validate({"kind": "dog", "good": False})
    assert isinstance(dog, Dog)
    assert compiled.project(dog) == {"kind": "dog", "good": False}


@pytest.mark.parametrize(
    ("tp", "value", "expected"),
    [
        (bytes, b"hi", "hi"),
        (complex, 1 + 2j, "(1+2j)"),
        (pathlib.PurePosixPath, pathlib.PurePosixPath("a/b"), "a/b"),
        (datetime.date, datetime.date(2024, 1, 2), "2024-01-02"),
        (datetime.time, datetime.time(3, 4), "03:04:00"),
        (datetime.timedelta, datetime.timedelta(0), "PT0S"),
        (datetime.timedelta, datetime.timedelta(days=-1, seconds=1), "-PT23H59M59S"),
        (datetime.timedelta, datetime.timedelta(days=2, microseconds=5), "P2DT0.000005S"),
        (tuple[int, str], (1, "a"), [1, "a"]),
        (dict[str, Role], {"r": Role.USER}, {"r": "user"}),
        (Literal["a"], "a", "a"),
        (Any, {"a": (Role.USER, datetime.date(2024, 1, 2))}, {"a": ["user", "2024-01-02"]}),
        (float, 1, 1.0),
    ],
)
def test_scalar_and_container_projection(tp: Any, value: Any, expected: Any) -> None:
    assert _compile(tp).project(value) == expected


def test_any_projection_handles_dataclasses_and_unknown_objects() -> None:
    compiled = _compile(Any)

    assert compiled.project(Address("x")) == {"city": "x"}
    assert compiled.project(object).startswith("<class")


def test_pydantic_models_project_with_model_dump() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Pet(pydantic.BaseModel):
        name: str = pydantic.Field(alias="petName")
        born: datetime.date

    compiled = _compile(Pet)
    pet = compiled.validate({"petName": "Rex", "born": "2020-01-02"})

    assert compiled.project(pet) == {"petName": "Rex", "born": "2020-01-02"}
    assert compiled.project({"petName": "Rex", "born": "2020-01-02"}) == {
        "petName": "Rex",
        "born": "2020-01-02",
    }


# ---------------------------------------------------------------------------
# JSON Schema
# ---------------------------------------------------------------------------


@validation_config(extra="forbid")
@dataclasses.dataclass
class CreateUser:
    email: Annotated[str, Pattern(r"@"), MaxLen(254)]
    age: Annotated[int, Ge(13)]
    nick: Annotated[str, Alias("nickName")] = "anon"
    address: Address | None = None


def test_dataclass_schema_with_constraints_alias_and_forbid() -> None:
    assert _compile(CreateUser).json_schema() == {
        "$ref": "#/$defs/CreateUser",
        "$defs": {
            "CreateUser": {
                "type": "object",
                "title": "CreateUser",
                "properties": {
                    "email": {"type": "string", "pattern": "@", "maxLength": 254},
                    "age": {"type": "integer", "minimum": 13},
                    "nickName": {"type": "string", "default": "anon"},
                    "address": {"anyOf": [{"$ref": "#/$defs/Address"}, {"type": "null"}], "default": None},
                },
                "required": ["email", "age"],
                "additionalProperties": False,
            },
            "Address": {
                "type": "object",
                "title": "Address",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        },
    }


def test_openapi_dialect_fills_the_components_dict() -> None:
    components: dict[str, Any] = {"Existing": {"type": "object"}}

    ref = _compile(list[Address]).json_schema(dialect="openapi-3.1", components=components)

    assert ref == {"type": "array", "items": {"$ref": "#/components/schemas/Address"}}
    assert set(components) == {"Existing", "Address"}


def test_existing_components_are_reused() -> None:
    components: dict[str, Any] = {"Address": {"type": "object", "title": "Address (F06)"}}

    _compile(Address).json_schema(dialect="openapi-3.1", components=components)

    assert components["Address"]["title"] == "Address (F06)"


def test_same_named_classes_get_distinct_definitions() -> None:
    def make() -> type:
        @dataclasses.dataclass
        class Address:
            zip: str

        return Address

    Other = make()

    @dataclasses.dataclass
    class Both:
        a: Address
        b: Other  # type: ignore[valid-type]

    defs = _compile(Both).json_schema()["$defs"]

    assert "Address" in defs
    assert any(name.endswith("__Address") for name in defs)


@dataclasses.dataclass
class Node:
    value: int
    children: list[Node] = dataclasses.field(default_factory=list)


def test_recursive_types_reference_themselves() -> None:
    schema = _compile(Node).json_schema()

    assert schema["$defs"]["Node"]["properties"]["children"] == {
        "type": "array",
        "items": {"$ref": "#/$defs/Node"},
    }


@pytest.mark.parametrize(
    ("tp", "expected"),
    [
        (str, {"type": "string"}),
        (bool, {"type": "boolean"}),
        (float, {"type": "number"}),
        (bytes, {"type": "string", "format": "binary"}),
        (uuid.UUID, {"type": "string", "format": "uuid"}),
        (datetime.datetime, {"type": "string", "format": "date-time"}),
        (datetime.date, {"type": "string", "format": "date"}),
        (datetime.time, {"type": "string", "format": "time"}),
        (datetime.timedelta, {"type": "string", "format": "duration"}),
        (pathlib.Path, {"type": "string", "format": "path"}),
        (complex, {"type": "string"}),
        (None, {"type": "null"}),
        (Any, {}),
        (Literal["a"], {"const": "a", "type": "string"}),
        (Literal[1, "a"], {"enum": [1, "a"]}),
        (set[int], {"type": "array", "items": {"type": "integer"}, "uniqueItems": True}),
        (
            tuple[int, str],
            {
                "type": "array",
                "prefixItems": [{"type": "integer"}, {"type": "string"}],
                "minItems": 2,
                "maxItems": 2,
            },
        ),
        (dict[str, int], {"type": "object", "additionalProperties": {"type": "integer"}}),
        (dict, {"type": "object", "additionalProperties": True}),
        (int | str, {"anyOf": [{"type": "integer"}, {"type": "string"}]}),
        (Annotated[list[int], MinLen(1)], {"type": "array", "items": {"type": "integer"}, "minItems": 1}),
        (Annotated[float, MultipleOf(0.5)], {"type": "number", "multipleOf": 0.5}),
    ],
)
def test_scalar_and_container_schemas(tp: Any, expected: dict[str, Any]) -> None:
    assert _compile(tp).json_schema() == expected


def test_enum_schema_is_a_named_definition() -> None:
    assert _compile(Role).json_schema() == {
        "$ref": "#/$defs/Role",
        "$defs": {"Role": {"title": "Role", "enum": ["admin", "user"], "type": "string"}},
    }


def test_decimal_schema_accepts_numbers_and_numeric_strings() -> None:
    schema = _compile(decimal.Decimal).json_schema()

    assert schema["anyOf"][0] == {"type": "number"}


def test_discriminated_schema_is_one_of_with_a_mapping() -> None:
    schema = _compile(Discriminated[Cat | Dog, "kind"]).json_schema(dialect="openapi-3.1", components={})

    assert schema == {
        "oneOf": [{"$ref": "#/components/schemas/Cat"}, {"$ref": "#/components/schemas/Dog"}],
        "discriminator": {
            "propertyName": "kind",
            "mapping": {"cat": "#/components/schemas/Cat", "dog": "#/components/schemas/Dog"},
        },
    }


def test_unknown_dialects_are_rejected() -> None:
    with pytest.raises(ValueError):
        _compile(int).json_schema(dialect="draft-04")


def test_pydantic_schemas_hoist_their_definitions() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Owner(pydantic.BaseModel):
        name: str

    class Pet(pydantic.BaseModel):
        owner: Owner

    components: dict[str, Any] = {}
    ref = _compile(Pet).json_schema(dialect="openapi-3.1", components=components)

    assert ref == {"$ref": "#/components/schemas/Pet"}
    assert components["Pet"]["properties"]["owner"] == {"$ref": "#/components/schemas/Owner"}
    assert "Owner" in components


def test_recursive_pydantic_schema_is_not_self_referential() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Tree(pydantic.BaseModel):
        children: list[Tree] = []

    Tree.model_rebuild()
    defs = _compile(Tree).json_schema()["$defs"]

    assert defs["Tree"]["type"] == "object"


def test_pydantic_tagged_unions_use_a_type_adapter() -> None:
    pydantic = pytest.importorskip("pydantic")

    class A(pydantic.BaseModel):
        kind: Literal["a"]

    class B(pydantic.BaseModel):
        kind: Literal["b"]

    compiled = _compile(Annotated[A | B, pydantic.Field(discriminator="kind")])

    assert isinstance(compiled.validate({"kind": "b"}), B)
    assert compiled.project(B(kind="b")) == {"kind": "b"}
    assert "oneOf" in compiled.json_schema()
