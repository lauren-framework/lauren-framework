"""Unit tests for the shared startup discovery pass (``lauren._discovery``).

The index is built straight from a :class:`DIContainer` with explicit marker
specs, so these tests never touch the process-wide marker registry.
"""

from __future__ import annotations

import threading
from typing import Annotated, Any

import pytest

from lauren import (
    DIContainer,
    Inject,
    Scope,
    Token,
    injectable,
    use_class,
    use_existing,
    use_factory,
    use_value,
)
from lauren._di.custom import CustomProvider
from lauren._discovery import (
    ARGUMENT,
    DI,
    FRAMEWORK,
    DiscoveryIndex,
    MarkerRegistry,
    MarkerSpec,
    build_discovery_index,
    register_marker,
    registered_markers,
)
from lauren.context import OperationScope
from lauren.exceptions import (
    LifecycleViolationError,
    MetadataInheritanceError,
    MissingProviderError,
    UnresolvableParameterError,
)

PROBE = "__lauren_test_probe__"
CLASS_PROBE = "__lauren_test_class_probe__"


def probe(label: str = "x") -> Any:
    """A metadata-only test decorator; stacking accumulates like real markers."""

    def deco(fn: Any) -> Any:
        existing = fn.__dict__.get(PROBE)
        setattr(fn, PROBE, [*(existing or []), label])
        return fn

    return deco


def class_probe(label: str = "c") -> Any:
    def deco(cls: type) -> type:
        setattr(cls, CLASS_PROBE, label)
        return cls

    return deco


def _container(*providers: Any) -> DIContainer:
    container = DIContainer()
    for p in providers:
        if isinstance(p, CustomProvider):
            container.register_custom(p)
        else:
            container.register(p)
    container.compile()
    return container


def _index(container: DIContainer, *markers: Any) -> DiscoveryIndex:
    return build_discovery_index(container, markers or (PROBE,))


class Clock:
    """A framework-supplied value type used by the call-plan tests."""


class SpecialClock(Clock):
    pass


@injectable()
class Repo:
    pass


# ---------------------------------------------------------------------------
# Method markers and the MRO rule
# ---------------------------------------------------------------------------


def test_marked_method_is_discovered_with_its_provider() -> None:
    @injectable()
    class Jobs:
        @probe("tick")
        async def tick(self) -> None: ...

        async def unmarked(self) -> None: ...

    container = _container(Jobs)

    (entry,) = _index(container).methods[PROBE]

    assert (entry.marker, entry.attr_name, entry.meta) == (PROBE, "tick", ["tick"])
    assert entry.provider is container.get_provider(Jobs)
    assert entry.owner is Jobs
    assert entry.function is Jobs.__dict__["tick"]
    assert entry.raw_descriptor is Jobs.__dict__["tick"]


def test_marked_base_method_propagates_to_a_registered_subclass() -> None:
    class Base:
        @probe("base")
        async def run(self) -> None: ...

    @injectable()
    class Child(Base):
        pass

    (entry,) = _index(_container(Child)).methods[PROBE]

    assert (entry.owner, entry.attr_name, entry.meta) == (Child, "run", ["base"])


def test_override_without_the_decorator_removes_the_marker() -> None:
    class Base:
        @probe()
        async def run(self) -> None: ...

    @injectable()
    class Child(Base):
        async def run(self) -> None: ...

    assert _index(_container(Child)).methods[PROBE] == ()


def test_redecorated_override_replaces_the_base_marker() -> None:
    class Base:
        @probe("base")
        async def run(self) -> None: ...

    @injectable()
    class Child(Base):
        @probe("child")
        async def run(self) -> None: ...

    (entry,) = _index(_container(Child)).methods[PROBE]

    assert entry.meta == ["child"]
    assert entry.function is Child.__dict__["run"]


@pytest.mark.parametrize("wrapper", [staticmethod, classmethod])
@pytest.mark.parametrize("marker_above_wrapper", [False, True])
def test_static_and_class_methods_are_normalised(wrapper: Any, marker_above_wrapper: bool) -> None:
    def run(*args: Any) -> str:
        return "ran"

    raw = probe("above")(wrapper(run)) if marker_above_wrapper else wrapper(probe("below")(run))
    Jobs = injectable()(type("Jobs", (), {"run": raw}))

    (entry,) = _index(_container(Jobs)).methods[PROBE]

    assert entry.function is run
    assert entry.meta == (["above"] if marker_above_wrapper else ["below"])
    assert entry.plan.binding == ("static" if wrapper is staticmethod else "classmethod")
    assert entry.bind(Jobs())() == "ran"


