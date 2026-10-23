"""Unit tests for the ``@on_event`` decorator (metadata only, no app)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from lauren.events import on_event
from lauren.exceptions import DecoratorUsageError

ON_EVENT = "__lauren_on_event__"


@dataclass(frozen=True)
class UserRegistered:
    email: str


@dataclass(frozen=True)
class UserDeleted:
    user_id: str


def test_bare_usage_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @on_event  # type: ignore[arg-type]
        async def handle(self: Any, event: UserRegistered) -> None: ...

    assert info.value.detail["decorator"] == "on_event"


@pytest.mark.parametrize("bad", ["UserRegistered", 42, UserRegistered("a@b")])
def test_a_non_class_event_type_is_rejected(bad: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        on_event(bad)

    assert info.value.detail["decorator"] == "on_event"
    assert "event_type" in info.value.detail


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        on_event(UserRegistered, mode="later")

    assert info.value.detail["mode"] == "'later'"


def test_an_unknown_error_policy_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        on_event(UserRegistered, errors="ignore")

    assert info.value.detail["errors"] == "'ignore'"


@pytest.mark.parametrize("priority", ["1", 1.5, True])
def test_priority_must_be_an_int(priority: Any) -> None:
    with pytest.raises(DecoratorUsageError):
        on_event(UserRegistered, priority=priority)


@pytest.mark.parametrize("mode", ["concurrent", "background"])
def test_propagate_is_rejected_on_deferred_modes(mode: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        on_event(UserRegistered, mode=mode, errors="propagate")

    assert info.value.detail == {"decorator": "on_event", "mode": mode, "errors": "propagate"}


def test_enabled_must_be_a_bool() -> None:
    with pytest.raises(DecoratorUsageError):
        on_event(UserRegistered, enabled="yes")  # type: ignore[arg-type]


def test_the_decorator_returns_the_same_function() -> None:
    async def handle(self: Any, event: UserRegistered) -> None: ...

    decorated = on_event(UserRegistered)(handle)

    assert decorated is handle


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("inline", "propagate"), ("concurrent", "isolate"), ("background", "isolate")],
)
def test_the_error_policy_defaults_from_the_mode(mode: str, expected: str) -> None:
    @on_event(UserRegistered, mode=mode)
    async def handle(self: Any, event: UserRegistered) -> None: ...

    assert getattr(handle, ON_EVENT)[0].errors == expected


def test_an_explicit_isolate_policy_is_kept_for_inline_handlers() -> None:
    @on_event(UserRegistered, errors="isolate")
    async def handle(self: Any, event: UserRegistered) -> None: ...

    assert getattr(handle, ON_EVENT)[0].errors == "isolate"


def test_stacking_accumulates_declarations() -> None:
    @on_event(UserDeleted, priority=5)
    @on_event(UserRegistered, mode="concurrent")
    async def handle(self: Any, event: UserRegistered | UserDeleted) -> None: ...

    metas = getattr(handle, ON_EVENT)

    assert [(m.event_type, m.mode, m.priority) for m in metas] == [
        (UserRegistered, "concurrent", 0),
        (UserDeleted, "inline", 5),
    ]


def test_metadata_records_every_option() -> None:
    @on_event(mode="background", priority=-3, enabled=False)
    async def handle(self: Any, event: UserRegistered) -> None: ...

    (meta,) = getattr(handle, ON_EVENT)

    assert (meta.event_type, meta.mode, meta.priority, meta.errors, meta.enabled) == (
        None,
        "background",
        -3,
        "isolate",
        False,
    )


def test_above_staticmethod_the_marker_lands_on_the_function() -> None:
    async def handle(event: UserRegistered) -> None: ...

    wrapped = staticmethod(handle)

    result = on_event(UserRegistered)(wrapped)

    assert result is wrapped
    assert getattr(handle, ON_EVENT)[0].event_type is UserRegistered


def test_decorating_a_non_callable_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError):
        on_event(UserRegistered)(42)
