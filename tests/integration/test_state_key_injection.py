"""``Annotated[T, StateKey]`` handler injection, HTTP and WebSocket (F23 §5.2)."""

from __future__ import annotations

import asyncio
import warnings
from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from lauren import (
    AppState,
    LaurenFactory,
    StateKey,
    UnauthorizedError,
    WebSocket,
    controller,
    get,
    middleware,
    module,
    on_connect,
    on_message,
    pipe,
    use_guards,
    ws_controller,
)
from lauren.extractors import State as StateMarker
from lauren.testing import TestClient, WsTestClient
from lauren.types import CallNext, ExecutionContext, Request, Response
from lauren.validation import ValidationPolicy


@dataclass
class AuthUser:
    id: str
    roles: tuple[str, ...] = ()


CurrentUser = StateKey[AuthUser]("it.user")
Tenant = StateKey[str]("it.tenant")
LoginUser = StateKey[AuthUser]("it.login", on_missing=UnauthorizedError)
User = Annotated[AuthUser, CurrentUser]


class JwtGuard:
    async def can_activate(self, ctx: Any) -> bool:
        if ctx.request.headers.get("x-user"):
            ctx.request.state[CurrentUser] = AuthUser(ctx.request.headers["x-user"], ("admin",))
        return True


def _client(*controllers: type, **kwargs: Any) -> TestClient:
    @module(controllers=list(controllers))
    class AppModule:
        pass

    return TestClient(LaurenFactory.create(AppModule, **kwargs))


@use_guards(JwtGuard)
@controller("/me")
class MeController:
    @get("/")
    async def me(self, user: User) -> dict:
        return {"id": user.id, "roles": list(user.roles)}

    @get("/optional")
    async def maybe(self, user: Annotated[AuthUser | None, CurrentUser]) -> dict:
        return {"anonymous": user is None}

    @get("/outer-optional")
    async def outer(self, user: Annotated[AuthUser, CurrentUser] | None) -> dict:
        return {"anonymous": user is None}

    @get("/default")
    async def default(self, user: Annotated[AuthUser, CurrentUser] = AuthUser("guest")) -> dict:
        return {"id": user.id}

    @get("/login")
    async def login(self, user: Annotated[AuthUser, LoginUser]) -> dict:
        return {"id": user.id}

    @get("/sync")
    def sync(self, user: User) -> dict:
        return {"id": user.id}

    @get("/piped")
    async def piped(self, user_id: Annotated[AuthUser, CurrentUser, pipe(lambda u: u.id.upper())]) -> dict:
        return {"id": user_id}


def test_guard_sets_the_key_and_the_handler_receives_it() -> None:
    with _client(MeController) as client:
        r = client.get("/me", headers={"x-user": "u-1"})

    assert r.json() == {"id": "u-1", "roles": ["admin"]}


@pytest.mark.parametrize("path", ["/me/optional", "/me/outer-optional"])
def test_optional_parameter_receives_none_when_absent(path: str) -> None:
    with _client(MeController) as client:
        assert client.get(path).json() == {"anonymous": True}
        assert client.get(path, headers={"x-user": "u"}).json() == {"anonymous": False}


def test_parameter_default_is_used_when_absent() -> None:
    with _client(MeController) as client:
        assert client.get("/me/default").json() == {"id": "guest"}


def test_missing_value_without_default_is_a_500() -> None:
    with _client(MeController) as client:
        r = client.get("/me")

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "missing_state"
    assert r.json()["error"]["detail"] == {"key": "it.user", "param": "user"}


def test_on_missing_unauthorized_answers_401() -> None:
    with _client(MeController) as client:
        r = client.get("/me/login")

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unauthorized"


def test_sync_handler_receives_the_value() -> None:
    with _client(MeController) as client:
        assert client.get("/me/sync", headers={"x-user": "s"}).json() == {"id": "s"}


def test_pipes_run_on_state_key_parameters() -> None:
    with _client(MeController) as client:
        assert client.get("/me/piped", headers={"x-user": "abc"}).json() == {"id": "ABC"}


