"""Integration tests for ``@exception_handler`` and ``@use_exception_handlers``.

These cover:

* class- and function-form handlers carrying nothing but metadata;
* composition order route → controller → global at dispatch time;
* DI injection on handlers (they're injectable like middleware/guards);
* multi-exception tuples;
* the new ``LaurenFactory.create`` globals (``global_middlewares``,
  ``global_guards``, ``global_exception_filters``);
* the equivalent imperative API on :class:`Lauren`
  (``add_exception_handler`` / ``add_guard``).
"""

from __future__ import annotations

import asyncio

import pytest

from lauren import (
    CallNext,
    ExecutionContext,
    Lauren,
    LaurenFactory,
    Request,
    Response,
    controller,
    exception_handler,
    get,
    injectable,
    middleware,
    module,
    use_exception_handlers,
    use_guards,
    use_middleware,
)
from lauren.exceptions import (
    ExceptionHandlerConfigError,
    ForbiddenError,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Custom exceptions used across tests
# ---------------------------------------------------------------------------


class DomainError(Exception):
    pass


class NotFoundDomainError(DomainError):
    pass


class ValidationDomainError(DomainError):
    pass


class TenantError(Exception):
    pass


# ---------------------------------------------------------------------------
# @exception_handler — usage validation
# ---------------------------------------------------------------------------


class TestExceptionHandlerDecoratorContract:
    def test_bare_decorator_rejected(self):
        with pytest.raises(Exception) as ei:

            @exception_handler  # type: ignore[arg-type]
            class Handler:
                async def catch(self, exc, request):
                    return Response.no_content()

        # Either DecoratorUsageError or ExceptionHandlerConfigError is fine.
        assert (
            "exception_handler" in str(ei.value).lower()
            or "parentheses" in str(ei.value).lower()
        )

    def test_empty_parens_rejected(self):
        with pytest.raises(ExceptionHandlerConfigError) as ei:

            @exception_handler()
            class Handler:
                async def catch(self, exc, request):
                    return Response.no_content()

        assert "at least one exception type" in str(ei.value)

    def test_non_exception_argument_rejected(self):
        with pytest.raises(ExceptionHandlerConfigError):
            exception_handler(int)  # type: ignore[arg-type]

    def test_class_without_catch_rejected(self):
        with pytest.raises(ExceptionHandlerConfigError) as ei:

            @exception_handler(DomainError)
            class Bad:
                pass

        assert "must define" in str(ei.value)
        assert "catch" in str(ei.value)

    def test_marker_attached_on_class(self):
        @exception_handler(DomainError, TenantError)
        class H:
            async def catch(self, exc, request):
                return Response.no_content()

        meta = getattr(H, "__lauren_exception_handler__")
        assert meta.exceptions == (DomainError, TenantError)
        # The handler is auto-marked injectable so DI can build it.
        assert hasattr(H, "__lauren_injectable__")

    def test_marker_attached_on_function(self):
        @exception_handler(ValueError)
        async def fn(exc, request):
            return Response.no_content()

        meta = getattr(fn, "__lauren_exception_handler__")
        assert meta.exceptions == (ValueError,)
        # Function-form handlers are intentionally NOT registered as
        # DI providers — they are invoked directly with (exc, request).
        assert not hasattr(fn, "__lauren_injectable__")


# ---------------------------------------------------------------------------
# @use_exception_handlers — usage validation
# ---------------------------------------------------------------------------


class TestUseExceptionHandlersContract:
    def test_undecorated_class_rejected(self):
        class NotAHandler:
            async def catch(self, exc, request):
                return Response.no_content()

        with pytest.raises(ExceptionHandlerConfigError) as ei:
            use_exception_handlers(NotAHandler)
        assert "is not an @exception_handler" in str(ei.value)

    def test_none_entries_filtered(self):
        @exception_handler(DomainError)
        class H1:
            async def catch(self, exc, request):
                return Response.no_content()

        @use_exception_handlers(H1, None)
        class C:
            pass

        assert getattr(C, "__lauren_use_exception_handlers__") == [H1]

    def test_class_and_method_attachments_independent(self):
        @exception_handler(DomainError)
        class A:
            async def catch(self, exc, request):
                return Response.no_content()

        @exception_handler(TenantError)
        class B:
            async def catch(self, exc, request):
                return Response.no_content()

        @use_exception_handlers(A)
        class Ctrl:
            def handler(self):
                pass

        use_exception_handlers(B)(Ctrl.handler)

        assert getattr(Ctrl, "__lauren_use_exception_handlers__") == [A]
        assert getattr(Ctrl.handler, "__lauren_use_exception_handlers__") == [B]


# ---------------------------------------------------------------------------
# End-to-end dispatch
# ---------------------------------------------------------------------------


@exception_handler(DomainError)
class DomainErrorHandler:
    async def catch(self, exc: DomainError, request: Request) -> Response:
        return Response.json(
            {"caught_by": "domain", "type": type(exc).__name__, "msg": str(exc)},
            status=400,
        )


@exception_handler(TenantError)
class TenantErrorHandler:
    async def catch(self, exc: TenantError, request: Request) -> Response:
        return Response.json({"caught_by": "tenant"}, status=409)


@exception_handler(NotFoundDomainError)
class RouteSpecificHandler:
    async def catch(self, exc, request: Request) -> Response:
        return Response.json({"caught_by": "route"}, status=404)


@use_exception_handlers(DomainErrorHandler)
@controller("/domain")
class DomainController:
    @get("/raise")
    async def raise_domain(self) -> Response:
        raise DomainError("boom")

    @get("/raise-not-found")
    @use_exception_handlers(RouteSpecificHandler)
    async def raise_not_found(self) -> Response:
        # NotFoundDomainError extends DomainError. The route-level handler
        # is more specific so it must run first; the controller-level
        # handler does NOT run because the route-level one already
        # returned a response.
        raise NotFoundDomainError("missing")

    @get("/raise-tenant")
    async def raise_tenant(self) -> Response:
        # No handler on this controller for TenantError — the global
        # filter (registered in build_app) catches it.
        raise TenantError("wrong tenant")

    @get("/ok")
    async def ok(self) -> dict:
        return {"ok": True}


@module(controllers=[DomainController])
class DomainModule:
    pass


def _build_domain_app(*, with_global_tenant_filter: bool = True):
    return TestClient(
        asyncio.run(
            LaurenFactory.create(
                DomainModule,
                global_exception_filters=[TenantErrorHandler]
                if with_global_tenant_filter
                else None,
            )
        )
    )


class TestEndToEndDispatch:
    def test_controller_level_handler_catches(self):
        client = _build_domain_app()
        r = client.get("/domain/raise")
        assert r.status_code == 400
        assert r.json() == {
            "caught_by": "domain",
            "type": "DomainError",
            "msg": "boom",
        }

    def test_route_level_handler_takes_priority(self):
        client = _build_domain_app()
        r = client.get("/domain/raise-not-found")
        # Route-level RouteSpecificHandler must win over the controller-level
        # DomainErrorHandler even though both could match.
        assert r.status_code == 404
        assert r.json() == {"caught_by": "route"}

    def test_global_filter_catches_when_nothing_local_matches(self):
        client = _build_domain_app()
        r = client.get("/domain/raise-tenant")
        assert r.status_code == 409
        assert r.json() == {"caught_by": "tenant"}

    def test_no_global_no_local_falls_back_to_500(self):
        client = _build_domain_app(with_global_tenant_filter=False)
        r = client.get("/domain/raise-tenant")
        assert r.status_code == 500

    def test_normal_route_unaffected(self):
        client = _build_domain_app()
        r = client.get("/domain/ok")
        assert r.status_code == 200
        assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Function-form handlers
# ---------------------------------------------------------------------------


@exception_handler(ValueError, KeyError)
async def value_or_key_handler(exc: Exception, request: Request) -> dict:
    return {"caught": "fn", "kind": type(exc).__name__}


@controller("/fn")
class FnController:
    @get("/value")
    @use_exception_handlers(value_or_key_handler)
    async def raise_value(self) -> dict:
        raise ValueError("v")

    @get("/key")
    @use_exception_handlers(value_or_key_handler)
    async def raise_key(self) -> dict:
        raise KeyError("k")


@module(controllers=[FnController])
class FnModule:
    pass


class TestFunctionFormHandler:
    def test_function_handler_catches_first_type(self):
        client = TestClient(asyncio.run(LaurenFactory.create(FnModule)))
        r = client.get("/fn/value")
        assert r.status_code == 200
        assert r.json() == {"caught": "fn", "kind": "ValueError"}

    def test_function_handler_catches_second_type(self):
        client = TestClient(asyncio.run(LaurenFactory.create(FnModule)))
        r = client.get("/fn/key")
        assert r.status_code == 200
        assert r.json() == {"caught": "fn", "kind": "KeyError"}


# ---------------------------------------------------------------------------
# Handler dependency injection
# ---------------------------------------------------------------------------


@injectable()
class AuditService:
    def __init__(self) -> None:
        self.captured: list[str] = []


@exception_handler(DomainError)
class AuditingHandler:
    def __init__(self, audit: AuditService) -> None:
        self.audit = audit

    async def catch(self, exc, request: Request) -> Response:
        self.audit.captured.append(f"{type(exc).__name__}:{exc}")
        return Response.json({"audited": True})


@controller("/audit")
class AuditController:
    @get("/raise")
    @use_exception_handlers(AuditingHandler)
    async def fire(self) -> dict:
        raise DomainError("see me")


@module(controllers=[AuditController], providers=[AuditService, AuditingHandler])
class AuditModule:
    pass


class TestHandlerDI:
    def test_handler_receives_injected_service(self):
        app = asyncio.run(LaurenFactory.create(AuditModule))
        client = TestClient(app)
        r = client.get("/audit/raise")
        assert r.status_code == 200
        assert r.json() == {"audited": True}
        # The DI container resolves AuditService once (singleton) so the
        # handler's reference and the one we'd resolve here are the same.
        captured = asyncio.run(app.container.resolve(AuditService)).captured
        assert captured == ["DomainError:see me"]


# ---------------------------------------------------------------------------
# Globals on LaurenFactory.create
# ---------------------------------------------------------------------------


@middleware
class StampMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        resp = await call_next(request)
        return resp.with_header("x-global-mw", "1")


class GlobalAdminGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-admin") == "yes"


@controller("/cfg")
class CfgController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[CfgController])
class CfgModule:
    pass


