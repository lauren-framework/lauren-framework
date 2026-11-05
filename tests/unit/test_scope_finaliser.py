"""Unit tests for the shared request-scope finaliser and its provider index."""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import pytest

from lauren import DIContainer, Scope, injectable, pre_destruct, use_existing, use_value
from lauren._di._scope import finalize_scope
from lauren.exceptions import DestructError


def _container(*providers: Any) -> DIContainer:
    from lauren._di.custom import CustomProvider

    container = DIContainer()
    for p in providers:
        if isinstance(p, CustomProvider):
            container.register_custom(p)
        else:
            container.register(p)
    container.compile()
    return container


def _recording_classes(events: list[str]) -> tuple[type, type]:
    @injectable(scope=Scope.REQUEST)
    class First:
        @pre_destruct
        async def close(self) -> None:
            events.append("First.pre_destruct")

        async def aclose(self) -> None:
            events.append("First.aclose")

    @injectable(scope=Scope.REQUEST)
    class Second:
        @pre_destruct
        def close(self) -> None:
            events.append("Second.pre_destruct")

    return First, Second


async def test_empty_cache_returns_no_errors() -> None:
    assert await finalize_scope(DIContainer(), {}) == []


async def test_instances_tear_down_newest_first_pre_destruct_before_aclose() -> None:
    events: list[str] = []
    First, Second = _recording_classes(events)
    container = _container(First, Second)
    cache: dict[Any, Any] = {}
    await container.resolve(First, request_cache=cache)
    await container.resolve(Second, request_cache=cache)

    errors = await finalize_scope(container, cache)

    assert errors == []
    assert events == ["Second.pre_destruct", "First.pre_destruct", "First.aclose"]


async def test_the_callers_cache_is_not_mutated() -> None:
    events: list[str] = []
    First, _ = _recording_classes(events)
    container = _container(First)
    cache: dict[Any, Any] = {}
    instance = await container.resolve(First, request_cache=cache)

    await finalize_scope(container, cache)

    assert cache == {First: instance}


@pytest.mark.parametrize("stage", ["pre_destruct", "aclose"])
async def test_teardown_errors_are_logged_collected_and_never_raised(
    stage: str, caplog: pytest.LogCaptureFixture
) -> None:
    events: list[str] = []
    boom = RuntimeError("boom")

    @injectable(scope=Scope.REQUEST)
    class Flaky:
        @pre_destruct
        def close(self) -> None:
            events.append("pre_destruct")
            if stage == "pre_destruct":
                raise boom

        def aclose(self) -> None:
            events.append("aclose")
            if stage == "aclose":
                raise boom

    container = _container(Flaky)
    cache: dict[Any, Any] = {}
    await container.resolve(Flaky, request_cache=cache)

    with caplog.at_level(logging.ERROR, logger="lauren"):
        (error,) = await finalize_scope(container, cache, origin="websocket")

    assert events == ["pre_destruct", "aclose"]  # a failing hook does not skip aclose()
    assert isinstance(error, DestructError)
    assert error.__cause__ is boom
    assert error.detail == {"class": "Flaky", "stage": stage, "origin": "websocket", "cause": "boom"}
    assert "Flaky" in caplog.text and "websocket teardown" in caplog.text


async def test_every_pre_destruct_hook_of_a_request_scoped_instance_runs() -> None:
    events: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class Session:
        @pre_destruct
        def flush(self) -> None:
            events.append("flush")

        @pre_destruct
        async def close(self) -> None:
            events.append("close")

    container = _container(Session)
    cache: dict[Any, Any] = {}
    await container.resolve(Session, request_cache=cache)

    errors = await finalize_scope(container, cache)

    assert errors == []
    assert events == ["flush", "close"]


async def test_generator_provider_teardown_runs_through_aclose() -> None:
    events: list[str] = []

    @injectable(scope=Scope.REQUEST)
    def session() -> Any:
        events.append("setup")
        yield "session"
        events.append("teardown")

    container = _container(session)
    cache: dict[Any, Any] = {}
    assert await container.resolve(session, request_cache=cache) == "session"

    await finalize_scope(container, cache)

    assert events == ["setup", "teardown"]


async def test_instance_without_a_known_provider_is_still_closed() -> None:
    closed: list[bool] = []

    class Loose:
        async def aclose(self) -> None:
            closed.append(True)

    await finalize_scope(DIContainer(), {"not-a-token": Loose()})

    assert closed == [True]


async def test_unknown_multi_binding_slot_is_labelled_by_type(caplog: pytest.LogCaptureFixture) -> None:
    class Orphan:
        def aclose(self) -> None:
            raise RuntimeError("x")

    with caplog.at_level(logging.ERROR, logger="lauren"):
        (error,) = await finalize_scope(DIContainer(), {12345: Orphan()})

    assert error.detail["class"] == "Orphan"


@runtime_checkable
class Plugin(Protocol):
    def name(self) -> str: ...


async def test_multi_bound_request_scoped_providers_run_pre_destruct() -> None:
    # Regression: the per-request ``{p.cls: p}`` map could not see multi-bound
    # providers (cached under ``id(provider)``), so their @pre_destruct never ran.
    events: list[str] = []

    @injectable(scope=Scope.REQUEST, provides=(Plugin,), multi=True)
    class A:
        def name(self) -> str:
            return "a"

        @pre_destruct
        def close(self) -> None:
            events.append("A")

    @injectable(scope=Scope.REQUEST, provides=(Plugin,), multi=True)
    class B:
        def name(self) -> str:
            return "b"

        @pre_destruct
        def close(self) -> None:
            events.append("B")

    container = _container(A, B)
    cache: dict[Any, Any] = {}
    await container.resolve(list[Plugin], request_cache=cache)

    await finalize_scope(container, cache)

    assert events == ["B", "A"]


def test_instance_index_is_built_at_compile_and_reused() -> None:
    @injectable(scope=Scope.REQUEST)
    class Svc:
        pass

    container = _container(Svc)

    assert container._instance_index is not None
    assert container.instance_index() is container.instance_index()
    assert container.instance_index()[Svc] is container.get_provider(Svc)


def test_instance_index_is_rebuilt_after_a_late_registration() -> None:
    @injectable()
    class Early:
        pass

    @injectable()
    class Late:
        pass

    container = _container(Early)
    before = container.instance_index()

    container.register(Late)

    assert container.instance_index() is not before
    assert set(container.instance_index()) == {Early, Late}


def test_instance_index_skips_aliases() -> None:
    @injectable()
    class Real:
        pass

    container = _container(Real, use_existing(provide="alias", existing=Real))

    assert set(container.instance_index()) == {Real}


def test_registered_providers_keep_multi_bound_siblings() -> None:
    container = _container(
        use_value(provide="plugins", value=1, multi=True),
        use_value(provide="plugins", value=2, multi=True),
    )

    assert len(container.all_providers()) == 1  # keyed by token
    assert [p.provider_kind for p in container.registered_providers()] == ["value", "value"]
