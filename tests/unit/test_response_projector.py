"""Unit: response projectors built over compiled validator nodes."""

from __future__ import annotations

import dataclasses
import datetime
from typing import Annotated, Any, ClassVar, Literal, NamedTuple, NotRequired, TypedDict

import pytest

from lauren import Discriminated
from lauren._schema._contract import build_projector
from lauren._schema._issues import _Invalid
from lauren.validation import Alias, Ge, MaxLen, TypeCompiler, validation_config


def _projector(tp: Any, *, strict: bool = False, limit: int = 50) -> Any:
    mode = "strict" if strict else "lax"
    return build_projector(TypeCompiler().compile(tp, mode=mode).node, strict=strict, limit=limit)


def _issues(tp: Any, value: Any, *, strict: bool = False, limit: int = 50) -> list[tuple[Any, str]]:
    with pytest.raises(_Invalid) as info:
        _projector(tp, strict=strict, limit=limit)(value)
    return [(issue.loc, issue.type) for issue in info.value.issues]


@dataclasses.dataclass
class User:
    id: int
    name: str


@dataclasses.dataclass
class UserInDB(User):
    password_hash: str


@dataclasses.dataclass
class WithDefault:
    id: int
    role: str = "member"


@validation_config(rename="camel")
@dataclasses.dataclass
class Camel:
    first_name: str
    nick: Annotated[str, Alias("handle")]