def test_markers_on_both_sides_of_a_wrapper_accumulate() -> None:
    @injectable()
    class Jobs:
        @probe("outer")
        @staticmethod
        @probe("inner")
        def run() -> None: ...

    (entry,) = _index(_container(Jobs)).methods[PROBE]

    assert entry.meta == ["inner", "outer"]


@pytest.mark.parametrize(
    "inner,outer,expected",
    [
        ({"a": 1, "b": 1}, {"b": 2}, {"a": 1, "b": 2}),  # dicts merge, the outer side wins a key
        ("inner", "outer", "inner"),  # anything else: the inner side wins
    ],
)
def test_non_list_markers_on_both_sides_merge(inner: Any, outer: Any, expected: Any) -> None:
    def run() -> None: ...

    setattr(run, PROBE, inner)
    wrapped = staticmethod(run)
    setattr(wrapped, PROBE, outer)
    Jobs = injectable()(type("Jobs", (), {"run": wrapped}))

    (entry,) = _index(_container(Jobs)).methods[PROBE]

    assert entry.meta == expected


def test_a_class_behind_several_providers_is_scanned_once_per_provider() -> None:
    class Impl:
        @probe()
        async def run(self) -> None: ...

    container = _container(use_class(provide="first", use=Impl), use_class(provide="second", use=Impl))

    entries = _index(container).methods[PROBE]

    assert [(e.provider.cls, e.owner) for e in entries] == [("first", Impl), ("second", Impl)]


def test_non_callable_descriptor_markers_are_read_through_wrapped() -> None:
    class Cached:
        """A non-callable descriptor wrapping a function (like a caching decorator)."""

        def __init__(self, fn: Any) -> None:
            self.__wrapped__ = fn

        def __get__(self, instance: Any, owner: type) -> Any:
            return self.__wrapped__.__get__(instance, owner)

    def run(self: Any) -> str:
        return "ran"

    Jobs = injectable()(type("Jobs", (), {"run": probe("outer")(Cached(run))}))

    (entry,) = _index(_container(Jobs)).methods[PROBE]

    assert entry.meta == ["outer"]
    assert entry.function is run
    assert entry.bind(Jobs())() == "ran"


def test_order_is_registration_order_then_mro_order() -> None:
    class Base:
        @probe()
        async def base_job(self) -> None: ...

    @injectable()
    class Second(Base):
        @probe()
        async def own_job(self) -> None: ...

    @injectable()
    class First:
        @probe()
        async def b(self) -> None: ...

        @probe()
        async def a(self) -> None: ...

    entries = _index(_container(First, Second)).methods[PROBE]

    assert [(e.owner.__name__, e.attr_name) for e in entries] == [
        ("First", "b"),
        ("First", "a"),
        ("Second", "own_job"),
        ("Second", "base_job"),
    ]


def test_one_method_can_carry_several_markers() -> None:
    other = "__lauren_test_other__"

    @injectable()
    class Jobs:
        @probe()
        async def run(self) -> None: ...

    setattr(Jobs.__dict__["run"], other, {"k": 1})

    index = _index(_container(Jobs), PROBE, other)

    assert [e.attr_name for e in index.methods[PROBE]] == ["run"]
    assert index.methods[other][0].meta == {"k": 1}


# ---------------------------------------------------------------------------
# Class markers and strict inheritance
# ---------------------------------------------------------------------------


def test_class_marker_is_read_from_the_class_own_dict() -> None:
    @class_probe("svc")
    @injectable()
    class Service:
        pass

    container = _container(Service, Repo)

    index = _index(container, MarkerSpec(CLASS_PROBE, kind="class"))

    assert index.classes[CLASS_PROBE] == ((container.get_provider(Service), "svc"),)


def test_inherited_class_marker_without_redecoration_raises() -> None:
    @class_probe()
    class Base:
        pass

    @injectable()
    class Child(Base):
        pass

    with pytest.raises(MetadataInheritanceError) as info:
        _index(_container(Child), MarkerSpec(CLASS_PROBE, kind="class"))

    assert info.value.detail == {"class": "Child", "inherits_from": "Base", "marker": CLASS_PROBE}


