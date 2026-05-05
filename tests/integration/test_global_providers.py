"""Integration tests for global_providers on LaurenFactory.create and Lauren."""

from __future__ import annotations

import asyncio
import io as _io
import json as _json

import pytest

from lauren import Lauren, controller, get, injectable, module
from lauren._asgi import LaurenFactory
from lauren._di.custom import use_value
from lauren.logging import (
    ConsoleLogger,
    InMemoryLogger,
    JsonLogger,
    Logger,
    NullLogger,
)
from lauren.testing import TestClient
from lauren.types import Scope


# ---------------------------------------------------------------------------
# Shared fixtures — simple single-service app used by the basic tests
# ---------------------------------------------------------------------------


@injectable()
class LoggingService:
    log: Logger

    def do_work(self) -> str:
        self.log.info("work done", context="LoggingService")
        return "ok"


@controller("/svc")
class SvcController:
    svc: LoggingService

    @get("/")
    async def index(self) -> dict:
        return {"result": self.svc.do_work()}


@module(controllers=[SvcController], providers=[LoggingService])
class AppModule:
    pass


# ---------------------------------------------------------------------------
# Basic injection — field annotation style
# ---------------------------------------------------------------------------


def test_field_injection_receives_console_logger() -> None:
    mem = InMemoryLogger()
    app = LaurenFactory.create(
        AppModule,
        global_providers=[use_value(provide=Logger, value=mem)],
    )
    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert r.json() == {"result": "ok"}
    assert any("work done" in m for m in mem.messages())


def test_console_logger_class_in_global_providers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """ConsoleLogger registered by class writes the log message to stdout."""
    app = LaurenFactory.create(AppModule, global_providers=[ConsoleLogger])
    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert r.json() == {"result": "ok"}
    captured = capsys.readouterr()
    assert "work done" in captured.out
    assert "LoggingService" in captured.out


def test_json_logger_class_in_global_providers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """JsonLogger registered by class emits a valid JSON line to stdout."""
    app = LaurenFactory.create(AppModule, global_providers=[JsonLogger])
    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert r.json() == {"result": "ok"}
    captured = capsys.readouterr()
    lines = [ln for ln in captured.out.splitlines() if ln.strip()]
    assert lines, "JsonLogger should have written at least one JSON line"
    last = _json.loads(lines[-1])
    assert last["message"] == "work done"
    assert last["level"] == "info"
    assert last["context"] == "LoggingService"


