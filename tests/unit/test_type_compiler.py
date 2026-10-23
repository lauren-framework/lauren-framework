"""Unit tests for the native type compiler (lauren.validation.TypeCompiler)."""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import pathlib
import typing
import uuid
from typing import Annotated, Any, Literal, NamedTuple, NotRequired, Required, TypedDict

import pytest

from lauren.exceptions import DecoratorUsageError, StartupError, UnresolvableParameterError
from lauren.validation import (
    Alias,
    Ge,
    Gt,
    Le,
    Lt,
    MaxLen,
    MinLen,
    MultipleOf,
    Pattern,
    Strict,
    TypeCompiler,
    ValidationFailed,
    ValidationPolicy,
    validate,
    validation_config,
)


class Color(enum.Enum):
    RED = "red"
    GREEN = "green"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


UID = "a2345678-1234-5678-1234-56781234abcd"


def _issue_types(tp: Any, value: Any, mode: str = "lax") -> list[str]:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(tp, mode=mode).validate(value)
    return [issue.type for issue in info.value.issues]


# ---------------------------------------------------------------------------
# The coercion table (PRD §5.2): type × input × mode → value
# ---------------------------------------------------------------------------

ACCEPTED = [
    (str, "x", "strict", "x"),
    (int, 12, "strict", 12),
    (int, "12", "lax", 12),
    (int, 12.0, "lax", 12),
    (float, 3, "strict", 3.0),
    (float, "1.5", "lax", 1.5),
    (bool, True, "strict", True),
    (bool, "on", "lax", True),
    (bool, "OFF", "lax", False),
    (bool, 0, "lax", False),
    (decimal.Decimal, "1.50", "strict", decimal.Decimal("1.50")),
    (decimal.Decimal, 2, "strict", decimal.Decimal(2)),
    (decimal.Decimal, 0.5, "lax", decimal.Decimal("0.5")),
    (uuid.UUID, UID, "strict", uuid.UUID(UID)),
    (uuid.UUID, "{" + UID.upper() + "}", "lax", uuid.UUID(UID)),
    (
        datetime.datetime,
        "2024-01-02T03:04:05Z",
        "strict",
        datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.UTC),
    ),
    (datetime.date, "2024-01-02", "strict", datetime.date(2024, 1, 2)),
    (datetime.time, "03:04", "strict", datetime.time(3, 4)),
    (datetime.timedelta, "PT5M", "strict", datetime.timedelta(minutes=5)),
    (datetime.timedelta, 90, "lax", datetime.timedelta(seconds=90)),
    (bytes, "hé", "strict", "hé".encode()),
    (complex, "1+2j", "lax", 1 + 2j),
    (pathlib.PurePosixPath, "a/b", "strict", pathlib.PurePosixPath("a/b")),
    (Color, "red", "strict", Color.RED),
    (Level, "2", "lax", Level.HIGH),
    (Level, 1, "strict", Level.LOW),
    (Literal["a", "b"], "a", "strict", "a"),
    (Literal[1, 2], "2", "lax", 2),
    (Literal[True], "true", "lax", True),
    (list[int], [1, "2"], "lax", [1, 2]),
    (tuple[int, ...], [1, 2], "strict", (1, 2)),
    (tuple[int, str], [1, "x"], "strict", (1, "x")),
    (set[int], [1, 1, 2], "strict", {1, 2}),
    (frozenset[str], ["a"], "strict", frozenset({"a"})),
    (dict[str, int], {"a": "1"}, "lax", {"a": 1}),
    (dict, {"a": [1]}, "strict", {"a": [1]}),
    (int | None, None, "strict", None),
    (int | str, "5", "lax", "5"),
    (int | str, 5, "lax", 5),
    (float | int, 5, "lax", 5),
    (Any, object, "strict", object),
    (typing.Sequence[int], [1], "strict", [1]),
]


@pytest.mark.parametrize(("tp", "value", "mode", "expected"), ACCEPTED)
def test_accepted_values_follow_the_coercion_table(tp: Any, value: Any, mode: str, expected: Any) -> None:
    result = TypeCompiler().compile(tp, mode=mode).validate(value)

    assert result == expected
    assert type(result) is type(expected)


