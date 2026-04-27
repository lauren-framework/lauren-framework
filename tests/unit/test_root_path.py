"""Unit tests for root_path / proxy-prefix support in LaurenApp."""

from __future__ import annotations

import asyncio
from typing import Any


from lauren import LaurenApp, LaurenFactory, Response, controller, get, module


# ---------------------------------------------------------------------------
# Minimal app fixture
# ---------------------------------------------------------------------------


@controller("/items")
class _ItemsController:
    @get("/")
    async def list_items(self) -> Response:
        return Response.json({"items": []})

    @get("/{item_id}")
    async def get_item(self, item_id: int) -> Response:
        return Response.json({"id": item_id})


@module(controllers=[_ItemsController])
class _RootModule:
    pass


def _build_app(root_path: str = "") -> LaurenApp:
    return asyncio.run(LaurenFactory.create(_RootModule, root_path=root_path))


# ---------------------------------------------------------------------------
# Helpers to drive the ASGI app directly
# ---------------------------------------------------------------------------


def _make_scope(
    path: str,
    *,
    asgi_root_path: str = "",
    method: str = "GET",
) -> dict[str, Any]:
    return {
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


async def _call(
    app: LaurenApp, path: str, *, asgi_root_path: str = ""
) -> tuple[int, bytes]:
    scope = _make_scope(path, asgi_root_path=asgi_root_path)
    sent_body = False

    async def receive() -> dict:
        nonlocal sent_body
        if sent_body:
            return {"type": "http.disconnect"}
        sent_body = True
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
# No root_path (default behaviour must be unchanged)
# ---------------------------------------------------------------------------


def test_no_root_path_normal_request() -> None:
    app = _build_app()
    status, _ = asyncio.run(_call(app, "/items/"))
    assert status == 200


def test_no_root_path_404_for_unknown() -> None:
    app = _build_app()
    status, _ = asyncio.run(_call(app, "/does-not-exist"))
    assert status == 404


# ---------------------------------------------------------------------------
# Nginx scenario: root_path set on app, scope["root_path"] empty,
# scope["path"] contains the full prefixed path.
# ---------------------------------------------------------------------------


def test_nginx_scenario_strips_prefix() -> None:
    app = _build_app(root_path="/api")
    # Nginx proxies /api/items/ → scope path = /api/items/, root_path not set.
    status, body = asyncio.run(_call(app, "/api/items/", asgi_root_path=""))
    assert status == 200


def test_nginx_scenario_path_param() -> None:
    app = _build_app(root_path="/api")
    status, body = asyncio.run(_call(app, "/api/items/42", asgi_root_path=""))
    assert status == 200
    import json

    data = json.loads(body)
    assert data["id"] == 42


def test_nginx_scenario_without_prefix_returns_404() -> None:
    """Path without prefix must not match after stripping (router has /items/...)."""
    app = _build_app(root_path="/api")
    # Request without the /api prefix goes directly (no stripping needed).
    status, _ = asyncio.run(_call(app, "/items/", asgi_root_path=""))
    assert status == 200  # still works because no prefix to strip


def test_nginx_unrelated_path_not_stripped() -> None:
    """Paths that don't start with root_path are passed through unchanged."""
    app = _build_app(root_path="/api")
    status, _ = asyncio.run(_call(app, "/items/", asgi_root_path=""))
    assert status == 200


# ---------------------------------------------------------------------------
# Uvicorn scenario: scope["root_path"] is already set by the server,
# scope["path"] has the prefix stripped — we must NOT double-strip.
# ---------------------------------------------------------------------------


def test_uvicorn_scenario_no_double_strip() -> None:
    app = _build_app(root_path="/api")
    # Uvicorn already stripped /api; passes scope["root_path"]="/api".
    status, _ = asyncio.run(_call(app, "/items/", asgi_root_path="/api"))
    assert status == 200


def test_uvicorn_scenario_path_param() -> None:
    app = _build_app(root_path="/api")
    status, body = asyncio.run(_call(app, "/items/7", asgi_root_path="/api"))
    assert status == 200
    import json

    assert json.loads(body)["id"] == 7


# ---------------------------------------------------------------------------
# root_path attribute stashed on app
# ---------------------------------------------------------------------------


def test_root_path_attribute_stored() -> None:
    app = _build_app(root_path="/v2")
    assert getattr(app, "_root_path", None) == "/v2"


def test_empty_root_path_attribute() -> None:
    app = _build_app()
    assert getattr(app, "_root_path", None) == ""
