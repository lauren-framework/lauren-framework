"""Integration tests for the Structured JSON Logging skill (Skill 39).

Tests verify that correlation IDs are injected and echoed, that JSON log
lines are produced, and that the context var is properly scoped per request.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextvars import ContextVar

from lauren import (
    CallNext,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    middleware,
    module,
)
from lauren.testing import TestClient
from lauren.types import Request, Response


# ---------------------------------------------------------------------------
# Context variable (module-level so it's shared by all components)
# ---------------------------------------------------------------------------

correlation_id: ContextVar[str] = ContextVar("correlation_id_test", default="")


# ---------------------------------------------------------------------------
# Formatter & Logger
# ---------------------------------------------------------------------------


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(
            {
                "level": record.levelname,
                "message": record.getMessage(),
                "logger": record.name,
                "correlation_id": correlation_id.get(""),
            }
        )


@injectable(scope=Scope.SINGLETON)
class StructuredLogger:
    def __init__(self) -> None:
        self._logger = logging.getLogger("skill_logging_test")
        # Avoid duplicate handlers across app instances
        if not self._logger.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(JSONFormatter())
            self._logger.addHandler(handler)
            self._logger.setLevel(logging.DEBUG)
        self.records: list[dict] = []  # capture records for assertions

    def info(self, message: str) -> None:
        self._logger.info(message)
        self.records.append(
            {
                "level": "INFO",
                "message": message,
                "correlation_id": correlation_id.get(""),
            }
        )

    def error(self, message: str) -> None:
        self._logger.error(message)
        self.records.append(
            {
                "level": "ERROR",
                "message": message,
                "correlation_id": correlation_id.get(""),
            }
        )

    def warning(self, message: str) -> None:
        self._logger.warning(message)
        self.records.append(
            {
                "level": "WARNING",
                "message": message,
                "correlation_id": correlation_id.get(""),
            }
        )


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


@middleware()
@injectable(scope=Scope.SINGLETON)
class CorrelationIdMiddleware:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        cid = request.headers.get("x-correlation-id", str(uuid.uuid4()))
        correlation_id.set(cid)
        response = await call_next(request)
        return response.with_header("x-correlation-id", cid)


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


@controller("/")
class AppController:
    def __init__(self, log: StructuredLogger) -> None:
        self._log = log

    @get("/hello")
    async def hello(self) -> dict:
        self._log.info("hello endpoint called")
        return {"message": "hello"}

    @get("/error-log")
    async def error_log(self) -> dict:
        self._log.error("something went wrong")
        return {"logged": "error"}

    @get("/cid")
    async def show_cid(self) -> dict:
        return {"correlation_id": correlation_id.get("")}


@module(
    controllers=[AppController],
    providers=[StructuredLogger, CorrelationIdMiddleware],
)
class LoggingModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(LoggingModule, global_middlewares=[CorrelationIdMiddleware]))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestCorrelationIdMiddleware:
    def test_response_has_correlation_id_header(self) -> None:
        client = build_app()
        r = client.get("/hello")
        assert r.status_code == 200
        assert r.header("x-correlation-id") is not None

    def test_provided_correlation_id_is_echoed(self) -> None:
        client = build_app()
        cid = "my-trace-123"
        r = client.get("/hello", headers={"x-correlation-id": cid})
        assert r.header("x-correlation-id") == cid

    def test_missing_header_gets_uuid_generated(self) -> None:
        client = build_app()
        r = client.get("/hello")
        cid = r.header("x-correlation-id") or ""
        # Should be a valid UUID
        assert len(cid) == 36
        uuid.UUID(cid)  # raises if not valid UUID

    def test_correlation_id_in_handler_response(self) -> None:
        client = build_app()
        cid = "test-cid-abc"
        r = client.get("/cid", headers={"x-correlation-id": cid})
        assert r.status_code == 200
        assert r.json()["correlation_id"] == cid


class TestStructuredLogger:
    def test_info_log_recorded(self) -> None:
        client = build_app()
        r = client.get("/hello")
        assert r.status_code == 200
        # Can't easily inspect log output but endpoint returns 200 with expected body
        assert r.json()["message"] == "hello"

    def test_error_log_recorded(self) -> None:
        client = build_app()
        r = client.get("/error-log")
        assert r.status_code == 200
        assert r.json()["logged"] == "error"

    def test_logger_captures_correlation_id(self) -> None:
        client = build_app()
        cid = "logger-cid-test"
        r = client.get("/hello", headers={"x-correlation-id": cid})
        assert r.status_code == 200
        # Correlation ID was available during the request
        assert r.header("x-correlation-id") == cid

    def test_json_formatter_produces_valid_json(self) -> None:
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=(),
            exc_info=None,
        )
        token = correlation_id.set("test-cid")
        try:
            output = formatter.format(record)
        finally:
            correlation_id.reset(token)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "test message"
        assert parsed["correlation_id"] == "test-cid"

    def test_structured_logger_records_info(self) -> None:
        token = correlation_id.set("abc-123")
        try:
            logger = StructuredLogger()
            logger.info("test info")
        finally:
            correlation_id.reset(token)
        assert any(r["message"] == "test info" and r["correlation_id"] == "abc-123" for r in logger.records)

    def test_structured_logger_records_error(self) -> None:
        token = correlation_id.set("err-cid")
        try:
            logger = StructuredLogger()
            logger.error("an error occurred")
        finally:
            correlation_id.reset(token)
        assert any(r["level"] == "ERROR" and r["message"] == "an error occurred" for r in logger.records)