REJECTED = [
    (str, 5, "lax", "string_type"),
    (int, True, "lax", "int_type"),
    (int, "12", "strict", "int_type"),
    (int, "abc", "lax", "int_parsing"),
    (int, 1.5, "lax", "int_from_float"),
    (int, float("inf"), "lax", "finite_number"),
    (float, "x", "lax", "float_parsing"),
    (float, "1.5", "strict", "float_type"),
    (bool, "maybe", "lax", "bool_parsing"),
    (bool, 2, "lax", "bool_parsing"),
    (bool, "true", "strict", "bool_type"),
    (decimal.Decimal, "x", "lax", "decimal_parsing"),
    (decimal.Decimal, "NaN", "lax", "finite_number"),
    (decimal.Decimal, 0.5, "strict", "decimal_type"),
    (uuid.UUID, "nope", "lax", "uuid_parsing"),
    (uuid.UUID, UID.upper(), "strict", "uuid_parsing"),
    (uuid.UUID, 5, "lax", "uuid_type"),
    (datetime.datetime, "nope", "lax", "datetime_parsing"),
    (datetime.datetime, 1_700_000_000, "lax", "datetime_type"),
    (datetime.date, "2024-13-01", "lax", "date_parsing"),
    (datetime.time, "25:00", "lax", "time_parsing"),
    (datetime.timedelta, "soon", "lax", "time_delta_parsing"),
    (datetime.timedelta, 90, "strict", "time_delta_type"),
    (bytes, 5, "lax", "bytes_type"),
    (complex, "x", "lax", "complex_str_parsing"),
    (complex, [1], "lax", "complex_type"),
    (pathlib.PurePosixPath, 5, "lax", "path_type"),
    (Color, "blue", "lax", "enum"),
    (Level, "2", "strict", "enum"),
    (Literal["a", "b"], "c", "lax", "literal_error"),
    (Literal[1], True, "lax", "literal_error"),
    (list[int], "x", "lax", "list_type"),
    (tuple[int, ...], "x", "lax", "tuple_type"),
    (set[int], {"a": 1}, "lax", "set_type"),
    (frozenset[int], "x", "lax", "frozen_set_type"),
    (dict[str, int], [1], "lax", "dict_type"),
    (type(None), 1, "lax", "none_required"),
]


@pytest.mark.parametrize(("tp", "value", "mode", "issue_type"), REJECTED)
def test_rejected_values_report_a_stable_issue_type(tp: Any, value: Any, mode: str, issue_type: str) -> None:
    assert _issue_types(tp, value, mode) == [issue_type]


def test_every_failing_list_item_is_reported_with_its_index() -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(list[int]).validate(["a", 2, "b"])

    assert [(i.loc, i.type) for i in info.value.issues] == [((0,), "int_parsing"), ((2,), "int_parsing")]


def test_dict_key_failures_are_located_under_the_key() -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(dict[int, int]).validate({"a": "x"})

    assert [i.loc for i in info.value.issues] == [("a", "[key]"), ("a",)]


def test_fixed_tuple_reports_missing_and_extra_positions() -> None:
    assert _issue_types(tuple[int, int], [1]) == ["missing"]
    assert _issue_types(tuple[int], [1, 2]) == ["too_long"]


def test_unhashable_set_items_are_rejected() -> None:
    assert _issue_types(set[Any], [[1]]) == ["set_item_not_hashable"]


def test_union_aggregates_every_member_issue() -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(int | bool).validate([1])

    assert [(i.loc, i.type) for i in info.value.issues] == [(("int",), "int_type"), (("bool",), "bool_type")]


def test_strict_union_only_tries_strict_members() -> None:
    assert _issue_types(int | float, "1", "strict") == ["int_type", "float_type"]


def test_strict_union_accepts_an_exact_match() -> None:
    assert TypeCompiler().compile(int | str, mode="strict").validate("5") == "5"


