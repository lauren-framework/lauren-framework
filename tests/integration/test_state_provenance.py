"""Startup provenance for ``StateKey(..., require_provider=True)`` (F23 §5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from lauren import (
    LaurenFactory,
    StateKey,
    UnresolvableParameterError,
    WebSocket,
    controller,
    get,
    injectable,
    middleware,
    module,
    on_connect,
    on_message,
    provides_state,
    use_guards,
    use_interceptors,
    use_middlewares,
    ws_controller,
)
from lauren.sessions import Session, SessionConfig
from lauren.testing import TestClient
from lauren.types import CallNext, Request, Response


@dataclass
class AuthUser:
    id: str


CurrentUser = StateKey[AuthUser]("pv.user", require_provider=True)


@injectable()
@provides_state(CurrentUser)
class JwtGuard:
    async def can_activate(self, ctx: Any) -> bool:
        ctx.request.state[CurrentUser] = AuthUser("jwt")
        return True


@injectable()
@provides_state(CurrentUser)
class ApiKeyGuard:
    async def can_activate(self, ctx: Any) -> bool:
        ctx.request.state[CurrentUser] = AuthUser("key")
        return True


@middleware()
@provides_state(CurrentUser)
class UserMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request.state[CurrentUser] = AuthUser("mw")
        return await call_next(request)


@provides_state(CurrentUser)
class UserInterceptor:
    async def intercept(self, ctx: Any, call_handler: Any) -> Any:
        ctx.request.state[CurrentUser] = AuthUser("icp")
        return await call_handler.handle()


def _create(*controllers: type, providers: list[type] | None = None, **kwargs: Any) -> Any:
    @module(controllers=list(controllers), providers=providers or [])
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, **kwargs)


def test_missing_provider_fails_startup_with_a_hint() -> None:
    @controller("/reports")
    class ReportsController:
        @get("/export")
        async def export(self, user: Annotated[AuthUser, CurrentUser]) -> dict:
            return {}

    with pytest.raises(UnresolvableParameterError) as info:
        _create(ReportsController, providers=[JwtGuard, ApiKeyGuard])

    assert info.value.detail == {
        "param": "user",
        "handler": "ReportsController.export",
        "route": "GET /reports/export",
        "state_key": "pv.user",
        "providers": ["ApiKeyGuard", "JwtGuard"],
        "reason": "state_not_provided",
    }
    assert "help: ApiKeyGuard, JwtGuard provide it" in str(info.value)


def test_hint_when_nothing_declares_the_key() -> None:
    orphan = StateKey[str]("pv.orphan", require_provider=True)

    @controller("/o")
    class Orphan:
        @get("/")
        async def h(self, value: Annotated[str, orphan]) -> dict:
            return {}

    with pytest.raises(UnresolvableParameterError) as info:
        _create(Orphan)

    assert info.value.detail["providers"] == []
    assert "@provides_state(key)" in str(info.value)


@pytest.mark.parametrize(
    ("placement", "expected"),
    [("controller_guard", "jwt"), ("route_guard", "jwt"), ("interceptor", "icp"), ("route_middleware", "mw")],
)
def test_providers_anywhere_in_the_chain_satisfy_the_check(placement: str, expected: str) -> None:
    if placement == "controller_guard":

        @use_guards(JwtGuard)
        @controller("/me")
        class Me:
            @get("/")
            async def me(self, user: Annotated[AuthUser, CurrentUser]) -> dict:
                return {"id": user.id}

    else:
        decorator = {
            "route_guard": use_guards(JwtGuard),
            "interceptor": use_interceptors(UserInterceptor),
            "route_middleware": use_middlewares(UserMiddleware),
        }[placement]

        @controller("/me")
        class Me:  # type: ignore[no-redef]
            @get("/")
            @decorator
            async def me(self, user: Annotated[AuthUser, CurrentUser]) -> dict:
                return {"id": user.id}

    with TestClient(_create(Me)) as client:
        assert client.get("/me").json() == {"id": expected}


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [({"global_middlewares": [UserMiddleware]}, "mw"), ({"global_guards": [JwtGuard]}, "jwt")],
)
def test_global_components_satisfy_the_check(kwargs: dict[str, Any], expected: str) -> None:
    @controller("/me")
    class Me:
        @get("/")
        async def me(self, user: Annotated[AuthUser, CurrentUser]) -> dict:
            return {"id": user.id}

    with TestClient(_create(Me, **kwargs)) as client:
        assert client.get("/me").json() == {"id": expected}


def test_optional_parameters_are_exempt() -> None:
    @controller("/me")
    class Me:
        @get("/")
        async def me(self, user: Annotated[AuthUser | None, CurrentUser]) -> dict:
            return {"anonymous": user is None}

    with TestClient(_create(Me)) as client:
        assert client.get("/me").json() == {"anonymous": True}


def test_keys_without_require_provider_are_not_checked() -> None:
    loose = StateKey[str]("pv.loose")

    @controller("/l")
    class Loose:
        @get("/")
        async def h(self, value: Annotated[str, loose] = "fallback") -> dict:
            return {"value": value}

    with TestClient(_create(Loose)) as client:
        assert client.get("/l").json() == {"value": "fallback"}


def test_subclass_of_a_provider_must_redeclare_it() -> None:
    @injectable()
    class CustomJwtGuard(JwtGuard): ...

    @use_guards(CustomJwtGuard)
    @controller("/me")
    class Me:
        @get("/")
        async def me(self, user: Annotated[AuthUser, CurrentUser]) -> dict:
            return {}

    with pytest.raises(UnresolvableParameterError):
        _create(Me)


def test_session_engine_provides_the_session_key() -> None:
    session_key = StateKey[Session]("session", require_provider=True)

    @controller("/s")
    class Sessions:
        @get("/")
        async def h(self, session: Annotated[Session, session_key]) -> dict:
            return {"new": session.is_new}

    with pytest.raises(UnresolvableParameterError):
        _create(Sessions)

    app = _create(Sessions, sessions=SessionConfig(secret="s" * 32))
    with TestClient(app) as client:
        assert client.get("/s").status_code == 200


# ---------------------------------------------------------------------------
# WebSockets
# ---------------------------------------------------------------------------


def test_ws_hook_without_provider_fails_startup() -> None:
    @ws_controller("/chat")
    class Chat:
        @on_message("whoami")
        async def whoami(self, ws: WebSocket, user: Annotated[AuthUser, CurrentUser]) -> None: ...

    with pytest.raises(UnresolvableParameterError) as info:
        _create(Chat)

    assert info.value.detail["route"] == "WEBSOCKET /chat"
    assert info.value.detail["handler"] == "Chat.whoami"


@pytest.mark.parametrize("placement", ["gateway", "global", "message"])
def test_ws_providers_satisfy_the_check(placement: str) -> None:
    kwargs: dict[str, Any] = {}
    if placement == "gateway":

        @use_guards(JwtGuard)
        @ws_controller("/chat")
        class Chat:
            @on_connect
            async def joined(self, ws: WebSocket, user: Annotated[AuthUser, CurrentUser]) -> None: ...

    elif placement == "global":
        kwargs["global_ws_guards"] = [JwtGuard]

        @ws_controller("/chat")
        class Chat:  # type: ignore[no-redef]
            @on_connect
            async def joined(self, ws: WebSocket, user: Annotated[AuthUser, CurrentUser]) -> None: ...

    else:

        @ws_controller("/chat")
        class Chat:  # type: ignore[no-redef]
            @on_message("whoami")
            @use_guards(JwtGuard)
            async def whoami(self, ws: WebSocket, user: Annotated[AuthUser, CurrentUser]) -> None: ...

    assert _create(Chat, **kwargs) is not None