class Row:
    """An ORM-like object: plain attributes, more than the DTO declares."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


class Movie(TypedDict):
    title: str
    year: NotRequired[int]


class Point(NamedTuple):
    x: int
    y: int


@dataclasses.dataclass
class Tree:
    value: int
    children: list[Tree] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"]
    lives: int


@dataclasses.dataclass
class Dog:
    kind: ClassVar[str] = "dog"  # the tag is not a field: projection re-adds it
    good: bool = True


Pet = Discriminated[Cat | Dog, "kind"]  # noqa: F821


# -- models ------------------------------------------------------------------


@pytest.mark.parametrize("strict", [False, True])
def test_subclass_instance_loses_undeclared_fields(strict: bool) -> None:
    out = _projector(User, strict=strict)(UserInDB(1, "ada", "bcrypt$x"))

    assert out == {"id": 1, "name": "ada"}


def test_mapping_extra_keys_are_dropped() -> None:
    assert _projector(User)({"id": 1, "name": "a", "secret": "s"}) == {"id": 1, "name": "a"}


def test_orm_object_attributes_are_read_for_declared_fields_only() -> None:
    assert _projector(User)(Row(id=2, name="b", api_key="sk")) == {"id": 2, "name": "b"}


def test_missing_required_field_is_a_violation() -> None:
    assert _issues(User, {"id": 1}) == [(("name",), "missing")]


def test_missing_optional_attribute_emits_the_default() -> None:
    assert _projector(WithDefault)(Row(id=1)) == {"id": 1, "role": "member"}


def test_fields_are_emitted_under_wire_names() -> None:
    out = _projector(Camel)(Camel(first_name="Ada", nick="ada"))

    assert out == {"firstName": "Ada", "handle": "ada"}


def test_mapping_values_may_use_python_or_wire_names() -> None:
    out = _projector(Camel)({"first_name": "Ada", "handle": "a"})

    assert out == {"firstName": "Ada", "handle": "a"}


@pytest.mark.parametrize("value", [None, 3, "text", [1, 2], b"x"])
def test_scalars_and_lists_cannot_stand_for_a_model(value: Any) -> None:
    assert _issues(User, value) == [((), "model_type")]


def test_typed_dict_omits_absent_not_required_keys() -> None:
    assert _projector(Movie)({"title": "Up", "rating": 5}) == {"title": "Up"}


def test_named_tuple_projects_from_instance_and_plain_tuple() -> None:
    project = _projector(Point)

    assert (project(Point(1, 2)), project((3, 4))) == ({"x": 1, "y": 2}, {"x": 3, "y": 4})


def test_recursive_types_project_every_level() -> None:
    tree = {"value": 1, "extra": 0, "children": [{"value": 2, "children": [], "x": 1}]}

    assert _projector(Tree)(tree) == {"value": 1, "children": [{"value": 2, "children": []}]}


def test_nested_issue_locations_are_paths() -> None:
    assert _issues(list[User], [{"id": 1, "name": "a"}, {"id": 2}]) == [((1, "name"), "missing")]


def test_issue_collection_stops_at_the_limit() -> None:
    issues = _issues(list[User], [{}, {}, {}], limit=3)

    assert 3 <= len(issues) < 6  # the item that crosses the limit is the last one read


@dataclasses.dataclass
class Wide:
    a: int
    b: int
    c: int
    d: int


def test_model_issue_collection_stops_at_the_limit() -> None:
    assert len(_issues(Wide, {}, limit=2)) == 2


# -- containers --------------------------------------------------------------


def test_sequences_accept_any_iterable_and_return_lists() -> None:
    project = _projector(list[int])

    assert (project((1, 2)), project({3}), project(iter([4]))) == ([1, 2], [3], [4])


@pytest.mark.parametrize("value", ["abc", {"a": 1}, 5, None])
def test_non_sequences_are_rejected_for_lists(value: Any) -> None:
    assert _issues(list[int], value) == [((), "list_type")]


def test_set_of_models_projects_items() -> None:
    assert _projector(frozenset[int])(frozenset({1})) == [1]


def test_fixed_tuple_drops_extra_items_in_filter_mode() -> None:
    assert _projector(tuple[int, str])((1, "a", "extra")) == [1, "a"]


def test_fixed_tuple_reports_extra_items_in_strict_mode() -> None:
    assert _issues(tuple[int, str], (1, "a", "extra"), strict=True) == [((), "too_long")]


def test_fixed_tuple_reports_missing_positions() -> None:
    assert _issues(tuple[int, str], (1,)) == [((1,), "missing")]


def test_fixed_tuple_reports_item_issues() -> None:
    assert _issues(tuple[User, int], ({"id": 1}, 2)) == [((0, "name"), "missing")]


def test_fixed_tuple_requires_a_sequence() -> None:
    assert _issues(tuple[int, str], 3) == [((), "tuple_type")]


def test_dict_values_are_projected() -> None:
    out = _projector(dict[str, User])({"a": UserInDB(1, "x", "h")})

    assert out == {"a": {"id": 1, "name": "x"}}


def test_dict_requires_a_mapping() -> None:
    assert _issues(dict[str, User], [1]) == [((), "dict_type")]


def test_untyped_dict_is_copied_as_is() -> None:
    assert _projector(dict)({"a": object}) == {"a": object}


def test_strict_dict_validates_keys() -> None:
    assert _issues(dict[int, str], {"x": "a"}, strict=True) == [(("x", "[key]"), "int_type")]


def test_dict_issue_collection_stops_at_the_limit() -> None:
    assert len(_issues(dict[str, User], {"a": {}, "b": {}, "c": {}}, limit=2)) == 2


def test_strict_dict_reports_value_issues() -> None:
    assert _issues(dict[str, int], {"a": "x"}, strict=True) == [(("a",), "int_type")]


# -- leaves, optionals and constraints ---------------------------------------


def test_filter_mode_passes_wrong_leaf_types_through() -> None:
    assert _projector(User)({"id": "not-an-int", "name": 3}) == {"id": "not-an-int", "name": 3}


def test_strict_mode_reports_wrong_leaf_types() -> None:
    assert _issues(User, {"id": "x", "name": "a"}, strict=True) == [(("id",), "int_type")]


def test_strict_mode_keeps_the_original_leaf_value() -> None:
    when = datetime.date(2024, 1, 2)

    assert _projector(datetime.date, strict=True)(when) is when


def test_optional_none_stays_none() -> None:
    assert _projector(User | None)(None) is None


def test_optional_leaf_passes_through_in_filter_mode() -> None:
    assert _projector(int | None)("not-an-int") == "not-an-int"


def test_optional_model_is_projected() -> None:
    assert _projector(User | None)(UserInDB(1, "a", "h")) == {"id": 1, "name": "a"}


def test_strict_mode_runs_constraint_checks() -> None:
    assert _issues(Annotated[list[int], MaxLen(1)], [1, 2], strict=True) == [((), "too_long")]


def test_strict_mode_checks_bounds() -> None:
    assert _issues(Annotated[int, Ge(0)], -1, strict=True) == [((), "greater_than_equal")]


def test_strict_mode_passes_values_within_constraints() -> None:
    assert _projector(Annotated[list[int], MaxLen(2)], strict=True)((1, 2)) == [1, 2]


def test_filter_mode_skips_constraint_checks() -> None:
    assert _projector(Annotated[int, Ge(0)])(-1) == -1


# -- unions ------------------------------------------------------------------


def test_all_leaf_union_passes_values_through_in_filter_mode() -> None:
    assert _projector(int | str)(1.5) == 1.5


def test_union_picks_the_member_the_value_is_an_instance_of() -> None:
    out = _projector(User | WithDefault)(UserInDB(1, "a", "h"))

    assert out == {"id": 1, "name": "a"}


def test_union_tries_structured_members_for_mappings() -> None:
    out = _projector(Point | User)({"id": 1, "name": "a", "x": None})

    assert out == {"id": 1, "name": "a"}


def test_leaf_member_does_not_swallow_an_unprojectable_value() -> None:
    issues = _issues(User | str, {"id": 1})

    assert issues == [(("User", "name"), "missing")]


def test_union_falls_back_past_a_matching_member_that_failed() -> None:
    out = _projector(Movie | User)({"id": 1, "name": "a"})

    assert out == {"id": 1, "name": "a"}


def test_union_reports_every_structured_member_that_failed() -> None:
    issues = _issues(User | Point, {"other": 1})

    assert {loc[0] for loc, _ in issues} == {"User", "Point"}


def test_any_member_lets_unmatched_values_through() -> None:
    assert _projector(User | Any)({"only": 1}) == {"only": 1}


def test_strict_union_reports_every_member() -> None:
    issues = _issues(int | bool, "x", strict=True)

    assert {loc[0] for loc, _ in issues} == {"int", "bool"}


def test_union_with_no_match_and_no_issues_reports_union_no_match() -> None:
    project = _projector(int | str, strict=True)

    with pytest.raises(_Invalid):
        project(object())


def test_discriminated_union_projects_instances() -> None:
    assert _projector(Pet)(Cat("cat", 9)) == {"kind": "cat", "lives": 9}


def test_discriminated_union_readds_an_undeclared_tag() -> None:
    assert _projector(Pet)(Dog(True)) == {"kind": "dog", "good": True}


def test_discriminated_union_routes_mappings_by_tag() -> None:
    out = _projector(Pet)({"kind": "cat", "lives": 3, "secret": 1})

    assert out == {"kind": "cat", "lives": 3}


@pytest.mark.parametrize(
    ("value", "issue"),
    [
        ({"lives": 3}, "union_tag_not_found"),
        ({"kind": "cow"}, "union_tag_invalid"),
        (3, "model_type"),
    ],
)
def test_discriminated_union_rejects_untagged_values(value: Any, issue: str) -> None:
    assert _issues(Pet, value) == [((), issue)]


def test_discriminated_variant_issues_are_prefixed_with_the_tag() -> None:
    assert _issues(Pet, {"kind": "cat"}) == [(("cat", "lives"), "missing")]


# -- pydantic ----------------------------------------------------------------


def test_pydantic_subclass_instance_is_filtered() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        id: int

    class InDB(Out):
        secret: str

    assert _projector(Out)(InDB(id=1, secret="s")) == {"id": 1}


def test_pydantic_orm_object_is_read_by_field_name() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        id: int
        name: str = pydantic.Field(alias="userName")

    assert _projector(Out)(Row(id=1, name="a", secret="s")) == {"id": 1, "userName": "a"}


def test_pydantic_mapping_that_cannot_convert_is_a_violation() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        id: int

    assert _issues(Out, {"nope": 1}) == [(("id",), "missing")]


@pytest.mark.parametrize("strict", [False, True])
def test_pydantic_valid_instance_passes(strict: bool) -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        when: datetime.date

    assert _projector(Out, strict=strict)(Out(when=datetime.date(2024, 1, 2))) == {"when": "2024-01-02"}


def test_pydantic_strict_catches_a_constructed_bad_leaf() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        id: int

    issues = _issues(Out, Out.model_construct(id="x"), strict=True)

    assert issues == [(("id",), "int_parsing")]


def test_pydantic_strict_reports_a_serializer_mismatch_validation_accepts() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        id: int

    issues = _issues(Out, Out.model_construct(id="5"), strict=True)

    assert issues == [((), "value_error")]


def test_pydantic_filter_passes_a_constructed_bad_leaf() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Out(pydantic.BaseModel):
        id: int

    assert _projector(Out)(Out.model_construct(id="x")) == {"id": "x"}


def test_pydantic_dataclass_goes_through_its_adapter() -> None:
    pydantic = pytest.importorskip("pydantic")

    @pydantic.dataclasses.dataclass
    class Out:
        id: int

    assert _projector(Out)({"id": 4, "other": 1}) == {"id": 4}


# -- msgspec -----------------------------------------------------------------


def test_msgspec_subclass_struct_is_filtered() -> None:
    msgspec = pytest.importorskip("msgspec")

    class Out(msgspec.Struct):
        id: int

    class InDB(Out):
        secret: str = "s"

    assert _projector(Out)(InDB(id=1)) == {"id": 1}


def test_msgspec_exact_instance_and_orm_object() -> None:
    msgspec = pytest.importorskip("msgspec")

    class Out(msgspec.Struct):
        id: int

    project = _projector(Out)

    assert (project(Out(id=1)), project(Row(id=2, x=1))) == ({"id": 1}, {"id": 2})


def test_msgspec_strict_catches_a_bad_leaf() -> None:
    msgspec = pytest.importorskip("msgspec")

    class Out(msgspec.Struct):
        id: int

    assert _issues(Out, Out(id="x"), strict=True) == [(("id",), "int_type")]


def test_msgspec_unconvertible_mapping_is_a_violation() -> None:
    msgspec = pytest.importorskip("msgspec")

    class Out(msgspec.Struct):
        id: int

    assert _issues(Out, {"nope": 1}) == [(("id",), "missing")]