def test_lax_union_falls_back_to_lax_members() -> None:
    assert TypeCompiler().compile(int | datetime.date).validate("7") == 7


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Address:
    city: str
    zip: Annotated[str, Pattern(r"^\d{5}$")]


@dataclasses.dataclass
class User:
    name: str
    age: int
    address: Address | None = None
    tags: list[str] = dataclasses.field(default_factory=list)


def test_nested_dataclasses_are_real_instances() -> None:
    user = (
        TypeCompiler()
        .compile(User)
        .validate({"name": "a", "age": "3", "address": {"city": "P", "zip": "75001"}})
    )

    assert isinstance(user.address, Address)
    assert user.age == 3
    assert user.tags == []


def test_dataclass_reports_every_failing_field() -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(User).validate({"age": "x", "address": {"city": 1, "zip": "1"}})

    assert [(i.loc, i.type) for i in info.value.issues] == [
        (("name",), "missing"),
        (("age",), "int_parsing"),
        (("address", "city"), "string_type"),
        (("address", "zip"), "string_pattern_mismatch"),
    ]


def test_dataclass_instances_pass_through_unchanged() -> None:
    user = User(name="a", age=1)

    assert TypeCompiler().compile(User).validate(user) is user


def test_dataclass_rejects_non_mappings() -> None:
    assert _issue_types(User, [1]) == ["model_type"]


def test_unknown_keys_are_ignored_by_default() -> None:
    user = TypeCompiler().compile(User).validate({"name": "a", "age": 1, "admin": True})

    assert not hasattr(user, "admin")


@validation_config(extra="forbid")
@dataclasses.dataclass
class Closed:
    name: str
    computed: str = dataclasses.field(init=False, default="x")


def test_extra_forbid_reports_each_unknown_key() -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(Closed).validate({"name": "a", "admin": True, "computed": "y"})

    assert [(i.loc, i.type) for i in info.value.issues] == [(("admin",), "extra_forbidden")]


def test_policy_extra_forbid_applies_to_undecorated_models() -> None:
    compiler = TypeCompiler(ValidationPolicy(extra="forbid"))

    with pytest.raises(ValidationFailed) as info:
        compiler.compile(User).validate({"name": "a", "age": 1, "x": 1})

    assert info.value.issues[0].type == "extra_forbidden"


@dataclasses.dataclass
class Guarded:
    tags: list[str]

    def __post_init__(self) -> None:
        if "admin" in self.tags:
            raise ValueError("tag 'admin' is reserved")


@pytest.mark.parametrize("error", [ValueError, TypeError, AssertionError])
def test_post_init_errors_become_value_error_issues(error: type[Exception]) -> None:
    @dataclasses.dataclass
    class Model:
        x: int

        def __post_init__(self) -> None:
            raise error("nope")

    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(Model).validate({"x": 1}, loc=("body",))

    assert info.value.issues[0].loc == ("body",)
    assert info.value.issues[0].type == "value_error"
    assert info.value.issues[0].msg == "nope"


def test_post_init_validation_failures_keep_their_issues() -> None:
    inner = TypeCompiler().compile(int)

    @dataclasses.dataclass
    class Model:
        raw: str

        def __post_init__(self) -> None:
            inner.validate(self.raw, loc=("raw",))

    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(Model).validate({"raw": "x"})

    assert [(i.loc, i.type) for i in info.value.issues] == [(("raw",), "int_parsing")]


def test_post_init_runs_on_valid_input() -> None:
    assert _issue_types(Guarded, {"tags": ["admin"]}) == ["value_error"]
    assert TypeCompiler().compile(Guarded).validate({"tags": ["x"]}).tags == ["x"]


def test_initvar_and_kw_only_and_frozen_dataclasses() -> None:
    @dataclasses.dataclass(frozen=True, kw_only=True)
    class Model:
        seed: dataclasses.InitVar[int]
        value: int = 0

        def __post_init__(self, seed: int) -> None:
            object.__setattr__(self, "value", seed * 2)

    result = TypeCompiler().compile(Model).validate({"seed": "4"})

    assert result.value == 8


