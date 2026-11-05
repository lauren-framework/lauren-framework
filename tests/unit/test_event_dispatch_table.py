"""Unit tests for handler-plan compilation and the event dispatch table.

Plans are compiled from a real discovery index over a bare ``DIContainer``,
exactly as ``LaurenFactory.create`` does after Phase 6a, without building an
app.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from lauren import Scope, injectable
from lauren._di import DIContainer
from lauren._discovery import MarkerSpec, build_discovery_index
from lauren._events import ON_EVENT_META, DispatchTable, HandlerPlan, compile_handler_plans
from lauren.events import EmitResult, EventContext, EventRecording, on_event
from lauren.exceptions import UnresolvableParameterError
from lauren.signals import EventHandlerFailed

SPEC = MarkerSpec(ON_EVENT_META, framework_types=(EventContext,), extra_params="collect")


@dataclass(frozen=True)
class DomainEvent:
    pass


@dataclass(frozen=True)
class UserRegistered(DomainEvent):
    email: str = "a@b"


@dataclass(frozen=True)
class AdminRegistered(UserRegistered):
    pass


@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: str = "o1"


@injectable(scope=Scope.REQUEST)
class Db:
    pass


def compile_for(*providers: type) -> tuple[HandlerPlan, ...]:
    container = DIContainer()
    for provider in providers:
        container.register(provider)
    container.compile()
    index = build_discovery_index(container, [SPEC])
    return compile_handler_plans(index.methods_for(ON_EVENT_META))


# ---------------------------------------------------------------------------
# Compilation
# ---------------------------------------------------------------------------


def test_the_event_type_is_inferred_from_the_annotation() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered) -> None: ...

    (plan,) = compile_for(Audit)

    assert (plan.name, plan.event_type, plan.event_param) == ("Audit.record", UserRegistered, "event")


def test_a_union_annotation_subscribes_to_each_member() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered | OrderPlaced) -> None: ...

    plans = compile_for(Audit)

    assert [p.event_type for p in plans] == [UserRegistered, OrderPlaced]


def test_an_optional_annotation_does_not_subscribe_to_none() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered | None) -> None: ...

    assert [p.event_type for p in compile_for(Audit)] == [UserRegistered]


@pytest.mark.parametrize(
    "annotation", [DomainEvent, UserRegistered, object, Any, "UserRegistered | OrderPlaced"]
)
def test_an_explicit_type_accepts_an_equal_or_wider_annotation(annotation: Any) -> None:
    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, event) -> None: ...  # type: ignore[no-untyped-def]

    Audit.record.__annotations__["event"] = (
        (UserRegistered | OrderPlaced) if annotation == "UserRegistered | OrderPlaced" else annotation
    )

    (plan,) = compile_for(Audit)

    assert plan.event_type is UserRegistered


def test_an_explicit_type_without_annotation_is_accepted() -> None:
    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, event) -> None: ...  # type: ignore[no-untyped-def]

    assert compile_for(Audit)[0].event_type is UserRegistered


def test_a_narrower_annotation_is_rejected() -> None:
    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, event: AdminRegistered) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["reason"] == "event_type_mismatch"
    assert info.value.detail["handler"] == "Audit.record"
    assert info.value.detail["param"] == "event"


def test_inference_without_an_annotation_is_rejected() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event) -> None: ...  # type: ignore[no-untyped-def]

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["reason"] == "missing_event_parameter"


def test_a_handler_without_parameters_is_rejected() -> None:
    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail == {
        "handler": "Audit.record",
        "target": "Audit.record",
        "param": None,
        "reason": "missing_event_parameter",
    }


def test_the_context_cannot_take_the_event_slot() -> None:
    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, ctx: EventContext, event: UserRegistered) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert (info.value.detail["reason"], info.value.detail["param"]) == ("missing_event_parameter", "ctx")


@pytest.mark.parametrize("annotation", [Any, list[int], "Unresolved"])
def test_inference_from_a_non_class_annotation_is_rejected(annotation: Any) -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event) -> None: ...  # type: ignore[no-untyped-def]

    Audit.record.__annotations__["event"] = annotation

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["reason"] == "invalid_event_type"


def test_an_explicit_type_with_an_uncheckable_annotation_is_rejected() -> None:
    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, event: list[int]) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["reason"] == "invalid_event_type"


def test_an_unresolvable_dependency_is_rejected() -> None:
    class NotAProvider:
        pass

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered, repo: NotAProvider) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["handler"] == "Audit.record"
    assert info.value.detail["param"] == "repo"
    assert info.value.detail["reason"] == "unresolvable_dependency"


def test_an_unannotated_extra_parameter_is_rejected() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered, extra) -> None: ...  # type: ignore[no-untyped-def]

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["token"] == "<no annotation>"


def test_an_uncheckable_protocol_annotation_is_rejected() -> None:
    from typing import Protocol

    class HasEmail(Protocol):
        email: str

    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, event) -> None: ...  # type: ignore[no-untyped-def]

    Audit.record.__annotations__["event"] = HasEmail

    with pytest.raises(UnresolvableParameterError) as info:
        compile_for(Audit)

    assert info.value.detail["reason"] == "invalid_event_type"


def test_parameters_are_planned_by_kind() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(
            self, event: UserRegistered, db: Db, ctx: EventContext, retries: int = 3
        ) -> None: ...

    (plan,) = compile_for(Audit, Db)

    assert plan.context_params == ("ctx",)
    assert plan.di_params == (("db", Db),)
    assert plan.needs_scope is True


@pytest.mark.parametrize(
    ("scope", "needs_scope"),
    [(Scope.SINGLETON, False), (Scope.REQUEST, True), (Scope.TRANSIENT, True)],
)
def test_the_provider_scope_decides_whether_a_scope_is_needed(scope: Scope, needs_scope: bool) -> None:
    @injectable(scope=scope)
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered) -> None: ...

    (plan,) = compile_for(Audit)

    assert (plan.needs_instance, plan.needs_scope) == (True, needs_scope)


def test_a_static_handler_needs_no_instance() -> None:
    @injectable(scope=Scope.REQUEST)
    class Audit:
        @staticmethod
        @on_event()
        async def record(event: UserRegistered) -> None: ...

    (plan,) = compile_for(Audit)

    assert (plan.needs_instance, plan.needs_scope) == (False, False)


def test_disabled_subscriptions_are_validated_but_left_out() -> None:
    @injectable()
    class Audit:
        @on_event(OrderPlaced)
        @on_event(UserRegistered, enabled=False)
        async def record(self, event: DomainEvent) -> None: ...

    assert [p.event_type for p in compile_for(Audit)] == [OrderPlaced]


def test_a_disabled_subscription_still_fails_validation() -> None:
    @injectable()
    class Audit:
        @on_event(DomainEvent, enabled=False)
        async def record(self, event: UserRegistered) -> None: ...

    with pytest.raises(UnresolvableParameterError):
        compile_for(Audit)


def test_discovery_order_sets_the_plan_index() -> None:
    @injectable()
    class First:
        @on_event()
        async def a(self, event: UserRegistered) -> None: ...

        @on_event()
        async def b(self, event: UserRegistered) -> None: ...

    @injectable()
    class Second:
        @on_event()
        async def c(self, event: UserRegistered) -> None: ...

    plans = compile_for(First, Second)

    assert [(p.name, p.index) for p in plans] == [("First.a", 0), ("First.b", 1), ("Second.c", 2)]


# ---------------------------------------------------------------------------
# The dispatch table
# ---------------------------------------------------------------------------


def _plan(
    event_type: type, *, priority: int = 0, index: int = 0, entry: Any = None, name: str = ""
) -> HandlerPlan:
    return HandlerPlan(
        index=index,
        name=name or f"H.{index}",
        event_type=event_type,
        mode="inline",
        priority=priority,
        errors="propagate",
        entry=entry if entry is not None else object(),  # type: ignore[arg-type]
        event_param="event",
        context_params=(),
        di_params=(),
        needs_instance=True,
        needs_scope=False,
    )


def test_a_handler_on_a_base_class_receives_subclasses() -> None:
    base = _plan(DomainEvent, index=0)
    table = DispatchTable([base])

    assert table.lookup(AdminRegistered) == (base,)


def test_an_unrelated_event_has_no_handlers() -> None:
    table = DispatchTable([_plan(UserRegistered)])

    assert table.lookup(OrderPlaced) == ()


def test_handlers_run_by_priority_then_discovery_order() -> None:
    low = _plan(UserRegistered, priority=-1, index=0)
    first = _plan(DomainEvent, priority=5, index=1)
    second = _plan(UserRegistered, priority=5, index=2)
    default = _plan(UserRegistered, priority=0, index=3)
    table = DispatchTable([low, first, second, default])

    assert table.lookup(UserRegistered) == (first, second, default, low)


def test_the_resolution_is_cached_once_per_event_class() -> None:
    table = DispatchTable([_plan(DomainEvent)])

    first = table.lookup(UserRegistered)
    second = table.lookup(UserRegistered)

    assert first is second
    assert list(table._resolved) == [UserRegistered]


def test_a_method_subscribed_along_the_mro_runs_once_under_the_most_specific_subscription() -> None:
    entry = object()
    on_base = _plan(DomainEvent, priority=9, index=0, entry=entry)
    on_user = _plan(UserRegistered, priority=1, index=1, entry=entry)
    table = DispatchTable([on_base, on_user])

    assert table.lookup(AdminRegistered) == (on_user,)
    assert table.lookup(OrderPlaced) == (on_base,)


def test_the_table_reports_its_subscribed_types() -> None:
    table = DispatchTable(
        [_plan(UserRegistered, index=0), _plan(DomainEvent, index=1), _plan(UserRegistered, index=2)]
    )

    assert table.subscribed_types == (UserRegistered, DomainEvent)
    assert len(table.plans) == 3


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


def test_emit_result_is_ok_without_errors() -> None:
    failure = EventHandlerFailed(
        event_type=UserRegistered, handler="A.b", error=ValueError("x"), mode="inline"
    )

    assert EmitResult(UserRegistered()).ok is True
    assert EmitResult(UserRegistered(), errors=(failure,)).ok is False


def test_a_recording_filters_events_by_class() -> None:
    recording = EventRecording()
    recording.events.extend([UserRegistered("a"), OrderPlaced(), AdminRegistered("b")])

    assert recording.of(UserRegistered) == [UserRegistered("a"), AdminRegistered("b")]


def test_a_recording_counts_calls_by_name_or_function() -> None:
    class Mailer:
        async def send(self, event: UserRegistered) -> None: ...

    recording = EventRecording()
    recording._record("Mailer.send", Mailer.send, None)
    recording._record("Mailer.send", Mailer.send, None)

    assert recording.handler_calls("Mailer.send") == 2
    assert recording.handler_calls(Mailer.send) == 2
    assert recording.handler_calls(Mailer().send) == 2
    assert recording.handler_calls("Other.send") == 0
    assert "2 handler call(s)" in repr(recording)