class TestLaurenFactoryGlobals:
    def test_global_middlewares_alias_runs(self):
        app = asyncio.run(
            LaurenFactory.create(CfgModule, global_middlewares=[StampMiddleware])
        )
        client = TestClient(app)
        r = client.get("/cfg/")
        assert r.status_code == 200
        assert r.header("x-global-mw") == "1"

    def test_legacy_singular_global_middleware_still_works(self):
        # The original kwarg must keep working for backward compatibility.
        app = asyncio.run(
            LaurenFactory.create(CfgModule, global_middleware=[StampMiddleware])
        )
        client = TestClient(app)
        r = client.get("/cfg/")
        assert r.header("x-global-mw") == "1"

    def test_passing_both_singular_and_plural_raises(self):
        from lauren.exceptions import StartupError

        with pytest.raises(StartupError):
            asyncio.run(
                LaurenFactory.create(
                    CfgModule,
                    global_middleware=[StampMiddleware],
                    global_middlewares=[StampMiddleware],
                )
            )

    def test_global_guards_run_for_every_route(self):
        app = asyncio.run(
            LaurenFactory.create(CfgModule, global_guards=[GlobalAdminGuard])
        )
        client = TestClient(app)
        # Without the header the global guard denies.
        assert client.get("/cfg/").status_code == 403
        assert client.get("/cfg/", headers={"x-admin": "yes"}).status_code == 200

    def test_global_exception_filter_must_be_decorated(self):
        class NotAFilter:
            async def catch(self, exc, request):
                return Response.no_content()

        with pytest.raises(ExceptionHandlerConfigError):
            asyncio.run(
                LaurenFactory.create(CfgModule, global_exception_filters=[NotAFilter])
            )


