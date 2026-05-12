"""Integration tests for every code example in docs/guides/dependency-injection.md.

Each test class maps to a Part / Section of the guide. Tests are self-contained:
every module, controller, provider, guard, interceptor, pipe, and middleware is
defined inline so the test is its own minimal reproduction of the documented pattern.
"""

# NOTE: intentionally NOT using ``from __future__ import annotations`` so that
# inline class references in type hints are evaluated at definition time
# (required for Pydantic schema inference and pipe/guard registration).

from typing import Annotated, Any, Protocol, runtime_checkable

import pytest

from lauren import (
    Depends,
    ExecutionContext,
    Inject,
    LaurenFactory,
    Path,
    Scope,
    Token,
    controller,
    get,
    injectable,
    interceptor,
    middleware,
    module,
    set_metadata,
    use_class,
    use_existing,
    use_factory,
    use_guards,
    use_interceptors,
    use_middlewares,
    use_value,
)
from lauren.exceptions import (
    DIScopeViolationError,
    HTTPError,
    MissingProviderError,
)
from lauren.extractors import Pipe, PipeContext, pipe
from lauren.testing import TestClient
from lauren.types import CallHandler


class NotFoundError(HTTPError):
    """Local domain error that maps to HTTP 404 — used in pipe tests."""

    status_code = 404


# ===========================================================================
# Part A — Provider forms
# ===========================================================================


class TestA1InjectableClass:
    """@injectable() on a class (SINGLETON, REQUEST, TRANSIENT)."""

    def test_singleton_is_default_scope(self):
        @injectable()
        class SomeSvc:
            pass

        @controller("/a1")
        class C:
            def __init__(self, svc: SomeSvc) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        @module(controllers=[C], providers=[SomeSvc])
        class M:
            pass

        assert TestClient(LaurenFactory.create(M)).get("/a1/").status_code == 200

    def test_request_scope_gives_fresh_instance_per_request(self):
        # Store the *instances* (not just their ids) to prevent CPython from
        # reusing the same memory address after the first instance is GC'd,
        # which would make id(instance1) == id(instance2) spuriously.
        instances: list[object] = []

        @injectable(scope=Scope.REQUEST)
        class RequestCtx:
            pass

        @controller("/a1r")
        @injectable(scope=Scope.REQUEST)
        class C:
            def __init__(self, ctx: RequestCtx) -> None:
                self.ctx = ctx

            @get("/")
            async def h(self) -> dict:
                instances.append(self.ctx)
                return {}

        @module(controllers=[C], providers=[RequestCtx])
        class M:
            pass

        c = TestClient(LaurenFactory.create(M))
        c.get("/a1r/")
        c.get("/a1r/")
        # Each request gets a distinct RequestCtx instance.
        assert len(instances) == 2
        assert instances[0] is not instances[1]

    def test_transient_gives_new_instance_each_resolve(self):
        """TRANSIENT scope: each container.resolve() call returns a fresh instance."""
        import asyncio

        @injectable(scope=Scope.TRANSIENT)
        class Unique:
            pass

        @controller("/a1t")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        @module(controllers=[C], providers=[Unique])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app)  # triggers startup

        # Resolve twice — each call must return a distinct instance.
        inst1 = asyncio.run(app.container.resolve(Unique))
        inst2 = asyncio.run(app.container.resolve(Unique))
        assert inst1 is not inst2

    def test_bare_injectable_without_parens_rejected(self):
        from lauren.exceptions import DecoratorUsageError

        with pytest.raises((DecoratorUsageError, TypeError)):

            @injectable  # type: ignore[arg-type]
            class Bad:
                pass