def test_redecorated_subclass_carries_its_own_class_marker() -> None:
    @class_probe("base")
    class Base:
        pass

    @class_probe("child")
    @injectable()
    class Child(Base):
        pass

    index = _index(_container(Child), MarkerSpec(CLASS_PROBE, kind="class"))

    assert [meta for _, meta in index.classes[CLASS_PROBE]] == ["child"]


# ---------------------------------------------------------------------------
# Skipped provider kinds
# ---------------------------------------------------------------------------


class Marked:
    @probe()
    async def run(self) -> None: ...


@injectable()
def marked_factory() -> Marked:
    return Marked()


setattr(marked_factory, PROBE, ["function"])


@pytest.mark.parametrize(
    "provider",
    [
        pytest.param(marked_factory, id="function-provider"),
        pytest.param(use_value(provide="marked", value=Marked()), id="use_value"),
        pytest.param(use_factory(provide="marked", factory=Marked), id="use_factory"),
    ],
)
def test_function_value_and_factory_providers_are_skipped(provider: Any) -> None:
    assert _index(_container(provider)).methods[PROBE] == ()


def test_use_existing_aliases_are_skipped() -> None:
    @injectable()
    class Real:
        @probe()
        async def run(self) -> None: ...

    entries = _index(_container(Real, use_existing(provide="alias", existing=Real))).methods[PROBE]

    assert [e.provider.cls for e in entries] == [Real]


def test_use_class_providers_are_scanned_on_the_implementation_class() -> None:
    token = Token("jobs")

    class Impl:
        @probe()
        async def run(self) -> None: ...

    container = _container(use_class(provide=token, use=Impl))

    (entry,) = _index(container).methods[PROBE]

    assert entry.provider.cls is token
    assert entry.owner is Impl


def test_multi_bound_siblings_are_each_discovered() -> None:
    token = Token("plugins")

    class A:
        @probe("a")
        async def run(self) -> None: ...

    class B:
        @probe("b")
        async def run(self) -> None: ...

    container = _container(
        use_class(provide=token, use=A, multi=True),
        use_class(provide=token, use=B, multi=True),
    )

    entries = _index(container).methods[PROBE]

    assert [e.owner for e in entries] == [A, B]


# ---------------------------------------------------------------------------
# The index itself
# ---------------------------------------------------------------------------


def test_every_requested_marker_has_a_key() -> None:
    index = _index(_container(Repo), PROBE, MarkerSpec(CLASS_PROBE, kind="class"))

    assert index.methods == {PROBE: ()}
    assert index.classes == {CLASS_PROBE: ()}
    assert index.methods_for("__lauren_unknown__") == ()
    assert index.classes_for("__lauren_unknown__") == ()


def test_the_index_is_immutable() -> None:
    index = _index(_container(Repo))

    with pytest.raises(TypeError):
        index.methods["new"] = ()  # type: ignore[index]


def test_no_markers_yields_an_empty_index() -> None:
    index = build_discovery_index(_container(Repo), ())

    assert (dict(index.methods), dict(index.classes)) == ({}, {})
    assert DiscoveryIndex.empty().methods == {}


# ---------------------------------------------------------------------------
# Call plans
# ---------------------------------------------------------------------------


def _single_plan(cls: type, *markers: Any, container: DIContainer | None = None) -> Any:
    container = container or _container(cls, Repo)
    (entry,) = _index(container, *(markers or (MarkerSpec(PROBE, framework_types=(Clock,)),))).methods[PROBE]
    return entry


def test_plan_classifies_framework_di_and_defaulted_parameters() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, clock: Clock, repo: Repo, *, retries: int = 3) -> None: ...

    plan = _single_plan(Jobs).plan

    assert [(p.name, p.kind, p.token) for p in plan.params] == [
        ("clock", FRAMEWORK, Clock),
        ("repo", DI, Repo),
    ]
    assert plan.needs_scope
    assert plan.is_coroutine


def test_plan_matches_a_framework_subclass_annotation() -> None:
    @injectable()
    class Jobs:
        @probe()
        def run(self, clock: Annotated[SpecialClock, "doc"]) -> None: ...

    plan = _single_plan(Jobs).plan

    assert [(p.kind, p.token) for p in plan.params] == [(FRAMEWORK, Clock)]
    assert not plan.needs_scope
    assert not plan.is_coroutine


