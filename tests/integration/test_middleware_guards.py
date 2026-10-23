"""Integration tests for middleware, guards, and metadata."""

from __future__ import annotations

import pytest

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
    use_middlewares,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@middleware()
class TraceMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        request.state.trace = "on"
        response = await call_next(request)
        return response.with_header("x-trace", "1")


@middleware()
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


@use_middlewares(AuthMiddleware)
@controller("/priv")
class PrivateController:
    @get("/")
    async def index(self, request: Request) -> Response:
        return Response.json({"token": request.state.token})

    @get("/extra")
    @use_middlewares(TraceMiddleware)
    async def extra(self, request: Request) -> Response:
        return Response.json({"trace": request.state.get("trace")})


@module(controllers=[PublicController, PrivateController])
class MwModule:
    pass


def build():
    return TestClient(LaurenFactory.create(MwModule, global_middlewares=[TraceMiddleware]))


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
        app = LaurenFactory.create(GuardModule)
        client = TestClient(app)
        r = client.get("/admin/")
        assert r.status_code == 403

    def test_allowed_with_role(self):
        app = LaurenFactory.create(GuardModule)
        client = TestClient(app)
        r = client.get("/admin/", headers={"x-role": "admin"})
        assert r.status_code == 200

    def test_metadata_overrides_guard_behaviour(self):
        app = LaurenFactory.create(GuardModule)
        client = TestClient(app)
        r = client.get("/admin/special", headers={"x-role": "admin"})
        assert r.status_code == 403
        r = client.get("/admin/special", headers={"x-role": "super"})
        assert r.status_code == 200


# ---------------------------------------------------------------------------
# Middleware order (onion model)
# ---------------------------------------------------------------------------


calls: list[str] = []


@middleware()
class M1:
    async def dispatch(self, request, call_next):
        calls.append("M1:before")
        resp = await call_next(request)
        calls.append("M1:after")
        return resp


@middleware()
class M2:
    async def dispatch(self, request, call_next):
        calls.append("M2:before")
        resp = await call_next(request)
        calls.append("M2:after")
        return resp


@use_middlewares(M1, M2)
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
        app = LaurenFactory.create(OrderModule)
        client = TestClient(app)
        client.get("/order/")
        assert calls == [
            "M1:before",
            "M2:before",
            "handler",
            "M2:after",
            "M1:after",
        ]


# ---------------------------------------------------------------------------
# Errors raised by middleware render the same at every registration level
# (D01 — global middleware errors used to become a 500 + Connection: close)
# ---------------------------------------------------------------------------


@middleware()
class DenyAfterCallNext:
    """Raises only after the downstream response exists."""

    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        response = await call_next(request)
        if request.headers.get("x-deny-after"):
            raise ForbiddenError("denied after the handler ran")
        return response


@middleware()
class ExplodingMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        if request.headers.get("x-explode"):
            raise ValueError("middleware bug")
        return await call_next(request)


@controller("/gm")
class GlobalMwController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[GlobalMwController])
class GlobalMwModule:
    pass


@use_middlewares(AuthMiddleware)
@controller("/lvl-ctrl")
class ControllerLevelAuth:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@controller("/lvl-route")
class RouteLevelAuth:
    @get("/")
    @use_middlewares(AuthMiddleware)
    async def index(self) -> dict:
        return {"ok": True}


@controller("/lvl-global")
class GlobalLevelAuth:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[ControllerLevelAuth])
class ControllerLevelModule:
    pass


@module(controllers=[RouteLevelAuth])
class RouteLevelModule:
    pass


@module(controllers=[GlobalLevelAuth])
class GlobalLevelModule:
    pass


def _global(*mws: type, **kw) -> TestClient:
    return TestClient(LaurenFactory.create(GlobalMwModule, global_middlewares=list(mws), **kw))


class TestGlobalMiddlewareErrors:
    def test_http_error_from_global_middleware_renders_its_status(self):
        r = _global(AuthMiddleware).get("/gm/")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "unauthorized"
        assert r.json()["error"]["message"] == "missing token"
        # A rendered error is a normal response: the connection stays usable.
        assert r.header("connection") is None

    def test_authorized_request_passes_through(self):
        r = _global(AuthMiddleware).get("/gm/", headers={"x-token": "t"})
        assert r.status_code == 200
        assert r.json() == {"ok": True}

    def test_error_raised_after_call_next_is_rendered(self):
        r = _global(DenyAfterCallNext).get("/gm/", headers={"x-deny-after": "1"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "forbidden"

    def test_unexpected_error_is_internal_error_and_keeps_connection(self):
        r = _global(ExplodingMiddleware).get("/gm/", headers={"x-explode": "1"})
        assert r.status_code == 500
        body = r.json()["error"]
        assert body["code"] == "internal_error"
        assert body["detail"] == {"type": "ValueError"}
        assert r.header("connection") is None

    def test_rfc7807_format_applies_to_global_middleware_errors(self):
        r = _global(AuthMiddleware, error_format="rfc7807").get("/gm/")
        assert r.status_code == 401
        assert r.header("content-type") == "application/problem+json"
        assert r.json()["status"] == 401
        assert r.json()["code"] == "unauthorized"

    @pytest.mark.parametrize("level", ["global", "controller", "route"])
    def test_same_middleware_renders_identically_at_every_level(self, level):
        if level == "global":
            app = LaurenFactory.create(GlobalLevelModule, global_middlewares=[AuthMiddleware])
            path = "/lvl-global/"
        elif level == "controller":
            app = LaurenFactory.create(ControllerLevelModule)
            path = "/lvl-ctrl/"
        else:
            app = LaurenFactory.create(RouteLevelModule)
            path = "/lvl-route/"
        r = TestClient(app).get(path)
        assert r.status_code == 401
        assert r.json() == {"error": {"code": "unauthorized", "message": "missing token", "detail": {}}}
        assert r.header("connection") is None
