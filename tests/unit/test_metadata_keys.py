"""Typed metadata keys: ``MetadataKey[T]`` / ``MetadataSetKey[T]`` (F23).

Covers decoration-time stacking, the cross-level merge policies the factory
applies, ``validate=True``, name collisions, bare-usage rejection, typed
reads and interoperability with legacy string keys.
"""

from __future__ import annotations

from typing import Any, Callable

import pytest

from lauren import DecoratorUsageError, MetadataKey, MetadataSetKey, set_metadata
from lauren._metadata import (
    METADATA_KEYS,
    SET_METADATA,
    merge_metadata_levels,
    own_metadata,
    read_metadata,
    stack_metadata_dicts,
)
from lauren.types import ExecutionContext, Request


def _levels(*targets: Any) -> Any:
    return merge_metadata_levels([(f"level{i}", *own_metadata(t)) for i, t in enumerate(targets)])


def _ctx(metadata: Any) -> ExecutionContext:
    return ExecutionContext(request=Request(method="GET", path="/"), metadata=metadata)


# ---------------------------------------------------------------------------
# Declaration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["", "   ", 3, None])
def test_key_name_must_be_a_non_empty_string(name: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        MetadataSetKey(name)

    assert info.value.detail["reason"] == "invalid_metadata_key"


@pytest.mark.parametrize(
    ("factory", "merge"),
    [
        (lambda m: MetadataKey("k", default=0, merge=m), "union"),
        (lambda m: MetadataSetKey("k", merge=m), "outer"),
        (lambda m: MetadataSetKey("k", merge=m), "error"),
    ],
)
def test_unknown_merge_policy_is_rejected(factory: Callable[[Any], Any], merge: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        factory(merge)

    assert info.value.detail["reason"] == "invalid_merge_policy"


def test_key_records_its_definition_site_and_is_immutable() -> None:
    key = MetadataKey[int]("app.limit", default=10)

    assert key.site.startswith(__name__ + ":")
    with pytest.raises(AttributeError):
        key.name = "other"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        del key.default


def test_key_repr_names_the_kind_and_name() -> None:
    assert repr(MetadataSetKey[str]("app.roles")) == "MetadataSetKey('app.roles')"
    assert repr(MetadataKey[bool]("app.public", default=False)) == "MetadataKey('app.public')"


# ---------------------------------------------------------------------------
# Same-target stacking
# ---------------------------------------------------------------------------


def test_set_key_items_accumulate_deduplicated_in_application_order() -> None:
    roles = MetadataSetKey[str]("app.roles")

    @roles("admin")
    @roles("auditor", "admin", "auditor")
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {"app.roles": ("auditor", "admin")}
    assert getattr(handler, METADATA_KEYS) == {"app.roles": roles}


def test_scalar_key_equal_values_stack() -> None:
    public = MetadataKey[bool]("app.public", default=False)

    @public(True)
    @public(True)
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {"app.public": True}


def test_scalar_key_conflicting_values_fail_at_decoration_time() -> None:
    public = MetadataKey[bool]("app.public", default=False)

    with pytest.raises(DecoratorUsageError) as info:

        @public(True)
        @public(False)
        def handler() -> None: ...

    assert info.value.detail["key"] == "app.public"
    assert info.value.detail["reason"] == "metadata_conflict"
    assert info.value.detail["target"].endswith("handler")


def test_two_key_objects_with_one_name_collide_on_one_target() -> None:
    first = MetadataSetKey[str]("app.roles")
    second = MetadataSetKey[str]("app.roles")

    with pytest.raises(DecoratorUsageError) as info:

        @first("a")
        @second("b")
        def handler() -> None: ...

    assert info.value.detail["reason"] == "metadata_key_collision"
    assert info.value.detail["definitions"] == [second.site, first.site]


def test_string_write_onto_a_typed_name_follows_the_key() -> None:
    roles = MetadataSetKey[str]("app.roles")

    @set_metadata("app.roles", ["auditor"])
    @roles("admin")
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {"app.roles": ("admin", "auditor")}


def test_typed_key_onto_an_earlier_string_value_normalizes_it() -> None:
    roles = MetadataSetKey[str]("app.roles")

    @roles("admin")
    @set_metadata("app.roles", ["auditor"])
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {"app.roles": ("auditor", "admin")}


def test_legacy_string_keys_still_overwrite() -> None:
    @set_metadata("tier", "gold")
    @set_metadata("tier", "silver")
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {"tier": "gold"}
    assert not hasattr(handler, METADATA_KEYS)


@pytest.mark.parametrize(
    ("key", "value", "expected"),
    [
        (MetadataSetKey[str]("app.roles"), ("admin", "auditor"), ("admin", "auditor")),
        (MetadataSetKey[str]("app.roles"), ["admin"], ("admin",)),
        (MetadataKey[int]("app.limit", default=0), 5, 5),
    ],
)
def test_set_metadata_accepts_typed_keys(key: Any, value: Any, expected: Any) -> None:
    @set_metadata(key, value)
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {key.name: expected}
    assert getattr(handler, METADATA_KEYS) == {key.name: key}


@pytest.mark.parametrize("value", ["admin", 3, None])
def test_set_metadata_with_a_set_key_needs_a_collection(value: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        set_metadata(MetadataSetKey[str]("app.roles"), value)

    assert info.value.detail["reason"] == "invalid_metadata_value"


# ---------------------------------------------------------------------------
# Bare usage and validate=True
# ---------------------------------------------------------------------------


def test_bare_scalar_key_is_rejected() -> None:
    public = MetadataKey[bool]("app.public", default=False)

    with pytest.raises(DecoratorUsageError) as info:

        @public  # type: ignore[arg-type]
        def handler() -> None: ...

    assert info.value.detail["reason"] == "bare_metadata_key"


def test_bare_set_key_is_rejected() -> None:
    roles = MetadataSetKey("app.roles")

    with pytest.raises(DecoratorUsageError):

        @roles
        class Target: ...


def test_class_value_is_accepted_when_the_key_declares_a_class_type() -> None:
    class Base: ...

    class Impl(Base): ...

    serializer = MetadataKey[type[Base]]("app.serializer", default=Base)

    @serializer(Impl)
    def handler() -> None: ...

    assert getattr(handler, SET_METADATA) == {"app.serializer": Impl}


@pytest.mark.parametrize(
    ("key", "good", "bad"),
    [
        (MetadataKey[int]("app.limit", default=60, validate=True), 5, "5"),
        (MetadataKey[int]("app.limit", default=60, validate=True), 5, True),
        (MetadataKey[int | None]("app.limit", default=None, validate=True), None, 1.5),
        (MetadataKey[float]("app.ratio", default=1.0, validate=True), 2, "2"),
        (MetadataSetKey[str]("app.roles", validate=True), "admin", 3),
        (MetadataKey[tuple[str, ...]]("app.tags", default=(), validate=True), ("a",), ["a"]),
    ],
)
def test_validate_checks_decorated_values(key: Any, good: Any, bad: Any) -> None:
    key(good)

    with pytest.raises(DecoratorUsageError) as info:
        key(bad)

    assert info.value.detail["reason"] == "invalid_metadata_value"


def test_validate_with_literal_type() -> None:
    from typing import Literal

    mode = MetadataKey[Literal["a", "b"]]("app.mode", default="a", validate=True)
    mode("b")

    with pytest.raises(DecoratorUsageError):
        mode("c")


def test_validate_needs_a_runtime_checkable_type() -> None:
    untyped = MetadataKey("app.limit", default=1, validate=True)

    with pytest.raises(DecoratorUsageError) as info:
        untyped(2)

    assert info.value.detail["reason"] == "unvalidatable_metadata_key"


# ---------------------------------------------------------------------------
# Cross-level merge
# ---------------------------------------------------------------------------


def _decorated(*decorators: Any) -> Any:
    def target() -> None: ...

    for decorator in reversed(decorators):
        decorator(target)
    return target


@pytest.mark.parametrize(
    ("merge", "expected"),
    [("union", ("admin", "auditor")), ("override", ("auditor",))],
)
def test_set_key_merge_policies(merge: str, expected: tuple[str, ...]) -> None:
    roles = MetadataSetKey[str]("app.roles", merge=merge)  # type: ignore[arg-type]
    outer, inner = _decorated(roles("admin")), _decorated(roles("auditor"))

    assert _levels(outer, inner)["app.roles"] == expected


def test_set_key_custom_merge_callable() -> None:
    roles = MetadataSetKey[str]("app.roles", merge=lambda outer, inner: tuple(sorted({*outer, *inner})))
    outer, inner = _decorated(roles("b", "c")), _decorated(roles("a"))

    assert _levels(outer, inner)["app.roles"] == ("a", "b", "c")


@pytest.mark.parametrize(("merge", "expected"), [("override", 10), ("outer", 100)])
def test_scalar_key_merge_policies(merge: str, expected: int) -> None:
    limit = MetadataKey[int]("app.limit", default=0, merge=merge)  # type: ignore[arg-type]
    outer, inner = _decorated(limit(100)), _decorated(limit(10))

    assert _levels(outer, inner)["app.limit"] == expected


def test_scalar_key_merge_error_fails_on_conflict() -> None:
    limit = MetadataKey[int]("app.limit", default=0, merge="error")
    outer, inner = _decorated(limit(100)), _decorated(limit(10))

    with pytest.raises(DecoratorUsageError) as info:
        _levels(outer, inner)

    assert info.value.detail == {
        "key": "app.limit",
        "reason": "metadata_merge_conflict",
        "outer": "level0",
        "inner": "level1",
    }


def test_scalar_key_merge_error_accepts_equal_values() -> None:
    limit = MetadataKey[int]("app.limit", default=0, merge="error")

    assert _levels(_decorated(limit(5)), _decorated(limit(5)))["app.limit"] == 5


def test_scalar_key_custom_merge_callable() -> None:
    limit = MetadataKey[int]("app.limit", default=0, merge=min)

    assert _levels(_decorated(limit(5)), _decorated(limit(50)))["app.limit"] == 5


def test_string_keys_keep_most_specific_wins() -> None:
    outer = _decorated(set_metadata("roles", ["admin"]))
    inner = _decorated(set_metadata("roles", ["auditor"]))

    assert _levels(outer, inner)["roles"] == ["auditor"]


def test_string_level_follows_a_typed_level_policy() -> None:
    roles = MetadataSetKey[str]("app.roles")
    outer = _decorated(set_metadata("app.roles", ["admin"]))
    inner = _decorated(roles("auditor"))

    assert _levels(outer, inner)["app.roles"] == ("admin", "auditor")


def test_collision_across_levels_names_both_definitions() -> None:
    first = MetadataSetKey[str]("app.roles")
    second = MetadataSetKey[str]("app.roles")

    with pytest.raises(DecoratorUsageError) as info:
        _levels(_decorated(first("a")), _decorated(second("b")))

    assert info.value.detail["definitions"] == [first.site, second.site]
    assert info.value.detail["where"] == "level0 and level1"


def test_merged_mapping_is_read_only() -> None:
    merged = _levels(_decorated(set_metadata("a", 1)))

    with pytest.raises(TypeError):
        merged["a"] = 2  # type: ignore[index]


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_typed_reads_through_execution_context() -> None:
    roles = MetadataSetKey[str]("app.roles")
    public = MetadataKey[bool]("app.public", default=False)
    ctx = _ctx({"app.roles": ("admin",)})

    assert ctx.get_metadata(roles) == ("admin",)
    assert ctx.get_metadata(public) is False


@pytest.mark.parametrize(("stored", "expected"), [(["a", "b"], ("a", "b")), ("a", ("a",)), (None, ())])
def test_set_key_read_normalizes_legacy_values(stored: Any, expected: tuple[str, ...]) -> None:
    roles = MetadataSetKey[str]("app.roles")

    assert read_metadata({"app.roles": stored}, roles) == expected


def test_legacy_string_read_sees_typed_values() -> None:
    roles = MetadataSetKey[str]("app.roles")
    target = _decorated(roles("admin"))

    assert _ctx(_levels(target)).get_metadata("app.roles") == ("admin",)
    assert _ctx({}).get_metadata("missing", 7) == 7
    assert _ctx({}).get_metadata("missing") is None


def test_typed_read_refuses_an_explicit_default() -> None:
    public = MetadataKey[bool]("app.public", default=False)

    with pytest.raises(TypeError):
        _ctx({}).get_metadata(public, True)  # type: ignore[call-overload]


# ---------------------------------------------------------------------------
# Descriptor merge (decorators above and below @staticmethod)
# ---------------------------------------------------------------------------


def test_stack_metadata_dicts_unions_set_keys_across_a_descriptor() -> None:
    roles = MetadataSetKey[str]("app.roles")

    values, keys = stack_metadata_dicts(
        {"app.roles": ("a",), "x": 1},
        {"app.roles": roles},
        {"app.roles": ("b",), "x": 2},
        {"app.roles": roles},
        "f",
    )

    assert values == {"app.roles": ("a", "b"), "x": 2}
    assert keys == {"app.roles": roles}


def test_stack_metadata_dicts_detects_collisions() -> None:
    with pytest.raises(DecoratorUsageError):
        stack_metadata_dicts({"k": ("a",)}, {"k": MetadataSetKey("k")}, {}, {"k": MetadataSetKey("k")}, "f")


def test_set_key_on_a_staticmethod_handler_unions_both_sides() -> None:
    from lauren._asgi import _unwrap_handler_descriptor

    roles = MetadataSetKey[str]("app.roles")

    class Ctrl:
        @roles("outer")
        @staticmethod
        @roles("inner")
        def handler() -> None: ...

    fn, binding = _unwrap_handler_descriptor(Ctrl.__dict__["handler"])

    assert binding == "static"
    assert getattr(fn, SET_METADATA)["app.roles"] == ("inner", "outer")


# ---------------------------------------------------------------------------
# Edge cases of the engine helpers
# ---------------------------------------------------------------------------


def test_target_that_rejects_attributes_is_a_usage_error() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        MetadataSetKey[str]("app.roles")("a")(42)

    assert info.value.detail["reason"] == "metadata_target"


def test_values_whose_equality_raises_count_as_different() -> None:
    from lauren._metadata import _equal

    class Weird:
        def __eq__(self, other: object) -> bool:
            raise RuntimeError("no")

        __hash__ = object.__hash__

    assert _equal(Weird(), Weird()) is False


@pytest.mark.parametrize(
    ("annotation", "value", "expected"),
    [
        (Any, object(), True),
        (object, 1, True),
        ("Annotated[int, 'doc']", 1, True),
        ("TypeVar", 1, None),
        ("Protocol", 1, None),
    ],
)
def test_runtime_type_matching(annotation: Any, value: Any, expected: bool | None) -> None:
    from typing import Annotated, Protocol, TypeVar

    from lauren._metadata import _matches

    class Shaped(Protocol):
        def shape(self) -> str: ...

    resolved = (
        {
            "Annotated[int, 'doc']": Annotated[int, "doc"],
            "TypeVar": TypeVar("X"),
            "Protocol": Shaped,
        }.get(annotation, annotation)
        if isinstance(annotation, str)
        else annotation
    )

    assert _matches(resolved, value) is expected


def test_propagating_from_a_source_without_metadata_is_a_no_op() -> None:
    from lauren import propagate_metadata

    def source() -> None: ...

    @propagate_metadata(source)
    @set_metadata("k", 1)
    def target() -> None: ...

    assert getattr(target, SET_METADATA) == {"k": 1}


def test_provided_state_reads_attributes_of_non_class_components() -> None:
    from lauren import StateKey
    from lauren._metadata import PROVIDES_STATE_META, provided_state

    key = StateKey[int]("n")

    class Component:
        pass

    instance = Component()
    setattr(instance, PROVIDES_STATE_META, (key,))

    assert provided_state(instance) == (key,)
