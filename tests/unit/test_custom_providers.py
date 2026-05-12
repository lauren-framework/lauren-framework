"""Unit tests for the four custom-provider helpers.

These tests exercise the :class:`DIContainer` directly so the
contract is locked in independently of the higher-level
:class:`LaurenFactory` pipeline. The integration tests in
``tests/integration/test_custom_providers_integration.py`` cover the
end-to-end story (modules, exports, route handlers).

The four families mirror NestJS:

* ``useValue``  \u2192 :func:`use_value`     \u2192 :meth:`DIContainer.register_value`
* ``useClass``  \u2192 :func:`use_class`     \u2192 :meth:`DIContainer.register_class`
* ``useFactory``\u2192 :func:`use_factory`   \u2192 :meth:`DIContainer.register_factory`
* ``useExisting``\u2192 :func:`use_existing`\u2192 :meth:`DIContainer.register_alias`
"""

from __future__ import annotations

from typing import Annotated, Protocol, runtime_checkable

import pytest

from lauren import (
    DIContainer,
    Inject,
    OptionalDep,
    Scope,
    Token,
    injectable,
    use_class,
    use_existing,
    use_factory,
    use_value,
)
from lauren._di.custom import CustomProvider
from lauren.exceptions import (
    CircularDependencyError,
    DecoratorUsageError,
    DuplicateBindingError,
    MissingProviderError,
)


# ---------------------------------------------------------------------------
# Token primitive
# ---------------------------------------------------------------------------


class TestToken:
    def test_unique_tokens_are_distinct_by_identity(self):
        a = Token("X")
        b = Token("X")
        # Same name, but unique=True (the default) makes them different
        # keys \u2014 mirrors NestJS's symbol-token convention.
        assert a is not b
        assert a != b
        assert hash(a) != hash(b)

    def test_shared_tokens_compare_by_name(self):
        a = Token("DB", unique=False)
        b = Token("DB", unique=False)
        assert a == b
        assert hash(a) == hash(b)

    def test_unique_and_shared_never_match(self):
        u = Token("Y", unique=True)
        s = Token("Y", unique=False)
        assert u != s

    def test_repr_includes_name_and_kind(self):
        assert "X" in repr(Token("X"))
        assert "unique" in repr(Token("X"))
        assert "shared" in repr(Token("X", unique=False))

    def test_empty_name_rejected(self):
        with pytest.raises(ValueError):
            Token("")

    def test_non_string_name_rejected(self):
        with pytest.raises(ValueError):
            Token(123)  # type: ignore[arg-type]

    def test_describe_uses_name_attribute(self):
        # Tokens expose a ``__name__`` so error messages render them
        # like classes \u2014 see the test on _describe behaviour below.
        from lauren._di import _describe

        assert _describe(Token("X")) == "Token(X)"


# ---------------------------------------------------------------------------
# CustomProvider construction
# ---------------------------------------------------------------------------


class TestCustomProviderRecords:
    def test_use_value_record_shape(self):
        rec = use_value(provide="X", value=42)
        assert isinstance(rec, CustomProvider)
        assert rec.provide == "X"
        assert rec.kind == "value"
        assert rec.value == 42

    def test_use_class_record_shape(self):
        class Impl:
            pass

        rec = use_class(provide=Impl, use=Impl)
        assert rec.kind == "class"
        assert rec.use_class is Impl

    def test_use_factory_record_shape(self):
        rec = use_factory(
            provide="C",
            factory=lambda: "value",
            injects=[],
        )
        assert rec.kind == "factory"
        assert callable(rec.factory)
        assert rec.inject == ()

    def test_use_existing_record_shape(self):
        rec = use_existing(provide="A", existing="B")
        assert rec.kind == "existing"
        assert rec.existing == "B"

    def test_use_value_rejects_unhashable_token(self):
        with pytest.raises(DecoratorUsageError):
            use_value(provide=[], value=1)  # type: ignore[arg-type]

    def test_use_value_rejects_none_token(self):
        with pytest.raises(DecoratorUsageError):
            use_value(provide=None, value=1)

    def test_use_class_rejects_non_class_use(self):
        with pytest.raises(DecoratorUsageError):
            use_class(provide="X", use=lambda: None)  # type: ignore[arg-type]

    def test_use_factory_rejects_non_callable(self):
        with pytest.raises(DecoratorUsageError):
            use_factory(provide="X", factory=42)  # type: ignore[arg-type]

    def test_use_existing_rejects_self_alias(self):
        with pytest.raises(DecoratorUsageError):
            use_existing(provide="X", existing="X")