@dataclasses.dataclass
class Tree:
    value: int
    children: list[Tree] = dataclasses.field(default_factory=list)


def test_recursive_dataclasses_validate_at_any_depth() -> None:
    compiled = TypeCompiler().compile(Tree)

    tree = compiled.validate({"value": 1, "children": [{"value": 2, "children": [{"value": "3"}]}]})

    assert tree.children[0].children[0].value == 3
    with pytest.raises(ValidationFailed) as info:
        compiled.validate({"value": 1, "children": [{"value": 2}, {"value": "x"}]})
    assert info.value.issues[0].loc == ("children", 1, "value")


class Movie(TypedDict, total=False):
    title: Required[str]
    year: int


class Strict2(TypedDict):
    name: str
    nick: NotRequired[str]


def test_typeddict_total_false_and_required() -> None:
    compiled = TypeCompiler().compile(Movie)

    assert compiled.validate({"title": "x", "year": "1999", "extra": 1}) == {"title": "x", "year": 1999}
    assert _issue_types(Movie, {"year": 1}) == ["missing"]


def test_typeddict_not_required() -> None:
    assert TypeCompiler().compile(Strict2).validate({"name": "a"}) == {"name": "a"}


class Point(NamedTuple):
    x: int
    y: int = 0


def test_namedtuple_accepts_objects_and_arrays() -> None:
    compiled = TypeCompiler().compile(Point)

    assert compiled.validate({"x": "1"}) == Point(1, 0)
    assert compiled.validate(["1", 2]) == Point(1, 2)
    assert compiled.validate(Point(3, 4)) == Point(3, 4)
    assert _issue_types(Point, [1, 2, 3]) == ["too_long"]


@validation_config(rename="camel")
@dataclasses.dataclass
class Profile:
    first_name: str
    last_login_at: Annotated[str, Alias("lastSeen")] = ""
    _internal: int = 0


def test_rename_camel_and_alias_set_the_wire_names() -> None:
    compiled = TypeCompiler().compile(Profile)

    profile = compiled.validate({"firstName": "Ada", "lastSeen": "now", "_internal": 1})

    assert (profile.first_name, profile.last_login_at, profile._internal) == ("Ada", "now", 1)
    assert compiled.project(profile) == {"firstName": "Ada", "lastSeen": "now", "_internal": 1}
    assert _issue_types(Profile, {"first_name": "Ada"}) == ["missing"]


@validation_config(strict=True)
@dataclasses.dataclass
class StrictModel:
    count: int


def test_validation_config_strict_disables_coercion() -> None:
    assert _issue_types(StrictModel, {"count": "1"}) == ["int_type"]


def test_strict_marker_forces_one_field_strict() -> None:
    @dataclasses.dataclass
    class Model:
        a: Annotated[int, Strict()]
        b: int

    assert _issue_types(Model, {"a": "1", "b": "1"}) == ["int_type"]


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------

CONSTRAINTS = [
    (Annotated[int, Ge(2)], 1, "greater_than_equal", {"ge": 2}),
    (Annotated[int, Gt(2)], 2, "greater_than", {"gt": 2}),
    (Annotated[float, Le(2)], 2.5, "less_than_equal", {"le": 2}),
    (Annotated[int, Lt(2)], 2, "less_than", {"lt": 2}),
    (Annotated[int, MultipleOf(3)], 4, "multiple_of", {"multiple_of": 3}),
    (Annotated[float, MultipleOf(0.5)], 0.7, "multiple_of", {"multiple_of": 0.5}),
    (Annotated[decimal.Decimal, MultipleOf(2)], decimal.Decimal(3), "multiple_of", {"multiple_of": 2}),
    (Annotated[str, MinLen(2)], "a", "string_too_short", {"min_length": 2}),
    (Annotated[str, MaxLen(1)], "ab", "string_too_long", {"max_length": 1}),
    (Annotated[bytes, MaxLen(1)], b"ab", "bytes_too_long", {"max_length": 1}),
    (Annotated[bytes, MinLen(3)], b"ab", "bytes_too_short", {"min_length": 3}),
    (
        Annotated[list[int], MinLen(2)],
        [1],
        "too_short",
        {"field_type": "List", "min_length": 2, "actual_length": 1},
    ),
    (
        Annotated[dict[str, int], MaxLen(0)],
        {"a": 1},
        "too_long",
        {"field_type": "Dictionary", "max_length": 0, "actual_length": 1},
    ),
    (Annotated[str, Pattern(r"\d")], "abc", "string_pattern_mismatch", {"pattern": r"\d"}),
]