# ---------------------------------------------------------------------------
# Composability sanity check: @use_guards / @use_middleware on routes
# (this was already supported but the docs implied it; locking it in).
# ---------------------------------------------------------------------------


class AllowGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


class DenyGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        raise ForbiddenError("nope")


@middleware
class MwA:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        resp = await call_next(request)
        return resp.with_header("x-mw-a", "1")


@middleware
class MwB:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        resp = await call_next(request)
        return resp.with_header("x-mw-b", "1")


@use_guards(AllowGuard)
@use_middleware(MwA)
@controller("/compose")
class ComposeController:
    @get("/open")
    async def open(self) -> dict:
        return {"area": "open"}

    @get("/locked")
    @use_guards(DenyGuard)  # adds to AllowGuard
    @use_middleware(MwB)  # adds to MwA
    async def locked(self) -> dict:
        return {"area": "locked"}


@module(controllers=[ComposeController])
class ComposeModule:
    pass


class TestComposability:
    def test_route_inherits_class_guards_and_middleware(self):
        client = TestClient(asyncio.run(LaurenFactory.create(ComposeModule)))
        r = client.get("/compose/open")
        assert r.status_code == 200
        assert r.header("x-mw-a") == "1"
        assert r.header("x-mw-b") is None

    def test_route_adds_its_own_guards_and_middleware_on_top(self):
        # Sanity check that route-level @use_guards composes with class
        # level guards — the additional DenyGuard fires and produces a
        # 403 even though the class-level AllowGuard would otherwise
        # admit the request. (Middleware around the error path is a
        # framework choice unrelated to composability; a guard rejection
        # short-circuits before middleware-after stages run, which is
        # the existing dispatcher contract.)
        client = TestClient(asyncio.run(LaurenFactory.create(ComposeModule)))
        r = client.get("/compose/locked")
        assert r.status_code == 403