# ---------------------------------------------------------------------------
# useValue \u2014 container behaviour
# ---------------------------------------------------------------------------


class TestUseValueContainer:
    @pytest.mark.asyncio
    async def test_resolves_to_literal_value(self):
        container = DIContainer()
        sentinel = {"deep": {"thought": 42}}
        container.register_value("CFG", sentinel)
        container.compile()
        assert await container.resolve("CFG") is sentinel

    @pytest.mark.asyncio
    async def test_singleton_caching_returns_same_instance(self):
        container = DIContainer()
        instance = object()
        container.register_value("X", instance)
        container.compile()
        a = await container.resolve("X")
        b = await container.resolve("X")
        assert a is b is instance

    @pytest.mark.asyncio
    async def test_class_token_works(self):
        # NestJS's "mock CatsService" pattern: register a value under
        # a class token. Other callers depending on the class still
        # resolve to the value.
        class CatsService:
            def meow(self) -> str:
                raise RuntimeError("real implementation \u2014 should never run")

        mock = type("MockCats", (), {"meow": lambda self: "mock-meow"})()
        container = DIContainer()
        container.register_value(CatsService, mock)
        container.compile()
        resolved = await container.resolve(CatsService)
        assert resolved is mock
        assert resolved.meow() == "mock-meow"

    @pytest.mark.asyncio
    async def test_token_instance_works(self):
        DB_URL = Token("DB_URL")
        container = DIContainer()
        container.register_value(DB_URL, "postgres://localhost/x")
        container.compile()
        assert await container.resolve(DB_URL) == "postgres://localhost/x"

    def test_duplicate_value_registration_rejected(self):
        container = DIContainer()
        container.register_value("X", 1)
        with pytest.raises(DuplicateBindingError):
            container.register_value("X", 2)

    @pytest.mark.asyncio
    async def test_value_with_none_is_legal(self):
        # NestJS allows ``useValue: null``; lauren must too. The token
        # itself can't be None (rejected by use_value), but the *value*
        # can be \u2014 useful for "feature absent" sentinels.
        container = DIContainer()
        container.register_value("MAYBE", None)
        container.compile()
        assert await container.resolve("MAYBE") is None


# ---------------------------------------------------------------------------
# useClass \u2014 container behaviour
# ---------------------------------------------------------------------------


class TestUseClassContainer:
    @pytest.mark.asyncio
    async def test_class_substitution(self):
        @injectable()
        class ConfigService:
            value = "default"

        class DevConfig:
            value = "dev"

        container = DIContainer()
        # ConfigService token, but DevConfig is what gets built.
        container.register_class(ConfigService, DevConfig)
        container.compile()
        instance = await container.resolve(ConfigService)
        assert isinstance(instance, DevConfig)
        assert instance.value == "dev"

    @pytest.mark.asyncio
    async def test_string_token_with_class_provider(self):
        class Connector:
            def ping(self) -> str:
                return "pong"

        container = DIContainer()
        container.register_class("CONN", Connector)
        container.compile()
        instance = await container.resolve("CONN")
        assert isinstance(instance, Connector)
        assert instance.ping() == "pong"

    @pytest.mark.asyncio
    async def test_use_class_resolves_constructor_deps(self):
        @injectable()
        class Database:
            def __init__(self) -> None:
                self.connections = 0

        class Repo:
            def __init__(self, db: Database) -> None:
                self.db = db

        container = DIContainer()
        container.register(Database)
        container.register_class("REPO", Repo)
        container.compile()
        repo = await container.resolve("REPO")
        assert isinstance(repo, Repo)
        assert isinstance(repo.db, Database)

    @pytest.mark.asyncio
    async def test_request_scope_use_class(self):
        class Counter:
            n = 0

            def __init__(self) -> None:
                Counter.n += 1
                self.id = Counter.n

        container = DIContainer()
        container.register_class("CTR", Counter, scope=Scope.REQUEST)
        container.compile()
        cache_a: dict = {}
        cache_b: dict = {}
        a = await container.resolve("CTR", request_cache=cache_a)
        a2 = await container.resolve("CTR", request_cache=cache_a)
        b = await container.resolve("CTR", request_cache=cache_b)
        assert a is a2  # same request \u2192 cached
        assert a is not b  # new request \u2192 fresh

    @pytest.mark.asyncio
    async def test_undecorated_class_auto_marked(self):
        # Even without @injectable, register_class must accept the
        # class \u2014 the user has explicitly opted-in by writing
        # ``use_class``. This trims a class of boilerplate.
        class Plain:
            pass

        container = DIContainer()
        container.register_class("X", Plain)
        container.compile()
        assert isinstance(await container.resolve("X"), Plain)