@pytest.mark.parametrize(("tp", "value", "issue_type", "ctx"), CONSTRAINTS)
def test_constraints_report_their_bound_in_ctx(
    tp: Any, value: Any, issue_type: str, ctx: dict[str, Any]
) -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(tp).validate(value)

    assert info.value.issues[0].type == issue_type
    assert dict(info.value.issues[0].ctx or {}) == ctx


def test_pattern_uses_search_semantics() -> None:
    assert TypeCompiler().compile(Annotated[str, Pattern(r"\d")]).validate("a1b") == "a1b"


def test_constraints_on_optional_skip_none() -> None:
    compiled = TypeCompiler().compile(Annotated[int | None, Ge(1)])

    assert compiled.validate(None) is None
    assert _issue_types(Annotated[int | None, Ge(1)], 0) == ["greater_than_equal"]


def test_duck_typed_annotated_types_metadata_is_honoured() -> None:
    annotated_types = pytest.importorskip("annotated_types")

    compiled = TypeCompiler().compile(Annotated[int, annotated_types.Interval(ge=1, lt=5)])

    assert compiled.validate(3) == 3
    assert _issue_types(Annotated[int, annotated_types.Interval(ge=1, lt=5)], 5) == ["less_than"]
    assert _issue_types(Annotated[str, annotated_types.Len(2, 3)], "a") == ["string_too_short"]


@pytest.mark.parametrize(
    ("tp", "reason"),
    [
        (Annotated[str, Ge(1)], "bad_constraint"),
        (Annotated[int, MinLen(1)], "bad_constraint"),
        (Annotated[int, Pattern("x")], "bad_constraint"),
        (Annotated[str, Pattern("(")], "bad_pattern"),
        (Annotated[str, MinLen(5), MaxLen(2)], "bad_constraint"),
        (Annotated[int, Ge(5), Le(2)], "bad_constraint"),
        (Annotated[int, Ge("x")], "bad_constraint"),
        (Annotated[str, MinLen(-1)], "bad_constraint"),
        (Annotated[int, MultipleOf(0)], "bad_constraint"),
        (Annotated[int | str, Ge(1)], "bad_constraint"),
        (typing.Callable[[], int], "unsupported_type"),
        (object(), "unsupported_type"),
        ("Undefined", "unsupported_type"),
    ],
)
def test_compile_errors_raise_unresolvable_parameter_error(tp: Any, reason: str) -> None:
    with pytest.raises(UnresolvableParameterError) as info:
        TypeCompiler().compile(tp)

    assert info.value.detail["reason"] == reason
    assert "type" in info.value.detail


def test_compile_errors_name_the_failing_model_field() -> None:
    @dataclasses.dataclass
    class Bad:
        handler: typing.Callable[[], int]

    with pytest.raises(UnresolvableParameterError) as info:
        TypeCompiler().compile(Bad)

    assert info.value.detail["field"] == "Bad.handler"


def test_unsupported_init_false_fields_are_projected_as_is() -> None:
    @dataclasses.dataclass
    class Model:
        x: int
        cb: typing.Callable[[], int] = dataclasses.field(init=False, default=print)

    assert TypeCompiler().compile(Model).validate({"x": 1}).x == 1


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_compiler_caches_per_type() -> None:
    compiler = TypeCompiler()

    assert compiler.compile(User).node is compiler.compile(User).node