# ---------------------------------------------------------------------------
# Lauren (FastAPI-style) imperative API
# ---------------------------------------------------------------------------


class TestLaurenImperativeAPI:
    @pytest.mark.asyncio
    async def test_add_guard_and_exception_handler(self):
        app = Lauren(docs_url=None, redoc_url=None, openapi_url=None)

        @exception_handler(DomainError)
        class H:
            async def catch(self, exc, request: Request) -> Response:
                return Response.json({"caught": "global"}, status=418)

        app.add_guard(GlobalAdminGuard)
        app.add_exception_handler(H)

        @app.get("/protected")
        async def protected() -> dict:
            return {"ok": True}

        @app.get("/blow")
        async def blow() -> dict:
            raise DomainError("kaboom")

        client = TestClient(app)
        # Global guard denies without the header.
        assert client.get("/protected").status_code == 403
        assert client.get("/protected", headers={"x-admin": "yes"}).status_code == 200
        # Global handler catches DomainError raised from any route.
        r = client.get("/blow", headers={"x-admin": "yes"})
        assert r.status_code == 418
        assert r.json() == {"caught": "global"}

    @pytest.mark.asyncio
    async def test_add_exception_handler_rejects_undecorated(self):
        app = Lauren(docs_url=None, redoc_url=None, openapi_url=None)

        class NotAHandler:
            async def catch(self, exc, request):
                return Response.no_content()

        with pytest.raises(ExceptionHandlerConfigError):
            app.add_exception_handler(NotAHandler)


# ---------------------------------------------------------------------------
# Multiple-handler ordering on the same scope
# ---------------------------------------------------------------------------


@exception_handler(ValidationDomainError)
class FirstFitHandler:
    async def catch(self, exc, request) -> dict:
        return {"who": "first"}


@exception_handler(DomainError)  # broader — would also match the subclass
class FallbackHandler:
    async def catch(self, exc, request) -> dict:
        return {"who": "fallback"}


@controller("/order")
class OrderController:
    @get("/precise")
    @use_exception_handlers(FirstFitHandler, FallbackHandler)
    async def precise(self) -> dict:
        raise ValidationDomainError()

    @get("/only-broad")
    @use_exception_handlers(FallbackHandler)
    async def only_broad(self) -> dict:
        raise ValidationDomainError()


@module(controllers=[OrderController])
class OrderModule:
    pass


class TestOrderingWithinScope:
    def test_first_matching_wins(self):
        client = TestClient(asyncio.run(LaurenFactory.create(OrderModule)))
        r = client.get("/order/precise")
        assert r.json() == {"who": "first"}

    def test_broad_handler_catches_subclass(self):
        client = TestClient(asyncio.run(LaurenFactory.create(OrderModule)))
        r = client.get("/order/only-broad")
        assert r.json() == {"who": "fallback"}
