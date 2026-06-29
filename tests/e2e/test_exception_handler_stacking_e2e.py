"""End-to-end test for stacked ``@exception_handler`` decorators.

Drives a realistic multi-controller app through the full request/response
cycle via ``testing.TestClient``, reproducing the reported scenario: a single
"redirect to login" handler declared by stacking ``@exception_handler`` for
both ``UnauthorizedError`` and ``ForbiddenError``. Both must redirect; normal
routes and unmatched exceptions must behave correctly; and three-tier
resolution (route → controller → global) must still hold with a stacked
handler in the mix.
"""

from __future__ import annotations

import pytest

from lauren import (
    ExecutionContext,
    LaurenFactory,
    Response,
    controller,
    exception_handler,
    get,
    module,
    use_guards,
    use_exception_handlers,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from lauren.testing import TestClient


# --- A handler that redirects auth failures, declared by STACKING. ----------


@exception_handler(UnauthorizedError)
@exception_handler(ForbiddenError)
def to_login(exc: Exception, request) -> Response:
    return Response.redirect("/auth/login", status=303)


# --- A guard that 401s anonymous users (raises UnauthorizedError). ----------


class RequireUser:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        if ctx.request.headers.get("x-user") is None:
            raise UnauthorizedError("login required")
        return True


# --- Controllers ------------------------------------------------------------


@controller("/dashboard")
class DashboardController:
    @get("/")
    @use_guards(RequireUser)
    async def index(self) -> dict:
        return {"page": "dashboard"}


@controller("/admin")
class AdminController:
    @get("/")
    async def index(self) -> dict:
        # Authenticated but unauthorized → ForbiddenError.
        raise ForbiddenError("admins only")

    @get("/ping")
    async def ping(self) -> dict:
        return {"pong": True}


@controller("/public")
class PublicController:
    @get("/boom")
    async def boom(self) -> dict:
        raise ValueError("unrelated failure")


@module(controllers=[DashboardController, AdminController, PublicController])
class AppModule:
    pass


@pytest.fixture(scope="module")
def client() -> TestClient:
    # The stacked handler is registered globally — one handler, two exception
    # types, covering auth failures raised by guards and by handlers alike.
    return TestClient(LaurenFactory.create(AppModule, global_exception_handlers=[to_login]))


class TestStackedAuthRedirect:
    def test_unauthorized_from_guard_redirects(self, client: TestClient):
        r = client.get("/dashboard/")  # no x-user → guard raises UnauthorizedError
        assert r.status_code == 303
        assert r.header("location") == "/auth/login"

    def test_forbidden_from_handler_redirects(self, client: TestClient):
        r = client.get("/admin/")  # handler raises ForbiddenError
        assert r.status_code == 303
        assert r.header("location") == "/auth/login"

    def test_authorized_request_passes_through(self, client: TestClient):
        r = client.get("/dashboard/", headers={"x-user": "ada"})
        assert r.status_code == 200
        assert r.json() == {"page": "dashboard"}

    def test_normal_route_unaffected(self, client: TestClient):
        assert client.get("/admin/ping").json() == {"pong": True}

    def test_unrelated_exception_is_not_caught(self, client: TestClient):
        r = client.get("/public/boom")
        assert r.status_code == 500
        assert r.header("location") is None


class TestStackedHandlerResolutionOrder:
    def test_route_stacked_handler_overrides_global(self):
        # A route-level stacked handler wins over the global one (most
        # specific wins), proving stacking participates in tiered resolution.
        @exception_handler(UnauthorizedError)
        @exception_handler(ForbiddenError)
        def route_handler(exc, request):
            return Response.json({"scope": "route"}, status=499)

        @controller("/scoped")
        class Scoped:
            @get("/u")
            @use_exception_handlers(route_handler)
            async def u(self) -> dict:
                raise UnauthorizedError("x")

        @module(controllers=[Scoped])
        class M:
            pass

        client = TestClient(LaurenFactory.create(M, global_exception_handlers=[to_login]))
        r = client.get("/scoped/u")
        assert r.status_code == 499
        assert r.json() == {"scope": "route"}