def test_two_compilers_share_nothing() -> None:
    assert TypeCompiler().compile(User).node is not TypeCompiler().compile(User).node


def test_validate_helper_compiles_and_validates() -> None:
    assert validate(int, "3") == 3
    with pytest.raises(ValidationFailed):
        validate(int, "3", mode="strict")


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        TypeCompiler().compile(int, mode="loose")


def test_policy_type_is_checked() -> None:
    with pytest.raises(TypeError):
        TypeCompiler(policy="strict")  # type: ignore[arg-type]


def test_max_issues_caps_the_issue_list() -> None:
    compiled = TypeCompiler(ValidationPolicy(max_issues=3)).compile(list[int])

    with pytest.raises(ValidationFailed) as info:
        compiled.validate(["x"] * 10)

    assert len(info.value.issues) == 3


def test_validation_failed_summarises_its_issues() -> None:
    with pytest.raises(ValidationFailed) as info:
        TypeCompiler().compile(list[int]).validate(["x"] * 7)

    text = str(info.value)
    assert text.startswith("7 validation errors: 0: Input should be a valid integer")
    assert "(+2 more)" in text
    assert info.value.errors()[0] == {
        "loc": [0],
        "type": "int_parsing",
        "msg": "Input should be a valid integer, unable to parse string as an integer",
    }
    assert info.value.errors(include_input=True)[0]["input"] == "x"


def test_compiled_type_reports_explicit_metadata() -> None:
    assert TypeCompiler().compile(Closed).explicit is True
    assert TypeCompiler().compile(User).explicit is True  # Address uses Pattern
    assert TypeCompiler().compile(Point).explicit is False
    assert repr(TypeCompiler().compile(list[int])) == "CompiledType(list[int])"


def test_newtype_and_type_alias_compile_to_their_target() -> None:
    UserId = typing.NewType("UserId", int)

    assert TypeCompiler().compile(UserId).validate("4") == 4


@pytest.mark.parametrize(
    ("kwargs", "field"),
    [
        ({"stdlib_models": "always"}, "stdlib_models"),
        ({"json_mode": "loose"}, "json_mode"),
        ({"extra": "allow"}, "extra"),
        ({"include_input": "yes"}, "include_input"),
        ({"max_issues": 0}, "max_issues"),
        ({"max_issues": True}, "max_issues"),
    ],
)
def test_invalid_policy_values_raise_startup_error(kwargs: dict[str, Any], field: str) -> None:
    with pytest.raises(StartupError) as info:
        ValidationPolicy(**kwargs)

    assert info.value.detail == {
        "reason": "invalid_validation_policy",
        "field": field,
        "got": repr(kwargs[field]),
    }


def test_validation_config_rejects_bare_usage() -> None:
    with pytest.raises(DecoratorUsageError):

        @validation_config  # type: ignore[arg-type]
        @dataclasses.dataclass
        class Model:
            x: int


@pytest.mark.parametrize("kwargs", [{"extra": "allow"}, {"strict": "yes"}, {"rename": "kebab"}])
def test_validation_config_rejects_invalid_values(kwargs: dict[str, Any]) -> None:
    with pytest.raises(DecoratorUsageError):
        validation_config(**kwargs)


def test_validation_config_rejects_positional_values_and_non_classes() -> None:
    with pytest.raises(DecoratorUsageError):
        validation_config("forbid")
    with pytest.raises(DecoratorUsageError):
        validation_config(extra="forbid")(lambda: None)


def test_validation_config_stacking_merges() -> None:
    @validation_config(strict=True)
    @validation_config(extra="forbid")
    @dataclasses.dataclass
    class Model:
        x: int

    config = Model.__dict__["__lauren_validation__"]
    assert (config.extra, config.strict) == ("forbid", True)


def test_validation_config_is_not_inherited() -> None:
    @dataclasses.dataclass
    class Child(Closed):
        pass

    assert TypeCompiler().compile(Child).validate({"name": "a", "admin": True}).name == "a"


