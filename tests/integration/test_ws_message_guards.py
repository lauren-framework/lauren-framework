"""Method-level guards and interceptors on WebSocket hooks (D03 §5.1).

``@use_guards`` / ``@use_interceptors`` on an ``@on_message`` handler run for
every frame routed to it; on ``@on_connect`` they run once, after the gateway's
own guards. Placements WebSockets cannot honour are rejected at startup — a
protection is never accepted and silently skipped.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    CallHandler,
    Json,
    LaurenFactory,
    StdlibJSONEncoder,
    WebSocket,
    WsConnectionContext,
    WsMessageContext,
    module,
    on_connect,
    on_disconnect,
    on_error,
    exception_handler,
    on_message,
    set_metadata,
    use_encoder,
    use_exception_handlers,
    use_guards,
    use_interceptors,
    use_middlewares,
    ws_controller,
)
from lauren.exceptions import (
    DecoratorUsageError,
    ExceptionHandlerConfigError,
    GuardConfigError,
    InterceptorConfigError,
    MiddlewareConfigError,
    UnauthorizedError,
)
from lauren.testing import WsTestClient


@dataclass
class Wipe:
    scope: str


class DenyAll:
    async def can_activate(self, ctx: Any) -> bool:
        return False


class RequireAdminHeader:
    async def can_activate(self, ctx: Any) -> bool:
        return ctx.request.headers.get("x-role") == "admin"


def _app(gateway: type, **kwargs: Any) -> Any:
    @module(controllers=[gateway])
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, **kwargs)


async def _recv(ws: Any) -> Any:
    # A regression that silently runs a guarded handler must fail, not hang.
    return await asyncio.wait_for(ws.receive_json(), timeout=2)


# ---------------------------------------------------------------------------
# Per-message enforcement
# ---------------------------------------------------------------------------


@ws_controller("/chat")
class GuardedChat:
    @on_message("admin.wipe")
    @use_guards(DenyAll)
    async def wipe(self, ws: WebSocket) -> None:
        await ws.send_json({"event": "wiped"})

    @on_message("ping")
    async def ping(self, ws: WebSocket) -> None:
        await ws.send_json({"event": "pong"})


async def test_denied_message_does_not_run_the_handler() -> None:
    async with WsTestClient(_app(GuardedChat)).connect("/chat") as ws:
        await ws.send_json({"event": "admin.wipe", "data": {}})
        reply = await _recv(ws)

    assert reply == {
        "error": {
            "code": "forbidden",
            "message": "guard DenyAll denied the message",
            "detail": {"guard": "DenyAll", "event": "admin.wipe"},
        }
    }


async def test_connection_stays_open_after_a_denial() -> None:
    async with WsTestClient(_app(GuardedChat)).connect("/chat") as ws:
        await ws.send_json({"event": "admin.wipe", "data": {}})
        await _recv(ws)
        await ws.send_json({"event": "ping"})
        reply = await _recv(ws)
        assert ws.close_code is None

    assert reply == {"event": "pong"}


async def test_guard_sees_event_metadata_and_validated_payload() -> None:
    seen: list[Any] = []

    class ScopeGuard:
        async def can_activate(self, ctx: Any) -> bool:
            seen.append(ctx)
            return ctx.payload.scope == ctx.get_metadata("allowed_scope")

    @set_metadata("gateway", "inventory")
    @ws_controller("/inventory")
    class Inventory:
        @on_message("wipe")
        @use_guards(ScopeGuard)
        @set_metadata("allowed_scope", "drafts")
        async def wipe(self, ws: WebSocket, body: Json[Wipe]) -> None:
            await ws.send_json({"event": "wiped", "scope": body.scope})

    async with WsTestClient(_app(Inventory)).connect("/inventory") as ws:
        await ws.send_json({"event": "wipe", "data": {"scope": "drafts"}})
        allowed = await _recv(ws)
        await ws.send_json({"event": "wipe", "data": {"scope": "everything"}})
        denied = await _recv(ws)

    assert allowed == {"event": "wiped", "scope": "drafts"}
    assert denied["error"]["code"] == "forbidden"
    ctx = seen[0]
    assert isinstance(ctx, WsMessageContext)
    assert isinstance(ctx, WsConnectionContext)
    assert ctx.event == "wipe"
    assert ctx.payload == Wipe(scope="drafts")
    assert ctx.handler_class is Inventory
    assert ctx.handler_func is not None and ctx.handler_func.__name__ == "wipe"
    assert ctx.route_template == "/inventory"
    assert ctx.metadata == {"gateway": "inventory", "allowed_scope": "drafts"}


async def test_class_guards_run_at_connect_and_method_guards_per_message() -> None:
    calls: list[str] = []

    class ConnectGuard:
        async def can_activate(self, ctx: Any) -> bool:
            calls.append(f"connect:{type(ctx).__name__}")
            return True

    class MessageGuard:
        async def can_activate(self, ctx: Any) -> bool:
            calls.append(f"message:{ctx.event}")
            return True

    @use_guards(ConnectGuard)
    @ws_controller("/ordered")
    class Ordered:
        @on_message("a")
        @use_guards(MessageGuard)
        async def a(self, ws: WebSocket) -> None:
            await ws.send_json({"ok": "a"})

        @on_message("b")
        async def b(self, ws: WebSocket) -> None:
            await ws.send_json({"ok": "b"})

    async with WsTestClient(_app(Ordered)).connect("/ordered") as ws:
        for event in ("a", "b", "a"):
            await ws.send_json({"event": event})
            await _recv(ws)

    assert calls == ["connect:WsConnectionContext", "message:a", "message:a"]


async def test_guard_raising_http_error_answers_with_its_envelope() -> None:
    class TokenGuard:
        async def can_activate(self, ctx: Any) -> bool:
            raise UnauthorizedError("token expired", detail={"event": ctx.event})

    @ws_controller("/tokens")
    class Tokens:
        @on_message("refresh")
        @use_guards(TokenGuard)
        async def refresh(self, ws: WebSocket) -> None:
            await ws.send_json({"event": "refreshed"})

        @on_message("ping")
        async def ping(self, ws: WebSocket) -> None:
            await ws.send_json({"event": "pong"})

    async with WsTestClient(_app(Tokens)).connect("/tokens") as ws:
        await ws.send_json({"event": "refresh"})
        denied = await _recv(ws)
        await ws.send_json({"event": "ping"})
        pong = await _recv(ws)

    assert denied == {
        "error": {"code": "unauthorized", "message": "token expired", "detail": {"event": "refresh"}}
    }
    assert pong == {"event": "pong"}


async def test_broken_guard_fails_closed() -> None:
    ran: list[bool] = []

    class BrokenGuard:
        async def can_activate(self, ctx: Any) -> bool:
            raise RuntimeError("guard bug")

    @ws_controller("/broken")
    class Broken:
        @on_message("go")
        @use_guards(BrokenGuard)
        async def go(self, ws: WebSocket) -> None:
            ran.append(True)

    async with WsTestClient(_app(Broken)).connect("/broken") as ws:
        await ws.send_json({"event": "go"})
        reply = await _recv(ws)

    assert ran == []
    assert reply["error"]["code"] == "websocket_error"
    assert reply["error"]["detail"] == {"type": "RuntimeError"}


async def test_wildcard_guard_sees_the_frame_event() -> None:
    events: list[str] = []

    class Recorder:
        async def can_activate(self, ctx: Any) -> bool:
            events.append(ctx.event)
            return ctx.event != "secret"

    @ws_controller("/any")
    class Anything:
        @on_message("*")
        @use_guards(Recorder)
        async def anything(self, ws: WebSocket) -> None:
            await ws.send_json({"ok": True})

    async with WsTestClient(_app(Anything)).connect("/any") as ws:
        await ws.send_json({"event": "hello"})
        ok = await _recv(ws)
        await ws.send_json({"event": "secret"})
        denied = await _recv(ws)

    assert events == ["hello", "secret"]
    assert ok == {"ok": True}
    assert denied["error"]["detail"]["event"] == "secret"


async def test_binary_handler_guard_is_enforced() -> None:
    @ws_controller("/blobs")
    class Blobs:
        @on_message("__binary__")
        @use_guards(DenyAll)
        async def blob(self, ws: WebSocket, body: bytes) -> None:
            await ws.send_bytes(body)

    async with WsTestClient(_app(Blobs)).connect("/blobs") as ws:
        await ws.send_bytes(b"\x00\x01")
        reply = await _recv(ws)

    assert reply["error"]["code"] == "forbidden"
    assert reply["error"]["detail"]["event"] == "__binary__"


async def test_guards_on_both_sides_of_staticmethod_all_run() -> None:
    # Regression: a guard stacked above @staticmethod was dropped when the
    # function itself also carried guards, so the handler ran unguarded.
    consulted: list[str] = []

    class RateLimit:
        async def can_activate(self, ctx: Any) -> bool:
            consulted.append("RateLimit")
            return True

    class AdminOnly:
        async def can_activate(self, ctx: Any) -> bool:
            consulted.append("AdminOnly")
            return False

    @ws_controller("/static-guards")
    class StaticGuards:
        @use_guards(AdminOnly)
        @staticmethod
        @use_guards(RateLimit)
        @on_message("wipe")
        async def wipe(ws: WebSocket) -> None:
            await ws.send_json({"event": "wiped"})

    async with WsTestClient(_app(StaticGuards)).connect("/static-guards") as ws:
        await ws.send_json({"event": "wipe"})
        reply = await _recv(ws)

    assert reply["error"]["code"] == "forbidden"
    assert consulted == ["RateLimit", "AdminOnly"]


# ---------------------------------------------------------------------------
# Interceptors
# ---------------------------------------------------------------------------


async def test_method_interceptor_wraps_only_its_handler() -> None:
    log: list[Any] = []

    class Audit:
        async def intercept(self, ctx: Any, call_handler: CallHandler) -> Any:
            log.append(("before", ctx.event))
            result = await call_handler.handle()
            log.append(("after", ctx.event, result))
            return result

    @ws_controller("/audited")
    class Audited:
        @on_message("transfer")
        @use_interceptors(Audit)
        async def transfer(self, ws: WebSocket) -> str:
            await ws.send_json({"event": "transferred"})
            return "receipt-1"

        @on_message("ping")
        async def ping(self, ws: WebSocket) -> None:
            await ws.send_json({"event": "pong"})

    async with WsTestClient(_app(Audited)).connect("/audited") as ws:
        await ws.send_json({"event": "ping"})
        await _recv(ws)
        await ws.send_json({"event": "transfer"})
        await _recv(ws)

    assert log == [("before", "transfer"), ("after", "transfer", "receipt-1")]


async def test_guards_run_before_interceptors() -> None:
    order: list[str] = []

    class Guard:
        async def can_activate(self, ctx: Any) -> bool:
            order.append("guard")
            return True

    class Wrap:
        async def intercept(self, ctx: Any, call_handler: CallHandler) -> Any:
            order.append("interceptor")
            return await call_handler.handle()

    @ws_controller("/order")
    class Order:
        @on_message("go")
        @use_interceptors(Wrap)
        @use_guards(Guard)
        async def go(self, ws: WebSocket) -> None:
            order.append("handler")
            await ws.send_json({"ok": True})

    async with WsTestClient(_app(Order)).connect("/order") as ws:
        await ws.send_json({"event": "go"})
        await _recv(ws)

    assert order == ["guard", "interceptor", "handler"]


def _recording_interceptor(name: str, log: list[str]) -> type:
    class Recorder:
        async def intercept(self, ctx: Any, call_handler: CallHandler) -> Any:
            log.append(f"{name}:before")
            result = await call_handler.handle()
            log.append(f"{name}:after")
            return result

    Recorder.__name__ = Recorder.__qualname__ = name
    return Recorder


async def test_stacked_message_interceptors_nest_in_declaration_order() -> None:
    # Regression: with two or more interceptors every layer called itself
    # (late-bound closures) and each frame failed with RecursionError.
    log: list[str] = []
    outer, inner = _recording_interceptor("Outer", log), _recording_interceptor("Inner", log)

    @ws_controller("/stacked")
    class Stacked:
        @on_message("go")
        @use_interceptors(outer, inner)
        async def go(self, ws: WebSocket) -> None:
            log.append("handler")
            await ws.send_json({"ok": True})

    async with WsTestClient(_app(Stacked)).connect("/stacked") as ws:
        await ws.send_json({"event": "go"})
        reply = await _recv(ws)

    assert reply == {"ok": True}
    assert log == ["Outer:before", "Inner:before", "handler", "Inner:after", "Outer:after"]


async def test_class_and_on_connect_interceptors_compose() -> None:
    log: list[str] = []
    class_level, method_level = _recording_interceptor("Class", log), _recording_interceptor("Method", log)

    @use_interceptors(class_level)
    @ws_controller("/composed")
    class Composed:
        @on_connect
        @use_interceptors(method_level)
        async def joined(self, ws: WebSocket) -> None:
            log.append("connect")

    async with WsTestClient(_app(Composed)).connect("/composed") as ws:
        assert ws.close_code is None

    assert log == ["Class:before", "Method:before", "connect", "Method:after", "Class:after"]


# ---------------------------------------------------------------------------
# @on_connect method-level policies
# ---------------------------------------------------------------------------


async def test_on_connect_method_guard_runs_after_class_guards() -> None:
    order: list[str] = []

    class ClassGuard:
        async def can_activate(self, ctx: Any) -> bool:
            order.append("class")
            return True

    class AdminOnly:
        async def can_activate(self, ctx: Any) -> bool:
            order.append("method")
            return ctx.request.headers.get("x-role") == "admin"

    @use_guards(ClassGuard)
    @ws_controller("/admin")
    class Admin:
        @on_connect
        @use_guards(AdminOnly)
        async def joined(self, ws: WebSocket) -> None:
            await ws.accept()

    app = _app(Admin)
    async with WsTestClient(app).connect("/admin") as rejected:
        assert rejected.close_code == 1008
    async with WsTestClient(app).connect("/admin", headers={"x-role": "admin"}) as accepted:
        assert accepted.close_code is None

    assert order == ["class", "method", "class", "method"]


async def test_on_connect_method_interceptor_wraps_the_connect_hook() -> None:
    log: list[str] = []

    class ConnectAudit:
        async def intercept(self, ctx: Any, call_handler: CallHandler) -> Any:
            log.append("before")
            result = await call_handler.handle()
            log.append("after")
            return result

    @ws_controller("/hello")
    class Hello:
        @on_connect
        @use_interceptors(ConnectAudit)
        async def joined(self, ws: WebSocket) -> None:
            log.append("connect")

    async with WsTestClient(_app(Hello)).connect("/hello"):
        pass

    assert log == ["before", "connect", "after"]


# ---------------------------------------------------------------------------
# DI registration — guards need not be listed as providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("placement", ["class", "method", "global"])
async def test_guards_resolve_without_a_provider_entry(placement: str) -> None:
    if placement == "class":

        @use_guards(RequireAdminHeader)
        @ws_controller("/secure")
        class Secure:
            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"event": "pong"})

        app = _app(Secure)
    elif placement == "method":

        @ws_controller("/secure")
        class Secure:
            @on_message("ping")
            @use_guards(RequireAdminHeader)
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"event": "pong"})

        app = _app(Secure)
    else:

        @ws_controller("/secure")
        class Secure:
            @on_message("ping")
            async def ping(self, ws: WebSocket) -> None:
                await ws.send_json({"event": "pong"})

        app = _app(Secure, global_ws_guards=[RequireAdminHeader])

    async with WsTestClient(app).connect("/secure", headers={"x-role": "admin"}) as ws:
        await ws.send_json({"event": "ping"})
        reply = await _recv(ws)

    assert reply == {"event": "pong"}


# ---------------------------------------------------------------------------
# Startup rejections
# ---------------------------------------------------------------------------


class AnyMiddleware:
    async def dispatch(self, request: Any, call_next: Any) -> Any:
        return await call_next(request)


class Passthrough:
    async def intercept(self, ctx: Any, call_handler: CallHandler) -> Any:
        return await call_handler.handle()


def test_middleware_on_message_handler_is_rejected() -> None:
    @ws_controller("/mw")
    class WithMiddleware:
        @on_message("x")
        @use_middlewares(AnyMiddleware)
        async def x(self, ws: WebSocket) -> None: ...

    with pytest.raises(MiddlewareConfigError) as excinfo:
        _app(WithMiddleware)

    assert excinfo.value.detail == {"gateway": "WithMiddleware", "method": "x", "hook": "on_message"}
    assert "@use_guards" in str(excinfo.value)


def test_middleware_on_connect_hook_is_rejected() -> None:
    @ws_controller("/mw")
    class ConnectMiddleware:
        @on_connect
        @use_middlewares(AnyMiddleware)
        async def joined(self, ws: WebSocket) -> None: ...

    with pytest.raises(MiddlewareConfigError) as excinfo:
        _app(ConnectMiddleware)

    assert excinfo.value.detail["hook"] == "on_connect"


@pytest.mark.parametrize("hook", ["on_disconnect", "on_error"])
def test_guard_on_disconnect_or_error_hook_is_rejected(hook: str) -> None:
    decorator = on_disconnect if hook == "on_disconnect" else on_error

    @ws_controller("/g")
    class Gateway:
        @decorator
        @use_guards(DenyAll)
        async def cleanup(self, ws: WebSocket) -> None: ...

    with pytest.raises(GuardConfigError) as excinfo:
        _app(Gateway)

    assert excinfo.value.detail == {"gateway": "Gateway", "method": "cleanup", "hook": hook}


@pytest.mark.parametrize("hook", ["on_disconnect", "on_error"])
def test_interceptor_on_disconnect_or_error_hook_is_rejected(hook: str) -> None:
    decorator = on_disconnect if hook == "on_disconnect" else on_error

    @ws_controller("/i")
    class Gateway:
        @decorator
        @use_interceptors(Passthrough)
        async def cleanup(self, ws: WebSocket) -> None: ...

    with pytest.raises(InterceptorConfigError) as excinfo:
        _app(Gateway)

    assert excinfo.value.detail["hook"] == hook


def test_exception_handlers_on_ws_hook_are_rejected() -> None:
    @exception_handler(ValueError)
    def to_400(exc: Exception, request: Any) -> Any: ...

    @ws_controller("/eh")
    class WithHandlers:
        @on_message("x")
        @use_exception_handlers(to_400)
        async def x(self, ws: WebSocket) -> None: ...

    with pytest.raises(ExceptionHandlerConfigError) as excinfo:
        _app(WithHandlers)

    assert excinfo.value.detail == {"gateway": "WithHandlers", "method": "x", "hook": "on_message"}
    assert "@on_error" in str(excinfo.value)


def test_encoder_on_ws_hook_is_rejected() -> None:
    @ws_controller("/enc")
    class WithEncoder:
        @on_message("x")
        @use_encoder(StdlibJSONEncoder())
        async def x(self, ws: WebSocket) -> None: ...

    with pytest.raises(DecoratorUsageError) as excinfo:
        _app(WithEncoder)

    assert excinfo.value.detail["method"] == "x"


def test_global_ws_guard_without_can_activate_is_rejected() -> None:
    class NotAGuard:
        pass

    with pytest.raises(GuardConfigError):
        _app(GuardedChat, global_ws_guards=[NotAGuard])


def test_global_ws_interceptor_without_intercept_is_rejected() -> None:
    class NotAnInterceptor:
        pass

    with pytest.raises(InterceptorConfigError):
        _app(GuardedChat, global_ws_interceptors=[NotAnInterceptor])


def test_guard_on_socketio_event_handler_is_rejected() -> None:
    from lauren.socketio import on_socketio_event, socketio_controller

    with pytest.raises(GuardConfigError) as excinfo:

        @socketio_controller("/sio")
        class Sio:
            @on_socketio_event("admin:wipe")
            @use_guards(DenyAll)
            async def wipe(self, conn: Any) -> None: ...

    assert excinfo.value.detail == {"controller": "Sio", "event": "admin:wipe"}


# ---------------------------------------------------------------------------
# Defense in depth
# ---------------------------------------------------------------------------


async def test_guarded_handler_invoked_without_frame_context_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A dispatch wrapper written against the old ``run_hook(fn, extractions)``
    # call shape must not run a guarded handler with its guards skipped.
    import lauren._ws_runtime as runtime

    ran: list[bool] = []

    async def legacy_dispatch(gateway: Any, run_hook: Any, text: str, ws: Any) -> None:
        compiled = gateway.messages["admin.wipe"]
        await run_hook(compiled.handler_fn, compiled.extractions)

    @ws_controller("/legacy")
    class Legacy:
        @on_message("admin.wipe")
        @use_guards(DenyAll)
        async def wipe(self, ws: WebSocket) -> None:
            ran.append(True)
            await ws.send_json({"event": "wiped"})

    monkeypatch.setattr(runtime, "_dispatch_text", legacy_dispatch)
    async with WsTestClient(_app(Legacy)).connect("/legacy") as ws:
        await ws.send_json({"event": "admin.wipe"})
        reply = await _recv(ws)

    assert ran == []
    assert reply["error"]["detail"] == {"type": "RuntimeError"}


def test_class_level_middlewares_on_a_gateway_warn_at_startup() -> None:
    from lauren.logging import InMemoryLogger, LogLevel

    @use_middlewares(AnyMiddleware)
    @ws_controller("/mw-class")
    class WithClassMiddleware:
        @on_message("x")
        async def x(self, ws: WebSocket) -> None: ...

    log = InMemoryLogger(level=LogLevel.DEBUG)
    _app(WithClassMiddleware, logger=log)

    warnings = [r for r in log.records if r.level == LogLevel.WARN and "do not run middleware" in r.message]
    assert len(warnings) == 1
    assert warnings[0].extra["gateway"] == "WithClassMiddleware"
