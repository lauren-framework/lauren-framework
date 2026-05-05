"""Integration tests for global_providers on LaurenFactory.create and Lauren."""

from __future__ import annotations

import asyncio
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
# Shared fixtures
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
    # Every line emitted by JsonLogger is a standalone JSON object.
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
    # Retrieve the DI-managed singleton and inspect its accumulated records.
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
    # Records are populated only when the SAME instance was injected.
    assert mem.records, "InMemoryLogger should have received log calls"
    assert any("work done" in r.message for r in mem.records)


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

    # Trigger compilation by making a request; verify the logger received it.
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