def test_framework_types_may_depend_on_the_marker_payload() -> None:
    @injectable()
    class Handlers:
        @probe("Clock")
        async def on_clock(self, event: Clock) -> None: ...

    spec = MarkerSpec(PROBE, framework_types=lambda meta: [Clock] if meta == ["Clock"] else [])

    plan = _single_plan(Handlers, spec).plan

    assert [(p.name, p.kind) for p in plan.params] == [("event", FRAMEWORK)]


def test_plan_resolves_inject_tokens() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, url: Annotated[str, Inject("db_url")]) -> None: ...

    container = _container(Jobs, use_value(provide="db_url", value="sqlite://"))

    plan = _single_plan(Jobs, container=container).plan

    assert [(p.kind, p.token) for p in plan.params] == [(DI, "db_url")]


def test_inject_token_without_provider_keeps_a_default() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, url: Annotated[str, Inject("missing")] = "x") -> None: ...

    assert _single_plan(Jobs).plan.params == ()


def test_inject_token_without_provider_fails_startup() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, url: Annotated[str, Inject("missing")]) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        _single_plan(Jobs)

    assert info.value.detail == {
        "target": "Jobs.run",
        "param": "url",
        "reason": "missing_provider",
        "token": "missing",
    }


def test_unresolvable_parameter_fails_startup_naming_method_and_parameter() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, limit: int) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        _single_plan(Jobs)

    assert info.value.detail == {"target": "Jobs.run", "param": "limit", "reason": "unresolvable"}


def test_collect_mode_turns_unresolvable_parameters_into_arguments() -> None:
    @injectable()
    class Commands:
        @probe()
        async def seed(self, count: int, *, reset: bool = False, repo: Repo) -> None: ...

    plan = _single_plan(Commands, MarkerSpec(PROBE, extra_params="collect")).plan

    assert [(p.name, p.kind, p.keyword_only, p.has_default) for p in plan.params] == [
        ("count", ARGUMENT, False, False),
        ("reset", ARGUMENT, True, True),
        ("repo", DI, True, False),
    ]
    assert [p.name for p in plan.arguments] == ["count", "reset"]


def test_positional_only_parameters_are_rejected() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, repo: Repo, /) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        _single_plan(Jobs)

    assert info.value.detail["reason"] == "positional_only"


def test_variadic_parameters_are_ignored() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, *args: Any, **kwargs: Any) -> None: ...

    assert _single_plan(Jobs).plan.params == ()


def test_di_parameters_respect_module_visibility() -> None:
    class OwnModule:
        pass

    @injectable()
    class Hidden:
        pass

    @injectable()
    class Jobs:
        @probe()
        async def run(self, hidden: Hidden) -> None: ...

    container = DIContainer()
    container.register(Jobs, owning_module=OwnModule)
    container.register(Hidden, owning_module=object)  # another module, not exported
    container.set_visible(OwnModule, frozenset({Jobs}))
    container.compile()

    with pytest.raises(UnresolvableParameterError) as info:
        _index(container)

    assert info.value.detail["param"] == "hidden"


def test_unreadable_signature_fails_startup() -> None:
    class NoSignature:
        """A callable whose signature cannot be read."""

        __signature__ = "not a signature"

        def __call__(self, *args: Any) -> None: ...

    Jobs = injectable()(type("Jobs", (), {"run": probe()(NoSignature())}))

    with pytest.raises(UnresolvableParameterError) as info:
        _single_plan(Jobs)

    assert info.value.detail["reason"] == "no_signature"


# ---------------------------------------------------------------------------
# Running a plan
# ---------------------------------------------------------------------------


@injectable(scope=Scope.REQUEST)
class Session:
    pass


@injectable()
class Worker:
    @probe()
    async def run(self, clock: Clock, session: Session, *, label: str = "default") -> tuple[Any, ...]:
        return (self, clock, session, label)

    @probe()
    def sync_run(self, clock: Clock) -> tuple[Any, str]:
        return (clock, threading.current_thread().name)


def _worker_entries() -> tuple[DIContainer, dict[str, Any]]:
    container = _container(Worker, Session)
    entries = _index(container, MarkerSpec(PROBE, framework_types=(Clock,))).methods[PROBE]
    return container, {e.attr_name: e for e in entries}


async def test_invoke_builds_arguments_from_the_plan_inside_a_scope() -> None:
    container, entries = _worker_entries()
    worker, clock = Worker(), Clock()

    async with OperationScope(container, "job", values={Clock: clock}) as scope:
        result = await entries["run"].invoke(worker, scope)
        session = await scope.resolve(Session)

    assert result == (worker, clock, session, "default")