def test_middleware_can_provide_a_key() -> None:
    @middleware()
    class TenantMiddleware:
        async def dispatch(self, request: Request, call_next: CallNext) -> Response:
            request.state[Tenant] = request.headers.get("x-tenant", "public")
            return await call_next(request)

    @controller("/t")
    class TenantController:
        @get("/")
        async def tenant(self, tenant: Annotated[str, Tenant]) -> dict:
            return {"tenant": tenant}

    with _client(TenantController, global_middlewares=[TenantMiddleware]) as client:
        assert client.get("/t", headers={"x-tenant": "acme"}).json() == {"tenant": "acme"}


def test_app_state_is_the_fallback() -> None:
    region = StateKey[str]("it.region")

    @controller("/r")
    class RegionController:
        @get("/")
        async def region(self, value: Annotated[str, region]) -> dict:
            return {"region": value}

    app_state = AppState()
    app_state[region] = "eu-west"

    with _client(RegionController, app_state=app_state) as client:
        assert client.get("/r").json() == {"region": "eu-west"}


def test_guard_reads_back_the_typed_value() -> None:
    seen: list[str] = []

    class AuditGuard:
        async def can_activate(self, ctx: ExecutionContext) -> bool:
            seen.append(ctx.request.state[CurrentUser].id)
            return True

    @use_guards(JwtGuard, AuditGuard)
    @controller("/a")
    class Audited:
        @get("/")
        async def h(self) -> dict:
            return {}

    with _client(Audited) as client:
        client.get("/a", headers={"x-user": "auditor"})

    assert seen == ["auditor"]


# ---------------------------------------------------------------------------
# Legacy name-based State extractor
# ---------------------------------------------------------------------------


@controller("/legacy")
class LegacyController:
    @get("/")
    async def h(self, tenant: Annotated[str, StateMarker]) -> dict:
        return {"tenant": tenant}


def test_legacy_state_extractor_warns_and_answers_422() -> None:
    with _client(LegacyController) as client:
        with pytest.warns(DeprecationWarning, match="MissingStateError"):
            r = client.get("/legacy")

    assert r.status_code == 422


def test_legacy_state_extractor_opt_in_answers_500() -> None:
    policy = ValidationPolicy(missing_state="error")

    with _client(LegacyController, validation=policy) as client:
        with warnings.catch_warnings():
            warnings.simplefilter("error", DeprecationWarning)
            r = client.get("/legacy")

    assert r.status_code == 500
    assert r.json()["error"]["detail"] == {"key": "tenant", "param": "tenant"}


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------


async def test_ws_hooks_receive_the_value_set_by_a_connect_guard() -> None:
    @use_guards(JwtGuard)
    @ws_controller("/chat")
    class Chat:
        @on_connect
        async def joined(self, ws: WebSocket, user: User) -> None:
            await ws.accept()
            await ws.send_json({"hello": user.id})

        @on_message("whoami")
        async def whoami(self, ws: WebSocket, user: User) -> None:
            await ws.send_json({"me": user.id, "via": ws.state[CurrentUser].id})

    @module(controllers=[Chat])
    class AppModule:
        pass

    client = WsTestClient(LaurenFactory.create(AppModule))
    async with client.connect("/chat", headers={"x-user": "ws-1"}) as ws:
        assert await asyncio.wait_for(ws.receive_json(), timeout=2) == {"hello": "ws-1"}
        await ws.send_json({"event": "whoami"})
        assert await asyncio.wait_for(ws.receive_json(), timeout=2) == {"me": "ws-1", "via": "ws-1"}


async def test_ws_optional_state_parameter() -> None:
    @ws_controller("/anon")
    class Anon:
        @on_connect
        async def joined(self, ws: WebSocket, user: Annotated[AuthUser | None, CurrentUser]) -> None:
            await ws.accept()
            await ws.send_json({"anonymous": user is None})

    @module(controllers=[Anon])
    class AppModule:
        pass

    async with WsTestClient(LaurenFactory.create(AppModule)).connect("/anon") as ws:
        assert await asyncio.wait_for(ws.receive_json(), timeout=2) == {"anonymous": True}
