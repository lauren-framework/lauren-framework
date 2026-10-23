"""Integration tests for the application event bus (``lauren.events``).

Every test builds a real app with ``LaurenFactory.create``. Deferred handlers
are awaited explicitly (``wait=True``, ``bus.drain()``, gates) — no test
depends on timing.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    LaurenApp,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    on_message,
    post,
    post_construct,
    use_value,
    ws_controller,
)
from lauren.events import (
    EmitResult,
    EventBus,
    EventContext,
    EventPublisher,
    EventsModule,
    TransactionPhase,
    UnitOfWork,
    on_event,
)
from lauren.exceptions import (
    DecoratorUsageError,
    DIScopeViolationError,
    LifecycleViolationError,
    StartupError,
    UnresolvableParameterError,
)
from lauren.signals import EventHandlerFailed, SignalBus
from lauren.testing import TestClient, WsTestClient
from lauren.websockets import WebSocket


@dataclass(frozen=True)
class DomainEvent:
    pass


@dataclass(frozen=True)
class UserRegistered(DomainEvent):
    user_id: str = "u1"
    email: str = "a@example.com"


@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: str = "o1"


@dataclass(frozen=True)
class Ping:
    n: int = 0


async def started(root: type, **kwargs: Any) -> LaurenApp:
    app = LaurenFactory.create(root, **kwargs)
    await app.startup()
    return app


async def bus_of(app: LaurenApp) -> EventBus:
    bus: EventBus = await app.container.resolve(EventBus)
    return bus


# ---------------------------------------------------------------------------
# Publishing from a request (EventPublisher)
# ---------------------------------------------------------------------------


async def test_inline_handlers_share_the_request_scope_with_the_controller() -> None:
    seen: dict[str, Any] = {}

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        pass

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered, db: DbSession, ctx: EventContext) -> None:
            seen["handler_db"] = db
            seen["in_request"] = ctx.in_request

    @controller("/users")
    class Users:
        @post("/")
        async def register(self, events: EventPublisher, db: DbSession) -> dict[str, Any]:
            seen["controller_db"] = db
            result = await events.emit(UserRegistered())
            return {"handled": result.handled}

    @module(controllers=[Users], providers=[DbSession, Audit])
    class AppModule:
        pass

    app = await started(AppModule)

    response = await TestClient(app).arequest("POST", "/users/")

    assert response.json() == {"handled": 1}
    assert seen["handler_db"] is seen["controller_db"]
    assert seen["in_request"] is True


async def test_background_handlers_run_after_the_response_in_a_fresh_scope() -> None:
    log: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        def __init__(self) -> None:
            self.name = f"db{sum(1 for entry in log if entry.startswith('open'))}"
            log.append(f"open {self.name}")

        async def aclose(self) -> None:
            log.append(f"close {self.name}")

    @injectable()
    class Mailer:
        @on_event(mode="background")
        async def send_welcome(self, event: UserRegistered, db: DbSession) -> None:
            log.append(f"mail via {db.name}")

    @controller("/users")
    class Users:
        @post("/")
        async def register(self, events: EventPublisher, db: DbSession) -> dict[str, str]:
            result = await events.emit(UserRegistered())
            log.append(f"scheduled {result.scheduled}")
            return {"db": db.name}

    @module(controllers=[Users], providers=[DbSession, Mailer])
    class AppModule:
        pass

    app = await started(AppModule)
    received = iter([{"type": "http.request", "body": b"", "more_body": False}])

    async def receive() -> dict[str, Any]:
        return next(received, {"type": "http.disconnect"})

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.body":
            log.append("response sent")

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/users/",
        "raw_path": b"/users/",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1),
        "server": ("testserver", 80),
    }

    await app(scope, receive, send)

    assert log == [
        "open db0",
        "scheduled 1",
        "close db0",
        "response sent",
        "open db1",
        "mail via db1",
        "close db1",
    ]


async def test_background_handlers_of_several_emissions_share_the_response_queue() -> None:
    mailed: list[str] = []

    @injectable()
    class Mailer:
        @on_event(mode="background")
        async def send(self, event: UserRegistered) -> None:
            mailed.append(event.email)

    @controller("/users")
    class Users:
        @post("/")
        async def register(self, events: EventPublisher) -> dict[str, bool]:
            await events.emit(UserRegistered(email="first@example.com"))
            await events.emit(UserRegistered(email="second@example.com"))
            return {"ok": True}

    @module(controllers=[Users], providers=[Mailer])
    class AppModule:
        pass

    app = await started(AppModule)

    await TestClient(app).arequest("POST", "/users/")

    assert mailed == ["first@example.com", "second@example.com"]


async def test_a_failing_inline_handler_turns_the_request_into_a_500_without_deferred_work() -> None:
    mailed: list[str] = []

    @injectable()
    class Audit:
        @on_event(priority=10)
        async def record(self, event: UserRegistered) -> None:
            raise RuntimeError("audit store down")

    @injectable()
    class Mailer:
        @on_event(mode="background")
        async def send(self, event: UserRegistered) -> None:
            mailed.append(event.email)

    @controller("/users")
    class Users:
        @post("/")
        async def register(self, events: EventPublisher) -> dict[str, bool]:
            await events.emit(UserRegistered())
            return {"ok": True}

    @module(controllers=[Users], providers=[Audit, Mailer])
    class AppModule:
        pass

    app = await started(AppModule)

    response = await TestClient(app).arequest("POST", "/users/")

    assert response.status_code == 500
    assert mailed == []


async def test_a_handler_can_publish_through_the_publisher_of_its_own_scope() -> None:
    seen: list[Any] = []

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        pass

    @injectable()
    class Orders:
        @on_event()
        async def place(self, event: UserRegistered, events: EventPublisher, db: DbSession) -> None:
            seen.append(db)
            await events.emit(OrderPlaced())

    @injectable()
    class Ledger:
        @on_event()
        async def book(self, event: OrderPlaced, db: DbSession, ctx: EventContext) -> None:
            seen.append(db)
            seen.append(ctx.depth)

    @module(providers=[DbSession, Orders, Ledger])
    class AppModule:
        pass

    app = await started(AppModule)

    await (await bus_of(app)).emit(UserRegistered())

    assert seen[0] is seen[1]
    assert seen[2] == 1


async def test_the_publisher_is_not_available_outside_a_request() -> None:
    @module()
    class AppModule:
        pass

    app = await started(AppModule)

    async with app.scope("script") as scope:
        with pytest.raises(LifecycleViolationError) as info:
            await scope.resolve(EventPublisher)

    assert info.value.detail["reason"] == "no_request_scope"


def test_a_singleton_cannot_depend_on_the_request_scoped_publisher() -> None:
    @injectable()
    class Service:
        def __init__(self, events: EventPublisher) -> None:
            self.events = events

    @module(providers=[Service])
    class AppModule:
        pass

    with pytest.raises(DIScopeViolationError):
        LaurenFactory.create(AppModule)


async def test_emit_threadsafe_publishes_from_a_sync_handler() -> None:
    seen: list[Any] = []

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered) -> None:
            seen.append(threading.get_ident())

    @controller("/users")
    class Users:
        @post("/")
        def register(self, events: EventPublisher) -> dict[str, int]:
            result = events.emit_threadsafe(UserRegistered()).result()
            return {"handled": result.handled, "thread": threading.get_ident()}

    @module(controllers=[Users], providers=[Audit])
    class AppModule:
        pass

    app = await started(AppModule)

    response = await TestClient(app).arequest("POST", "/users/")

    body = response.json()
    assert body["handled"] == 1
    assert seen == [threading.get_ident()]
    assert body["thread"] != threading.get_ident()


async def test_emit_threadsafe_is_rejected_on_the_event_loop() -> None:
    errors: list[BaseException] = []

    @controller("/users")
    class Users:
        @post("/")
        async def register(self, events: EventPublisher) -> dict[str, bool]:
            try:
                events.emit_threadsafe(UserRegistered())
            except RuntimeError as exc:
                errors.append(exc)
            return {"ok": True}

    @module(controllers=[Users])
    class AppModule:
        pass

    app = await started(AppModule)

    await TestClient(app).arequest("POST", "/users/")

    assert len(errors) == 1


@injectable(scope=Scope.REQUEST)
class WsDbSession:
    """Module level: the WebSocket compiler resolves hook annotations from module globals."""


async def test_websocket_hooks_publish_in_the_connection_scope() -> None:
    seen: dict[str, Any] = {}

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered, db: WsDbSession, ctx: EventContext) -> None:
            seen["handler_db"] = db
            seen["in_request"] = ctx.in_request

        @on_event(mode="background")  # no response on a socket: runs like "concurrent"
        async def later(self, event: UserRegistered) -> None:
            seen["background"] = True

    @ws_controller("/ws")
    class Gateway:
        @on_message("register")
        async def register(self, ws: WebSocket, events: EventPublisher, db: WsDbSession) -> None:
            seen["hook_db"] = db
            result = await events.emit(UserRegistered(), wait=True)
            await ws.send_json({"handled": result.handled})

    @module(controllers=[Gateway], providers=[WsDbSession, Audit])
    class AppModule:
        pass

    app = await started(AppModule)

    async with WsTestClient(app).connect("/ws") as ws:
        await ws.send_json({"event": "register"})
        reply = await ws.receive_json()

    assert reply == {"handled": 2}
    assert seen["handler_db"] is seen["hook_db"]
    assert (seen["in_request"], seen["background"]) == (True, True)


# ---------------------------------------------------------------------------
# Publishing from services (EventBus)
# ---------------------------------------------------------------------------


async def test_each_bus_emission_gets_a_fresh_operation_scope_that_is_closed() -> None:
    log: list[str] = []
    instances: list[Any] = []

    @injectable(scope=Scope.REQUEST)
    class DbSession:
        def __init__(self) -> None:
            instances.append(self)

        async def aclose(self) -> None:
            log.append(f"close {instances.index(self)}")

    @injectable()
    class Audit:
        @on_event()
        async def first(self, event: UserRegistered, db: DbSession) -> None:
            log.append(f"first {instances.index(db)}")

        @on_event()
        async def second(self, event: UserRegistered, db: DbSession) -> None:
            log.append(f"second {instances.index(db)}")

    @injectable()
    class UserService:
        def __init__(self, events: EventBus) -> None:
            self.events = events

        async def register(self) -> EmitResult:
            return await self.events.emit(UserRegistered())

    @module(providers=[DbSession, Audit, UserService])
    class AppModule:
        pass

    app = await started(AppModule)
    service = await app.container.resolve(UserService)

    await service.register()
    await service.register()

    assert log == ["first 0", "second 0", "close 0", "first 1", "second 1", "close 1"]


async def test_inline_handlers_run_by_priority_then_discovery_order() -> None:
    order: list[str] = []

    @injectable()
    class A:
        @on_event()
        async def plain(self, event: UserRegistered) -> None:
            order.append("A.plain")

        @on_event(priority=10)
        async def urgent(self, event: DomainEvent) -> None:
            order.append("A.urgent")

    @injectable()
    class B:
        @on_event(priority=10)
        async def urgent(self, event: UserRegistered) -> None:
            order.append("B.urgent")

        @on_event(priority=-5)
        async def late(self, event: UserRegistered) -> None:
            order.append("B.late")

    @module(providers=[A, B])
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)

    await bus.emit(UserRegistered())

    assert order == ["A.urgent", "B.urgent", "A.plain", "B.late"]
    assert bus.handlers_for(UserRegistered) == ("A.urgent", "B.urgent", "A.plain", "B.late")


async def test_a_propagating_failure_aborts_the_emission_and_schedules_nothing() -> None:
    calls: list[str] = []
    failures: list[EventHandlerFailed] = []
    signals = SignalBus()
    signals.on(EventHandlerFailed)(failures.append)

    @injectable()
    class Handlers:
        @on_event(priority=3)
        async def first(self, event: UserRegistered) -> None:
            calls.append("first")

        @on_event(priority=2)
        async def boom(self, event: UserRegistered) -> None:
            raise ValueError("boom")

        @on_event(priority=1)
        async def never(self, event: UserRegistered) -> None:
            calls.append("never")

        @on_event(mode="concurrent")
        async def deferred(self, event: UserRegistered) -> None:
            calls.append("deferred")

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule, signals=signals)
    bus = await bus_of(app)

    with pytest.raises(ValueError, match="boom"):
        await bus.emit(UserRegistered())
    await bus.drain()

    assert calls == ["first"]
    assert [(f.handler, f.mode, type(f.error)) for f in failures] == [("Handlers.boom", "inline", ValueError)]


async def test_an_isolated_failure_is_recorded_and_the_emission_continues() -> None:
    calls: list[str] = []

    @injectable()
    class Handlers:
        @on_event(priority=2, errors="isolate")
        async def flaky(self, event: UserRegistered) -> None:
            raise ValueError("flaky")

        @on_event(priority=1)
        async def steady(self, event: UserRegistered) -> None:
            calls.append("steady")

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)

    result = await (await bus_of(app)).emit(UserRegistered())

    assert calls == ["steady"]
    assert (result.handled, result.ok) == (1, False)
    assert [(f.handler, f.event_type, str(f.error)) for f in result.errors] == [
        ("Handlers.flaky", UserRegistered, "flaky")
    ]


async def test_wait_awaits_deferred_handlers_and_reports_their_failures() -> None:
    done: list[str] = []

    @injectable()
    class Handlers:
        @on_event(mode="concurrent")
        async def ok(self, event: UserRegistered) -> None:
            await asyncio.sleep(0)
            done.append("ok")

        @on_event(mode="background")
        async def broken(self, event: UserRegistered) -> None:
            raise KeyError("missing")

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)

    result = await (await bus_of(app)).emit(UserRegistered(), wait=True)

    assert done == ["ok"]
    assert (result.handled, result.scheduled) == (1, 2)
    assert [(f.handler, f.mode) for f in result.errors] == [("Handlers.broken", "background")]


async def test_the_event_context_describes_each_delivery() -> None:
    contexts: list[EventContext] = []

    @injectable()
    class Handlers:
        @on_event()
        async def inline(self, event: UserRegistered, ctx: EventContext) -> None:
            contexts.append(ctx)

        @on_event(mode="concurrent")
        async def deferred(self, event: UserRegistered, context: EventContext) -> None:
            contexts.append(context)

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)

    await bus.emit(UserRegistered(), wait=True)
    await bus.emit(UserRegistered(), wait=True)

    first, second, third, _fourth = contexts
    assert (first.mode, second.mode, first.depth, first.in_request) == ("inline", "concurrent", 0, False)
    assert first.event_id == second.event_id != third.event_id
    assert first.emitted_at.tzinfo is not None
    assert first.log is first.log
    first.log.log("hello")  # the bound logger forwards to the app logger


async def test_the_context_logger_defaults_the_context_to_the_event_class() -> None:
    from lauren.logging import InMemoryLogger, LogLevel, LogRecord

    logger = InMemoryLogger(level=LogLevel.DEBUG)

    @injectable()
    class Handlers:
        @on_event()
        async def chatty(self, event: UserRegistered, ctx: EventContext) -> None:
            for method in (ctx.log.log, ctx.log.info, ctx.log.debug, ctx.log.verbose, ctx.log.warn):
                method("hi")
            ctx.log.warning("hi")
            ctx.log.error("hi", context="Custom")
            ctx.log.log_record(LogRecord(level=LogLevel.INFO, message="raw", context="Raw"))

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule, logger=logger)
    logger.clear()

    await (await bus_of(app)).emit(UserRegistered())

    assert logger.contexts() == ["Event:UserRegistered"] * 6 + ["Custom", "Raw"]


async def test_wait_reports_a_cancelled_deferred_handler() -> None:
    @injectable()
    class Handlers:
        @on_event(mode="concurrent")
        async def gives_up(self, event: UserRegistered) -> None:
            raise asyncio.CancelledError

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)

    result = await (await bus_of(app)).emit(UserRegistered(), wait=True)

    assert [(f.handler, type(f.error)) for f in result.errors] == [
        ("Handlers.gives_up", asyncio.CancelledError)
    ]


async def test_handlers_reemitting_deeper_than_max_depth_raise_recursion_error() -> None:
    @injectable()
    class Echo:
        def __init__(self, events: EventBus) -> None:
            self.events = events

        @on_event()
        async def echo(self, event: Ping, ctx: EventContext) -> None:
            await self.events.emit(Ping(event.n + 1))

    @module(imports=[EventsModule.for_root(max_depth=2)], providers=[Echo])
    class AppModule:
        pass

    app = await started(AppModule)

    with pytest.raises(RecursionError, match="Ping -> Ping -> Ping -> Ping"):
        await (await bus_of(app)).emit(Ping())


async def test_sync_handlers_run_in_a_worker_thread() -> None:
    threads: list[int] = []

    @injectable()
    class Handlers:
        @on_event()
        def record(self, event: UserRegistered) -> None:
            threads.append(threading.get_ident())

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)

    await (await bus_of(app)).emit(UserRegistered())

    assert threads and threads[0] != threading.get_ident()


async def test_static_and_class_method_handlers_are_called() -> None:
    calls: list[Any] = []

    @injectable(scope=Scope.REQUEST)
    class Handlers:
        @staticmethod
        @on_event()
        async def static(event: UserRegistered) -> None:
            calls.append("static")

        @on_event()
        @classmethod
        async def klass(cls, event: UserRegistered) -> None:
            calls.append(cls)

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)

    await (await bus_of(app)).emit(UserRegistered())

    assert calls == ["static", Handlers]


async def test_transient_handler_providers_are_built_per_dispatch() -> None:
    instances: list[Any] = []

    @injectable(scope=Scope.TRANSIENT)
    class Handlers:
        @on_event()
        async def record(self, event: UserRegistered) -> None:
            instances.append(self)

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)

    await bus.emit(UserRegistered())
    await bus.emit(UserRegistered())

    assert len({id(i) for i in instances}) == 2


async def test_inherited_handlers_follow_the_method_rule() -> None:
    calls: list[str] = []

    class Base:
        @on_event()
        async def record(self, event: UserRegistered) -> None:
            calls.append(type(self).__name__)

    @injectable()
    class Inherits(Base):
        pass

    @injectable()
    class Overrides(Base):
        async def record(self, event: UserRegistered) -> None:  # undecorated: no longer a handler
            calls.append("override")

    @module(providers=[Inherits, Overrides])
    class AppModule:
        pass

    app = await started(AppModule)

    await (await bus_of(app)).emit(UserRegistered())

    assert calls == ["Inherits"]


async def test_emitting_a_class_instead_of_an_instance_is_rejected() -> None:
    @module()
    class AppModule:
        pass

    app = await started(AppModule)

    with pytest.raises(TypeError, match="UserRegistered"):
        await (await bus_of(app)).emit(UserRegistered)


async def test_an_event_without_handlers_is_a_no_op() -> None:
    @module()
    class AppModule:
        pass

    app = await started(AppModule)

    result = await (await bus_of(app)).emit(OrderPlaced())

    assert (result.handled, result.scheduled, result.errors) == (0, 0, ())


async def test_a_bare_bus_records_emissions_for_unit_tests() -> None:
    bus = EventBus()

    with bus.recording() as recording:
        result = await bus.emit(UserRegistered("u9"))

    assert result.handled == 0
    assert recording.of(UserRegistered) == [UserRegistered("u9")]
    assert "0 subscription(s)" in repr(bus)


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_a_narrower_annotation_fails_startup() -> None:
    @dataclass(frozen=True)
    class AdminRegistered(UserRegistered):
        pass

    @injectable()
    class Audit:
        @on_event(UserRegistered)
        async def record(self, event: AdminRegistered) -> None: ...

    @module(providers=[Audit])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["handler"] == "Audit.record"
    assert info.value.detail["reason"] == "event_type_mismatch"


def test_an_unresolvable_dependency_fails_startup() -> None:
    class Repository:
        pass

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered, repo: Repository) -> None: ...

    @module(providers=[Audit])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["param"] == "repo"
    assert info.value.detail["reason"] == "unresolvable_dependency"


def test_inference_without_an_annotation_fails_startup() -> None:
    @injectable()
    class Audit:
        @on_event()
        async def record(self, event) -> None: ...  # type: ignore[no-untyped-def]

    @module(providers=[Audit])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["reason"] == "missing_event_parameter"


def test_propagate_on_a_deferred_mode_fails_at_declaration() -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @injectable()
        class Mailer:
            @on_event(UserRegistered, mode="background", errors="propagate")
            async def send(self, event: UserRegistered) -> None: ...

    assert info.value.detail["mode"] == "background"


def test_a_handler_cannot_inject_the_request() -> None:
    from lauren import Request

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: UserRegistered, request: Request) -> None: ...

    @module(providers=[Audit])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["param"] == "request"


# ---------------------------------------------------------------------------
# Modules and isolation
# ---------------------------------------------------------------------------


async def test_handlers_in_unrelated_modules_receive_events() -> None:
    received: list[str] = []

    @injectable()
    class Mailer:
        def send(self, to: str) -> None:
            received.append(to)

    @injectable()
    class Notifications:
        @on_event()
        async def welcome(self, event: UserRegistered, mailer: Mailer) -> None:
            mailer.send(event.email)

    @module(providers=[Mailer, Notifications])
    class NotificationsModule:
        pass

    @injectable()
    class UserService:
        def __init__(self, events: EventBus) -> None:
            self.events = events

    @module(providers=[UserService], exports=[UserService])
    class UsersModule:
        pass

    @module(imports=[UsersModule, NotificationsModule])
    class AppModule:
        pass

    app = await started(AppModule)
    users = await app.container.resolve(UserService)

    await users.events.emit(UserRegistered(email="n@example.com"))

    assert received == ["n@example.com"]


def test_handler_dependencies_resolve_only_within_their_module() -> None:
    @injectable()
    class UserRepository:
        pass

    @module(providers=[UserRepository])  # not exported
    class UsersModule:
        pass

    @injectable()
    class Notifications:
        @on_event()
        async def welcome(self, event: UserRegistered, repo: UserRepository) -> None: ...

    @module(imports=[UsersModule], providers=[Notifications])
    class NotificationsModule:
        pass

    @module(imports=[NotificationsModule])
    class AppModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["param"] == "repo"


async def test_two_apps_have_independent_buses() -> None:
    calls: list[str] = []

    @injectable()
    class First:
        @on_event()
        async def handle(self, event: UserRegistered) -> None:
            calls.append("first")

    @injectable()
    class Second:
        @on_event()
        async def handle(self, event: UserRegistered) -> None:
            calls.append("second")

    @module(providers=[First])
    class FirstModule:
        pass

    @module(providers=[Second])
    class SecondModule:
        pass

    first = await started(FirstModule)
    second = await started(SecondModule)
    first_bus, second_bus = await bus_of(first), await bus_of(second)

    await first_bus.emit(UserRegistered())

    assert first_bus is not second_bus
    assert calls == ["first"]
    assert second_bus.handlers_for(UserRegistered) == ("Second.handle",)


async def test_an_application_binding_for_the_bus_wins() -> None:
    fake = EventBus()

    @module(providers=[use_value(provide=EventBus, value=fake)])
    class AppModule:
        pass

    app = await started(AppModule)

    assert await app.container.resolve(EventBus) is fake


def test_events_module_options_are_validated() -> None:
    with pytest.raises(DecoratorUsageError):
        EventsModule.for_root(max_depth=-1)
    with pytest.raises(DecoratorUsageError):
        EventsModule.for_root(drain_timeout=True)  # type: ignore[arg-type]


def test_conflicting_events_module_options_fail_startup() -> None:
    @module(imports=[EventsModule.for_root(max_depth=4)])
    class Feature:
        pass

    @module(imports=[Feature, EventsModule.for_root(max_depth=8)])
    class AppModule:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["reason"] == "conflicting_events_options"


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def test_startup_delivers_inline_events_to_built_singletons_and_queues_deferred_ones() -> None:
    log: list[str] = []

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: Ping) -> None:
            log.append("inline")

        @on_event(mode="concurrent")
        async def later(self, event: Ping) -> None:
            log.append("deferred")

    @injectable()
    class Seeder:
        def __init__(self, audit: Audit, events: EventBus) -> None:
            self.events = events

        @post_construct
        async def seed(self) -> None:
            result = await self.events.emit(Ping())
            log.append(f"emitted {result.handled}/{result.scheduled}")

    @module(providers=[Audit, Seeder])
    class AppModule:
        pass

    app = await started(AppModule)
    await (await bus_of(app)).drain()

    assert log == ["inline", "emitted 1/1", "deferred"]


async def test_startup_rejects_inline_delivery_to_an_unbuilt_singleton() -> None:
    @injectable()
    class Seeder:
        def __init__(self, events: EventBus) -> None:
            self.events = events

        @post_construct
        async def seed(self) -> None:
            await self.events.emit(Ping())

    @injectable()
    class Audit:
        def __init__(self, seeder: Seeder) -> None:  # built after Seeder
            self.seeder = seeder

        @on_event()
        async def record(self, event: Ping) -> None: ...

    @module(providers=[Seeder, Audit])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    with pytest.raises(LifecycleViolationError) as info:
        await app.startup()

    assert info.value.detail == {"event_type": "Ping", "handler": "Audit.record", "reason": "app_not_ready"}


async def test_waiting_during_startup_is_rejected() -> None:
    @injectable()
    class Audit:
        @on_event(mode="concurrent")
        async def later(self, event: Ping) -> None: ...

    @injectable()
    class Seeder:
        def __init__(self, events: EventBus) -> None:
            self.events = events

        @post_construct
        async def seed(self) -> None:
            await self.events.emit(Ping(), wait=True)

    @module(providers=[Audit, Seeder])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    with pytest.raises(LifecycleViolationError, match="wait=True"):
        await app.startup()


async def test_work_queued_by_a_failed_startup_is_reported_cancelled_at_shutdown() -> None:
    failures: list[EventHandlerFailed] = []
    signals = SignalBus()
    signals.on(EventHandlerFailed)(failures.append)

    @injectable()
    class Audit:
        @on_event(mode="concurrent")
        async def later(self, event: Ping) -> None: ...

    @injectable()
    class Seeder:
        def __init__(self, audit: Audit, events: EventBus) -> None:
            self.events = events

        @post_construct
        async def seed(self) -> None:
            await self.events.emit(Ping())
            raise RuntimeError("seed failed")

    @module(providers=[Audit, Seeder])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule, signals=signals)
    with pytest.raises(RuntimeError):
        await app.startup()

    await app.shutdown()

    assert [(f.handler, type(f.error)) for f in failures] == [("Audit.later", asyncio.CancelledError)]


async def test_shutdown_awaits_running_handlers_within_the_drain_timeout() -> None:
    gate = asyncio.Event()
    started_handler = asyncio.Event()
    log: list[str] = []

    @injectable()
    class Slow:
        @on_event(mode="concurrent")
        async def work(self, event: UserRegistered) -> None:
            started_handler.set()
            await gate.wait()
            log.append("finished")

    @module(providers=[Slow])
    class AppModule:
        pass

    app = await started(AppModule)
    await (await bus_of(app)).emit(UserRegistered())
    await started_handler.wait()

    shutdown = asyncio.create_task(app.shutdown(drain_timeout=60))
    await asyncio.sleep(0)
    gate.set()
    await shutdown

    assert log == ["finished"]


async def test_shutdown_cancels_handlers_past_the_drain_timeout() -> None:
    started_handler = asyncio.Event()
    failures: list[EventHandlerFailed] = []
    signals = SignalBus()
    signals.on(EventHandlerFailed)(failures.append)

    @injectable()
    class Stuck:
        @on_event(mode="concurrent")
        async def work(self, event: UserRegistered) -> None:
            started_handler.set()
            await asyncio.Event().wait()

    @module(providers=[Stuck])
    class AppModule:
        pass

    app = await started(AppModule, signals=signals)
    await (await bus_of(app)).emit(UserRegistered())
    await started_handler.wait()

    await app.shutdown(drain_timeout=0)

    assert [(f.handler, type(f.error)) for f in failures] == [("Stuck.work", asyncio.CancelledError)]


async def test_events_module_drain_timeout_overrides_the_shutdown_budget() -> None:
    started_handler = asyncio.Event()
    failures: list[EventHandlerFailed] = []
    signals = SignalBus()
    signals.on(EventHandlerFailed)(failures.append)

    @injectable()
    class Stuck:
        @on_event(mode="concurrent")
        async def work(self, event: UserRegistered) -> None:
            started_handler.set()
            await asyncio.Event().wait()

    @module(imports=[EventsModule.for_root(drain_timeout=0)], providers=[Stuck])
    class AppModule:
        pass

    app = await started(AppModule, signals=signals)
    await (await bus_of(app)).emit(UserRegistered())
    await started_handler.wait()

    await app.shutdown(drain_timeout=3600)

    assert len(failures) == 1


async def test_emitting_after_shutdown_raises() -> None:
    @module()
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)
    await app.shutdown()

    with pytest.raises(LifecycleViolationError) as info:
        await bus.emit(UserRegistered())

    assert info.value.detail["reason"] == "app_stopped"


async def test_handlers_spawned_after_the_drain_are_cancelled_when_shutdown_completes() -> None:
    failures: list[EventHandlerFailed] = []
    signals = SignalBus()
    signals.on(EventHandlerFailed)(failures.append)

    @injectable()
    class Late:
        @on_event(mode="concurrent")
        async def work(self, event: Ping) -> None:
            await asyncio.Event().wait()

    @module(providers=[Late])
    class AppModule:
        pass

    app = await started(AppModule, signals=signals)
    bus = await bus_of(app)

    @app.on_shutdown
    async def announce() -> None:
        await bus.emit(Ping())

    await app.shutdown(drain_timeout=60)

    assert [f.handler for f in failures] == ["Late.work"]


async def test_an_app_that_is_never_started_still_dispatches() -> None:
    calls: list[str] = []

    @injectable()
    class Audit:
        @on_event()
        async def record(self, event: Ping) -> None:
            calls.append("inline")

    @module(providers=[Audit])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)

    await (await bus_of(app)).emit(Ping())

    assert calls == ["inline"]


# ---------------------------------------------------------------------------
# Testing aids
# ---------------------------------------------------------------------------


async def test_recording_and_drain_capture_deferred_outcomes() -> None:
    @injectable()
    class Mailer:
        @on_event(mode="concurrent")
        async def send(self, event: UserRegistered) -> None:
            await asyncio.sleep(0)

        @on_event(mode="concurrent")
        async def broken(self, event: UserRegistered) -> None:
            raise RuntimeError("smtp")

    @module(providers=[Mailer])
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)

    with bus.recording() as recording:
        await bus.emit(UserRegistered(email="r@example.com"))
        await bus.drain()

    assert recording.of(UserRegistered)[0].email == "r@example.com"
    assert recording.handler_calls(Mailer.send) == 1
    assert recording.handler_calls("Mailer.broken") == 1
    assert [f.handler for f in recording.failures] == ["Mailer.broken"]


async def test_drain_raises_when_tasks_outlive_the_timeout() -> None:
    gate = asyncio.Event()

    @injectable()
    class Slow:
        @on_event(mode="concurrent")
        async def work(self, event: UserRegistered) -> None:
            await gate.wait()

    @module(providers=[Slow])
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)
    await bus.emit(UserRegistered())

    with pytest.raises(TimeoutError):
        await bus.drain(timeout=0)
    gate.set()
    await bus.drain()


async def test_disabled_handlers_are_skipped_inside_the_block() -> None:
    calls: list[str] = []

    @injectable()
    class Handlers:
        @on_event()
        async def a(self, event: UserRegistered) -> None:
            calls.append("a")

        @on_event()
        async def b(self, event: UserRegistered) -> None:
            calls.append("b")

    @module(providers=[Handlers])
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)

    with bus.disabled(Handlers.a):
        with bus.disabled("Handlers.a", "Handlers.b"):
            await bus.emit(UserRegistered())
        await bus.emit(UserRegistered())
    await bus.emit(UserRegistered())

    assert calls == ["b", "a", "b"]


async def test_disabling_an_unknown_handler_is_rejected() -> None:
    @module()
    class AppModule:
        pass

    app = await started(AppModule)
    bus = await bus_of(app)

    with pytest.raises(ValueError):
        with bus.disabled("Nope.handle"):
            pass
    with pytest.raises(ValueError):
        with bus.disabled():
            pass


def test_handlers_for_needs_a_class() -> None:
    with pytest.raises(TypeError):
        EventBus().handlers_for(UserRegistered())  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# After-commit delivery (UnitOfWork contract)
# ---------------------------------------------------------------------------


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commit_callbacks: list[Any] = []
        self.rollback_callbacks: list[Any] = []

    def on_commit(self, callback: Any) -> None:
        self.commit_callbacks.append(callback)

    def on_rollback(self, callback: Any) -> None:
        self.rollback_callbacks.append(callback)

    async def commit(self) -> None:
        for callback in self.commit_callbacks:
            await callback()

    async def rollback(self) -> None:
        for callback in self.rollback_callbacks:
            await callback()


def _after_commit_app(unit: FakeUnitOfWork, delivered: list[Any]) -> type:
    @injectable()
    class Ledger:
        @on_event()
        async def book(self, event: OrderPlaced) -> None:
            delivered.append(event)

    @controller("/orders")
    class Orders:
        @post("/{outcome}")
        async def place(self, outcome: str, events: EventPublisher) -> dict[str, int]:
            result = await events.emit(OrderPlaced("o7"), after=TransactionPhase.COMMIT)
            assert result.handled == 0 and delivered == []
            await (unit.commit() if outcome == "commit" else unit.rollback())
            return {"delivered": len(delivered)}

    @module(controllers=[Orders], providers=[Ledger, use_value(provide=UnitOfWork, value=unit)])
    class AppModule:
        pass

    return AppModule


@pytest.mark.parametrize(("outcome", "expected"), [("commit", 1), ("rollback", 0)])
async def test_after_commit_emissions_follow_the_unit_of_work(outcome: str, expected: int) -> None:
    delivered: list[Any] = []
    app = await started(_after_commit_app(FakeUnitOfWork(), delivered))

    response = await TestClient(app).arequest("POST", f"/orders/{outcome}")

    assert response.json() == {"delivered": expected}


async def test_after_commit_without_a_unit_of_work_is_rejected() -> None:
    errors: list[LifecycleViolationError] = []

    @controller("/orders")
    class Orders:
        @post("/")
        async def place(self, events: EventPublisher) -> dict[str, bool]:
            try:
                await events.emit(OrderPlaced(), after=TransactionPhase.COMMIT)
            except LifecycleViolationError as exc:
                errors.append(exc)
            return {"ok": True}

    @module(controllers=[Orders])
    class AppModule:
        pass

    app = await started(AppModule)

    await TestClient(app).arequest("POST", "/orders/")

    assert errors[0].detail["reason"] == "no_unit_of_work"


async def test_after_requires_a_transaction_phase() -> None:
    errors: list[BaseException] = []

    @controller("/orders")
    class Orders:
        @get("/")
        async def place(self, events: EventPublisher) -> dict[str, bool]:
            for bad in ("commit", OrderPlaced):
                try:
                    if bad == "commit":
                        await events.emit(OrderPlaced(), after=bad)  # type: ignore[arg-type]
                    else:
                        await events.emit(bad, after=TransactionPhase.COMMIT)
                except (TypeError, ValueError) as exc:
                    errors.append(exc)
            return {"in_request": events.in_request}

    @module(controllers=[Orders])
    class AppModule:
        pass

    app = await started(AppModule)

    response = await TestClient(app).arequest("GET", "/orders/")

    assert response.json() == {"in_request": True}
    assert [type(e) for e in errors] == [ValueError, TypeError]
