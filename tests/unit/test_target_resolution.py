"""Unit tests for ``python -m lauren`` TARGET resolution (``lauren._commands._runner``)."""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from lauren import ApplicationContext, Lauren, LaurenApp, LaurenFactory, injectable, module
from lauren._commands._runner import (
    EXIT_STARTUP,
    EXIT_USAGE,
    _TargetError,
    context_for,
    import_target,
    load_context,
    render_startup_error,
    split_target,
)
from lauren.exceptions import MissingProviderError


@injectable()
class Service:
    pass


@module(providers=[Service])
class TargetModule:
    pass


class NotAModule:
    pass


@pytest.fixture
def fake_module() -> Iterator[types.ModuleType]:
    """A module importable as ``lauren_target_fixture`` for the duration of a test."""
    mod = types.ModuleType("lauren_target_fixture")
    mod.AppModule = TargetModule  # type: ignore[attr-defined]
    mod.nested = types.SimpleNamespace(AppModule=TargetModule)  # type: ignore[attr-defined]
    sys.modules[mod.__name__] = mod
    try:
        yield mod
    finally:
        del sys.modules[mod.__name__]


@pytest.mark.parametrize(
    ("spec", "expected"),
    [("app:AppModule", ("app", "AppModule")), ("pkg.main:factory.create", ("pkg.main", "factory.create"))],
)
def test_split_target_accepts_module_colon_attr(spec: str, expected: tuple[str, str]) -> None:
    assert split_target(spec) == expected


@pytest.mark.parametrize(
    "spec", ["app", "app:", ":AppModule", "app:App-Module", "my app:x", "a..b:x", "a:b:c"]
)
def test_split_target_rejects_malformed_specs(spec: str) -> None:
    with pytest.raises(_TargetError) as info:
        split_target(spec)

    assert info.value.exit_code == EXIT_USAGE


def test_import_target_returns_the_attribute(fake_module: types.ModuleType) -> None:
    assert import_target("lauren_target_fixture:AppModule") is TargetModule
    assert import_target("lauren_target_fixture:nested.AppModule") is TargetModule


def test_import_target_reports_a_missing_attribute(fake_module: types.ModuleType) -> None:
    with pytest.raises(_TargetError) as info:
        import_target("lauren_target_fixture:Missing")

    assert info.value.exit_code == EXIT_STARTUP
    assert "has no attribute 'Missing'" in str(info.value)


def test_import_target_reports_a_missing_module() -> None:
    with pytest.raises(_TargetError) as info:
        import_target("lauren_no_such_module_xyz.sub:AppModule")

    assert "cannot import" in str(info.value)


def test_import_errors_inside_the_target_module_propagate(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_import(name: str) -> Any:
        raise ModuleNotFoundError("No module named 'sqlalchemy'", name="sqlalchemy")

    monkeypatch.setattr("importlib.import_module", broken_import)

    with pytest.raises(ModuleNotFoundError):
        import_target("app.main:AppModule")


async def test_a_module_class_is_built_into_a_context() -> None:
    ctx = await context_for(TargetModule)

    assert isinstance(ctx, ApplicationContext)
    assert ctx.app.container.has_provider(Service)
    assert ctx.app._started is False


async def test_a_lauren_app_is_wrapped() -> None:
    app = LaurenFactory.create(TargetModule)

    ctx = await context_for(app)

    assert ctx.app is app


async def test_an_imperative_lauren_app_is_compiled_and_wrapped() -> None:
    lauren_app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)
    lauren_app.add_provider(Service)

    ctx = await context_for(lauren_app)

    assert isinstance(ctx.app, LaurenApp)
    assert ctx.app._started is False
    assert lauren_app._compiled is ctx.app


async def test_a_context_is_returned_as_is() -> None:
    existing = LaurenFactory.create_context(TargetModule)

    assert await context_for(existing) is existing


@pytest.mark.parametrize("kind", ["sync", "async"])
async def test_a_zero_argument_factory_is_called(kind: str) -> None:
    if kind == "sync":

        def create_app() -> LaurenApp:
            return LaurenFactory.create(TargetModule)

    else:

        async def create_app() -> LaurenApp:
            return LaurenFactory.create(TargetModule)

    ctx = await context_for(create_app)

    assert isinstance(ctx, ApplicationContext)


async def test_a_factory_returning_a_factory_is_rejected() -> None:
    def outer() -> Any:
        return lambda: LaurenFactory.create(TargetModule)

    with pytest.raises(_TargetError):
        await context_for(outer)


async def test_a_factory_needing_arguments_is_rejected() -> None:
    def create_app(settings: Any) -> LaurenApp:  # pragma: no cover - never called
        return LaurenFactory.create(TargetModule)

    with pytest.raises(_TargetError) as info:
        await context_for(create_app, "main:create_app")

    assert "needs arguments" in str(info.value)


async def test_a_plain_class_is_rejected() -> None:
    with pytest.raises(_TargetError) as info:
        await context_for(NotAModule, "main:NotAModule")

    assert "not a @module" in str(info.value)


@pytest.mark.parametrize("obj", [42, "app", None])
async def test_other_objects_are_rejected(obj: Any) -> None:
    with pytest.raises(_TargetError) as info:
        await context_for(obj, "main:thing")

    assert "expected a @module class" in str(info.value)


async def test_load_context_imports_and_wraps(fake_module: types.ModuleType) -> None:
    ctx = await load_context("lauren_target_fixture:AppModule")

    assert ctx.app.container.has_provider(Service)


def test_render_startup_error_shows_the_detail() -> None:
    error = MissingProviderError("No provider for Db", detail={"token": "Db", "module": "AppModule"})

    text = render_startup_error(error)

    assert text.splitlines()[:4] == [
        "error: the application failed to build",
        "MissingProviderError: No provider for Db",
        '  token: "Db"',
        '  module: "AppModule"',
    ]


def test_render_other_errors_suggests_verbose() -> None:
    text = render_startup_error(ValueError("boom"), action="start")

    assert "failed to start: ValueError: boom" in text
    assert "--verbose" in text


def test_render_verbose_includes_the_traceback() -> None:
    try:
        raise ValueError("boom")
    except ValueError as exc:
        text = render_startup_error(exc, verbose=True)

    assert "Traceback (most recent call last)" in text