async def test_explicit_values_win_over_scope_values() -> None:
    container, entries = _worker_entries()
    first, second = Clock(), Clock()

    async with OperationScope(container, values={Clock: first}) as scope:
        result = await entries["run"].invoke(Worker(), scope, values={Clock: second})

    assert result[1] is second


async def test_sync_methods_run_in_a_worker_thread() -> None:
    _, entries = _worker_entries()
    clock = Clock()

    seen_clock, thread_name = await entries["sync_run"].invoke(Worker(), values={Clock: clock})

    assert seen_clock is clock
    assert thread_name != threading.current_thread().name


async def test_di_parameters_require_a_scope() -> None:
    _, entries = _worker_entries()

    with pytest.raises(LifecycleViolationError) as info:
        await entries["run"].plan.build_kwargs(values={Clock: Clock()})

    assert info.value.detail == {"target": "Worker.run", "param": "session", "reason": "no_active_scope"}


async def test_missing_framework_value_raises() -> None:
    container, entries = _worker_entries()

    async with OperationScope(container) as scope:
        with pytest.raises(MissingProviderError) as info:
            await entries["run"].plan.build_kwargs(scope)

    assert info.value.detail["reason"] == "missing_framework_value"


async def test_arguments_are_taken_by_name_and_defaults_apply() -> None:
    @injectable()
    class Commands:
        @probe()
        async def seed(self, count: int, *, reset: bool = False) -> tuple[int, bool]:
            return (count, reset)

    container = _container(Commands)
    (entry,) = _index(container, MarkerSpec(PROBE, extra_params="collect")).methods[PROBE]

    assert await entry.invoke(Commands(), arguments={"count": 3}) == (3, False)
    with pytest.raises(UnresolvableParameterError) as info:
        await entry.plan.build_kwargs()
    assert info.value.detail["reason"] == "missing_argument"


async def test_framework_parameter_with_default_may_be_omitted() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, clock: Clock | None = None) -> Any:
            return clock

    container = _container(Jobs)
    spec = MarkerSpec(PROBE, framework_types=lambda meta: ())
    (entry,) = _index(container, spec).methods[PROBE]

    assert entry.plan.params == ()
    assert await entry.invoke(Jobs()) is None


async def test_defaulted_framework_parameter_is_skipped_when_unseeded() -> None:
    @injectable()
    class Jobs:
        @probe()
        async def run(self, clock: Clock = None) -> Any:  # type: ignore[assignment]
            return clock

    container = _container(Jobs)
    (entry,) = _index(container, MarkerSpec(PROBE, framework_types=(Clock,))).methods[PROBE]

    assert await entry.invoke(Jobs()) is None


# ---------------------------------------------------------------------------
# Marker declarations and the registry
# ---------------------------------------------------------------------------


def test_bare_name_is_a_default_method_marker() -> None:
    registry = MarkerRegistry(["__lauren_a__"])

    assert registry.specs() == (MarkerSpec("__lauren_a__"),)
    assert "__lauren_a__" in registry
    assert len(registry) == 1


def test_registering_an_equal_spec_twice_is_a_no_op() -> None:
    registry = MarkerRegistry()

    first = registry.register(MarkerSpec("__lauren_a__", framework_types=(Clock,)))
    second = registry.register(MarkerSpec("__lauren_a__", framework_types=(Clock,)))

    assert first is second
    assert len(registry) == 1


def test_registering_a_conflicting_spec_raises() -> None:
    registry = MarkerRegistry(["__lauren_a__"])

    with pytest.raises(ValueError, match="already registered"):
        registry.register(MarkerSpec("__lauren_a__", kind="class"))


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "__lauren_a__", "kind": "function"},
        {"name": "__lauren_a__", "extra_params": "ignore"},
    ],
)
def test_invalid_marker_specs_are_rejected(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        MarkerSpec(**kwargs)


def test_register_marker_adds_to_the_markers_create_indexes(monkeypatch: pytest.MonkeyPatch) -> None:
    import lauren._discovery as discovery

    monkeypatch.setattr(discovery, "_REGISTRY", MarkerRegistry())

    spec = register_marker(MarkerSpec(PROBE, framework_types=(Clock,)))

    assert registered_markers() == (spec,)
