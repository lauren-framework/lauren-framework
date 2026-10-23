"""End-to-end test for errors raised outside the route boundary (D01).

A realistic stack — a global authentication middleware, core sessions (whose
engine is itself a global middleware), a global exception handler and an app
encoder override — driven through ``TestClient``. Every request must produce a
rendered response with the right status and envelope, keep the connection
usable, and emit exactly one ``RequestComplete``.
"""

from __future__ import annotations

from typing import Any

import pytest

from lauren import (
    CallNext,
    InMemorySessionStore,
    LaurenFactory,
    Request,
    RequestComplete,
    Response,
    Session,
    SessionConfig,
    SignalBus,
    StdlibJSONEncoder,
    controller,
    exception_handler,
    get,
    middleware,
    module,
    post,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from lauren.testing import TestClient


class TenantSuspended(Exception):
    """A domain error raised by the auth middleware for suspended tenants."""


@middleware()
class ApiKeyAuth:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if request.path.startswith("/public"):
            return await call_next(request)
        key = request.headers.get("x-api-key")
        if key is None:
            raise UnauthorizedError("api key required", detail={"header": "x-api-key"})
        if key == "suspended":
            raise TenantSuspended("tenant is suspended")
        if key == "readonly" and request.method != "GET":
            raise ForbiddenError("read-only key")
        request.state.key = key
        return await call_next(request)


@exception_handler(TenantSuspended)
def suspended_to_402(exc: Exception, request: Request) -> Response:
    return Response.json({"error": {"code": "tenant_suspended", "message": str(exc)}}, status=402)


@controller("/public")
class PublicController:
    @get("/ping")
    async def ping(self) -> dict:
        return {"pong": True}


@controller("/account")
class AccountController:
    @get("/")
    async def read(self, session: Session, request: Request) -> dict:
        session["seen"] = session.get("seen", 0) + 1
        return {"key": request.state.key, "seen": session["seen"]}

    @post("/")
    async def write(self) -> dict:
        return {"written": True}


@module(controllers=[PublicController, AccountController])
class AppModule:
    pass


@pytest.fixture()
def harness() -> tuple[TestClient, list[tuple[str, int, str | None]]]:
    bus = SignalBus()
    events: list[tuple[str, int, str | None]] = []

    @bus.on(RequestComplete)
    def on_done(event: RequestComplete) -> None:
        err = type(event.error).__name__ if event.error is not None else None
        events.append((event.request.path, event.status, err))

    app = LaurenFactory.create(
        AppModule,
        global_middlewares=[ApiKeyAuth],
        global_exception_handlers=[suspended_to_402],
        sessions=SessionConfig(secret="s" * 32, store=InMemorySessionStore(), secure=False),
        json_encoder=StdlibJSONEncoder(),
        signals=bus,
    )
    return TestClient(app), events


def _assert_rendered(r: Any) -> None:
    # A rendered error is a normal HTTP response — never a dropped connection.
    assert r.header("connection") is None
    assert r.header("content-type") is not None


def test_public_route_bypasses_auth(harness) -> None:
    client, events = harness
    r = client.get("/public/ping")
    assert r.status_code == 200 and r.json() == {"pong": True}
    assert events == [("/public/ping", 200, None)]


def test_missing_key_is_a_401_envelope(harness) -> None:
    client, events = harness
    r = client.get("/account/")
    assert r.status_code == 401
    assert r.json() == {
        "error": {"code": "unauthorized", "message": "api key required", "detail": {"header": "x-api-key"}}
    }
    _assert_rendered(r)
    assert events == [("/account/", 401, "UnauthorizedError")]


def test_read_only_key_is_a_403_on_writes(harness) -> None:
    client, events = harness
    assert client.get("/account/", headers={"x-api-key": "readonly"}).status_code == 200
    r = client.post("/account/", headers={"x-api-key": "readonly"})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "forbidden"
    _assert_rendered(r)
    assert [e[1] for e in events] == [200, 403]


def test_domain_error_from_middleware_is_mapped_by_global_handler(harness) -> None:
    client, events = harness
    r = client.get("/account/", headers={"x-api-key": "suspended"})
    assert r.status_code == 402
    assert r.json() == {"error": {"code": "tenant_suspended", "message": "tenant is suspended"}}
    assert events == [("/account/", 402, "TenantSuspended")]


def test_authenticated_requests_keep_their_session(harness) -> None:
    client, events = harness
    first = client.get("/account/", headers={"x-api-key": "k1"})
    assert first.json() == {"key": "k1", "seen": 1}
    cookie = (first.header("set-cookie") or "").split(";")[0]
    name, _, value = cookie.partition("=")
    second = client.get("/account/", headers={"x-api-key": "k1"}, cookies={name: value})
    assert second.json() == {"key": "k1", "seen": 2}
    assert [e[1] for e in events] == [200, 200]


def test_one_request_complete_per_request(harness) -> None:
    client, events = harness
    client.get("/public/ping")
    client.get("/account/")
    client.get("/account/", headers={"x-api-key": "suspended"})
    client.post("/account/", headers={"x-api-key": "readonly"})
    assert [status for _, status, _ in events] == [200, 401, 402, 403]