def test_null_logger_class_in_global_providers(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """NullLogger runs at SILENT level — no output reaches stdout."""
    app = LaurenFactory.create(AppModule, global_providers=[NullLogger])
    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert r.json() == {"result": "ok"}
    captured = capsys.readouterr()
    assert captured.out == "", "NullLogger must not write anything to stdout"


def test_in_memory_logger_class_in_global_providers() -> None:
    """InMemoryLogger registered by class accumulates the expected record."""
    app = LaurenFactory.create(AppModule, global_providers=[InMemoryLogger])
    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert r.json() == {"result": "ok"}
    mem = asyncio.run(app.container.resolve(Logger))
    assert isinstance(mem, InMemoryLogger)
    messages = mem.messages()
    assert "work done" in messages, f"expected 'work done' in {messages}"


# ---------------------------------------------------------------------------
# Constructor injection style
# ---------------------------------------------------------------------------


@injectable()
class CtorService:
    def __init__(self, log: Logger) -> None:
        self.log = log

    def greet(self) -> str:
        self.log.info("hello", context="CtorService")
        return "hello"


@controller("/ctor")
class CtorController:
    svc: CtorService

    @get("/")
    async def index(self) -> dict:
        return {"result": self.svc.greet()}


@module(controllers=[CtorController], providers=[CtorService])
class CtorModule:
    pass


def test_constructor_injection_of_logger() -> None:
    mem = InMemoryLogger()
    app = LaurenFactory.create(
        CtorModule,
        global_providers=[use_value(provide=Logger, value=mem)],
    )
    r = TestClient(app).get("/ctor/")
    assert r.status_code == 200
    assert any("hello" in m for m in mem.messages())


# ---------------------------------------------------------------------------
# Identity — injected instance IS the registered value
# ---------------------------------------------------------------------------


def test_injected_logger_identity_with_use_value() -> None:
    """use_value → the exact registered object is injected and receives calls."""
    mem = InMemoryLogger()
    app = LaurenFactory.create(
        AppModule,
        global_providers=[use_value(provide=Logger, value=mem)],
    )
    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert r.json() == {"result": "ok"}
    assert mem.records, "InMemoryLogger should have received log calls"
    assert any("work done" in rec.message for rec in mem.records)


# ---------------------------------------------------------------------------
# Multi-module visibility — nested module sees global providers
# ---------------------------------------------------------------------------


@injectable()
class DeepService:
    log: Logger

    def ping(self) -> str:
        self.log.info("ping", context="DeepService")
        return "pong"


@controller("/deep")
class DeepController:
    svc: DeepService

    @get("/")
    async def index(self) -> dict:
        return {"result": self.svc.ping()}


@module(controllers=[DeepController], providers=[DeepService])
class DeepModule:
    pass


@module(imports=[DeepModule])
class RootModule:
    pass


def test_global_provider_visible_in_nested_module() -> None:
    mem = InMemoryLogger()
    app = LaurenFactory.create(
        RootModule,
        global_providers=[use_value(provide=Logger, value=mem)],
    )
    r = TestClient(app).get("/deep/")
    assert r.status_code == 200
    assert any("ping" in m for m in mem.messages())


# ---------------------------------------------------------------------------
# REQUEST-scoped service can depend on SINGLETON logger
# ---------------------------------------------------------------------------


@injectable(scope=Scope.REQUEST)
class RequestScopedService:
    log: Logger

    def action(self) -> str:
        self.log.info("request action", context="RequestScopedService")
        return "done"


@controller("/req")
class ReqController:
    @get("/")
    async def index(self, svc: RequestScopedService) -> dict:
        return {"result": svc.action()}


@module(controllers=[ReqController], providers=[RequestScopedService])
class ReqModule:
    pass


def test_request_scoped_service_injects_singleton_logger() -> None:
    mem = InMemoryLogger()
    app = LaurenFactory.create(
        ReqModule,
        global_providers=[use_value(provide=Logger, value=mem)],
    )
    r = TestClient(app).get("/req/")
    assert r.status_code == 200
    assert any("request action" in m for m in mem.messages())


# ---------------------------------------------------------------------------
# Lauren-style app — global_providers= constructor arg
# ---------------------------------------------------------------------------


def test_lauren_style_global_providers_constructor() -> None:
    mem = InMemoryLogger()
    app = Lauren(global_providers=[use_value(provide=Logger, value=mem)])
    app.include_module(AppModule)

    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert any("work done" in m for m in mem.messages())


# ---------------------------------------------------------------------------
# Lauren-style app — add_provider() imperative method
# ---------------------------------------------------------------------------


def test_lauren_add_provider_method() -> None:
    mem = InMemoryLogger()
    app = Lauren()
    app.add_provider(use_value(provide=Logger, value=mem))
    app.include_module(AppModule)

    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert any("work done" in m for m in mem.messages())


def test_lauren_add_provider_rejected_after_compile() -> None:
    from lauren.exceptions import LifecycleViolationError

    mem = InMemoryLogger()
    app = Lauren(global_providers=[use_value(provide=Logger, value=mem)])
    app.include_module(AppModule)

    r = TestClient(app).get("/svc/")
    assert r.status_code == 200
    assert any("work done" in m for m in mem.messages())

    with pytest.raises(LifecycleViolationError):
        app.add_provider(NullLogger)


# ---------------------------------------------------------------------------
# No global_providers → MissingProviderError at compile if Logger is needed
# ---------------------------------------------------------------------------


def test_missing_logger_provider_raises_at_startup() -> None:
    from lauren.exceptions import MissingProviderError

    with pytest.raises(MissingProviderError):
        LaurenFactory.create(AppModule)  # no global_providers — Logger unregistered


# ---------------------------------------------------------------------------
# global_providers= [] (empty) — no change to existing behaviour
# ---------------------------------------------------------------------------


@module()
class EmptyModule:
    pass


def test_empty_global_providers_list_is_noop() -> None:
    app = LaurenFactory.create(EmptyModule, global_providers=[])
    r = TestClient(app).get("/nonexistent")
    assert r.status_code == 404


# ===========================================================================
# Comprehensive: multiple modules × multiple services × mixed logger types
#
# Services are free to inject the Logger protocol OR a concrete type
# (ConsoleLogger / JsonLogger) directly. When all three tokens are
# registered in global_providers, each service resolves its preferred one
# with no ambiguity — concrete-type tokens (ConsoleLogger, JsonLogger) are
# distinct from the Logger protocol token.
# ===========================================================================


# ── Fulfillment domain ──────────────────────────────────────────────────────


@injectable()
class ShippingService:
    """Injects ConsoleLogger directly — one human-readable line per shipment."""

    log: ConsoleLogger

    def ship(self, order_id: str) -> str:
        self.log.info(f"shipped {order_id}", context="ShippingService")
        return order_id


@injectable()
class BillingService:
    """Injects JsonLogger directly — every charge produces a structured record."""

    log: JsonLogger

    def charge(self, amount: float) -> str:
        self.log.info(f"charged {amount:.2f}", context="BillingService")
        return f"billed-{amount:.2f}"


@injectable()
class WarehouseService:
    """Injects the Logger protocol — works with any registered implementation."""

    log: Logger

    def pack(self, item: str) -> str:
        self.log.info(f"packed {item}", context="WarehouseService")
        return item


@controller("/fulfill")
class FulfillmentController:
    shipping: ShippingService
    billing: BillingService
    warehouse: WarehouseService

    @get("/order")
    async def fulfill_order(self) -> dict:
        self.warehouse.pack("widget-A")
        self.billing.charge(49.99)
        self.shipping.ship("ORD-001")
        return {"status": "fulfilled", "order": "ORD-001"}


@module(
    controllers=[FulfillmentController],
    providers=[ShippingService, BillingService, WarehouseService],
)
class FulfillmentModule:
    pass


# ── Users domain ─────────────────────────────────────────────────────────────


@injectable()
class ProfileService:
    """Injects ConsoleLogger — human-readable profile-change events."""

    log: ConsoleLogger

    def update(self, user_id: str) -> str:
        self.log.info(f"profile updated for {user_id}", context="ProfileService")
        return user_id


@injectable()
class AuditService:
    """Injects JsonLogger — machine-parseable audit trail."""

    log: JsonLogger

    def record(self, action: str) -> None:
        self.log.info(action, context="AuditService")


@injectable()
class SessionService:
    """Injects Logger protocol and orchestrates ProfileService + AuditService."""

    log: Logger
    profile: ProfileService
    audit: AuditService

    def login(self, user_id: str) -> str:
        self.profile.update(user_id)
        self.audit.record(f"login:{user_id}")
        self.log.info(f"session started for {user_id}", context="SessionService")
        return user_id


@controller("/users")
class UsersController:
    session: SessionService

    @get("/login")
    async def login(self) -> dict:
        uid = self.session.login("usr-42")
        return {"user": uid}


@module(
    controllers=[UsersController],
    providers=[ProfileService, AuditService, SessionService],
)
class UsersModule:
    pass


# ── Composite root that composes both domains ────────────────────────────────


@module(imports=[FulfillmentModule, UsersModule])
class CompositeRootModule:
    pass


# ── Test helpers ─────────────────────────────────────────────────────────────


def _json_lines(stream: _io.StringIO) -> list[dict]:
    """Parse every non-empty line in a StringIO as a standalone JSON object."""
    return [_json.loads(ln) for ln in stream.getvalue().splitlines() if ln.strip()]


def _make_loggers() -> tuple[
    ConsoleLogger, JsonLogger, InMemoryLogger, _io.StringIO, _io.StringIO
]:
    """Return fresh logger instances with capturable streams to avoid test bleed."""
    cs = _io.StringIO()
    js = _io.StringIO()
    return (
        ConsoleLogger(stream=cs, use_colour=False),
        JsonLogger(stream=js),
        InMemoryLogger(),
        cs,
        js,
    )


def _build_app(
    root_module: type, console: ConsoleLogger, json_log: JsonLogger, mem: InMemoryLogger
):
    """Wire all three logger types as global providers."""
    return LaurenFactory.create(
        root_module,
        global_providers=[
            use_value(provide=ConsoleLogger, value=console),
            use_value(provide=JsonLogger, value=json_log),
            use_value(provide=Logger, value=mem),
        ],
    )


# ── Comprehensive tests ───────────────────────────────────────────────────────


def test_fulfillment_module_all_three_logger_types() -> None:
    """One fulfillment request exercises ConsoleLogger, JsonLogger, and Logger."""
    console, json_log, mem, cs, js = _make_loggers()
    app = _build_app(FulfillmentModule, console, json_log, mem)

    r = TestClient(app).get("/fulfill/order")
    assert r.status_code == 200
    assert r.json() == {"status": "fulfilled", "order": "ORD-001"}

    # ShippingService → ConsoleLogger
    console_out = cs.getvalue()
    assert "shipped ORD-001" in console_out, (
        "ShippingService must write to ConsoleLogger"
    )
    assert "ShippingService" in console_out

    # BillingService → JsonLogger
    billing = [l for l in _json_lines(js) if l.get("context") == "BillingService"]
    assert billing, "BillingService must write at least one JSON record"
    assert any("charged 49.99" in l["message"] for l in billing)
    assert all(l["level"] == "info" for l in billing)

    # WarehouseService → Logger (InMemoryLogger)
    assert any("packed widget-A" in m for m in mem.messages())
    assert "WarehouseService" in mem.contexts()


def test_users_module_all_three_logger_types() -> None:
    """A login request exercises all three loggers via the SessionService chain."""
    console, json_log, mem, cs, js = _make_loggers()
    app = _build_app(UsersModule, console, json_log, mem)

    r = TestClient(app).get("/users/login")
    assert r.status_code == 200
    assert r.json() == {"user": "usr-42"}

    # ProfileService → ConsoleLogger
    console_out = cs.getvalue()
    assert "profile updated for usr-42" in console_out
    assert "ProfileService" in console_out

    # AuditService → JsonLogger
    audit = [l for l in _json_lines(js) if l.get("context") == "AuditService"]
    assert audit, "AuditService must write at least one JSON record"
    assert any("login:usr-42" in l["message"] for l in audit)
    assert all(l["level"] == "info" for l in audit)

    # SessionService → Logger (InMemoryLogger)
    assert any("session started for usr-42" in m for m in mem.messages())
    assert "SessionService" in mem.contexts()


def test_composite_app_both_modules_all_six_services_log_correctly() -> None:
    """Both domains run in one app; each of the six services logs to the right logger."""
    console, json_log, mem, cs, js = _make_loggers()
    app = _build_app(CompositeRootModule, console, json_log, mem)
    client = TestClient(app)

    r1 = client.get("/fulfill/order")
    r2 = client.get("/users/login")
    assert r1.status_code == r2.status_code == 200

    console_out = cs.getvalue()
    all_json = _json_lines(js)
    all_mem_msgs = mem.messages()

    # ConsoleLogger: both ShippingService (fulfillment) and ProfileService (users)
    assert "shipped ORD-001" in console_out, "ShippingService → ConsoleLogger"
    assert "ShippingService" in console_out
    assert "profile updated for usr-42" in console_out, "ProfileService → ConsoleLogger"
    assert "ProfileService" in console_out

    # JsonLogger: both BillingService (fulfillment) and AuditService (users)
    billing = [l for l in all_json if l.get("context") == "BillingService"]
    audit = [l for l in all_json if l.get("context") == "AuditService"]
    assert billing and any("charged 49.99" in l["message"] for l in billing)
    assert audit and any("login:usr-42" in l["message"] for l in audit)

    # Logger/InMemoryLogger: both WarehouseService (fulfillment) and SessionService (users)
    assert any("packed widget-A" in m for m in all_mem_msgs), (
        "WarehouseService → Logger"
    )
    assert any("session started for usr-42" in m for m in all_mem_msgs), (
        "SessionService → Logger"
    )


def test_multiple_requests_accumulate_records_in_all_loggers() -> None:
    """Three requests produce exactly three records per service in each logger."""
    console, json_log, mem, cs, js = _make_loggers()
    app = _build_app(FulfillmentModule, console, json_log, mem)
    client = TestClient(app)

    for _ in range(3):
        r = client.get("/fulfill/order")
        assert r.status_code == 200

    # InMemoryLogger: three WarehouseService records
    warehouse_msgs = [m for m in mem.messages() if "packed widget-A" in m]
    assert len(warehouse_msgs) == 3, (
        f"expected 3 warehouse records, got {warehouse_msgs}"
    )

    # JsonLogger: three BillingService JSON records
    billing_lines = [l for l in _json_lines(js) if l.get("context") == "BillingService"]
    assert len(billing_lines) == 3, f"expected 3 billing records, got {billing_lines}"

    # ConsoleLogger: three ShippingService lines
    shipping_lines = [
        ln for ln in cs.getvalue().splitlines() if "ShippingService" in ln
    ]
    assert len(shipping_lines) == 3, f"expected 3 shipping lines, got {shipping_lines}"


def test_service_dependency_chain_all_loggers_fire_on_single_request() -> None:
    """SessionService → ProfileService + AuditService: all three loggers fire once."""
    console, json_log, mem, cs, js = _make_loggers()
    app = _build_app(UsersModule, console, json_log, mem)

    r = TestClient(app).get("/users/login")
    assert r.status_code == 200

    # Each logger received exactly one record from its service
    profile_lines = [ln for ln in cs.getvalue().splitlines() if "ProfileService" in ln]
    assert len(profile_lines) == 1, "ProfileService should log exactly once per request"

    audit_records = [l for l in _json_lines(js) if l.get("context") == "AuditService"]
    assert len(audit_records) == 1, "AuditService should log exactly once per request"

    session_msgs = [m for m in mem.messages() if "session started" in m]
    assert len(session_msgs) == 1, "SessionService should log exactly once per request"

    # Ordering: profile → audit → session (matches call order in SessionService.login)
    assert "login:usr-42" in audit_records[0]["message"]
    assert "session started for usr-42" in session_msgs[0]


def test_concrete_and_protocol_tokens_resolve_independently() -> None:
    """ConsoleLogger, JsonLogger, and Logger each resolve to their own instance."""
    console, json_log, mem, _, _ = _make_loggers()
    app = _build_app(FulfillmentModule, console, json_log, mem)

    resolved_console = asyncio.run(app.container.resolve(ConsoleLogger))
    resolved_json = asyncio.run(app.container.resolve(JsonLogger))
    resolved_logger = asyncio.run(app.container.resolve(Logger))

    assert resolved_console is console, (
        "ConsoleLogger token → pre-built console instance"
    )
    assert resolved_json is json_log, "JsonLogger token → pre-built json_log instance"
    assert resolved_logger is mem, "Logger protocol token → InMemoryLogger instance"
    # All three are distinct objects
    assert resolved_console is not resolved_json
    assert resolved_console is not resolved_logger
    assert resolved_json is not resolved_logger


def test_json_logger_records_have_correct_schema() -> None:
    """Every record emitted by JsonLogger contains the required fields."""
    _, json_log, mem, _, js = _make_loggers()
    console = ConsoleLogger(stream=_io.StringIO(), use_colour=False)
    app = _build_app(CompositeRootModule, console, json_log, mem)
    client = TestClient(app)

    client.get("/fulfill/order")
    client.get("/users/login")

    for record in _json_lines(js):
        assert "ts" in record, f"missing 'ts' in {record}"
        assert "level" in record, f"missing 'level' in {record}"
        assert "message" in record, f"missing 'message' in {record}"
        assert "context" in record, f"missing 'context' in {record}"
        assert record["level"] == "info"

    contexts = {r["context"] for r in _json_lines(js)}
    assert "BillingService" in contexts
    assert "AuditService" in contexts


def test_console_logger_captures_all_services_using_it() -> None:
    """Both ConsoleLogger services across both modules write to the same stream."""
    console, json_log, mem, cs, _ = _make_loggers()
    app = _build_app(CompositeRootModule, console, json_log, mem)
    client = TestClient(app)

    client.get("/fulfill/order")  # ShippingService
    client.get("/users/login")  # ProfileService

    output = cs.getvalue()
    # Both services that chose ConsoleLogger appear in the same stream
    assert "ShippingService" in output
    assert "shipped ORD-001" in output
    assert "ProfileService" in output
    assert "profile updated for usr-42" in output
    # Services that chose other loggers do NOT appear here
    assert "BillingService" not in output
    assert "AuditService" not in output
    assert "WarehouseService" not in output
    assert "SessionService" not in output


def test_in_memory_logger_captures_only_protocol_injected_services() -> None:
    """Logger-protocol services accumulate in InMemoryLogger; concrete ones do not."""
    console, json_log, mem, _, _ = _make_loggers()
    app = _build_app(CompositeRootModule, console, json_log, mem)
    client = TestClient(app)

    client.get("/fulfill/order")  # WarehouseService
    client.get("/users/login")  # SessionService

    contexts = set(mem.contexts())
    # Only services that injected Logger protocol end up here
    assert "WarehouseService" in contexts
    assert "SessionService" in contexts
    # Services using concrete types do not log through InMemoryLogger
    assert "ShippingService" not in contexts
    assert "BillingService" not in contexts
    assert "ProfileService" not in contexts
    assert "AuditService" not in contexts