class TestA2InjectableFunction:
    """@injectable() on a function — return value is the dependency."""

    def test_function_provider_return_value_injected_via_depends(self):
        @injectable()
        def make_greeting() -> str:
            return "hello"

        @injectable()
        class Greeter:
            msg: Depends[make_greeting]

        @controller("/a2")
        class C:
            def __init__(self, g: Greeter) -> None:
                self.g = g

            @get("/")
            async def h(self) -> dict:
                return {"msg": self.g.msg}

        @module(controllers=[C], providers=[make_greeting, Greeter])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a2/")
        assert r.json()["msg"] == "hello"

    def test_async_function_factory_awaited(self):
        @injectable()
        async def make_token() -> str:
            return "async_secret"

        @injectable()
        class Auth:
            token: Depends[make_token]

        @controller("/a2async")
        class C:
            def __init__(self, a: Auth) -> None:
                self.a = a

            @get("/")
            async def h(self) -> dict:
                return {"token": self.a.token}

        @module(controllers=[C], providers=[make_token, Auth])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a2async/")
        assert r.json()["token"] == "async_secret"

    def test_function_provider_params_resolved_via_di(self):
        @injectable()
        class Config:
            db = "sqlite://:memory:"

        @injectable()
        def make_url(cfg: Config) -> str:
            return f"pg://{cfg.db}"

        @injectable()
        class Repo:
            url: Depends[make_url]

        @controller("/a2params")
        class C:
            def __init__(self, r: Repo) -> None:
                self.r = r

            @get("/")
            async def h(self) -> dict:
                return {"url": self.r.url}

        @module(controllers=[C], providers=[Config, make_url, Repo])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a2params/")
        assert r.json()["url"] == "pg://sqlite://:memory:"

    def test_singleton_function_called_exactly_once(self):
        calls = [0]

        @injectable(scope=Scope.SINGLETON)
        def expensive_init() -> dict:
            calls[0] += 1
            return {"ready": True}

        @injectable()
        class SvcA:
            cfg: Depends[expensive_init]

        @injectable()
        class SvcB:
            cfg: Depends[expensive_init]

        @controller("/a2once")
        class C:
            def __init__(self, a: SvcA, b: SvcB) -> None:
                self.a = a
                self.b = b

            @get("/")
            async def h(self) -> dict:
                return {"same": self.a.cfg is self.b.cfg}

        @module(controllers=[C], providers=[expensive_init, SvcA, SvcB])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a2once/")
        assert r.json()["same"] is True
        assert calls[0] == 1


