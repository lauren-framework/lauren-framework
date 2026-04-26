"""Unit tests for function-based injectables.

Exercises :class:`DIContainer` with providers declared via
``@injectable()`` on a ``def`` rather than a class. The key contract
points:

* The function's parameters are resolved through DI, exactly like a
  class constructor's.
* The function's return value IS the dependency \u2014 the container does
  not wrap it.
* Async functions are awaited, sync functions are not.
* Scope rules (SINGLETON / REQUEST / TRANSIENT) still apply; a
  ``SINGLETON`` function factory is called exactly once per app.
* A class can depend on a function provider via a field annotation
  ``field: Depends[factory_fn]`` or ``field: Annotated[T, Depends]``.
* The usual error paths (missing dep, duplicate registration) still
  fire cleanly for function providers.
"""

from __future__ import annotations

import pytest

from lauren import Depends, Scope, injectable
from lauren._di import DIContainer
from lauren.exceptions import DuplicateBindingError, MissingProviderError


@injectable()
class _ConfigService:
    def __init__(self) -> None:
        self.db_url = "sqlite://:memory:"
        self.created_at = "2030-01-01"


class TestFunctionProvider:
    @pytest.mark.asyncio
    async def test_simple_function_factory(self):
        @injectable()
        def make_url(cfg: _ConfigService) -> str:
            return f"postgres://{cfg.db_url}"

        c = DIContainer()
        c.register(_ConfigService)
        c.register(make_url)
        c.compile()
        value = await c.resolve(make_url)
        assert value == "postgres://sqlite://:memory:"

    @pytest.mark.asyncio
    async def test_async_function_factory_is_awaited(self):
        @injectable()
        async def make_async(cfg: _ConfigService) -> dict:
            return {"url": cfg.db_url}

        c = DIContainer()
        c.register(_ConfigService)
        c.register(make_async)
        c.compile()
        value = await c.resolve(make_async)
        assert value == {"url": "sqlite://:memory:"}

    @pytest.mark.asyncio
    async def test_singleton_factory_called_once(self):
        call_count = {"n": 0}

        @injectable(scope=Scope.SINGLETON)
        def counter(cfg: _ConfigService) -> int:
            call_count["n"] += 1
            return call_count["n"]

        c = DIContainer()
        c.register(_ConfigService)
        c.register(counter)
        c.compile()
        a = await c.resolve(counter)
        b = await c.resolve(counter)
        assert a == 1 and b == 1
        assert call_count["n"] == 1

    @pytest.mark.asyncio
    async def test_transient_factory_called_each_time(self):
        counter = {"n": 0}

        @injectable(scope=Scope.TRANSIENT)
        def step() -> int:
            counter["n"] += 1
            return counter["n"]

        c = DIContainer()
        c.register(step)
        c.compile()
        assert await c.resolve(step) == 1
        assert await c.resolve(step) == 2
        assert await c.resolve(step) == 3

    @pytest.mark.asyncio
    async def test_function_without_params(self):
        @injectable()
        def constant() -> str:
            return "hello"

        c = DIContainer()
        c.register(constant)
        c.compile()
        assert await c.resolve(constant) == "hello"

    def test_missing_dep_fails_compile(self):
        class _NotRegistered:
            pass

        @injectable()
        def broken(dep: _NotRegistered) -> str:
            return "unreachable"

        c = DIContainer()
        c.register(broken)
        with pytest.raises(MissingProviderError):
            c.compile()

    def test_duplicate_registration_rejected(self):
        @injectable()
        def factory() -> int:
            return 1

        c = DIContainer()
        c.register(factory)
        with pytest.raises(DuplicateBindingError):
            c.register(factory)


class TestFieldLevelDependsOnFunction:
    @pytest.mark.asyncio
    async def test_field_annotated_depends_on_function(self):
        @injectable()
        def session_factory(cfg: _ConfigService) -> str:
            return f"Sess({cfg.db_url})"

        @injectable()
        class UserRepo:
            # The example from the prompt: a class declares a field-level
            # dependency on a function provider via Depends[fn].
            sess: Depends[session_factory]

            def describe(self) -> str:
                return self.sess

        c = DIContainer()
        c.register(_ConfigService)
        c.register(session_factory)
        c.register(UserRepo)
        c.compile()
        repo = await c.resolve(UserRepo)
        assert repo.describe() == "Sess(sqlite://:memory:)"

    @pytest.mark.asyncio
    async def test_classic_init_param_depends_on_function(self):
        @injectable()
        def session_factory(cfg: _ConfigService) -> str:
            return f"Sess({cfg.db_url})"

        @injectable()
        class UserRepo:
            def __init__(self, sess: Depends[session_factory]) -> None:
                self.sess = sess

        c = DIContainer()
        c.register(_ConfigService)
        c.register(session_factory)
        c.register(UserRepo)
        c.compile()
        repo = await c.resolve(UserRepo)
        assert repo.sess == "Sess(sqlite://:memory:)"

    @pytest.mark.asyncio
    async def test_same_factory_shared_across_consumers(self):
        @injectable(scope=Scope.SINGLETON)
        def factory() -> object:
            return object()

        @injectable()
        class A:
            dep: Depends[factory]

        @injectable()
        class B:
            dep: Depends[factory]

        c = DIContainer()
        c.register(factory)
        c.register(A)
        c.register(B)
        c.compile()
        a = await c.resolve(A)
        b = await c.resolve(B)
        # Both classes share the same singleton instance produced by
        # the factory \u2014 identity check is enough.
        assert a.dep is b.dep
