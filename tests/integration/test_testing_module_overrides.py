"""``Test.create_testing_module(...)``: build-time overrides validated by the real factory."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import pytest

from lauren import (
    Depends,
    Request,
    Response,
    Scope,
    Token,
    WebSocket,
    controller,
    exception_handler,
    get,
    injectable,
    interceptor,
    middleware,
    module,
    on_connect,
    post_construct,
    pre_destruct,
    use_class,
    use_exception_handlers,
    use_guards,
    use_interceptors,
    use_middlewares,
    use_value,
    ws_controller,
)
from lauren._di import INJECTABLE_META
from lauren.exceptions import DIScopeViolationError, MissingProviderError, StartupError
from lauren.testing import Test, TestClient

EVENTS: list[str] = []


# -- a three-level module graph -----------------------------------------------------


@injectable()
class Repository:
    """Owned by the deepest module; its startup hook must never run when overridden."""

    @post_construct
    async def connect(self) -> None:
        EVENTS.append("real-repository-connected")

    def find(self) -> str:
        return "real"


@module(providers=[Repository], exports=[Repository])
class StorageModule:
    pass


@injectable()
class UserService:
    def __init__(self, repo: Repository) -> None:
        self.repo = repo

    def describe(self) -> str:
        return f"user from {self.repo.find()}"


@module(imports=[StorageModule], providers=[UserService], exports=[UserService])
class UsersModule:
    pass


CLOCK = Token("CLOCK")


@injectable()
class Settings:
    region = "eu"


@injectable()
def make_session():
    EVENTS.append("generator-setup")
    yield "real-session"
    EVENTS.append("generator-teardown")


@runtime_checkable
class Plugin(Protocol):
    name: str


@injectable(provides=[Plugin], multi=True)
class AlphaPlugin:
    name = "alpha"


@injectable(provides=[Plugin], multi=True)
class BetaPlugin:
    name = "beta"


class Denied(Exception):
    pass


@injectable()
class AuthGuard:
    async def can_activate(self, context) -> bool:
        return False


@middleware()
class StampMiddleware:
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        return response.with_header("x-stamp", "real")


@interceptor()
class WrapInterceptor:
    async def intercept(self, context, call_handler) -> Response:
        response = await call_handler.handle()
        return response.with_header("x-wrapped", "real")


@exception_handler(Denied)
class DeniedHandler:
    def catch(self, exc: Denied, request: Request) -> tuple[dict, int]:
        return {"handled_by": "real-class"}, 409


@exception_handler(LookupError)
def lookup_handler(exc: LookupError, request: Request) -> tuple[dict, int]:
    return {"handled_by": "real-function"}, 410


@controller("/")
class AppController:
    def __init__(self, users: UserService, plugins: list[Plugin], settings: Settings) -> None:
        self.users = users
        self.plugins = plugins
        self.settings = settings

    @get("/user")
    async def user(self) -> dict:
        return {"user": self.users.describe()}

    @get("/plugins")
    async def plugin_names(self) -> dict:
        return {"plugins": [p.name for p in self.plugins]}

    @get("/session")
    async def session(self, value: Depends[make_session]) -> dict:
        return {"session": value}

    @get("/clock")
    async def clock(self, request: Request) -> dict:
        return {"ok": True}

    @get("/guarded")
    @use_guards(AuthGuard)
    async def guarded(self) -> dict:
        return {"ok": True}

    @get("/stamped")
    @use_middlewares(StampMiddleware)
    @use_interceptors(WrapInterceptor)
    async def stamped(self) -> dict:
        return {"ok": True}

    @get("/denied")
    @use_exception_handlers(DeniedHandler)
    async def denied(self) -> dict:
        raise Denied()

    @get("/lookup")
    @use_exception_handlers(lookup_handler)
    async def lookup(self) -> dict:
        raise LookupError()


@module(
    imports=[UsersModule],
    controllers=[AppController],
    providers=[Settings, make_session, AlphaPlugin, BetaPlugin, use_value(provide=CLOCK, value="real-clock")],
)
class AppModule:
    pass


@pytest.fixture(autouse=True)
def _reset_events():
    EVENTS.clear()


class FakeRepository:
    def find(self) -> str:
        return "fake"


# -- providers ------------------------------------------------------------------------


def test_provider_three_imports_deep_is_replaced_with_one_call() -> None:
    app = (
        Test.create_testing_module(AppModule)
        .override_provider(Repository)
        .use_value(FakeRepository())
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").json() == {"user": "user from fake"}
    assert "real-repository-connected" not in EVENTS


def test_use_class_override_runs_its_own_hooks_and_is_not_stamped() -> None:
    class HookedRepository:
        @post_construct
        async def up(self) -> None:
            EVENTS.append("fake-up")

        @pre_destruct
        async def down(self) -> None:
            EVENTS.append("fake-down")

        def find(self) -> str:
            return "hooked"

    app = (
        Test.create_testing_module(AppModule)
        .override_provider(Repository)
        .use_class(HookedRepository)
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").json()["user"] == "user from hooked"
    assert [e for e in EVENTS if e.startswith("fake")] == ["fake-up", "fake-down"]
    assert INJECTABLE_META not in HookedRepository.__dict__


def test_use_class_override_runs_every_lifecycle_hook() -> None:
    class TwoHookRepository:
        @post_construct
        async def open_pool(self) -> None:
            EVENTS.append("fake-open")

        @post_construct
        async def warm_cache(self) -> None:
            EVENTS.append("fake-warm")

        @pre_destruct
        async def close_pool(self) -> None:
            EVENTS.append("fake-close")

        @pre_destruct
        async def flush_cache(self) -> None:
            EVENTS.append("fake-flush")

        def find(self) -> str:
            return "two-hook"

    app = (
        Test.create_testing_module(AppModule)
        .override_provider(Repository)
        .use_class(TwoHookRepository)
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").json()["user"] == "user from two-hook"
    assert [e for e in EVENTS if e.startswith("fake")] == [
        "fake-open",
        "fake-warm",
        "fake-close",
        "fake-flush",
    ]


def test_use_class_override_resolves_deps_from_the_owning_module() -> None:
    @injectable()
    class Helper:
        pass

    @module(providers=[Helper], exports=[Helper])
    class HelperModule:
        pass

    class NeedsHelper:
        def __init__(self, helper: Helper) -> None:
            self.helper = helper

        def find(self) -> str:
            return "with-helper"

    @module(imports=[AppModule, HelperModule])
    class RootWithHelper:
        pass

    # Helper is registered (HelperModule) but not visible inside StorageModule,
    # which owns Repository — so the override cannot see it either.
    builder = Test.create_testing_module(RootWithHelper).override_provider(Repository).use_class(NeedsHelper)
    with pytest.raises(MissingProviderError) as info:
        builder.compile()
    assert info.value.detail.get("module") == "StorageModule"
    # Substituting StorageModule with one that imports HelperModule makes it visible.

    @module(imports=[HelperModule], providers=[Repository], exports=[Repository])
    class StorageWithHelper:
        pass

    app = (
        Test.create_testing_module(AppModule)
        .override_module(StorageModule)
        .with_module(StorageWithHelper)
        .override_provider(Repository)
        .use_class(NeedsHelper)
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").json()["user"] == "user from with-helper"


def test_override_that_narrows_scope_fails_compile() -> None:
    builder = (
        Test.create_testing_module(AppModule)
        .override_provider(Repository)
        .use_class(FakeRepository, scope=Scope.REQUEST)
    )
    with pytest.raises(DIScopeViolationError):
        builder.compile()


def test_missing_target_fails_compile_with_reason() -> None:
    class Unregistered:
        pass

    builder = Test.create_testing_module(AppModule).override_provider(Unregistered).use_value(object())
    with pytest.raises(MissingProviderError) as info:
        builder.compile()
    assert info.value.detail["reason"] == "override_target_missing"
    assert info.value.detail["token"].endswith("Unregistered")


def test_optional_override_of_a_missing_target_is_ignored() -> None:
    class Unregistered:
        pass

    app = (
        Test.create_testing_module(AppModule)
        .override_provider(Unregistered)
        .optional()
        .use_value(1)
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").status_code == 200


def test_token_and_custom_provider_targets() -> None:
    app = (
        Test.create_testing_module(AppModule)
        .override_provider(CLOCK)
        .use_value("frozen")
        .override_provider(Settings)
        .use_factory(lambda clock: type("S", (), {"region": clock})(), injects=[CLOCK])
        .compile()
    )
    with TestClient(app) as client:
        assert client.resolve(CLOCK) == "frozen"
        assert client.resolve(Settings).region == "frozen"


def test_use_existing_aliases_another_token() -> None:
    app = Test.create_testing_module(AppModule).override_provider(CLOCK).use_existing(Settings).compile()
    with TestClient(app) as client:
        assert client.resolve(CLOCK) is client.resolve(Settings)


def test_generator_provider_value_override_skips_setup_and_teardown() -> None:
    app = (
        Test.create_testing_module(AppModule)
        .override_provider(make_session)
        .use_value("fake-session")
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/session").json() == {"session": "fake-session"}
    assert not [e for e in EVENTS if e.startswith("generator")]


def test_multi_binding_element_override() -> None:
    gamma = type("Gamma", (), {"name": "gamma"})()
    app = Test.create_testing_module(AppModule).override_provider(BetaPlugin).use_value(gamma).compile()
    with TestClient(app) as client:
        assert sorted(client.get("/plugins").json()["plugins"]) == ["alpha", "gamma"]


def test_whole_multi_binding_set_override() -> None:
    one, two = (type(n, (), {"name": n})() for n in ("one", "two"))
    app = Test.create_testing_module(AppModule).override_provider(Plugin).use_values([one, two]).compile()
    with TestClient(app) as client:
        assert client.get("/plugins").json() == {"plugins": ["one", "two"]}


def test_declarative_overrides() -> None:
    app = Test.create_testing_module(
        AppModule,
        overrides=[
            use_value(provide=Repository, value=FakeRepository()),
            use_class(provide=Settings, use=Settings),
        ],
    ).compile()
    with TestClient(app) as client:
        assert client.get("/user").json()["user"] == "user from fake"


def test_two_testing_modules_coexist_in_one_process() -> None:
    class OtherRepository:
        def find(self) -> str:
            return "other"

    first = (
        Test.create_testing_module(AppModule)
        .override_provider(Repository)
        .use_value(FakeRepository())
        .compile()
    )
    second = (
        Test.create_testing_module(AppModule)
        .override_provider(Repository)
        .use_class(OtherRepository)
        .compile()
    )
    with TestClient(first) as a, TestClient(second) as b:
        assert a.get("/user").json()["user"] == "user from fake"
        assert b.get("/user").json()["user"] == "user from other"


def test_production_build_is_unaffected_after_a_testing_build() -> None:
    from lauren import LaurenFactory

    Test.create_testing_module(AppModule).override_provider(Repository).use_value(FakeRepository()).compile()
    with TestClient(LaurenFactory.create(AppModule)) as client:
        assert client.get("/user").json()["user"] == "user from real"


# -- guards, middleware, interceptors, exception handlers -----------------------------------


@pytest.mark.parametrize(("allow", "status"), [(True, 200), (False, 403)])
def test_guard_allow_and_deny(allow: bool, status: int) -> None:
    step = Test.create_testing_module(AppModule).override_guard(AuthGuard)
    app = (step.allow() if allow else step.deny()).compile()
    with TestClient(app) as client:
        assert client.get("/guarded").status_code == status


def test_global_guard_override() -> None:
    app = (
        Test.create_testing_module(AppModule, global_guards=[AuthGuard])
        .override_guard(AuthGuard)
        .allow()
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").status_code == 200


def test_guard_use_class() -> None:
    class AlwaysYes:
        async def can_activate(self, context) -> bool:
            return True

    app = Test.create_testing_module(AppModule).override_guard(AuthGuard).use_class(AlwaysYes).compile()
    with TestClient(app) as client:
        assert client.get("/guarded").status_code == 200


def test_middleware_and_interceptor_passthrough() -> None:
    app = (
        Test.create_testing_module(AppModule)
        .override_middleware(StampMiddleware)
        .passthrough()
        .override_interceptor(WrapInterceptor)
        .passthrough()
        .compile()
    )
    with TestClient(app) as client:
        response = client.get("/stamped")
    assert response.status_code == 200
    assert "x-stamp" not in response.headers and "x-wrapped" not in response.headers


def test_global_middleware_passthrough() -> None:
    app = (
        Test.create_testing_module(AppModule, global_middlewares=[StampMiddleware])
        .override_middleware(StampMiddleware)
        .passthrough()
        .compile()
    )
    with TestClient(app) as client:
        assert "x-stamp" not in client.get("/user").headers


def test_class_form_exception_handler_override() -> None:
    class Quiet:
        def catch(self, exc, request) -> tuple[dict, int]:
            return {"handled_by": "fake-class"}, 400

    app = (
        Test.create_testing_module(AppModule)
        .override_exception_handler(DeniedHandler)
        .use_value(Quiet())
        .compile()
    )
    with TestClient(app) as client:
        response = client.get("/denied")
    assert (response.status_code, response.json()) == (400, {"handled_by": "fake-class"})


def test_class_form_exception_handler_use_class() -> None:
    class Quiet:
        def catch(self, exc, request) -> tuple[dict, int]:
            return {"handled_by": "fake-cls"}, 400

    app = (
        Test.create_testing_module(AppModule)
        .override_exception_handler(DeniedHandler)
        .use_class(Quiet)
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/denied").json() == {"handled_by": "fake-cls"}


@pytest.mark.parametrize(
    "replacement",
    [
        lambda exc, request: ({"handled_by": "fake-function"}, 400),
        type("C", (), {"catch": lambda self, exc, request: ({"handled_by": "fake-function"}, 400)})(),
    ],
)
def test_function_form_exception_handler_override(replacement) -> None:
    app = (
        Test.create_testing_module(AppModule)
        .override_exception_handler(lookup_handler)
        .use_value(replacement)
        .compile()
    )
    with TestClient(app) as client:
        response = client.get("/lookup")
    assert (response.status_code, response.json()) == (400, {"handled_by": "fake-function"})


def test_global_function_handler_override() -> None:
    app = (
        Test.create_testing_module(AppModule, global_exception_handlers=[lookup_handler])
        .override_exception_handler(lookup_handler)
        .use_value(lambda exc, request: ({"global": True}, 400))
        .compile()
    )
    assert app._global_exception_handlers[0] is not lookup_handler


def test_unused_function_handler_override_fails_compile() -> None:
    @exception_handler(KeyError)
    def unused(exc, request):  # pragma: no cover - never invoked
        return {}

    builder = (
        Test.create_testing_module(AppModule).override_exception_handler(unused).use_value(lambda e, r: {})
    )
    with pytest.raises(MissingProviderError) as info:
        builder.compile()
    assert info.value.detail["reason"] == "override_target_missing"


def test_unused_function_handler_override_can_be_optional() -> None:
    @exception_handler(KeyError)
    def unused(exc, request):  # pragma: no cover - never invoked
        return {}

    Test.create_testing_module(AppModule).override_exception_handler(unused).optional().use_value(
        lambda e, r: {}
    ).compile()


# -- modules ------------------------------------------------------------------------------


def test_module_substitution() -> None:
    @injectable()
    class FakeUsers:
        def describe(self) -> str:
            return "fake users module"

    @module(providers=[use_value(provide=UserService, value=FakeUsers())], exports=[UserService])
    class FakeUsersModule:
        pass

    app = (
        Test.create_testing_module(AppModule)
        .override_module(UsersModule)
        .with_module(FakeUsersModule)
        .compile()
    )
    with TestClient(app) as client:
        assert client.get("/user").json() == {"user": "fake users module"}
    assert "real-repository-connected" not in EVENTS


def test_substitute_module_must_export_what_importers_need() -> None:
    @module()
    class EmptyModule:
        pass

    builder = Test.create_testing_module(AppModule).override_module(UsersModule).with_module(EmptyModule)
    with pytest.raises(MissingProviderError):
        builder.compile()


def test_overriding_a_module_that_is_not_imported_fails() -> None:
    @module()
    class Stray:
        pass

    @module()
    class Replacement:
        pass

    builder = Test.create_testing_module(AppModule).override_module(Stray).with_module(Replacement)
    with pytest.raises(StartupError) as info:
        builder.compile()
    assert info.value.detail["reason"] == "override_target_missing"


def test_websocket_gateway_guard_override() -> None:
    @ws_controller("/ws")
    @use_guards(AuthGuard)
    class GuardedGateway:
        @on_connect
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    @module(controllers=[GuardedGateway])
    class WsModule:
        pass

    denied = Test.create_testing_module(WsModule).compile()
    with TestClient(denied) as client:
        with client.websocket_connect("/ws") as ws:
            assert not ws.accepted
    allowed = Test.create_testing_module(WsModule).override_guard(AuthGuard).allow().compile()
    with TestClient(allowed) as client:
        with client.websocket_connect("/ws") as ws:
            assert ws.accepted
