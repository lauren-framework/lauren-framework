"""Integration tests for root_path / proxy-prefix support."""

from __future__ import annotations

import asyncio
import json
from typing import Any


from lauren import (
    LaurenFactory,
    Response,
    LaurenApp,
    controller,
    get,
    module,
)


# ---------------------------------------------------------------------------
# App fixture
# ---------------------------------------------------------------------------


@controller("/v1")
class _V1Controller:
    @get("/hello")
    async def hello(self) -> Response:
        return Response.json({"hello": "world"})

    @get("/echo/{value}")
    async def echo(self, value: str) -> Response:
        return Response.json({"value": value})


@module(controllers=[_V1Controller])
class _ProxyModule:
    pass


def _build(root_path: str = "") -> LaurenApp:  # type: ignore[name-defined]
    return asyncio.run(LaurenFactory.create(_ProxyModule, root_path=root_path))


# ---------------------------------------------------------------------------
# Low-level ASGI helpers
# ---------------------------------------------------------------------------


async def _raw_request(
    app: Any,
    path: str,
    *,
    asgi_root_path: str = "",
    method: str = "GET",
) -> tuple[int, bytes]:
    """Call app directly with a custom scope (bypasses TestClient path building)."""
    scope: dict[str, Any] = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 9999),
        "server": ("testserver", 80),
        "root_path": asgi_root_path,
    }
    sent = False

    async def receive() -> dict:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": b"", "more_body": False}

    status = 500
    body = bytearray()

    async def send(msg: dict) -> None:
        nonlocal status
        if msg["type"] == "http.response.start":
            status = msg["status"]
        elif msg["type"] == "http.response.body":
            body.extend(msg.get("body", b""))

    await app(scope, receive, send)
    return status, bytes(body)


# ---------------------------------------------------------------------------
# Default (no root_path) — must not regress
# ---------------------------------------------------------------------------


def test_default_no_root_path() -> None:
    app = _build()
    status, _ = asyncio.run(_raw_request(app, "/v1/hello"))
    assert status == 200


def test_default_404() -> None:
    app = _build()
    status, _ = asyncio.run(_raw_request(app, "/missing"))
    assert status == 404


# ---------------------------------------------------------------------------
# Nginx-style: app has root_path, scope["root_path"] is empty,
# scope["path"] contains the full prefixed path.
# ---------------------------------------------------------------------------


def test_nginx_prefix_stripped() -> None:
    app = _build(root_path="/api")
    status, body = asyncio.run(_raw_request(app, "/api/v1/hello", asgi_root_path=""))
    assert status == 200
    assert json.loads(body)["hello"] == "world"


def test_nginx_path_param_works() -> None:
    app = _build(root_path="/api")
    status, body = asyncio.run(
        _raw_request(app, "/api/v1/echo/test123", asgi_root_path="")
    )
    assert status == 200
    assert json.loads(body)["value"] == "test123"


def test_nginx_404_for_unknown_after_strip() -> None:
    app = _build(root_path="/api")
    status, _ = asyncio.run(_raw_request(app, "/api/unknown", asgi_root_path=""))
    assert status == 404


def test_nginx_path_without_prefix_still_works() -> None:
    """When path does not start with root_path, fall through unstripped."""
    app = _build(root_path="/api")
    # /v1/hello doesn't start with /api so no stripping → router matches it.
    status, _ = asyncio.run(_raw_request(app, "/v1/hello", asgi_root_path=""))
    assert status == 200


# ---------------------------------------------------------------------------
# Uvicorn-style: scope["root_path"] populated, scope["path"] already stripped.
# ---------------------------------------------------------------------------


def test_uvicorn_prefix_no_double_strip() -> None:
    app = _build(root_path="/api")
    # Uvicorn already stripped /api from path.
    status, body = asyncio.run(_raw_request(app, "/v1/hello", asgi_root_path="/api"))
    assert status == 200
    assert json.loads(body)["hello"] == "world"


def test_uvicorn_path_param_works() -> None:
    app = _build(root_path="/api")
    status, body = asyncio.run(
        _raw_request(app, "/v1/echo/hello", asgi_root_path="/api")
    )
    assert status == 200
    assert json.loads(body)["value"] == "hello"


def test_no_root_path_scope_root_path_ignored() -> None:
    """When app has no root_path configured, scope root_path doesn't matter."""
    app = _build(root_path="")
    status, _ = asyncio.run(_raw_request(app, "/v1/hello", asgi_root_path="/api"))
    assert status == 200


# ---------------------------------------------------------------------------
# OpenAPI servers field reflects root_path
# ---------------------------------------------------------------------------


def test_openapi_servers_contains_root_path() -> None:
    app = asyncio.run(
        LaurenFactory.create(
            _ProxyModule,
            root_path="/api",
            openapi_url="/openapi.json",
        )
    )
    doc = app.openapi()
    servers = doc.get("servers", [])
    urls = [s["url"] for s in servers]
    assert "/api" in urls


def test_openapi_no_servers_when_no_root_path() -> None:
    app = asyncio.run(
        LaurenFactory.create(
            _ProxyModule,
            openapi_url="/openapi.json",
        )
    )
    doc = app.openapi()
    # Without a root_path there should be no auto-generated servers entry.
    assert "servers" not in doc or doc["servers"] == []


def test_explicit_openapi_servers_not_overridden() -> None:
    """Explicit openapi_servers must take precedence over root_path fallback."""
    custom_servers = [{"url": "https://example.com/v2"}]
    app = asyncio.run(
        LaurenFactory.create(
            _ProxyModule,
            root_path="/api",
            openapi_servers=custom_servers,
            openapi_url="/openapi.json",
        )
    )
    doc = app.openapi()
    servers = doc.get("servers", [])
    assert len(servers) == 1
    assert servers[0]["url"] == "https://example.com/v2"
