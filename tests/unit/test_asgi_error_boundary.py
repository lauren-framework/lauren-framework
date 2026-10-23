"""Unit tests for the request-wide error boundary in ``LaurenApp.handle`` (D01).

``handle`` must always return a :class:`Response` — whatever raises, wherever
it raises — and attribute unhandled errors to the component that raised them.
These tests call ``handle`` directly (no ASGI server, no test client).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import (
    Depends,
    LaurenFactory,
    Request,
    Response,
    Scope,
    controller,
    exception_handler,
    get,
    injectable,
    middleware,
    module,
    use_exception_handlers,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from lauren.logging import InMemoryLogger, LogLevel


class _Boom(Exception):
    pass


def _raising_middleware(name: str, *, when: str, exc: BaseException | None) -> type:
    """Build a global middleware that raises ``exc`` before or after ``call_next``."""

    async def dispatch(self: Any, request: Request, call_next: Any) -> Response:
        if exc is not None and when == "before":
            raise exc
        response = await call_next(request)
        if exc is not None and when == "after":
            raise exc
        return response

    return middleware()(type(name, (), {"dispatch": dispatch}))


@controller("/r")
class _Routes:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[_Routes])
class _Module:
    pass


def _handle(app: Any, path: str = "/r/") -> Response:
    async def run() -> Response:
        await app.startup()
        return await app.handle(Request(method="GET", path=path))

    return asyncio.run(run())


_ERRORS: list[tuple[str, BaseException, int]] = [
    ("http_error", UnauthorizedError("no"), 401),
    ("value_error", ValueError("bad"), 500),
    ("key_error", KeyError("k"), 500),
]


@pytest.mark.parametrize("count", [1, 3])
@pytest.mark.parametrize("when", ["before", "after"])
@pytest.mark.parametrize("label,exc,status", _ERRORS, ids=[e[0] for e in _ERRORS])
def test_handle_always_returns_a_response(
    count: int, when: str, label: str, exc: BaseException, status: int
) -> None:
    # The innermost middleware raises; the outer ones pass through.
    mws = [_raising_middleware(f"Pass{i}", when=when, exc=None) for i in range(count - 1)]
    mws.append(_raising_middleware("Raiser", when=when, exc=exc))
    app = LaurenFactory.create(_Module, global_middlewares=mws)

    response = _handle(app)

    assert isinstance(response, Response)
    assert response.status == status
    assert response.headers.get("connection") is None


def test_innermost_raising_middleware_is_named_in_the_log() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)
    outer = _raising_middleware("Outer", when="before", exc=None)
    middle = _raising_middleware("Middle", when="before", exc=None)
    inner = _raising_middleware("Inner", when="before", exc=_Boom("inner failed"))
    app = LaurenFactory.create(_Module, global_middlewares=[outer, middle, inner], logger=log)

    response = _handle(app)

    assert response.status == 500
    origins = [r.extra["origin"] for r in log.records if "origin" in r.extra]
    assert origins == ["global middleware Inner"]


def test_middleware_reraising_downstream_error_does_not_steal_attribution() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    @middleware()
    class Observer:
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            try:
                return await call_next(request)
            except Exception:
                raise

    raiser = _raising_middleware("Raiser", when="before", exc=_Boom("x"))
    app = LaurenFactory.create(_Module, global_middlewares=[Observer, raiser], logger=log)

    assert _handle(app).status == 500
    origins = [r.extra["origin"] for r in log.records if "origin" in r.extra]
    assert origins == ["global middleware Raiser"]


def test_failing_handler_without_global_middleware_returns_500() -> None:
    # Regression: this used to raise UnboundLocalError out of handle().
    @exception_handler(_Boom)
    def broken(exc: Exception, request: Request) -> Response:
        raise RuntimeError("handler is broken")

    @use_exception_handlers(broken)
    @controller("/b")
    class Boomer:
        @get("/")
        async def index(self) -> dict:
            raise _Boom("route failed")

    @module(controllers=[Boomer])
    class BoomModule:
        pass

    app = LaurenFactory.create(BoomModule)
    response = _handle(app, "/b/")

    assert isinstance(response, Response)
    assert response.status == 500


def test_unclaimed_errors_outside_the_route_keep_the_error_format() -> None:
    raiser = _raising_middleware("Raiser", when="before", exc=UnauthorizedError("no"))
    app = LaurenFactory.create(_Module, global_middlewares=[raiser], error_format="rfc7807")

    response = _handle(app)

    assert response.status == 401
    assert response.headers.get("content-type") == "application/problem+json"


def test_global_middleware_resolution_failure_is_rendered() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    @injectable(scope=Scope.REQUEST)
    def tenant_lookup() -> str:
        raise ConnectionError("tenant directory unavailable")

    @middleware()
    @injectable(scope=Scope.REQUEST)
    class TenantMiddleware:
        def __init__(self, tenant: Depends[tenant_lookup]) -> None:
            self.tenant = tenant

        async def dispatch(self, request: Request, call_next: Any) -> Response:
            return await call_next(request)

    app = LaurenFactory.create(
        _Module,
        global_middlewares=[TenantMiddleware],
        global_providers=[tenant_lookup],
        logger=log,
    )
    response = _handle(app)

    assert isinstance(response, Response)
    assert response.status == 500
    origins = [r.extra["origin"] for r in log.records if "origin" in r.extra]
    assert origins == [f"global middleware {TenantMiddleware.__qualname__} (resolution)"]


# ---------------------------------------------------------------------------
# Regressions found in review: logging, teardown order, unrenderable errors
# ---------------------------------------------------------------------------


class _ProtocolLogger:
    """A logger that implements only the public ``lauren.logging.Logger`` protocol."""

    def __init__(self, level: LogLevel = LogLevel.DEBUG, *, broken: bool = False) -> None:
        self.level = level
        self.broken = broken
        self.records: list[Any] = []

    def log_record(self, record: Any) -> None:
        if self.broken:
            raise RuntimeError("log sink is down")
        self.records.append(record)

    def _sugar(self, message: str, *, context: str = "", **extra: Any) -> None:
        pass

    log = info = debug = verbose = warn = warning = error = _sugar


@pytest.mark.parametrize("broken", [False, True])
def test_protocol_only_logger_keeps_error_statuses(broken: bool) -> None:
    # Regression: request logging called the private ``_emit``; with a
    # protocol-only (or failing) logger a 401 became a bare 500.
    raiser = _raising_middleware("Raiser", when="before", exc=UnauthorizedError("no"))
    log = _ProtocolLogger(broken=broken)
    app = LaurenFactory.create(_Module, global_middlewares=[raiser], logger=log)

    response = _handle(app)

    assert response.status == 401
    if not broken:
        assert any(getattr(r, "context", "") == "Request" for r in log.records)


@pytest.mark.parametrize("when", ["before", "after"])
def test_outer_error_handlers_see_live_request_scoped_dependencies(when: str) -> None:
    # Regression: request-scoped instances were finalized at the end of the
    # route, before global middleware and outer error rendering finished.
    events: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        def __init__(self) -> None:
            self.closed = False
            events.append("opened")

        async def aclose(self) -> None:
            self.closed = True
            events.append("closed")

    @exception_handler(UnauthorizedError)
    @injectable(scope=Scope.REQUEST)
    class AuditUnauthorized:
        def __init__(self, db: DbSession) -> None:
            self.db = db

        def catch(self, exc: Exception, request: Request) -> Response:
            return Response.json({"session_closed": self.db.closed}, status=401)

    raiser = _raising_middleware("Raiser", when=when, exc=UnauthorizedError("no"))
    app = LaurenFactory.create(
        _Module,
        global_middlewares=[raiser],
        global_exception_handlers=[AuditUnauthorized],
        global_providers=[DbSession],
    )

    response = _handle(app)

    assert response.status == 401
    assert response.body == b'{"session_closed":false}'
    assert events == ["opened", "closed"]


def _complete_events(app: Any) -> list[Any]:
    from lauren.signals import RequestComplete

    events: list[Any] = []
    app.signals.on(RequestComplete)(lambda event: events.append(event))
    return events


def test_handler_result_that_cannot_be_encoded_is_a_handler_failure() -> None:
    calls: list[str] = []

    @exception_handler(Exception)
    def catch_all(exc: Exception, request: Request) -> Any:
        calls.append(type(exc).__name__)
        return {"value": 1j}  # not JSON-encodable

    @controller("/b")
    class Boom:
        @get("/")
        async def index(self) -> dict:
            raise ValueError("boom")

    @module(controllers=[Boom])
    class BoomModule:
        pass

    app = LaurenFactory.create(BoomModule, global_exception_handlers=[catch_all])
    events = _complete_events(app)

    response = _handle(app, "/b/")

    assert response.status == 500
    assert calls == ["ValueError"]  # not offered its own TypeError again
    assert len(events) == 1


def test_http_error_with_unencodable_detail_keeps_its_status() -> None:
    raiser = _raising_middleware("Raiser", when="before", exc=ForbiddenError("nope", detail={"value": 1j}))
    app = LaurenFactory.create(_Module, global_middlewares=[raiser])
    events = _complete_events(app)

    response = _handle(app)

    assert response.status == 403
    assert response.body == b'{"error":{"code":"forbidden","message":"nope","detail":{}}}'
    assert len(events) == 1


@pytest.mark.parametrize("returned", [None, {"not": "a response"}])
def test_middleware_returning_a_non_response_is_a_500(returned: Any) -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    @middleware()
    class Sloppy:
        async def dispatch(self, request: Request, call_next: Any) -> Any:
            await call_next(request)
            return returned

    app = LaurenFactory.create(_Module, global_middlewares=[Sloppy], logger=log)
    events = _complete_events(app)

    response = _handle(app)

    assert isinstance(response, Response)
    assert response.status == 500
    assert len(events) == 1
    origins = [r.extra["origin"] for r in log.records if "origin" in r.extra]
    assert origins == [f"global middleware {Sloppy.__qualname__}"]


def test_broken_encoder_still_yields_a_500_envelope() -> None:
    from lauren import StdlibJSONEncoder

    class BrokenEncoder(StdlibJSONEncoder):
        def encode(self, value: Any) -> bytes:
            raise RuntimeError("encoder down")

        encode_compact = encode

    raiser = _raising_middleware("Raiser", when="before", exc=ValueError("bad"))
    app = LaurenFactory.create(_Module, global_middlewares=[raiser], json_encoder=BrokenEncoder())

    response = _handle(app)

    assert response.status == 500
    assert response.body.startswith(b'{"error":{"code":"internal_error"')