class TestA3UseValue:
    """use_value — bind a token to a pre-built value."""

    def test_use_value_with_token(self):
        DB_URL = Token("DB_URL")

        @injectable()
        class Repo:
            def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
                self.url = url

        @controller("/a3")
        class C:
            def __init__(self, r: Repo) -> None:
                self.r = r

            @get("/")
            async def h(self) -> dict:
                return {"url": self.r.url}

        @module(
            controllers=[C],
            providers=[
                use_value(provide=DB_URL, value="postgres://localhost/app"),
                Repo,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a3/")
        assert r.json()["url"] == "postgres://localhost/app"

    def test_use_value_with_string_token(self):
        @injectable()
        class FeatureService:
            def __init__(self, flags: Annotated[dict, Inject("FEATURE_FLAGS")]) -> None:
                self.flags = flags

        @controller("/a3str")
        class C:
            def __init__(self, svc: FeatureService) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return self.svc.flags

        @module(
            controllers=[C],
            providers=[
                use_value(provide="FEATURE_FLAGS", value={"new_ui": True}),
                FeatureService,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a3str/")
        assert r.json()["new_ui"] is True

    def test_use_value_as_test_mock(self):
        @runtime_checkable
        class EmailSvc(Protocol):
            def send(self, to: str) -> str: ...

        class RealEmailSvc:
            def send(self, to: str) -> str:
                return f"sent_to:{to}"

        class MockEmailSvc:
            def send(self, to: str) -> str:
                return f"mock:{to}"

        @injectable()
        class Notifier:
            def __init__(self, email: EmailSvc) -> None:
                self.email = email

        @controller("/a3mock")
        class C:
            def __init__(self, n: Notifier) -> None:
                self.n = n

            @get("/")
            async def h(self) -> dict:
                return {"result": self.n.email.send("user@example.com")}

        @module(
            controllers=[C],
            providers=[
                use_value(provide=EmailSvc, value=MockEmailSvc()),
                Notifier,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a3mock/")
        assert r.json()["result"] == "mock:user@example.com"


class TestA4UseClass:
    """use_class — bind a token to a different class."""

    def test_use_class_swaps_implementation(self):
        class ConfigService:
            @property
            def name(self) -> str:
                return "base"

        class DevConfig:
            @property
            def name(self) -> str:
                return "dev"

        class ProdConfig:
            @property
            def name(self) -> str:
                return "prod"

        @injectable()
        class AppSvc:
            def __init__(self, cfg: ConfigService) -> None:
                self.cfg = cfg

        @controller("/a4")
        class C:
            def __init__(self, svc: AppSvc) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return {"cfg": self.svc.cfg.name}

        @module(
            controllers=[C],
            providers=[
                use_class(provide=ConfigService, use=DevConfig),
                AppSvc,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a4/")
        assert r.json()["cfg"] == "dev"

    def test_use_class_impl_does_not_need_injectable(self):
        """The class in use=… is constructed by the container without @injectable."""

        class Token_:
            def value(self) -> str:
                return "service_token"

        class ConcreteToken_:
            # No @injectable decorator needed here
            def value(self) -> str:
                return "concrete"

        @injectable()
        class Consumer:
            def __init__(self, t: Token_) -> None:
                self.t = t

        @controller("/a4ni")
        class C:
            def __init__(self, c: Consumer) -> None:
                self.c = c

            @get("/")
            async def h(self) -> dict:
                return {"v": self.c.t.value()}

        @module(
            controllers=[C],
            providers=[
                use_class(provide=Token_, use=ConcreteToken_),
                Consumer,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a4ni/")
        assert r.json()["v"] == "concrete"


class TestA5UseFactory:
    """use_factory — compute the value from DI-resolved inputs."""

    def test_use_factory_with_inject_list(self):
        DB_URL_TOK = Token("DB_URL_F")

        def make_connection(url: str) -> dict:
            return {"connected": True, "url": url}

        @injectable()
        class DbClient:
            def __init__(self, conn: Annotated[dict, Inject("CONN")]) -> None:
                self.conn = conn

        @controller("/a5")
        class C:
            def __init__(self, db: DbClient) -> None:
                self.db = db

            @get("/")
            async def h(self) -> dict:
                return self.db.conn

        @module(
            controllers=[C],
            providers=[
                use_value(provide=DB_URL_TOK, value="postgres://localhost/app"),
                use_factory(
                    provide="CONN",
                    factory=make_connection,
                    injects=[DB_URL_TOK],
                    scope=Scope.SINGLETON,
                ),
                DbClient,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a5/")
        assert r.json()["connected"] is True
        assert r.json()["url"] == "postgres://localhost/app"

    def test_use_factory_async_factory(self):
        async def async_build() -> dict:
            return {"async": True}

        @injectable()
        class AsyncSvc:
            def __init__(self, data: Annotated[dict, Inject("ASYNC_DATA")]) -> None:
                self.data = data

        @controller("/a5async")
        class C:
            def __init__(self, svc: AsyncSvc) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return self.svc.data

        @module(
            controllers=[C],
            providers=[
                use_factory(provide="ASYNC_DATA", factory=async_build, injects=[]),
                AsyncSvc,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a5async/")
        assert r.json()["async"] is True

    def test_use_factory_optional_dep_resolves_none_when_missing(self):
        from lauren import OptionalDep

        def make_thing(maybe: str | None) -> dict:
            return {"has_dep": maybe is not None}

        @injectable()
        class ThingSvc:
            def __init__(self, t: Annotated[dict, Inject("THING")]) -> None:
                self.t = t

        @controller("/a5opt")
        class C:
            def __init__(self, svc: ThingSvc) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return self.svc.t

        @module(
            controllers=[C],
            providers=[
                use_factory(
                    provide="THING",
                    factory=make_thing,
                    injects=[OptionalDep("MISSING_TOKEN")],
                ),
                ThingSvc,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a5opt/")
        assert r.json()["has_dep"] is False


class TestA6UseExisting:
    """use_existing — alias one token to another."""

    def test_alias_resolves_to_same_instance(self):
        @injectable()
        class RealLogger:
            def log(self, msg: str) -> str:
                return f"log:{msg}"

        @injectable()
        class ServiceA:
            def __init__(self, log: RealLogger) -> None:
                self.log = log

        @injectable()
        class ServiceB:
            def __init__(self, alias_log: Annotated[RealLogger, Inject("AliasLogger")]) -> None:
                self.log = alias_log

        @controller("/a6")
        class C:
            def __init__(self, a: ServiceA, b: ServiceB) -> None:
                self.a = a
                self.b = b

            @get("/")
            async def h(self) -> dict:
                return {
                    "same": self.a.log is self.b.log,
                    "result": self.a.log.log("hello"),
                }

        @module(
            controllers=[C],
            providers=[
                RealLogger,
                use_existing(provide="AliasLogger", existing=RealLogger),
                ServiceA,
                ServiceB,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a6/")
        assert r.json()["same"] is True
        assert r.json()["result"] == "log:hello"


class TestA7TokenAndInject:
    """Token + Inject for non-class tokens."""

    def test_constructor_injection_with_token_and_inject(self):
        JWT_SECRET = Token("JWT_SECRET")

        @injectable()
        class JwtService:
            def __init__(self, secret: Annotated[str, Inject(JWT_SECRET)]) -> None:
                self.secret = secret

            def sign(self) -> str:
                return f"signed:{self.secret}"

        @controller("/a7")
        class C:
            def __init__(self, jwt: JwtService) -> None:
                self.jwt = jwt

            @get("/")
            async def h(self) -> dict:
                return {"token": self.jwt.sign()}

        @module(
            controllers=[C],
            providers=[
                use_value(provide=JWT_SECRET, value="super_secret"),
                JwtService,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a7/")
        assert r.json()["token"] == "signed:super_secret"

    def test_field_injection_with_annotated_inject(self):
        API_KEY = Token("API_KEY")

        @injectable()
        class ExternalClient:
            key: Annotated[str, Inject(API_KEY)]

        @controller("/a7f")
        class C:
            def __init__(self, client: ExternalClient) -> None:
                self.client = client

            @get("/")
            async def h(self) -> dict:
                return {"key": self.client.key}

        @module(
            controllers=[C],
            providers=[
                use_value(provide=API_KEY, value="my_api_key"),
                ExternalClient,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a7f/")
        assert r.json()["key"] == "my_api_key"

    def test_shared_token_by_name_across_modules(self):
        SHARED = Token("SHARED_VAL", unique=False)

        @injectable()
        class Consumer:
            def __init__(self, val: Annotated[str, Inject(SHARED)]) -> None:
                self.val = val

        @module(
            providers=[use_value(provide=Token("SHARED_VAL", unique=False), value="shared_v")],
            exports=[SHARED],
        )
        class SharedMod:
            pass

        @controller("/a7shared")
        class C:
            def __init__(self, c: Consumer) -> None:
                self.c = c

            @get("/")
            async def h(self) -> dict:
                return {"val": self.c.val}

        @module(controllers=[C], providers=[Consumer], imports=[SharedMod])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/a7shared/")
        assert r.json()["val"] == "shared_v"


# ===========================================================================
# Part B — @module(providers=[…]) rules
# ===========================================================================


class TestBModuleRules:
    def test_provider_visible_only_if_declared_locally(self):
        @injectable()
        class Hidden:
            pass

        @injectable()
        class Consumer:
            def __init__(self, h: Hidden) -> None:
                self.h = h

        @controller("/")
        class C:
            def __init__(self, c: Consumer) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        # Hidden is NOT in providers — MissingProviderError expected
        @module(controllers=[C], providers=[Consumer])
        class M:
            pass

        with pytest.raises(MissingProviderError):
            LaurenFactory.create(M)

    def test_export_makes_provider_visible_to_importer(self):
        @injectable()
        class Clock:
            def tick(self) -> str:
                return "tick"

        @module(providers=[Clock], exports=[Clock])
        class SharedMod:
            pass

        @injectable()
        class Scheduler:
            def __init__(self, c: Clock) -> None:
                self.c = c

        @controller("/bmod")
        class C:
            def __init__(self, s: Scheduler) -> None:
                self.s = s

            @get("/")
            async def h(self) -> dict:
                return {"tick": self.s.c.tick()}

        @module(controllers=[C], providers=[Scheduler], imports=[SharedMod])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/bmod/")
        assert r.json()["tick"] == "tick"

    def test_not_exported_is_not_visible_to_grandparent(self):
        @injectable()
        class Hidden:
            pass

        @module(providers=[Hidden])  # no exports
        class DataMod:
            pass

        @injectable()
        class Consumer:
            def __init__(self, h: Hidden) -> None:
                self.h = h

        @controller("/")
        class C:
            def __init__(self, c: Consumer) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[Consumer], imports=[DataMod])
        class M:
            pass

        with pytest.raises(MissingProviderError):
            LaurenFactory.create(M)

    def test_scope_violation_detected_at_startup(self):
        @injectable(scope=Scope.REQUEST)
        class ReqScoped:
            pass

        @injectable(scope=Scope.SINGLETON)
        class BadSingleton:
            def __init__(self, r: ReqScoped) -> None:
                self.r = r

        @controller("/")
        class C:
            def __init__(self, s: BadSingleton) -> None: ...

            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[ReqScoped, BadSingleton])
        class M:
            pass

        with pytest.raises(DIScopeViolationError):
            LaurenFactory.create(M)


# ===========================================================================
# Part C — Injection positions
# ===========================================================================


class TestCInjectionPositions:
    def test_c1_constructor_injection(self):
        @injectable()
        class MsgSvc:
            def msg(self) -> str:
                return "from_constructor"

        @injectable()
        class Consumer:
            def __init__(self, svc: MsgSvc) -> None:  # constructor position
                self.svc = svc

        @controller("/c1")
        class C:
            def __init__(self, c: Consumer) -> None:
                self.c = c

            @get("/")
            async def h(self) -> dict:
                return {"msg": self.c.svc.msg()}

        @module(controllers=[C], providers=[MsgSvc, Consumer])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c1/")
        assert r.json()["msg"] == "from_constructor"

    def test_c2_field_injection(self):
        @injectable()
        class MsgSvc2:
            def msg(self) -> str:
                return "from_field"

        @injectable()
        class Consumer2:
            svc: MsgSvc2  # field position

        @controller("/c2")
        class C:
            def __init__(self, c: Consumer2) -> None:
                self.c = c

            @get("/")
            async def h(self) -> dict:
                return {"msg": self.c.svc.msg()}

        @module(controllers=[C], providers=[MsgSvc2, Consumer2])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c2/")
        assert r.json()["msg"] == "from_field"

    def test_c3_depends_in_constructor(self):
        @injectable()
        def make_val() -> str:
            return "depends_value"

        @injectable()
        class DependsConsumer:
            def __init__(self, val: Depends[make_val]) -> None:
                self.val = val

        @controller("/c3c")
        class C:
            def __init__(self, d: DependsConsumer) -> None:
                self.d = d

            @get("/")
            async def h(self) -> dict:
                return {"val": self.d.val}

        @module(controllers=[C], providers=[make_val, DependsConsumer])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c3c/")
        assert r.json()["val"] == "depends_value"

    def test_c3_depends_in_field(self):
        @injectable()
        def make_token() -> str:
            return "token_field"

        @injectable()
        class DependsField:
            val: Depends[make_token]

        @controller("/c3f")
        class C:
            def __init__(self, d: DependsField) -> None:
                self.d = d

            @get("/")
            async def h(self) -> dict:
                return {"val": self.d.val}

        @module(controllers=[C], providers=[make_token, DependsField])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c3f/")
        assert r.json()["val"] == "token_field"

    def test_c3_depends_in_handler_param_with_class_provider(self):
        @injectable()
        class InfoSvc:
            def info(self) -> str:
                return "svc_info"

        @controller("/c3h")
        class C:
            @get("/")
            async def h(self, svc: Depends[InfoSvc]) -> dict:
                return {"info": svc.info()}

        @module(controllers=[C], providers=[InfoSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c3h/")
        assert r.json()["info"] == "svc_info"

    def test_c3_depends_in_handler_param_with_function_provider(self):
        @injectable()
        def get_current_user() -> dict:
            return {"id": 42, "name": "Alice"}

        @controller("/c3hfn")
        class C:
            @get("/")
            async def h(self, user: Depends[get_current_user]) -> dict:
                return user

        @module(controllers=[C], providers=[get_current_user])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c3hfn/")
        assert r.json()["id"] == 42

    def test_implicit_di_in_handler_param(self):
        """A registered provider type in a handler param is injected implicitly (no Depends needed)."""

        @injectable()
        class AutoSvc:
            def value(self) -> str:
                return "implicit"

        @controller("/c3implicit")
        class C:
            @get("/")
            async def h(self, svc: AutoSvc) -> dict:  # no Depends — still resolved via DI
                return {"val": svc.value()}

        @module(controllers=[C], providers=[AutoSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/c3implicit/")
        assert r.json()["val"] == "implicit"


# ===========================================================================
# Part D1 — Controller injection
# ===========================================================================


class TestD1ControllerInjection:
    def test_constructor_injection_into_controller(self):
        @injectable()
        class UserSvc:
            def lookup(self, uid: int) -> str:
                return f"user_{uid}"

        @controller("/d1")
        class C:
            def __init__(self, svc: UserSvc) -> None:
                self.svc = svc

            @get("/{id}")
            async def get_user(self, id: int) -> dict:
                return {"name": self.svc.lookup(id)}

        @module(controllers=[C], providers=[UserSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d1/7")
        assert r.json()["name"] == "user_7"

    def test_field_injection_into_controller(self):
        @injectable()
        class PriceSvc:
            def price(self, item: str) -> float:
                return 9.99

        @controller("/d1f")
        class C:
            svc: PriceSvc  # field injection

            @get("/{item}")
            async def price(self, item: str) -> dict:
                return {"price": self.svc.price(item)}

        @module(controllers=[C], providers=[PriceSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d1f/widget")
        assert r.json()["price"] == 9.99

    def test_multiple_deps_in_constructor(self):
        @injectable()
        class Repo:
            def get(self, uid: int) -> str:
                return f"user_{uid}"

        @injectable()
        class Audit:
            log: list[str]

            def __init__(self) -> None:
                self.log = []

            def record(self, msg: str) -> None:
                self.log.append(msg)

        @controller("/d1m")
        class C:
            def __init__(self, repo: Repo, audit: Audit) -> None:
                self.repo = repo
                self.audit = audit

            @get("/{id}")
            async def get(self, id: int) -> dict:
                user = self.repo.get(id)
                self.audit.record(f"get:{id}")
                return {"user": user, "logged": len(self.audit.log)}

        @module(controllers=[C], providers=[Repo, Audit])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d1m/3")
        assert r.json()["user"] == "user_3"
        assert r.json()["logged"] == 1


# ===========================================================================
# Part D2 — Route handler injection
# ===========================================================================


class TestD2HandlerInjection:
    def test_di_injected_alongside_path_param(self):
        @injectable()
        class ProductSvc:
            def find(self, pid: int) -> dict:
                return {"id": pid, "name": "widget"}

        @controller("/d2")
        class C:
            @get("/{pid}")
            async def get(self, pid: int, svc: ProductSvc) -> dict:
                return svc.find(pid)

        @module(controllers=[C], providers=[ProductSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d2/5")
        assert r.json()["id"] == 5

    def test_depends_with_function_provider_in_handler(self):
        @injectable()
        def current_user() -> dict:
            return {"id": 99, "role": "admin"}

        @controller("/d2fn")
        class C:
            @get("/profile")
            async def profile(self, user: Depends[current_user]) -> dict:
                return user

        @module(controllers=[C], providers=[current_user])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d2fn/profile")
        assert r.json()["role"] == "admin"

    def test_mixed_handler_params_di_path_query(self):
        @injectable()
        class PricingSvc:
            def price(self, item_id: int, currency: str) -> dict:
                return {"item": item_id, "currency": currency, "amount": 9.99}

        @controller("/d2mix")
        class C:
            @get("/{item_id}")
            async def price(
                self,
                item_id: int,  # implicit path
                currency: str = "USD",  # implicit query
                svc: PricingSvc = ...,  # type: ignore[assignment]  # DI
            ) -> dict:
                return svc.price(item_id, currency)

        @module(controllers=[C], providers=[PricingSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d2mix/7?currency=EUR")
        assert r.json()["currency"] == "EUR"
        assert r.json()["item"] == 7


# ===========================================================================
# Part D3 — Guard injection
# ===========================================================================


class TestD3GuardInjection:
    def test_guard_without_di(self):
        """A plain class with can_activate — no @injectable needed."""

        class RoleGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                return ctx.request.headers.get("x-role") == "admin"

        @use_guards(RoleGuard)
        @controller("/d3plain")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        @module(controllers=[C])
        class M:
            pass

        c = TestClient(LaurenFactory.create(M))
        assert c.get("/d3plain/").status_code == 403
        assert c.get("/d3plain/", headers={"x-role": "admin"}).status_code == 200

    def test_guard_with_di_constructor_injection(self):
        @injectable()
        class TokenStore:
            _valid = {"secret_token"}

            def is_valid(self, token: str) -> bool:
                return token in self._valid

        @injectable(scope=Scope.SINGLETON)
        class TokenGuard:
            def __init__(self, store: TokenStore) -> None:
                self.store = store

            async def can_activate(self, ctx: ExecutionContext) -> bool:
                token = ctx.request.headers.get("x-token", "")
                return self.store.is_valid(token)

        @use_guards(TokenGuard)
        @controller("/d3di")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"ok": True}

        @module(controllers=[C], providers=[TokenStore, TokenGuard])
        class M:
            pass

        c = TestClient(LaurenFactory.create(M))
        assert c.get("/d3di/").status_code == 403
        assert c.get("/d3di/", headers={"x-token": "secret_token"}).status_code == 200

    def test_guard_enriches_request_state(self):
        @injectable(scope=Scope.SINGLETON)
        class AuthGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                token = ctx.request.headers.get("x-token", "")
                if not token:
                    return False
                ctx.request.state.set("user_id", f"user_{token}")
                return True

        from lauren import Request

        @use_guards(AuthGuard)
        @controller("/d3state")
        class C:
            @get("/")
            async def h(self, request: Request) -> dict:
                return {"user_id": request.state.get("user_id")}

        @module(controllers=[C], providers=[AuthGuard])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d3state/", headers={"x-token": "abc"})
        assert r.json()["user_id"] == "user_abc"

    def test_guard_with_set_metadata(self):
        @injectable(scope=Scope.SINGLETON)
        class RoleGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                required = ctx.get_metadata("required_role", "user")
                actual = ctx.request.headers.get("x-role", "")
                return actual == required

        @controller("/d3meta")
        class C:
            @get("/admin")
            @use_guards(RoleGuard)
            @set_metadata("required_role", "admin")
            async def admin(self) -> dict:
                return {"ok": True}

            @get("/user")
            @use_guards(RoleGuard)
            @set_metadata("required_role", "user")
            async def user_route(self) -> dict:
                return {"ok": True}

        @module(controllers=[C], providers=[RoleGuard])
        class M:
            pass

        c = TestClient(LaurenFactory.create(M))
        assert c.get("/d3meta/admin", headers={"x-role": "admin"}).status_code == 200
        assert c.get("/d3meta/admin", headers={"x-role": "user"}).status_code == 403
        assert c.get("/d3meta/user", headers={"x-role": "user"}).status_code == 200


# ===========================================================================
# Part D4 — Interceptor injection
# ===========================================================================


class TestD4InterceptorInjection:
    def test_interceptor_without_di(self):
        @interceptor()
        class AddField:
            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                result = await ch.handle()
                if isinstance(result, dict):
                    result["intercepted"] = True
                return result

        @use_interceptors(AddField)
        @controller("/d4plain")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"value": 1}

        @module(controllers=[C])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d4plain/")
        assert r.json()["intercepted"] is True

    def test_interceptor_with_di_injection(self):
        call_log: list[str] = []

        @injectable()
        class EventBus:
            def emit(self, event: str) -> None:
                call_log.append(event)

        @interceptor()
        @injectable(scope=Scope.SINGLETON)
        class AuditInterceptor:
            def __init__(self, bus: EventBus) -> None:
                self._bus = bus

            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                result = await ch.handle()
                self._bus.emit(f"request:{ctx.handler_func.__name__}")
                return result

        @use_interceptors(AuditInterceptor)
        @controller("/d4di")
        class C:
            @get("/")
            async def do_thing(self) -> dict:
                return {"done": True}

        @module(controllers=[C], providers=[EventBus, AuditInterceptor])
        class M:
            pass

        TestClient(LaurenFactory.create(M)).get("/d4di/")
        assert "request:do_thing" in call_log

    def test_global_interceptor_with_di(self):
        counter = [0]

        @injectable()
        class ReqCounter:
            def inc(self) -> None:
                counter[0] += 1

        @interceptor()
        @injectable(scope=Scope.SINGLETON)
        class CountingInterceptor:
            def __init__(self, rc: ReqCounter) -> None:
                self._rc = rc

            async def intercept(self, ctx: ExecutionContext, ch: CallHandler) -> Any:
                self._rc.inc()
                return await ch.handle()

        @controller("/d4global")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[ReqCounter, CountingInterceptor])
        class M:
            pass

        app = LaurenFactory.create(M, global_interceptors=[CountingInterceptor])
        c = TestClient(app)
        c.get("/d4global/")
        c.get("/d4global/")
        assert counter[0] == 2


# ===========================================================================
# Part D5 — Pipe injection
# ===========================================================================


class TestD5PipeInjection:
    def test_pipe_without_di(self):
        @pipe()
        class Uppercase(Pipe):
            def transform(self, value: str) -> str:
                return value.upper()

        @controller("/d5plain")
        class C:
            @get("/{name}")
            async def h(self, name: Path[str] = pipe(Uppercase)) -> dict:
                return {"name": name}

        @module(controllers=[C])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d5plain/hello")
        assert r.json()["name"] == "HELLO"

    def test_pipe_with_di_injection(self):
        looked_up: list[int] = []

        @injectable()
        class UserRepo:
            def get(self, uid: int) -> dict | None:
                looked_up.append(uid)
                return {"id": uid, "name": f"User{uid}"} if uid > 0 else None

        @pipe()
        @injectable(scope=Scope.SINGLETON)
        class UserLookup(Pipe):
            def __init__(self, repo: UserRepo) -> None:
                self.repo = repo

            async def transform(self, value: int, ctx: PipeContext) -> dict:
                user = self.repo.get(value)
                if user is None:
                    raise NotFoundError("user not found")
                return user

        @controller("/d5di")
        class C:
            @get("/{uid}")
            async def h(self, uid: Path[int] = pipe(UserLookup)) -> dict:
                return uid  # uid is now a dict from UserLookup

        @module(controllers=[C], providers=[UserRepo, UserLookup])
        class M:
            pass

        c = TestClient(LaurenFactory.create(M))
        r = c.get("/d5di/7")
        assert r.json()["name"] == "User7"
        assert 7 in looked_up

        r = c.get("/d5di/0")
        assert r.status_code == 404

    def test_pipe_resolves_service_via_ctx_container(self):
        """A function pipe that resolves a service from the DI container via ctx."""

        @injectable()
        class PriceSvc:
            def get(self, pid: int) -> float:
                return pid * 1.5

        @pipe()
        async def enrich_price(value: int, ctx: PipeContext) -> dict:
            svc = await ctx.container.resolve(
                PriceSvc,
                request_cache=ctx.request_cache,
                owning_module=ctx.owning_module,
            )
            return {"id": value, "price": svc.get(value)}

        @controller("/d5ctx")
        class C:
            @get("/{pid}")
            async def h(self, pid: Path[int] = pipe(enrich_price)) -> dict:
                return pid  # type: ignore[return-value]

        @module(controllers=[C], providers=[PriceSvc])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d5ctx/4")
        assert r.json()["price"] == 6.0


# ===========================================================================
# Part D6 — Middleware injection
# ===========================================================================


class TestD6MiddlewareInjection:
    def test_middleware_without_di(self):
        @middleware()
        class RequestId:
            async def dispatch(self, request, call_next):
                import uuid

                rid = uuid.uuid4().hex
                request.state.set("rid", rid)
                response = await call_next(request)
                return response.with_header("x-rid", rid)

        @controller("/d6plain")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M, global_middlewares=[RequestId])).get("/d6plain/")
        assert r.header("x-rid") is not None

    def test_middleware_with_di_injection(self):
        log: list[str] = []

        @injectable()
        class AppLogger:
            def info(self, msg: str) -> None:
                log.append(msg)

        @middleware()
        @injectable(scope=Scope.SINGLETON)
        class AccessLog:
            def __init__(self, logger: AppLogger) -> None:
                self._log = logger

            async def dispatch(self, request, call_next):
                response = await call_next(request)
                self._log.info(f"{request.method} {request.path}")
                return response

        @controller("/d6di")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[AppLogger, AccessLog])
        class M:
            pass

        TestClient(LaurenFactory.create(M, global_middlewares=[AccessLog])).get("/d6di/")
        assert any("GET" in entry for entry in log)

    def test_controller_level_middleware_with_di(self):
        header_values: list[str] = []

        @injectable()
        class TenantSvc:
            def resolve(self, host: str) -> str:
                return f"tenant:{host}"

        @middleware()
        @injectable(scope=Scope.SINGLETON)
        class TenantScope:
            def __init__(self, svc: TenantSvc) -> None:
                self._svc = svc

            async def dispatch(self, request, call_next):
                tenant = self._svc.resolve(request.headers.get("host", "localhost"))
                request.state.set("tenant", tenant)
                response = await call_next(request)
                header_values.append(tenant)
                return response.with_header("x-tenant", tenant)

        @use_middlewares(TenantScope)
        @controller("/d6ctrl")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        @module(controllers=[C], providers=[TenantSvc, TenantScope])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d6ctrl/")
        assert r.header("x-tenant") is not None


# ===========================================================================
# Part D7 — Other injectables (transitive dependency chains)
# ===========================================================================


class TestD7TransitiveDependencies:
    def test_deep_chain_resolved_at_startup(self):
        @injectable()
        def config_url() -> str:
            return "sqlite://:memory:"

        @injectable()
        class Database:
            url: Depends[config_url]

        @injectable()
        class UserRepository:
            def __init__(self, db: Database) -> None:
                self.db = db

            def find(self, uid: int) -> str:
                return f"user_{uid}@{self.db.url}"

        @injectable()
        class UserService:
            def __init__(self, repo: UserRepository) -> None:
                self.repo = repo

            def get(self, uid: int) -> str:
                return self.repo.find(uid)

        @controller("/d7")
        class C:
            def __init__(self, svc: UserService) -> None:
                self.svc = svc

            @get("/{uid}")
            async def h(self, uid: int) -> dict:
                return {"user": self.svc.get(uid)}

        @module(
            controllers=[C],
            providers=[config_url, Database, UserRepository, UserService],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d7/5")
        assert "user_5" in r.json()["user"]
        assert "sqlite" in r.json()["user"]

    def test_protocol_based_transitive_chain(self):
        @runtime_checkable
        class Store(Protocol):
            def load(self) -> list[str]: ...

        @injectable(provides=[Store])
        class InMemoryStore:
            def load(self) -> list[str]:
                return ["item_a", "item_b"]

        @injectable()
        class CatalogService:
            def __init__(self, store: Store) -> None:
                self.store = store

            def list(self) -> list[str]:
                return self.store.load()

        @controller("/d7proto")
        class C:
            def __init__(self, svc: CatalogService) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> list:
                return self.svc.list()

        @module(controllers=[C], providers=[InMemoryStore, CatalogService])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/d7proto/")
        assert r.json() == ["item_a", "item_b"]


# ===========================================================================
# Part E — Mixed real-world module (all provider forms together)
# ===========================================================================


class TestEMixedModule:
    def test_all_provider_forms_in_one_module(self):
        DB_TOKEN = Token("DB_E")

        class BaseLogger:
            def log(self, msg: str) -> str:
                return f"LOG:{msg}"

        class DevLogger:
            def log(self, msg: str) -> str:
                return f"DEV:{msg}"

        def make_conn(dsn: str) -> dict:
            return {"dsn": dsn, "open": True}

        @injectable()
        class AppService:
            def __init__(
                self,
                conn: Annotated[dict, Inject("CONN_E")],
                logger: BaseLogger,
                dsn: Annotated[str, Inject(DB_TOKEN)],
            ) -> None:
                self.conn = conn
                self.logger = logger
                self.dsn = dsn

        @controller("/emix")
        class C:
            def __init__(self, svc: AppService) -> None:
                self.svc = svc

            @get("/")
            async def h(self) -> dict:
                return {
                    "open": self.svc.conn["open"],
                    "logger_type": type(self.svc.logger).__name__,
                    "dsn": self.svc.dsn,
                }

        @module(
            controllers=[C],
            providers=[
                use_value(provide=DB_TOKEN, value="postgres://localhost/etest"),
                use_class(provide=BaseLogger, use=DevLogger),
                use_factory(
                    provide="CONN_E",
                    factory=make_conn,
                    injects=[DB_TOKEN],
                    scope=Scope.SINGLETON,
                ),
                AppService,
            ],
        )
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/emix/")
        assert r.json()["open"] is True
        assert r.json()["logger_type"] == "DevLogger"
        assert r.json()["dsn"] == "postgres://localhost/etest"
