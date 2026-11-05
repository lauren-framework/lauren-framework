"""Unit tests for edge behaviour of the individual validator nodes."""

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
from lauren.types import UploadFile
from lauren.validation import Ge, MaxLen, TypeCompiler, ValidationFailed, ValidationPolicy


def _compiled(tp: Any, mode: str = "lax") -> Any:
    return TypeCompiler().compile(tp, mode=mode)


def _issues(tp: Any, value: Any, mode: str = "lax") -> list[tuple[Any, str]]:
    with pytest.raises(ValidationFailed) as info:
        _compiled(tp, mode).validate(value)
    return [(issue.loc, issue.type) for issue in info.value.issues]


class Level(enum.IntEnum):
    LOW = 1


class Mixed(enum.Enum):
    ONE = 1
    LIST = [1, 2]  # unhashable value


class Ratio(enum.Enum):
    HALF = 0.5


def test_int_accepts_int_subclasses_as_plain_ints() -> None:
    result = _compiled(int).validate(Level.LOW)

    assert result == 1
    assert type(result) is int


def test_float_rejects_booleans_and_overflowing_ints() -> None:
    assert _issues(float, True) == [((), "float_type")]
    assert _issues(float, 10**400) == [((), "float_parsing")]


def test_decimal_rejects_booleans() -> None:
    assert _issues(decimal.Decimal, True) == [((), "decimal_type")]


@pytest.mark.parametrize(
    ("tp", "value"),
    [
        (complex, 1j),
        (bytes, bytearray(b"x")),
        (uuid.UUID, uuid.UUID(int=1)),
        (datetime.date, datetime.date(2024, 1, 1)),
        (datetime.timedelta, datetime.timedelta(seconds=1)),
        (pathlib.PurePosixPath, pathlib.PurePosixPath("x")),
    ],
)
def test_instances_of_the_target_type_pass_through(tp: Any, value: Any) -> None:
    assert _compiled(tp).validate(value) == value


def test_date_rejects_datetimes() -> None:
    assert _issues(datetime.date, datetime.datetime(2024, 1, 1)) == [((), "date_type")]


def test_complex_accepts_numbers_in_lax_mode_only() -> None:
    assert _compiled(complex).validate(2) == 2 + 0j
    assert _issues(complex, 2, "strict") == [((), "complex_type")]


def test_timedelta_numeric_strings_are_lax_only() -> None:
    assert _compiled(datetime.timedelta).validate("90") == datetime.timedelta(seconds=90)
    assert _issues(datetime.timedelta, "90", "strict") == [((), "time_delta_parsing")]
    assert _issues(datetime.timedelta, "1e400") == [((), "time_delta_parsing")]
    assert _issues(datetime.timedelta, 1e300) == [((), "time_delta_parsing")]


def test_upload_files_are_accepted_as_instances() -> None:
    compiled = _compiled(UploadFile)
    upload = UploadFile(data=b"x", filename="a.txt", content_type="text/plain", headers={}, name="f")

    assert compiled.validate(upload) is upload
    assert _issues(UploadFile, "x") == [((), "is_instance_of")]
    assert compiled.json_schema() == {"type": "string", "format": "binary"}
    assert _issues(int | UploadFile, "x")[1] == (("UploadFile",), "is_instance_of")


def test_enums_with_unhashable_values_and_cross_type_numbers() -> None:
    assert _compiled(Mixed).validate([1, 2]) is Mixed.LIST
    assert _compiled(Mixed).validate(1.0) is Mixed.ONE
    assert _compiled(Ratio).validate(0.5) is Ratio.HALF
    assert _compiled(Mixed).json_schema()["$defs"]["Mixed"] == {"title": "Mixed", "enum": [1, [1, 2]]}


def test_enum_schema_types() -> None:
    assert _compiled(Ratio).json_schema()["$defs"]["Ratio"]["type"] == "number"
    assert _compiled(Level).json_schema()["$defs"]["Level"]["type"] == "integer"


def test_literal_schema_types() -> None:
    assert _compiled(Literal[True]).json_schema() == {"const": True, "type": "boolean"}
    assert _compiled(Literal[0.5]).json_schema() == {"const": 0.5, "type": "number"}
    assert _compiled(Literal[None]).json_schema() == {"const": None, "type": "null"}


def test_union_labels_name_containers_optionals_and_models() -> None:
    @dataclasses.dataclass
    class Box:
        v: int

    issues = _issues(list[int] | Annotated[str, MaxLen(1)] | Box, 5)

    assert [loc for loc, _ in issues] == [("list[int]",), ("str",), ("Box",)]


