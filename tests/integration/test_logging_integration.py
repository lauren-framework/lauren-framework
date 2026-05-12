"""Integration tests for lauren's built-in logging pipeline.

Verifies that the framework emits the expected events through the logger
end users install via ``LaurenFactory.create(..., logger=...)``.
"""

from __future__ import annotations

import asyncio
import io
import json


from lauren import (
    LaurenFactory,
    controller,
    get,
    module,
    post,
)
from lauren.logging import (
    ConsoleLogger,
    InMemoryLogger,
    JsonLogger,
    LogLevel,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# A small app reused across tests
# ---------------------------------------------------------------------------


@controller("/api", tags=["api"])
class ApiController:
    @get("/ping")
    async def ping(self) -> dict:
        return {"pong": True}

    @get("/users/{id}")
    async def user(self, id: int) -> dict:
        return {"id": id}

    @post("/echo")
    async def echo(self) -> dict:
        return {"echoed": True}

    @get("/boom")
    async def boom(self) -> dict:
        raise RuntimeError("intentional failure")


@module(controllers=[ApiController])
class AppModule:
    pass


def _build(logger):
    return LaurenFactory.create(AppModule, logger=logger)


# ---------------------------------------------------------------------------
# Startup events
# ---------------------------------------------------------------------------


class TestStartupEvents:
    def test_all_phases_logged(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        _build(logger)
        # The seven factory phases should each log an event.
        contexts = [r.context for r in logger.records]
        for expected in (
            "LaurenFactory",
            "ModuleGraph",
            "DIContainer",
            "RouterExplorer",
            "Lifecycle",
            "LaurenApp",
        ):
            assert expected in contexts, f"missing event context={expected}"

    def test_each_route_logged(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        _build(logger)
        router_logs = [r for r in logger.records if r.context == "RouterExplorer"]
        route_lines = [r for r in router_logs if r.message.startswith("Mapped")]
        assert len(route_lines) == 4  # ping, user, echo, boom
        paths = {r.extra["path"] for r in route_lines}
        assert paths == {
            "/api/ping",
            "/api/users/{id}",
            "/api/echo",
            "/api/boom",
        }

    def test_startup_includes_route_count(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        _build(logger)
        completion = next(
            r for r in logger.records if r.context == "LaurenFactory" and "completed" in r.message.lower()
        )
        assert completion.extra["routes"] == 4
        assert completion.extra["providers"] >= 1

    def test_silent_when_no_logger_passed(self):
        # Default logger is NullLogger; no exception and no records accessible.
        app = LaurenFactory.create(AppModule)
        # No assertion on "records" — just verify the app booted.
        assert len(app.routes()) == 4


# ---------------------------------------------------------------------------
# Per-request logging
# ---------------------------------------------------------------------------


class TestRequestLogging:
    def test_request_logged_at_debug(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)
        client = TestClient(app)
        logger.clear()
        r = client.get("/api/ping")
        assert r.status_code == 200
        reqs = [x for x in logger.records if x.context == "Request"]
        assert len(reqs) == 1
        rec = reqs[0]
        assert rec.extra["method"] == "GET"
        assert rec.extra["path"] == "/api/ping"
        assert rec.extra["status"] == 200
        assert rec.extra["duration_ms"] >= 0
        assert "handler" in rec.extra

    def test_request_not_logged_at_info(self):
        logger = InMemoryLogger(level=LogLevel.INFO)
        app = _build(logger)
        client = TestClient(app)
        logger.clear()
        client.get("/api/ping")
        req_logs = [x for x in logger.records if x.context == "Request"]
        assert req_logs == []

    def test_4xx_logged_at_warn(self):
        logger = InMemoryLogger(level=LogLevel.WARN)
        app = _build(logger)
        client = TestClient(app)
        logger.clear()
        client.get("/api/nope")  # 404
        warns = [x for x in logger.records if x.context == "Request"]
        assert len(warns) == 1
        assert warns[0].level is LogLevel.WARN
        assert warns[0].extra["status"] == 404

    def test_5xx_logged_at_error(self):
        logger = InMemoryLogger(level=LogLevel.ERROR)
        app = _build(logger)
        client = TestClient(app)
        logger.clear()
        client.get("/api/boom")
        request_errors = [x for x in logger.records if x.context == "Request" and x.level is LogLevel.ERROR]
        # Two records expected: (1) the unhandled-exception notice and
        # (2) the request trace with status=500.
        assert len(request_errors) == 2
        summary = next(x for x in request_errors if "status" in x.extra)
        assert summary.extra["status"] == 500
        assert summary.extra["path"] == "/api/boom"

    def test_request_log_includes_handler_name(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)
        client = TestClient(app)
        logger.clear()
        client.get("/api/users/42")
        rec = next(x for x in logger.records if x.context == "Request")
        assert "ApiController.user" in rec.extra["handler"]


# ---------------------------------------------------------------------------
# Shutdown events
# ---------------------------------------------------------------------------


class TestShutdownEvents:
    def test_shutdown_logs_phases(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)
        logger.clear()
        asyncio.run(app.shutdown())
        messages = [r.message for r in logger.records if r.context == "Shutdown"]
        assert any("Shutdown initiated" in m for m in messages)
        assert any("pre_destruct" in m for m in messages)
        assert any("Goodbye" in m for m in messages)

    def test_on_shutdown_callback_runs(self):
        calls: list[str] = []
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        async def async_cb():
            calls.append("async")

        def sync_cb():
            calls.append("sync")

        app.on_shutdown(async_cb)
        app.on_shutdown(sync_cb)
        asyncio.run(app.shutdown())
        # Reverse LIFO ordering:
        assert calls == ["sync", "async"]

    def test_on_shutdown_callback_errors_logged(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        def bad():
            raise RuntimeError("oops")

        app.on_shutdown(bad)
        asyncio.run(app.shutdown())
        errors = [r for r in logger.records if r.level is LogLevel.ERROR]
        assert any("oops" in r.message or "bad" in r.message for r in errors)

    def test_on_shutdown_decorator_usage(self):
        calls: list[str] = []
        app = _build(InMemoryLogger())

        @app.on_shutdown
        async def cleanup():
            calls.append("decorated")

        asyncio.run(app.shutdown())
        assert calls == ["decorated"]

    def test_shutdown_is_idempotent(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        calls: list[int] = []
        app.on_shutdown(lambda: calls.append(1))

        async def run():
            await asyncio.gather(app.shutdown(), app.shutdown(), app.shutdown())

        asyncio.run(run())
        # The callback runs exactly once.
        assert calls == [1]

    def test_drain_timeout_logged_as_warning(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        # Simulate an in-flight task that won't finish in time.
        async def run():
            async def never():
                try:
                    await asyncio.sleep(5)
                except asyncio.CancelledError:
                    raise
                return None

            task = asyncio.create_task(never())
            app._in_flight.add(task)
            try:
                await app.shutdown(drain_timeout=0.05)
            finally:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        asyncio.run(run())
        warns = [r for r in logger.records if r.level is LogLevel.WARN]
        assert any("Drain timeout" in r.message for r in warns)


# ---------------------------------------------------------------------------
# End-to-end formatting: full JSON and Console pipelines
# ---------------------------------------------------------------------------


class TestEndToEndFormats:
    def test_json_pipeline(self):
        buf = io.StringIO()
        logger = JsonLogger(level=LogLevel.INFO, stream=buf)
        app = _build(logger)
        client = TestClient(app)
        client.get("/api/ping")
        asyncio.run(app.shutdown())
        lines = [ln for ln in buf.getvalue().splitlines() if ln]
        # Every line must be valid JSON.
        for ln in lines:
            json.loads(ln)

    def test_console_pipeline_strips_to_expected_text(self):
        buf = io.StringIO()
        logger = ConsoleLogger(level=LogLevel.INFO, stream=buf, use_colour=False)
        app = _build(logger)
        asyncio.run(app.shutdown())
        content = buf.getvalue()
        # Check major milestones appear in the rendered output.
        for marker in (
            "[LaurenFactory]",
            "[RouterExplorer]",
            "Mapped {GET /api/ping}",
            "[Shutdown]",
            "Goodbye",
        ):
            assert marker in content, f"missing: {marker!r}"
