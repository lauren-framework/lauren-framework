"""``compose(...)`` and public ``reject_bare_usage`` (F23 §5.4)."""

from __future__ import annotations

import functools
from typing import Any

import pytest

from lauren import (
    DecoratorUsageError,
    MetadataKey,
    MetadataSetKey,
    compose,
    controller,
    get,
    post,
    raises,
    reject_bare_usage,
    set_metadata,
    use_guards,
    use_interceptors,
)
from lauren.cache import cache_response, idempotent
from lauren.exceptions import NotFoundError
from lauren.reflect import ReflectedComposition, reflect_composed

Roles = MetadataSetKey[str]("test.roles")
IsPublic = MetadataKey[bool]("test.public", default=False)


class GuardA:
    async def can_activate(self, ctx: Any) -> bool:
        return True


class GuardB:
    async def can_activate(self, ctx: Any) -> bool:
        return True


class Timing:
    async def intercept(self, ctx: Any, call_handler: Any) -> Any:
        return await call_handler.handle()


_LAUREN_ATTRS = (
    "__lauren_route__",
    "__lauren_use_guards__",
    "__lauren_use_interceptors__",
    "__lauren_metadata__",
    "__lauren_metadata_keys__",
    "__lauren_raises__",
    "__lauren_cache_response__",
    "__lauren_idempotent__",
)


def _snapshot(target: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for attr in _LAUREN_ATTRS:
        value = getattr(target, attr, None)
        if attr == "__lauren_route__" and value is not None:
            value = [vars(meta) for meta in value]
        out[attr] = value
    return out


def test_compose_applies_like_manual_stacking() -> None:
    bundle = compose(
        get("/stats", status_code=201, response_headers={"X-Trace": "trace id"}),
        use_guards(GuardA, GuardB),
        use_interceptors(Timing),
        Roles("admin"),
        IsPublic(False),
        raises(NotFoundError),
        cache_response(ttl=30),
    )

    @bundle
    async def composed() -> dict:
        return {}

    @get("/stats", status_code=201, response_headers={"X-Trace": "trace id"})
    @use_guards(GuardA, GuardB)
    @use_interceptors(Timing)
    @Roles("admin")
    @IsPublic(False)
    @raises(NotFoundError)
    @cache_response(ttl=30)
    async def stacked() -> dict:
        return {}

    assert _snapshot(composed) == _snapshot(stacked)


def test_compose_preserves_idempotent_metadata_like_manual_stacking() -> None:
    bundled = compose(post("/charge", status_code=201), idempotent(ttl=120, required=True))

    @bundled
    async def composed() -> dict:
        return {}

    @post("/charge", status_code=201)
    @idempotent(ttl=120, required=True)
    async def stacked() -> dict:
        return {}

    assert _snapshot(composed) == _snapshot(stacked)


def test_compose_returns_the_same_object() -> None:
    def handler() -> None: ...

    assert compose(Roles("a"))(handler) is handler


def test_compose_applies_in_written_order() -> None:
    order: list[str] = []

    def record(label: str) -> Any:
        def decorator(target: Any) -> Any:
            order.append(label)
            return target

        return decorator

    compose(record("a"), record("b"), record("c"))(lambda: None)

    assert order == ["c", "b", "a"]


def test_wrapping_decorator_is_rejected_with_its_name() -> None:
    def logged(fn: Any) -> Any:
        @functools.wraps(fn)
        def inner(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        return inner

    with pytest.raises(DecoratorUsageError) as info:

        @compose(Roles("a"), logged, name="bad")
        def handler() -> None: ...

    assert info.value.detail["reason"] == "decorator_replaced_target"
    assert info.value.detail["decorator"] == "logged"
    assert info.value.detail["composition"] == "bad"


def test_uncalled_decorator_factory_is_rejected() -> None:
    def tag(label: Any = "x") -> Any:
        def decorator(target: Any) -> Any:
            return target

        return decorator

    with pytest.raises(DecoratorUsageError) as info:

        @compose(tag)
        def handler() -> None: ...

    assert info.value.detail["decorator"] == "tag"


@pytest.mark.parametrize(
    ("args", "kwargs", "reason"),
    [
        ((), {}, "empty_composition"),
        ((42,), {}, "not_a_decorator"),
        ((Roles("a"),), {"name": 3}, "invalid_name"),
    ],
)
def test_invalid_compose_arguments(args: tuple[Any, ...], kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        compose(*args, **kwargs)

    assert info.value.detail["reason"] == reason


def test_provenance_is_recorded_on_functions() -> None:
    admin = compose(use_guards(GuardA), Roles("admin"), name="admin")

    @admin
    def handler() -> None: ...

    assert reflect_composed(handler) == (
        ReflectedComposition(name="admin", decorators=("use_guards", "test.roles('admin')")),
    )


def test_provenance_on_classes_follows_the_own_class_rule() -> None:
    marked = compose(set_metadata("k", 1), name="marked")

    @marked
    @controller("/base")
    class Base: ...

    @controller("/child")
    class Child(Base): ...

    assert [c.name for c in reflect_composed(Base)] == ["marked"]
    assert reflect_composed(Child) == ()


def test_nested_compositions_record_inner_first() -> None:
    inner = compose(Roles("a"), name="inner")
    outer = compose(inner, IsPublic(True), name="outer")

    @outer
    def handler() -> None: ...

    records = reflect_composed(handler)
    assert [r.name for r in records] == ["inner", "outer"]
    assert records[1].decorators == ("inner", "test.public(True)")


def test_repr_lists_the_decorators() -> None:
    assert repr(compose(Roles("a"), name="x")) == "compose(test.roles('a'), name='x')"
    assert repr(compose(IsPublic(True))) == "compose(test.public(True))"


def test_unnamed_nested_composition_is_described_by_its_repr() -> None:
    inner = compose(Roles("a"))

    @compose(inner)
    def handler() -> None: ...

    assert reflect_composed(handler)[-1].decorators == ("compose(test.roles('a'))",)


def test_sub_decorator_errors_surface_unchanged() -> None:
    with pytest.raises(Exception, match="classes"):

        @compose(cache_response(ttl=5))
        class NotAHandler: ...


def test_reject_bare_usage_is_public() -> None:
    def auth(*roles: str) -> Any:
        reject_bare_usage("auth", roles[0] if roles else None)
        return compose(use_guards(GuardA), Roles(*roles), name="auth")

    with pytest.raises(DecoratorUsageError) as info:

        @auth  # type: ignore[arg-type]
        def handler() -> None: ...

    assert info.value.detail["decorator"] == "auth"

    @auth("admin")
    def ok() -> None: ...

    assert ok.__lauren_metadata__ == {"test.roles": ("admin",)}  # type: ignore[attr-defined]
