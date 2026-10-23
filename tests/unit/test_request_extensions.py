"""``Request.extensions`` — the ASGI ``scope["extensions"]``, read-only and pool-safe."""

from __future__ import annotations

from typing import Any

import pytest

from lauren.types import AppState, ClientInfo, Headers, Request, ServerInfo


async def _receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _reset_kwargs(**extra: Any) -> dict[str, Any]:
    return {
        "method": "GET",
        "path": "/",
        "raw_query_string": b"",
        "headers": Headers(),
        "client": ClientInfo(None, None),
        "server": ServerInfo(None, None),
        "receive": _receive,
        "app_state": AppState(),
        "max_body_size": 1024,
        **extra,
    }


def test_extensions_default_to_an_empty_mapping() -> None:
    assert dict(Request(method="GET", path="/").extensions) == {}


def test_extensions_expose_the_scope_dict() -> None:
    request = Request(method="GET", path="/", extensions={"tls": {"version": 772}})
    assert request.extensions["tls"] == {"version": 772}


def test_extensions_mapping_is_read_only() -> None:
    request = Request(method="GET", path="/", extensions={"tls": {}})
    with pytest.raises(TypeError):
        request.extensions["x"] = 1  # type: ignore[index]


def test_reset_drops_the_previous_requests_extensions() -> None:
    request = Request(method="GET", path="/", extensions={"tls": {}})
    request.reset(**_reset_kwargs())
    assert dict(request.extensions) == {}


def test_reset_installs_new_extensions() -> None:
    request = Request(method="GET", path="/")
    request.reset(**_reset_kwargs(extensions={"http.response.trailers": {}}))
    assert "http.response.trailers" in request.extensions