def test_union_with_an_exact_type_match_that_fails_falls_back() -> None:
    assert _issues(Annotated[int, Ge(5)] | str, 3) == [
        (("int",), "greater_than_equal"),
        (("str",), "string_type"),
    ]
    assert _issues(Literal["a"] | int, "b") == [(("literal",), "literal_error"), (("int",), "int_parsing")]


def test_union_projection_falls_back_to_builtins() -> None:
    assert _compiled(int | str).project(b"x") == "x"
    assert _compiled(int | str).node.is_instance(3)


def test_optional_projection_and_labels() -> None:
    compiled = _compiled(list[int] | None)

    assert compiled.project(None) is None
    assert compiled.node.is_instance(None)
    assert compiled.node.label == "list[int] | None"


def test_tuple_rejects_non_sequences() -> None:
    assert _issues(tuple[int, str], "ab") == [((), "tuple_type")]
    assert _issues(tuple[int, str], ["x", 1]) == [((0,), "int_parsing"), ((1,), "string_type")]


def test_dict_stops_collecting_at_max_issues() -> None:
    compiled = TypeCompiler(ValidationPolicy(max_issues=2)).compile(dict[str, int])

    with pytest.raises(ValidationFailed) as info:
        compiled.validate({"a": "x", "b": "x", "c": "x"})

    assert len(info.value.issues) == 2


def test_model_stops_collecting_at_max_issues() -> None:
    @dataclasses.dataclass
    class Wide:
        a: int
        b: int
        c: int

    compiled = TypeCompiler(ValidationPolicy(max_issues=2)).compile(Wide)

    with pytest.raises(ValidationFailed) as info:
        compiled.validate({"a": "x", "b": "x", "c": "x"})

    assert len(info.value.issues) == 2


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"]


@dataclasses.dataclass
class Dog:
    kind: Literal["dog"]


def test_discriminated_unions_reject_non_objects() -> None:
    assert _issues(Discriminated[Cat | Dog, "kind"], [1]) == [((), "model_type")]


def test_discriminated_projection_of_unknown_values() -> None:
    compiled = _compiled(Discriminated[Cat | Dog, "kind"])

    assert compiled.project({"kind": "cat"}) == {"kind": "cat"}
    assert compiled.node.is_instance(Dog(kind="dog"))


def test_discriminated_schema_without_named_variants() -> None:
    class MovieDict(TypedDict):
        kind: Literal["movie"]

    class BookDict(TypedDict):
        kind: Literal["book"]

    schema = _compiled(Discriminated[MovieDict | BookDict, "kind"]).json_schema()

    assert schema["discriminator"]["mapping"] == {"movie": "#/$defs/MovieDict", "book": "#/$defs/BookDict"}


@dataclasses.dataclass
class Tree:
    value: int
    child: Tree | None = None


def test_recursive_nodes_project_and_match_instances() -> None:
    compiled = _compiled(Tree)
    tree = compiled.validate({"value": 1, "child": {"value": 2}})

    assert compiled.project(tree) == {"value": 1, "child": {"value": 2, "child": None}}
    assert compiled.node.fields[1].node.inner.label == "Tree"
    assert compiled.node.fields[1].node.inner.is_instance(tree)


def test_projection_skips_missing_attributes_and_reads_wire_keys() -> None:
    @dataclasses.dataclass
    class Out:
        a: int
        b: int = 0

    compiled = _compiled(Out)

    class Partial:
        a = 1

    assert compiled.project(Partial()) == {"a": 1}
    assert compiled.project({"b": 2}) == {"b": 2}


def test_namedtuple_and_typeddict_nodes() -> None:
    class Point(NamedTuple):
        x: int

    class Movie(TypedDict):
        title: str

    assert _compiled(Point).node.label == "Point"
    assert _compiled(Movie).node.is_instance({})
    assert _issues(Point, "x") == [((), "model_type")]


def test_constrained_nodes_delegate_projection_and_labels() -> None:
    compiled = _compiled(Annotated[list[int], MaxLen(2)])

    assert compiled.project((1, 2)) == [1, 2]
    assert compiled.node.label == "list[int]"
    assert compiled.node.is_instance([1])


def test_base_node_instance_check_without_exact_types() -> None:
    compiled = _compiled(Any)

    assert compiled.node.is_instance(1) is False
    assert compiled.node.label == "any"


def test_any_projection_of_iso_values() -> None:
    compiled = _compiled(Any)

    assert compiled.project(datetime.timedelta(minutes=1)) == "PT1M"
    assert compiled.project(decimal.Decimal("1.5")) == "1.5"
    assert compiled.project(bytearray(b"z")) == "z"
    assert compiled.project({(1, 2): 1}) == {"(1, 2)": 1}
