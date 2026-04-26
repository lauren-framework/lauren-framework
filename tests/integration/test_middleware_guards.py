"""Integration tests for middleware, guards, and metadata."""

from __future__ import annotations

import asyncio


from lauren import (
    CallNext,
    ExecutionContext,
    LaurenFactory,
    Request,
    Response,
    controller,
    get,
    middleware,
    module,
    set_metadata,
    use_guards,
    use_middleware,
)
from lauren.exceptions import UnauthorizedError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@middleware
class TraceMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request.state.trace = "on"
        response = await call_next(request)
        return response.with_header("x-trace", "1")


@middleware
class AuthMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        token = request.headers.get("x-token")
        if not token:
            raise UnauthorizedError("missing token")
        request.state.token = token
        return await call_next(request)


@controller("/pub")
class PublicController:
    @get("/")
    async def index(self) -> Response:
        return Response.json({"public": True})


@use_middleware(AuthMiddleware)
@controller("/priv")
class PrivateController:
    @get("/")
    async def index(self, request: Request) -> Response:
        return Response.json({"token": request.state.token})

    @get("/extra")
    @use_middleware(TraceMiddleware)
    async def extra(self, request: Request) -> Response:
        return Response.json({"trace": request.state.get("trace")})


@module(controllers=[PublicController, PrivateController])
class MwModule:
    pass


def build():
    return TestClient(
        asyncio.run(LaurenFactory.create(MwModule, global_middleware=[TraceMiddleware]))
    )


class TestMiddleware:
    def test_global_middleware_runs(self):
        client = build()
        r = client.get("/pub/")
        assert r.header("x-trace") == "1"

    def test_controller_level_middleware_unauthorized(self):
        client = build()
        r = client.get("/priv/")
        assert r.status_code == 401

    def test_controller_middleware_authorized(self):
        client = build()
        r = client.get("/priv/", headers={"x-token": "abc"})
        assert r.status_code == 200
        assert r.json() == {"token": "abc"}

    def test_route_level_middleware_layering(self):
        client = build()
        r = client.get("/priv/extra", headers={"x-token": "abc"})
        assert r.json() == {"trace": "on"}
        assert r.header("x-trace") == "1"


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


class AdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        role = ctx.request.headers.get("x-role")
        required = ctx.get_metadata("required_role", "admin")
        return role == required


@controller("/admin")
class AdminController:
    @get("/")
    @use_guards(AdminGuard)
    async def index(self) -> Response:
        return Response.json({"area": "admin"})

    @get("/special")
    @use_guards(AdminGuard)
    @set_metadata("required_role", "super")
    async def special(self) -> Response:
        return Response.json({"area": "super"})


@module(controllers=[AdminController])
class GuardModule:
    pass


class TestGuards:
    def test_forbidden_without_role(self):
        app = asyncio.run(LaurenFactory.create(GuardModule))
        client = TestClient(app)
        r = client.get("/admin/")
        assert r.status_code == 403

    def test_allowed_with_role(self):
        app = asyncio.run(LaurenFactory.create(GuardModule))
        client = TestClient(app)
        r = client.get("/admin/", headers={"x-role": "admin"})
        assert r.status_code == 200

    def test_metadata_overrides_guard_behaviour(self):
        app = asyncio.run(LaurenFactory.create(GuardModule))
        client = TestClient(app)
        r = client.get("/admin/special", headers={"x-role": "admin"})
        assert r.status_code == 403
        r = client.get("/admin/special", headers={"x-role": "super"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Middleware order (onion model)
# ---------------------------------------------------------------------------


calls: list[str] = []


@middleware
class M1:
    async def dispatch(self, request, call_next):
        calls.append("M1:before")
        resp = await call_next(request)
        calls.append("M1:after")
        return resp


@middleware
class M2:
    async def dispatch(self, request, call_next):
        calls.append("M2:before")
        resp = await call_next(request)
        calls.append("M2:after")
        return resp


@use_middleware(M1, M2)
@controller("/order")
class OrderController:
    @get("/")
    async def idx(self) -> dict:
        calls.append("handler")
        return {"ok": True}


@module(controllers=[OrderController])
class OrderModule:
    pass


class TestMiddlewareOrder:
    def test_onion_order(self):
        calls.clear()
        app = asyncio.run(LaurenFactory.create(OrderModule))
        client = TestClient(app)
        client.get("/order/")
        assert calls == [
            "M1:before",
            "M2:before",
            "handler",
            "M2:after",
            "M1:after",
        ]
