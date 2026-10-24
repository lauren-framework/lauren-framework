"""Lazy single-flight startup for ASGI drivers that never send lifespan events."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from lauren import Lauren, LaurenFactory, controller, get, injectable, module, post_construct
from lauren.exceptions import LifecycleViolationError


def _build(*, lazy_startup: bool = True, fail: bool = False) -> tuple[Any, dict[str, int]]:
    calls = {"post_construct": 0}

    @injectable()
    class Database:
        @post_construct
        async def connect(self) -> None:
            calls["post_construct"] += 1
            await asyncio.sleep(0)
            if fail:
                raise RuntimeError("database unreachable")

    @controller("/")
    class StatusController:
        def __init__(self, db: Database) -> None:
            self.db = db

        @get("/status")
        async def status(self) -> dict:
            return {"started": True}

    @module(controllers=[StatusController], providers=[Database])
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, lazy_startup=lazy_startup), calls


async def _call(app: Any, path: str = "/status") -> list[dict[str, Any]]:
    sent: list[dict[str, Any]] = []
    scope = {"type": "http", "method": "GET", "path": path, "query_string": b"", "headers": []}

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app(scope, receive, send)
    return sent


async def test_first_request_without_lifespan_starts_the_app() -> None:
    app, calls = _build()
    sent = await _call(app)
    assert sent[0]["status"] == 200
    assert app._started is True
    assert calls["post_construct"] == 1


async def test_lazy_startup_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    app, _ = _build()
    with caplog.at_level(logging.WARNING, logger="lauren"):
        await _call(app)
    assert "started lazily" in caplog.text


async def test_concurrent_first_requests_start_the_app_once() -> None:
    app, calls = _build()
    results = await asyncio.gather(*(_call(app) for _ in range(8)))
    assert all(r[0]["status"] == 200 for r in results)
    assert calls["post_construct"] == 1


async def test_lazy_startup_false_keeps_the_app_cold() -> None:
    app, calls = _build(lazy_startup=False)
    await _call(app)
    assert app._started is False
    assert calls["post_construct"] == 0


async def test_a_lifespan_message_disables_lazy_startup() -> None:
    app, calls = _build()
    messages = iter([{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}])

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        pass

    await app({"type": "lifespan"}, receive, send)
    assert calls["post_construct"] == 1
    assert app._lazy_start_task is None


async def test_lazy_startup_failure_is_raised_to_every_request() -> None:
    app, calls = _build(fail=True)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="database unreachable"):
            await _call(app)
    assert calls["post_construct"] == 1


async def test_no_lazy_restart_after_shutdown() -> None:
    app, calls = _build()
    await app.shutdown()
    await _call(app)
    assert calls["post_construct"] == 0


async def test_explicit_startup_after_lazy_start_is_a_second_startup() -> None:
    app, _ = _build()
    await _call(app)
    with pytest.raises(LifecycleViolationError):
        await app.startup()


async def test_httpx_asgi_transport_now_runs_post_construct() -> None:
    httpx = pytest.importorskip("httpx")
    app, calls = _build()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/status")
    assert response.status_code == 200
    assert calls["post_construct"] == 1


async def test_lauren_app_accepts_lazy_startup_flag() -> None:
    app = Lauren(lazy_startup=False, openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/x")
    async def handler() -> dict:
        return {}

    await _call(app, "/x")
    assert app._compiled is not None and app._compiled._started is False
