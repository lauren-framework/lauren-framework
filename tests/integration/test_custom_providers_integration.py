"""End-to-end tests for custom providers used inside ``@module`` and routes.

The unit tests in ``tests/unit/test_custom_providers.py`` already
lock the :class:`DIContainer` contract. These tests exercise the full
NestJS-style developer experience:

* declare ``use_value`` / ``use_class`` / ``use_factory`` /
  ``use_existing`` inside ``@module(providers=[...])``;
* export the resulting tokens via ``exports=[...]``;
* inject them into controllers / services using
  ``Annotated[T, Inject(\"X\")]``;
* hit a real route through :class:`TestClient` and observe the
  resolved value at the controller's edge.

The aim is for every snippet in NestJS's *Custom Providers* docs to
have an exact equivalent here, so the migration story is a
copy/paste.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

import pytest

from lauren import (
    Inject,
    LaurenFactory,
    OptionalDep,
    Scope,
    Token,
    controller,
    get,
    injectable,
    module,
    use_class,
    use_existing,
    use_factory,
    use_value,
)
from lauren.exceptions import (
    MissingProviderError,
    ModuleExportViolation,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# 1. useValue \u2014 mock CatsService for tests
# ---------------------------------------------------------------------------


class CatsService:
    """The real service \u2014 should never run in this test."""

    def meow(self) -> str:
        raise RuntimeError("real implementation invoked")


class TestUseValueAsMock:
    def test_value_provider_replaces_class_provider(self):
        # The classic NestJS pattern: in-test, swap the real class for
        # a hand-built mock with the same shape. Lauren's ``use_value``
        # is the literal equivalent.
        mock_cats = type("MockCats", (), {"meow": lambda self: "mock"})()

        @controller("/cats")
        class CatsController:
            def __init__(self, svc: CatsService) -> None:
                self.svc = svc

            @get("/")
            async def speak(self) -> dict:
                return {"sound": self.svc.meow()}

        @module(
            controllers=[CatsController],
            providers=[use_value(provide=CatsService, value=mock_cats)],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/cats/")
        assert r.status_code == 200
        assert r.json() == {"sound": "mock"}


# ---------------------------------------------------------------------------
# 2. useValue with a string token \u2014 inject an external object
# ---------------------------------------------------------------------------


class _FakeConnection:
    """Stand-in for an externally-built database / Redis / etc. client."""

    def __init__(self, url: str) -> None:
        self.url = url

    def execute(self, sql: str) -> str:
        return f"executed {sql} on {self.url}"


class TestUseValueWithStringToken:
    def test_string_token_injected_via_annotation(self):
        connection = _FakeConnection("postgres://localhost")

        @injectable()
        class CatsRepository:
            def __init__(
                self,
                conn: Annotated[_FakeConnection, Inject("CONNECTION")],
            ) -> None:
                self.conn = conn

            def all(self) -> str:
                return self.conn.execute("SELECT * FROM cats")

        @controller("/cats")
        class CatsController:
            def __init__(self, repo: CatsRepository) -> None:
                self.repo = repo

            @get("/")
            async def index(self) -> dict:
                return {"result": self.repo.all()}

        @module(
            controllers=[CatsController],
            providers=[
                use_value(provide="CONNECTION", value=connection),
                CatsRepository,
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/cats/")
        assert r.json() == {
            "result": "executed SELECT * FROM cats on postgres://localhost",
        }

    def test_token_instance_for_safer_dx(self):
        # Token() carries a name for nicer error messages but otherwise
        # behaves exactly like a string. Recommended over bare strings
        # for shared module surfaces.
        DB_URL = Token("DB_URL")

        @injectable()
        class App:
            def __init__(self, url: Annotated[str, Inject(DB_URL)]) -> None:
                self.url = url

        @controller("/info")
        class InfoController:
            def __init__(self, app: App) -> None:
                self.app = app

            @get("/")
            async def index(self) -> dict:
                return {"db": self.app.url}

        @module(
            controllers=[InfoController],
            providers=[
                use_value(provide=DB_URL, value="sqlite://memory"),
                App,
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/info/").json() == {"db": "sqlite://memory"}

    def test_string_token_injected_directly_into_route_handler(self):
        # Route handler parameters use the same Inject() machinery as
        # constructors. Useful for tiny endpoints that don't need a
        # service layer.
        @controller("/cfg")
        class ConfigController:
            @get("/")
            async def index(self, cfg: Annotated[dict, Inject("CONFIG")]) -> dict:
                return cfg

        @module(
            controllers=[ConfigController],
            providers=[
                use_value(
                    provide="CONFIG",
                    value={"feature_x": True, "max_uploads": 10},
                ),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/cfg/").json() == {
            "feature_x": True,
            "max_uploads": 10,
        }


# ---------------------------------------------------------------------------
# 3. useClass \u2014 environment-conditional configuration
# ---------------------------------------------------------------------------


@injectable()
class ConfigService:
    """Default \u2014 never instantiated when use_class swaps it."""

    name = "default"


@injectable()
class DevelopmentConfigService:
    name = "development"


@injectable()
class ProductionConfigService:
    name = "production"


class TestUseClassEnvironmentSwap:
    def _build(self, env: str):
        @controller("/env")
        class EnvController:
            def __init__(self, cfg: ConfigService) -> None:
                self.cfg = cfg

            @get("/")
            async def index(self) -> dict:
                return {"env": self.cfg.name}

        @module(
            controllers=[EnvController],
            providers=[
                use_class(
                    provide=ConfigService,
                    use=DevelopmentConfigService if env == "development" else ProductionConfigService,
                ),
            ],
        )
        class AppModule:
            pass

        return AppModule

    def test_dev_env(self):
        AppModule = self._build("development")
        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/env/").json() == {"env": "development"}

    def test_prod_env(self):
        AppModule = self._build("production")
        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/env/").json() == {"env": "production"}


# ---------------------------------------------------------------------------
# 4. useFactory \u2014 dynamic providers with injected deps
# ---------------------------------------------------------------------------


@injectable()
class MyOptionsProvider:
    def get(self) -> dict:
        return {"host": "localhost", "port": 5432}


class _DatabaseConnection:
    def __init__(self, options: dict, log_prefix: str = "") -> None:
        self.options = options
        self.log_prefix = log_prefix


class TestUseFactory:
    def test_factory_with_class_dep(self):
        connection_provider = use_factory(
            provide="CONNECTION",
            factory=lambda opts: _DatabaseConnection(opts.get()),
            injects=[MyOptionsProvider],
        )

        @controller("/db")
        class DbController:
            @get("/")
            async def info(self, conn: Annotated[_DatabaseConnection, Inject("CONNECTION")]) -> dict:
                return {"host": conn.options["host"]}

        @module(
            controllers=[DbController],
            providers=[connection_provider, MyOptionsProvider],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/db/").json() == {"host": "localhost"}

    def test_factory_with_optional_dep_present(self):
        # Mirror NestJS's ``{ token, optional: true }`` syntax via
        # OptionalDep. When the optional provider is registered, the
        # factory receives its value.
        @injectable()
        class Tracer:
            id = "trace-1"

        connection_provider = use_factory(
            provide="CONNECTION",
            factory=lambda opts, tracer: _DatabaseConnection(
                opts.get(), log_prefix=f"[{tracer.id}]" if tracer else "[no-trace]"
            ),
            injects=[MyOptionsProvider, OptionalDep(Tracer)],
        )

        @controller("/db")
        class DbController:
            @get("/")
            async def info(self, conn: Annotated[_DatabaseConnection, Inject("CONNECTION")]) -> dict:
                return {"prefix": conn.log_prefix}

        @module(
            controllers=[DbController],
            providers=[connection_provider, MyOptionsProvider, Tracer],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/db/").json() == {"prefix": "[trace-1]"}

    def test_factory_with_optional_dep_missing(self):
        # Same shape, but Tracer is NOT registered. The factory still
        # runs; tracer is None.
        connection_provider = use_factory(
            provide="CONNECTION",
            factory=lambda opts, tracer: _DatabaseConnection(
                opts.get(), log_prefix="[no-trace]" if tracer is None else "[have]"
            ),
            injects=[MyOptionsProvider, OptionalDep("UNREGISTERED")],
        )

        @controller("/db")
        class DbController:
            @get("/")
            async def info(self, conn: Annotated[_DatabaseConnection, Inject("CONNECTION")]) -> dict:
                return {"prefix": conn.log_prefix}

        @module(
            controllers=[DbController],
            providers=[connection_provider, MyOptionsProvider],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/db/").json() == {"prefix": "[no-trace]"}

    def test_async_factory(self):
        # ``async def`` factories are first-class \u2014 lauren awaits them.
        async def make_message() -> str:
            await asyncio.sleep(0)  # prove it's awaited
            return "from-async"

        @controller("/msg")
        class MsgController:
            @get("/")
            async def index(self, msg: Annotated[str, Inject("MSG")]) -> dict:
                return {"message": msg}

        @module(
            controllers=[MsgController],
            providers=[use_factory(provide="MSG", factory=make_message)],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/msg/").json() == {"message": "from-async"}

    def test_factory_with_request_scope(self):
        # REQUEST-scoped factories get fresh values per request \u2014
        # useful for things like per-request correlation IDs.
        counter = {"n": 0}

        def make_request_id() -> int:
            counter["n"] += 1
            return counter["n"]

        @controller("/r")
        class RController:
            @get("/")
            async def index(self, rid: Annotated[int, Inject("REQUEST_ID")]) -> dict:
                return {"request_id": rid}

        @module(
            controllers=[RController],
            providers=[
                use_factory(
                    provide="REQUEST_ID",
                    factory=make_request_id,
                    scope=Scope.REQUEST,
                ),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        a = client.get("/r/").json()
        b = client.get("/r/").json()
        assert a["request_id"] == 1
        assert b["request_id"] == 2  # fresh per request


# ---------------------------------------------------------------------------
# 5. useExisting \u2014 alias one token to another
# ---------------------------------------------------------------------------


@injectable()
class LoggerService:
    def info(self, msg: str) -> str:
        return f"[INFO] {msg}"


class TestUseExisting:
    def test_alias_resolves_to_same_singleton(self):
        # Two endpoints injecting the alias and the original; the
        # resolved instance must be identical.
        @controller("/log")
        class LogController:
            @get("/via-class")
            async def via_class(self, log: LoggerService) -> dict:
                return {"id": id(log)}

            @get("/via-alias")
            async def via_alias(self, log: Annotated[LoggerService, Inject("AliasedLogger")]) -> dict:
                return {"id": id(log)}

        @module(
            controllers=[LogController],
            providers=[
                LoggerService,
                use_existing(provide="AliasedLogger", existing=LoggerService),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        a = client.get("/log/via-class").json()["id"]
        b = client.get("/log/via-alias").json()["id"]
        assert a == b


# ---------------------------------------------------------------------------
# 6. Non-service values \u2014 a config dict per environment
# ---------------------------------------------------------------------------


class TestNonServiceProvider:
    def test_config_dict_via_factory(self):
        # NestJS's *Non-service-based providers* example: a factory
        # returns a different config dict per environment. Implemented
        # here with use_factory.
        dev_config = {"debug": True, "endpoint": "http://dev"}

        def config_factory() -> dict:
            return dev_config  # pretend NODE_ENV=development

        @controller("/config")
        class CfgController:
            @get("/")
            async def index(self, cfg: Annotated[dict, Inject("CONFIG")]) -> dict:
                return cfg

        @module(
            controllers=[CfgController],
            providers=[use_factory(provide="CONFIG", factory=config_factory)],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/config/").json() == dev_config


# ---------------------------------------------------------------------------
# 7. Cross-module exports
# ---------------------------------------------------------------------------


class TestExports:
    def test_string_token_export_consumed_by_importing_module(self):
        # CoreModule defines ``"CONNECTION"`` and exports it; AppModule
        # imports CoreModule and consumes the same token.
        connection = _FakeConnection("postgres://shared")

        @injectable()
        class Repo:
            def __init__(
                self,
                conn: Annotated[_FakeConnection, Inject("CONNECTION")],
            ) -> None:
                self.conn = conn

        @controller("/repo")
        class RepoController:
            def __init__(self, repo: Repo) -> None:
                self.repo = repo

            @get("/")
            async def index(self) -> dict:
                return {"url": self.repo.conn.url}

        @module(
            providers=[use_value(provide="CONNECTION", value=connection)],
            exports=["CONNECTION"],
        )
        class CoreModule:
            pass

        @module(
            imports=[CoreModule],
            controllers=[RepoController],
            providers=[Repo],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/repo/").json() == {"url": "postgres://shared"}

    def test_export_unknown_token_rejected(self):
        @module(
            providers=[use_value(provide="X", value=1)],
            exports=["NOT_DECLARED"],
        )
        class BadModule:
            pass

        with pytest.raises(ModuleExportViolation, match="NOT_DECLARED"):
            LaurenFactory.create(BadModule)


# ---------------------------------------------------------------------------
# 8. Module visibility \u2014 a string token must NOT leak
# ---------------------------------------------------------------------------


class TestModuleEncapsulation:
    def test_unexported_string_token_invisible_to_other_module(self):
        # Same encapsulation guarantee as standard providers: a token
        # registered in CoreModule but NOT exported is invisible to
        # AppModule.
        @injectable()
        class Consumer:
            def __init__(self, val: Annotated[int, Inject("HIDDEN")]) -> None:
                self.val = val

        @module(
            providers=[use_value(provide="HIDDEN", value=42)],
            # Intentionally NO ``exports=`` here.
        )
        class CoreModule:
            pass

        @module(
            imports=[CoreModule],
            providers=[Consumer],
        )
        class AppModule:
            pass

        # The graph compiles, but resolving Consumer fails because
        # ``HIDDEN`` is invisible from AppModule.
        with pytest.raises(MissingProviderError, match="HIDDEN"):
            LaurenFactory.create(AppModule)


# ---------------------------------------------------------------------------
# 9. Combined recipe \u2014 alias chain through factory and value
# ---------------------------------------------------------------------------


class TestComposition:
    def test_alias_to_factory_to_value(self):
        @controller("/c")
        class C:
            @get("/")
            async def index(self, x: Annotated[int, Inject("ALIAS")]) -> dict:
                return {"value": x}

        @module(
            controllers=[C],
            providers=[
                use_value(provide="BASE", value=10),
                use_factory(
                    provide="DOUBLED",
                    factory=lambda b: b * 2,
                    injects=["BASE"],
                ),
                use_existing(provide="ALIAS", existing="DOUBLED"),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/c/").json() == {"value": 20}


# ---------------------------------------------------------------------------
# 10. Mock CatsService for testing \u2014 the canonical example
# ---------------------------------------------------------------------------


class TestCanonicalNestJSExample:
    def test_canonical_documented_example(self):
        # Reproduces the very first NestJS docs example almost
        # verbatim, just transcribed to lauren idioms.
        @injectable()
        class _CatsService:
            def find_all(self) -> list[str]:
                return ["real-cat-1", "real-cat-2"]

        mock_cats_service = type(
            "Mock",
            (),
            {"find_all": lambda self: ["fluffy", "whiskers"]},
        )()

        @controller("/cats")
        class CatsController:
            def __init__(self, svc: _CatsService) -> None:
                self.svc = svc

            @get("/")
            async def index(self) -> dict:
                return {"cats": self.svc.find_all()}

        @module(
            controllers=[CatsController],
            providers=[
                use_value(provide=_CatsService, value=mock_cats_service),
            ],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        assert client.get("/cats/").json() == {"cats": ["fluffy", "whiskers"]}