# ---------------------------------------------------------------------------
# Less common annotations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tp", "name"),
    [
        (None, "None"),
        (Any, "Any"),
        (typing.ForwardRef("User"), "User"),
        (Literal["a", 1], "Literal['a', 1]"),
        (typing.List, "list"),
        (dict[str, int], "dict[str, int]"),
        (tuple[int, ...], "tuple[int, ...]"),
        (int | None, "int | None"),
        (Annotated[int, Ge(1)], "int"),
    ],
)
def test_type_names(tp: Any, name: str) -> None:
    from lauren._schema._compiler import type_name

    assert type_name(tp) == name


def test_pydantic_field_metadata_is_honoured_on_stdlib_models() -> None:
    pydantic = pytest.importorskip("pydantic")

    @dataclasses.dataclass
    class Model:
        n: Annotated[int, pydantic.Field(ge=1, alias="num")]
        s: Annotated[int, pydantic.Strict()] = 0

    compiled = TypeCompiler().compile(Model)

    assert compiled.validate({"num": "2"}).n == 2
    assert _issue_types(Model, {"num": 0}) == ["greater_than_equal"]
    assert _issue_types(Model, {"num": 1, "s": "1"}) == ["int_type"]


def test_pydantic_dataclasses_use_pydantic() -> None:
    pydantic = pytest.importorskip("pydantic")

    @pydantic.dataclasses.dataclass
    class Model:
        x: int

    compiled = TypeCompiler().compile(Model)

    assert compiled.validate({"x": "3"}).x == 3
    assert compiled.project(Model(x=3)) == {"x": 3}
    assert _issue_types(Model, {"x": "a"}) == ["int_parsing"]


def test_initvar_and_empty_tuples_compile() -> None:
    assert TypeCompiler().compile(dataclasses.InitVar[int]).validate("1") == 1
    assert TypeCompiler().compile(tuple[()]).validate([]) == ()


def test_classvars_are_not_fields() -> None:
    @dataclasses.dataclass
    class Model:
        limit: typing.ClassVar[int] = 3
        x: int = 0

    assert TypeCompiler().compile(Model).validate({"limit": "x"}).x == 0


def test_invalid_field_descriptor_patterns_are_dropped_in_soft_mode() -> None:
    from lauren.extractors import FieldDescriptor

    compiler = TypeCompiler()

    compiled = compiler.compile(Annotated[str, FieldDescriptor(pattern="(")])

    assert compiled.validate("anything") == "anything"
    assert any("invalid FieldDescriptor pattern" in w for w in compiler.warnings)


def test_field_descriptor_problems_raise_under_enforce() -> None:
    from lauren.extractors import FieldDescriptor

    compiler = TypeCompiler(ValidationPolicy(stdlib_models="enforce"))

    with pytest.raises(UnresolvableParameterError):
        compiler.compile(Annotated[list[str], FieldDescriptor(max_length=2)])


def test_field_descriptor_patterns_use_full_match() -> None:
    from lauren.extractors import FieldDescriptor

    compiled = TypeCompiler().compile(Annotated[str, FieldDescriptor(pattern=r"\d+")])

    assert compiled.validate("12") == "12"
    assert _issue_types(Annotated[str, FieldDescriptor(pattern=r"\d+")], "12a") == ["string_pattern_mismatch"]
    assert compiled.json_schema() == {"type": "string", "pattern": r"^(?:\d+)$"}


def test_typeddict_required_inside_annotated() -> None:
    class Movie(TypedDict, total=False):
        title: Annotated[Required[str], MaxLen(5)]
        year: Required[Annotated[int, Ge(1900)]]

    assert _issue_types(Movie, {}) == ["missing", "missing"]
    assert _issue_types(Movie, {"title": "toolong", "year": 1800}) == [
        "string_too_long",
        "greater_than_equal",
    ]


def test_strict_marked_union_members_keep_labels() -> None:
    compiled = TypeCompiler().compile(Annotated[int, Strict()] | None)

    assert compiled.validate(None) is None
    assert compiled.node.inner.label == "int"
    assert compiled.node.inner.is_instance(3)
    assert compiled.project(3) == 3