# ---------------------------------------------------------------------------
# useFactory \u2014 container behaviour
# ---------------------------------------------------------------------------


class TestUseFactoryContainer:
    @pytest.mark.asyncio
    async def test_zero_arg_factory(self):
        container = DIContainer()
        container.register_factory("MSG", lambda: "hello")
        container.compile()
        assert await container.resolve("MSG") == "hello"

    @pytest.mark.asyncio
    async def test_factory_with_class_dep(self):
        @injectable()
        class Options:
            def __init__(self) -> None:
                self.url = "postgres://x"

        def make_conn(opts: Options) -> dict:
            return {"connected_to": opts.url}

        container = DIContainer()
        container.register(Options)
        container.register_factory("CONN", make_conn, inject=[Options])
        container.compile()
        result = await container.resolve("CONN")
        assert result == {"connected_to": "postgres://x"}

    @pytest.mark.asyncio
    async def test_factory_with_string_dep(self):
        # Factory whose dep is itself a string-token.
        container = DIContainer()
        container.register_value("A", 10)
        container.register_factory("B", lambda a: a * 2, inject=["A"])
        container.compile()
        assert await container.resolve("B") == 20

    @pytest.mark.asyncio
    async def test_factory_passes_deps_in_declared_order(self):
        # Lambda parameter names are arbitrary; the contract is purely
        # positional, matching NestJS. Verify by intentionally mixing
        # parameter names that don't match the inject names.
        container = DIContainer()
        container.register_value("A", "alpha")
        container.register_value("B", "beta")
        container.register_factory(
            "C",
            lambda first, second: f"{first}-{second}",
            inject=["A", "B"],
        )
        container.compile()
        assert await container.resolve("C") == "alpha-beta"

    @pytest.mark.asyncio
    async def test_async_factory_awaited(self):
        async def make_thing() -> str:
            return "ok"

        container = DIContainer()
        container.register_factory("ASYNC", make_thing)
        container.compile()
        assert await container.resolve("ASYNC") == "ok"

    @pytest.mark.asyncio
    async def test_optional_dep_missing_lowers_to_none(self):
        # The factory is called with ``None`` rather than raising.
        seen: list = []

        def make(thing) -> str:
            seen.append(thing)
            return "made"

        container = DIContainer()
        container.register_factory(
            "OUT",
            make,
            inject=[OptionalDep("MISSING")],
        )
        container.compile()
        assert await container.resolve("OUT") == "made"
        assert seen == [None]

    @pytest.mark.asyncio
    async def test_optional_dep_present_uses_value(self):
        container = DIContainer()
        container.register_value("LOGGER", "stdout")
        container.register_factory("OUT", lambda log: f"log={log}", inject=[OptionalDep("LOGGER")])
        container.compile()
        assert await container.resolve("OUT") == "log=stdout"

    @pytest.mark.asyncio
    async def test_factory_singleton_called_once(self):
        calls = {"n": 0}

        def make() -> int:
            calls["n"] += 1
            return calls["n"]

        container = DIContainer()
        container.register_factory("CTR", make, scope=Scope.SINGLETON)
        container.compile()
        a = await container.resolve("CTR")
        b = await container.resolve("CTR")
        assert a == 1 and b == 1  # same singleton instance reused

    @pytest.mark.asyncio
    async def test_factory_transient_called_each_time(self):
        calls = {"n": 0}

        def make() -> int:
            calls["n"] += 1
            return calls["n"]

        container = DIContainer()
        container.register_factory("CTR", make, scope=Scope.TRANSIENT)
        container.compile()
        a = await container.resolve("CTR")
        b = await container.resolve("CTR")
        assert (a, b) == (1, 2)


# ---------------------------------------------------------------------------
# useExisting \u2014 container behaviour
# ---------------------------------------------------------------------------


