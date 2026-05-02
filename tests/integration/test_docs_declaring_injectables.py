"""Verify every code example in docs/guides/declaring-injectables.md.

Each test class corresponds to a section of the guide and runs the exact
pattern shown there. If a guide example silently breaks, one of these tests
will catch it before readers discover it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Annotated, Protocol, runtime_checkable

import pytest

from lauren import (
    Depends,
    Inject,
    LaurenFactory,
    Scope,
    Token,
    controller,
    get,
    injectable,
    module,
    post_construct,
    pre_destruct,
    use_value,
)
from lauren._di import DIContainer
from lauren.exceptions import (
    DIScopeViolationError,
    DuplicateBindingError,
    MetadataInheritanceError,
    MissingProviderError,
    ProtocolAmbiguityError,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers — stand-ins for external deps used in the doc snippets
# ---------------------------------------------------------------------------


@injectable()
class _Database:
    """Placeholder for a 'Database' class used in field-injection examples."""


# ---------------------------------------------------------------------------
# Section: The minimum viable injectable
# ---------------------------------------------------------------------------


class TestMinimumViableInjectable:
    def test_injectable_decorator_preserves_class_identity(self):
        @injectable()
        class Clock:
            def now(self) -> float:
                return time.monotonic()

        assert Clock.__name__ == "Clock"
        # The decorator must return the original class, never a wrapper.
        inst = Clock()
        assert isinstance(inst, Clock)
        assert inst.now() > 0

    def test_injectable_registered_in_module(self):
        @injectable()
        class Clock2:
            def now(self) -> float:
                return time.monotonic()

        @controller("/")
        class _C:
            def __init__(self, clock: Clock2) -> None:
                self.clock = clock

            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        @module(controllers=[_C], providers=[Clock2])
        class AppModule:
            pass

        app = LaurenFactory.create(AppModule)
        assert TestClient(app).get("/").status_code == 200

    def test_bare_injectable_without_parens_is_rejected(self):
        from lauren.exceptions import DecoratorUsageError

        with pytest.raises((DecoratorUsageError, TypeError)):

            @injectable  # type: ignore[arg-type]
            class Oops:
                pass


# ---------------------------------------------------------------------------
# Section: Choosing a scope
# ---------------------------------------------------------------------------


class TestScopes:
    @pytest.mark.asyncio
    async def test_singleton_resolves_same_instance(self):
        @injectable(scope=Scope.SINGLETON)
        class Svc:
            pass

        c = DIContainer()
        c.register(Svc)
        c.compile()
        a = await c.resolve(Svc)
        b = await c.resolve(Svc)
        assert a is b

    @pytest.mark.asyncio
    async def test_transient_resolves_new_instance_each_time(self):
        @injectable(scope=Scope.TRANSIENT)
        class Unique:
            pass

        c = DIContainer()
        c.register(Unique)
        c.compile()
        a = await c.resolve(Unique)
        b = await c.resolve(Unique)
        assert a is not b

    def test_scope_violation_rejected_at_startup(self):
        @injectable(scope=Scope.REQUEST)
        class DbSession:
            pass

        @injectable(scope=Scope.SINGLETON)
        class BadSvc:
            def __init__(self, session: DbSession) -> None:
                self.session = session

        @controller("/")
        class _C:
            def __init__(self, svc: BadSvc) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[_C], providers=[DbSession, BadSvc])
        class BadModule:
            pass

        with pytest.raises(DIScopeViolationError):
            LaurenFactory.create(BadModule)


# ---------------------------------------------------------------------------
# Section: Constructor injection
# ---------------------------------------------------------------------------


class TestConstructorInjection:
    @pytest.mark.asyncio
    async def test_constructor_injection_resolved(self):
        @injectable()
        class _Clock:
            pass

        @injectable()
        class _UserRepo:
            pass

        @injectable()
        class UserService:
            def __init__(self, repo: _UserRepo, clock: _Clock) -> None:
                self.repo = repo
                self.clock = clock

        c = DIContainer()
        c.register(_Clock)
        c.register(_UserRepo)
        c.register(UserService)
        c.compile()
        svc = await c.resolve(UserService)
        assert isinstance(svc.repo, _UserRepo)
        assert isinstance(svc.clock, _Clock)

    @pytest.mark.asyncio
    async def test_optional_param_default_used_when_no_provider(self):
        @injectable(scope=Scope.SINGLETON)
        @dataclass
        class Settings:
            database_url: str = "sqlite:///:memory:"
            jwt_secret: str = "dev"

        c = DIContainer()
        c.register(Settings)
        c.compile()
        s = await c.resolve(Settings)
        assert s.database_url == "sqlite:///:memory:"
        assert s.jwt_secret == "dev"

    def test_token_and_inject_resolve_string_value(self):
        # use_value goes in @module(providers=[...]), not DIContainer.register() —
        # the module pipeline routes it to the appropriate registration method.
        DB_URL = Token("DB_URL")

        @injectable()
        class Repo:
            def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
                self.url = url

        @controller("/repo")
        class _C:
            def __init__(self, repo: Repo) -> None:
                self.repo = repo

            @get("/url")
            async def h(self) -> dict:
                return {"url": self.repo.url}

        @module(
            controllers=[_C],
            providers=[
                use_value(provide=DB_URL, value="postgres://localhost/app"),
                Repo,
            ],
        )
        class AppModule:
            pass

        app = LaurenFactory.create(AppModule)
        r = TestClient(app).get("/repo/url")
        assert r.status_code == 200
        assert r.json()["url"] == "postgres://localhost/app"


# ---------------------------------------------------------------------------
# Section: Class-field injection
# ---------------------------------------------------------------------------


class TestClassFieldInjection:
    @pytest.mark.asyncio
    async def test_field_injection_without_init(self):
        @injectable()
        class FieldDb:
            pass

        @injectable()
        class FieldClock:
            pass

        @injectable()
        class RepoWithFields:
            db: FieldDb
            clock: FieldClock

        c = DIContainer()
        c.register(FieldDb)
        c.register(FieldClock)
        c.register(RepoWithFields)
        c.compile()
        repo = await c.resolve(RepoWithFields)
        assert isinstance(repo.db, FieldDb)
        assert isinstance(repo.clock, FieldClock)

    def test_annotated_field_with_inject_token(self):
        DB_URL_TOK = Token("DB_URL_FIELD")

        @injectable()
        class RepoAnnotated:
            url: Annotated[str, Inject(DB_URL_TOK)]

        @controller("/ranno")
        class _C:
            def __init__(self, repo: RepoAnnotated) -> None:
                self.repo = repo

            @get("/url")
            async def h(self) -> dict:
                return {"url": self.repo.url}

        @module(
            controllers=[_C],
            providers=[
                use_value(provide=DB_URL_TOK, value="sqlite://test"),
                RepoAnnotated,
            ],
        )
        class AppModule:
            pass

        app = LaurenFactory.create(AppModule)
        r = TestClient(app).get("/ranno/url")
        assert r.status_code == 200
        assert r.json()["url"] == "sqlite://test"


# ---------------------------------------------------------------------------
# Section: Binding to Protocols
# ---------------------------------------------------------------------------


class TestProtocols:
    @pytest.mark.asyncio
    async def test_protocol_bound_provider_resolves(self):
        @runtime_checkable
        class EmailSender(Protocol):
            def send(self, to: str, msg: str) -> None: ...

        @injectable(provides=[EmailSender])
        class SmtpSender:
            def send(self, to: str, msg: str) -> None:
                pass

        @injectable()
        class Notifier:
            def __init__(self, sender: EmailSender) -> None:
                self._sender = sender

        c = DIContainer()
        c.register(SmtpSender)
        c.register(Notifier)
        c.compile()
        notifier = await c.resolve(Notifier)
        assert isinstance(notifier._sender, SmtpSender)

    def test_two_providers_same_protocol_without_multi_raises(self):
        @runtime_checkable
        class Mailer(Protocol):
            def send(self, msg: str) -> None: ...

        @injectable(provides=[Mailer])
        class MailerA:
            def send(self, msg: str) -> None:
                pass

        @injectable(provides=[Mailer])
        class MailerB:
            def send(self, msg: str) -> None:
                pass

        @injectable()
        class Consumer:
            def __init__(self, m: Mailer) -> None:
                self.m = m

        c = DIContainer()
        c.register(MailerA)
        c.register(MailerB)
        c.register(Consumer)
        with pytest.raises(ProtocolAmbiguityError):
            c.compile()


# ---------------------------------------------------------------------------
# Section: Multi-bindings — list[T]
# ---------------------------------------------------------------------------


class TestMultiBindings:
    @pytest.mark.asyncio
    async def test_list_injection_receives_all_multi_providers(self):
        @runtime_checkable
        class Sender(Protocol):
            def send(self, to: str, msg: str) -> None: ...

        @injectable(provides=[Sender], multi=True)
        class SmtpSender:
            def send(self, to: str, msg: str) -> None:
                pass

        @injectable(provides=[Sender], multi=True)
        class SmsSender:
            def send(self, to: str, msg: str) -> None:
                pass

        @injectable()
        class Dispatcher:
            def __init__(self, senders: list[Sender]) -> None:
                self._senders = senders

        c = DIContainer()
        c.register(SmtpSender)
        c.register(SmsSender)
        c.register(Dispatcher)
        c.compile()
        dispatcher = await c.resolve(Dispatcher)
        assert len(dispatcher._senders) == 2
        types = {type(s) for s in dispatcher._senders}
        assert SmtpSender in types
        assert SmsSender in types

    def test_list_injection_of_non_multi_raises(self):
        @runtime_checkable
        class Bus(Protocol):
            def publish(self, msg: str) -> None: ...

        @injectable(provides=[Bus])
        class EventBus:
            def publish(self, msg: str) -> None:
                pass

        @injectable()
        class Consumer:
            def __init__(self, buses: list[Bus]) -> None:
                self.buses = buses

        c = DIContainer()
        c.register(EventBus)
        c.register(Consumer)
        with pytest.raises(ProtocolAmbiguityError):
            c.compile()


# ---------------------------------------------------------------------------
# Section: Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    def test_post_construct_runs_after_creation(self):
        log = []

        @injectable()
        class TrackedService:
            @post_construct
            async def setup(self) -> None:
                log.append("setup")

        @controller("/")
        class _C:
            def __init__(self, svc: TrackedService) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[_C], providers=[TrackedService])
        class LM:
            pass

        TestClient(LaurenFactory.create(LM))
        assert "setup" in log

    @pytest.mark.asyncio
    async def test_pre_destruct_runs_on_shutdown(self):
        log = []

        @injectable()
        class ManagedResource:
            @post_construct
            async def open(self) -> None:
                log.append("open")

            @pre_destruct
            async def close(self) -> None:
                log.append("close")

        @controller("/")
        class _C:
            def __init__(self, res: ManagedResource) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[_C], providers=[ManagedResource])
        class LM2:
            pass

        app = LaurenFactory.create(LM2)
        TestClient(app)  # triggers startup → @post_construct runs
        await app.shutdown()  # triggers @pre_destruct

        assert log == ["open", "close"]


# ---------------------------------------------------------------------------
# Section: Strict inheritance — opt-in only
# ---------------------------------------------------------------------------


class TestStrictInheritance:
    def test_subclass_without_redecoration_raises_metadata_error(self):
        @injectable()
        class Base:
            pass

        class Internal(Base):
            pass

        @controller("/")
        class _C:
            def __init__(self, dep: Internal) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[_C], providers=[Internal])
        class BadMod:
            pass

        with pytest.raises(MetadataInheritanceError):
            LaurenFactory.create(BadMod)

    @pytest.mark.asyncio
    async def test_subclass_with_redecoration_works(self):
        @injectable()
        class Base2:
            pass

        @injectable()
        class External(Base2):
            pass

        c = DIContainer()
        c.register(External)
        c.compile()
        inst = await c.resolve(External)
        assert isinstance(inst, External)


# ---------------------------------------------------------------------------
# Section: Function-based providers
# ---------------------------------------------------------------------------


class TestFunctionBasedProviders:
    @pytest.mark.asyncio
    async def test_function_return_value_is_the_dependency(self):
        @injectable()
        def make_greeting() -> str:
            return "hello"

        # Consumed via Depends[factory_fn]
        @injectable()
        class Greeter:
            msg: Depends[make_greeting]

        c = DIContainer()
        c.register(make_greeting)
        c.register(Greeter)
        c.compile()
        greeter = await c.resolve(Greeter)
        assert greeter.msg == "hello"

    @pytest.mark.asyncio
    async def test_function_params_resolved_via_di(self):
        @injectable()
        class _Config:
            db_url = "sqlite://:memory:"

        @injectable()
        def make_url(cfg: _Config) -> str:
            return f"postgres://{cfg.db_url}"

        @injectable()
        class Repo:
            url: Depends[make_url]

        c = DIContainer()
        c.register(_Config)
        c.register(make_url)
        c.register(Repo)
        c.compile()
        repo = await c.resolve(Repo)
        assert repo.url == "postgres://sqlite://:memory:"

    @pytest.mark.asyncio
    async def test_async_function_factory_is_awaited(self):
        @injectable()
        async def make_token() -> str:
            return "secret"

        @injectable()
        class Auth:
            token: Depends[make_token]

        c = DIContainer()
        c.register(make_token)
        c.register(Auth)
        c.compile()
        auth = await c.resolve(Auth)
        assert auth.token == "secret"

    @pytest.mark.asyncio
    async def test_singleton_function_called_once(self):
        calls = {"n": 0}

        @injectable(scope=Scope.SINGLETON)
        def singleton_factory() -> object:
            calls["n"] += 1
            return object()

        @injectable()
        class ConsumerA:
            dep: Depends[singleton_factory]

        @injectable()
        class ConsumerB:
            dep: Depends[singleton_factory]

        c = DIContainer()
        c.register(singleton_factory)
        c.register(ConsumerA)
        c.register(ConsumerB)
        c.compile()
        a = await c.resolve(ConsumerA)
        b = await c.resolve(ConsumerB)
        assert a.dep is b.dep
        assert calls["n"] == 1

    def test_duplicate_function_registration_raises(self):
        @injectable()
        def dup_factory() -> int:
            return 1

        c = DIContainer()
        c.register(dup_factory)
        with pytest.raises(DuplicateBindingError):
            c.register(dup_factory)

    def test_function_with_missing_dep_raises_at_compile(self):
        class _Unregistered:
            pass

        @injectable()
        def broken(dep: _Unregistered) -> str:
            return "unreachable"

        c = DIContainer()
        c.register(broken)
        with pytest.raises(MissingProviderError):
            c.compile()

    @pytest.mark.asyncio
    async def test_depends_constructor_injection(self):
        """Docs show Depends[fn] in __init__ params too."""

        @injectable()
        def base_url() -> str:
            return "http://example.com"

        @injectable()
        class ApiClient:
            def __init__(self, url: Depends[base_url]) -> None:  # type: ignore[type-arg]
                self.url = url

        c = DIContainer()
        c.register(base_url)
        c.register(ApiClient)
        c.compile()
        client = await c.resolve(ApiClient)
        assert client.url == "http://example.com"


# ---------------------------------------------------------------------------
# Section: Verifying with the test client
# ---------------------------------------------------------------------------


class TestVerifyingWithTestClient:
    def test_full_app_resolves_injectable_via_container(self):
        @injectable()
        class Clock:
            def now(self) -> float:
                return time.monotonic()

        @controller("/")
        class _C:
            def __init__(self, clock: Clock) -> None:
                self.clock = clock

            @get("/time")
            async def h(self) -> dict:
                return {"t": self.clock.now()}

        @module(controllers=[_C], providers=[Clock])
        class AppModule:
            pass

        app = LaurenFactory.create(AppModule)
        c = TestClient(app)
        r = c.get("/time")
        assert r.status_code == 200
        assert isinstance(r.json()["t"], float)

    def test_singleton_swap_via_set_singleton(self):
        @injectable()
        class Clock:
            def now(self) -> float:
                return time.monotonic()

        @controller("/")
        class _C:
            def __init__(self, clock: Clock) -> None:
                self.clock = clock

            @get("/time")
            async def h(self) -> dict:
                return {"t": self.clock.now()}

        @module(controllers=[_C], providers=[Clock])
        class AppModule:
            pass

        app = LaurenFactory.create(AppModule)

        class FakeClock:
            def now(self) -> float:
                return 1234.0

        app.container.set_singleton(Clock, FakeClock())

        r = TestClient(app).get("/time")
        assert r.status_code == 200
        assert r.json()["t"] == 1234.0


# ---------------------------------------------------------------------------
# Section: Common pitfalls — error paths
# ---------------------------------------------------------------------------


class TestCommonPitfalls:
    def test_missing_provider_error(self):
        @injectable()
        class Dep:
            pass

        @injectable()
        class NeedsUnregistered:
            def __init__(self, dep: Dep) -> None:
                self.dep = dep

        @controller("/")
        class _C:
            def __init__(self, svc: NeedsUnregistered) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        # Note: Dep is absent from providers — MissingProviderError expected.
        @module(controllers=[_C], providers=[NeedsUnregistered])
        class BadMod:
            pass

        with pytest.raises(MissingProviderError):
            LaurenFactory.create(BadMod)

    def test_scope_violation_error(self):
        @injectable(scope=Scope.REQUEST)
        class RequestScoped:
            pass

        @injectable(scope=Scope.SINGLETON)
        class BadSingleton:
            def __init__(self, dep: RequestScoped) -> None:
                self.dep = dep

        @controller("/")
        class _C:
            def __init__(self, svc: BadSingleton) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[_C], providers=[RequestScoped, BadSingleton])
        class BadMod:
            pass

        with pytest.raises(DIScopeViolationError):
            LaurenFactory.create(BadMod)

    def test_metadata_inheritance_error(self):
        @injectable()
        class Parent:
            pass

        class Child(Parent):
            pass  # not re-decorated → MetadataInheritanceError when registered

        @controller("/")
        class _C:
            def __init__(self, c: Child) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[_C], providers=[Child])
        class BadMod:
            pass

        with pytest.raises(MetadataInheritanceError):
            LaurenFactory.create(BadMod)

    def test_protocol_ambiguity_error(self):
        @runtime_checkable
        class IService(Protocol):
            def run(self) -> None: ...

        @injectable(provides=[IService])
        class ImplA:
            def run(self) -> None:
                pass

        @injectable(provides=[IService])
        class ImplB:
            def run(self) -> None:
                pass

        @injectable()
        class Consumer:
            def __init__(self, svc: IService) -> None:
                self.svc = svc

        c = DIContainer()
        c.register(ImplA)
        c.register(ImplB)
        c.register(Consumer)
        with pytest.raises(ProtocolAmbiguityError):
            c.compile()