class TestUseExistingContainer:
    @pytest.mark.asyncio
    async def test_alias_resolves_to_target_instance(self):
        @injectable()
        class LoggerService:
            pass

        container = DIContainer()
        container.register(LoggerService)
        container.register_alias("ALIAS", LoggerService)
        container.compile()
        a = await container.resolve(LoggerService)
        b = await container.resolve("ALIAS")
        # Same singleton via two tokens.
        assert a is b

    @pytest.mark.asyncio
    async def test_alias_chain(self):
        @injectable()
        class Real:
            pass

        container = DIContainer()
        container.register(Real)
        container.register_alias("A", Real)
        container.register_alias("B", "A")
        container.compile()
        assert await container.resolve("B") is await container.resolve(Real)

    def test_alias_to_unknown_token_fails_at_compile(self):
        container = DIContainer()
        container.register_alias("A", "NEVER_REGISTERED")
        with pytest.raises(MissingProviderError, match="Alias"):
            container.compile()

    def test_alias_cycle_detected(self):
        # Two aliases pointing at each other create a cycle that
        # the compile-time DFS catches.
        container = DIContainer()
        container.register_alias("A", "B")
        container.register_alias("B", "A")
        with pytest.raises(CircularDependencyError):
            container.compile()


# ---------------------------------------------------------------------------
# Inject() annotation marker
# ---------------------------------------------------------------------------


class TestInjectAnnotation:
    @pytest.mark.asyncio
    async def test_inject_overrides_type_token_in_init(self):
        # ``Annotated[Connection, Inject("CONN")]`` resolves against
        # ``"CONN"``, not against ``Connection``.
        class Connection:
            def execute(self, sql: str) -> str:
                return f"executed {sql}"

        @injectable()
        class CatsRepo:
            def __init__(self, conn: Annotated[Connection, Inject("CONN")]) -> None:
                self.conn = conn

        real_conn = Connection()
        container = DIContainer()
        container.register_value("CONN", real_conn)
        container.register(CatsRepo)
        container.compile()
        repo = await container.resolve(CatsRepo)
        assert repo.conn is real_conn

    @pytest.mark.asyncio
    async def test_inject_in_class_body_field(self):
        class Connection: ...

        @injectable()
        class CatsRepo:
            conn: Annotated[Connection, Inject("CONN")]

        real_conn = Connection()
        container = DIContainer()
        container.register_value("CONN", real_conn)
        container.register(CatsRepo)
        container.compile()
        repo = await container.resolve(CatsRepo)
        assert repo.conn is real_conn

    @pytest.mark.asyncio
    async def test_inject_with_token_instance(self):
        DB_URL = Token("DB_URL")

        @injectable()
        class App:
            def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
                self.url = url

        container = DIContainer()
        container.register_value(DB_URL, "postgres://x")
        container.register(App)
        container.compile()
        app = await container.resolve(App)
        assert app.url == "postgres://x"


# ---------------------------------------------------------------------------
# Multi-binding interaction
# ---------------------------------------------------------------------------


class TestMultiBindingInteraction:
    @pytest.mark.asyncio
    async def test_multi_value_providers_collected(self):
        @runtime_checkable
        class Plugin(Protocol):
            name: str

        container = DIContainer()
        container.register_value(Plugin, type("P1", (), {"name": "a"})(), multi=True)
        container.register_value(Plugin, type("P2", (), {"name": "b"})(), multi=True)
        container.compile()
        plugins = await container.resolve(Plugin)
        assert sorted(p.name for p in plugins) == ["a", "b"]


# ---------------------------------------------------------------------------
# Cross-helper interaction
# ---------------------------------------------------------------------------


class TestCrossHelperInteraction:
    @pytest.mark.asyncio
    async def test_factory_resolves_use_class_token(self):
        # use_class registers a class under a string token, then a
        # factory injects that token.
        class RealOptions:
            url = "real"

        container = DIContainer()
        container.register_class("OPTS", RealOptions)
        container.register_factory("CONN", lambda opts: f"connected:{opts.url}", inject=["OPTS"])
        container.compile()
        assert await container.resolve("CONN") == "connected:real"

    @pytest.mark.asyncio
    async def test_alias_to_use_factory(self):
        container = DIContainer()
        container.register_factory("ORIGINAL", lambda: "value")
        container.register_alias("NICE_NAME", "ORIGINAL")
        container.compile()
        assert await container.resolve("NICE_NAME") is await container.resolve("ORIGINAL")

    @pytest.mark.asyncio
    async def test_class_token_overridden_by_value(self):
        # The classic test mock: ``use_value`` swaps a real service.
        class RealService:
            def __init__(self) -> None:
                raise RuntimeError("never instantiated in tests")

            def query(self) -> str:
                return "real"

        # No ``register(RealService)`` \u2014 the value provider replaces it.
        mock = type("Mock", (), {"query": lambda self: "mock"})()
        container = DIContainer()
        container.register_value(RealService, mock)
        container.compile()
        assert (await container.resolve(RealService)).query() == "mock"
