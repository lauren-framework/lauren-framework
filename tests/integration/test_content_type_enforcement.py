"""Content-type enforcement: inferred and declared ``consumes`` (F03 §5.2).

``tests/integration/test_content_type_policy.py`` covers the JSON gate itself
(D03); this file covers the matrix, declared ``consumes=``, controller
defaults, form bodies and the 415 headers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    ByteStream,
    Bytes,
    ContentConfig,
    Form,
    Json,
    LaurenFactory,
    UnauthorizedError,
    controller,
    get,
    module,
    patch,
    post,
    use_guards,
)
from lauren.logging import InMemoryLogger, LogLevel
from lauren.testing import TestClient


@dataclass
class Item:
    name: str


class NeedsToken:
    deny_error = UnauthorizedError

    async def can_activate(self, ctx: Any) -> bool:
        return ctx.request.headers.get("authorization") == "Bearer ok"


@controller("/items")
class Items:
    @post("/")
    async def create(self, body: Json[Item]) -> dict:
        return {"name": body.name}

    @patch("/{id}", consumes=["application/merge-patch+json"])
    async def merge(self, id: int, body: Json[Item]) -> dict:
        return {"id": id, "name": body.name}

    @post("/import", consumes=["text/csv"])
    async def import_csv(self, body: Bytes) -> dict:
        return {"rows": body.decode().count("\n") + 1}

    @post("/raw")
    async def raw(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/stream")
    async def stream(self, body: ByteStream) -> dict:
        return {"size": sum([len(chunk) async for chunk in body])}

    @post("/any", consumes=["*/*"])
    async def anything(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/form")
    async def form(self, data: Form[dict]) -> dict:
        return {"keys": sorted(data)}

    @post("/locked")
    @use_guards(NeedsToken)
    async def locked(self, body: Json[Item]) -> dict:
        return {"name": body.name}


@controller("/csv", consumes=["text/csv"])
class CsvController:
    @post("/")
    async def upload(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/json", consumes=["application/json"])
    async def override(self, body: Json[Item]) -> dict:
        return {"name": body.name}

    @get("/")
    async def listing(self) -> dict:
        return {"ok": True}


@module(controllers=[Items, CsvController])
class AppModule:
    pass


def _client(strict: Any = True, **kwargs: Any) -> TestClient:
    return TestClient(
        LaurenFactory.create(AppModule, content=ContentConfig(strict_content_type=strict), **kwargs)
    )


def _post(client: TestClient, url: str, content_type: str | None, body: bytes = b'{"name": "x"}') -> Any:
    headers = {"content-type": content_type} if content_type is not None else {}
    return client.post(url, content=body, headers=headers)


@pytest.mark.parametrize(
    ("content_type", "lenient", "strict"),
    [
        (None, 200, 415),
        ("text/plain", 200, 415),
        ("application/x-www-form-urlencoded", 200, 415),
        ("application/json", 200, 200),
        ("application/json; charset=latin-1", 200, 415),
        ("application/merge-patch+json", 200, 200),
    ],
)
@pytest.mark.parametrize("mode", [False, "warn", True])
def test_json_body_matrix(mode: Any, content_type: str | None, lenient: int, strict: int) -> None:
    response = _post(_client(mode), "/items/", content_type)

    assert response.status_code == (strict if mode is True else lenient)


def test_warn_mode_logs_once_per_route_and_content_type() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)
    client = _client("warn", logger=log)

    for _ in range(3):
        assert _post(client, "/items/", "text/plain").status_code == 200

    warnings = [r for r in log.records if r.context == "ContentPolicy"]
    assert [r.extra["content_type"] for r in warnings] == ["text/plain"]


@pytest.mark.parametrize("mode", [False, "warn", True])
@pytest.mark.parametrize("url", ["/items/raw", "/items/stream"])
def test_raw_body_parameters_are_not_gated_without_consumes(mode: Any, url: str) -> None:
    response = _post(_client(mode), url, "text/plain", body=b"abc")

    assert response.status_code == 200
    assert response.json() == {"size": 3}


@pytest.mark.parametrize("mode", [False, "warn", True])
def test_declared_consumes_is_always_enforced(mode: Any) -> None:
    client = _client(mode)

    accepted = _post(client, "/items/import", "text/csv; charset=utf-8", body=b"a,b\nc,d")
    rejected = _post(client, "/items/import", "application/json", body=b"[]")

    assert accepted.status_code == 200 and accepted.json() == {"rows": 2}
    assert rejected.status_code == 415
    assert rejected.json()["error"]["detail"] == {
        "content_type": "application/json",
        "supported": ["text/csv"],
    }


def test_415_advertises_accept_post_and_accept_patch() -> None:
    client = _client(True)

    post_response = _post(client, "/items/", "text/plain")
    patch_response = client.patch("/items/1", content=b"{}", headers={"content-type": "application/json"})

    assert post_response.headers["accept-post"] == "application/json, application/*+json"
    assert patch_response.status_code == 415
    assert patch_response.headers["accept-patch"] == "application/merge-patch+json"
    assert "accept-post" not in patch_response.headers


def test_declared_suffix_type_decodes_as_json() -> None:
    response = _client(False).patch(
        "/items/7", content=b'{"name": "y"}', headers={"content-type": "application/merge-patch+json"}
    )

    assert response.status_code == 200
    assert response.json() == {"id": 7, "name": "y"}


def test_a_wildcard_consumes_accepts_anything() -> None:
    response = _post(_client(True), "/items/any", "image/png", body=b"\x89PNG")

    assert response.status_code == 200


def test_guards_run_before_the_gate() -> None:
    client = _client(True)

    denied = _post(client, "/items/locked", "text/plain")
    allowed = client.post("/items/locked", json={"name": "z"}, headers={"authorization": "Bearer ok"})

    assert denied.status_code == 401
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    ("content_type", "expected"),
    [
        ("application/x-www-form-urlencoded", 200),
        # Multipart is now supported; URL-encoded bytes are malformed framing.
        ("multipart/form-data; boundary=x", 422),
        ("application/json", 415),
    ],
)
def test_form_bodies_are_gated_in_strict_mode(content_type: str, expected: int) -> None:
    response = _post(_client(True), "/items/form", content_type, body=b"a=1&b=2")

    assert response.status_code == expected


def test_valid_multipart_form_is_accepted_in_strict_mode() -> None:
    with _client(True) as client:
        response = client.post("/items/form", data={"a": "1"}, files={"ignored": ("x", b"x")})
    assert response.status_code == 200
    assert response.json() == {"keys": ["a"]}


def test_form_bodies_are_not_gated_when_lenient() -> None:
    response = _post(_client(False), "/items/form", "application/json", body=b"a=1")

    assert response.status_code == 200


def test_controller_consumes_applies_to_body_routes_and_yields_to_the_route() -> None:
    client = _client(False)

    assert _post(client, "/csv/", "text/csv", body=b"a").status_code == 200
    assert _post(client, "/csv/", "application/json", body=b"{}").status_code == 415
    assert _post(client, "/csv/json", "application/json").status_code == 200
    assert _post(client, "/csv/json", "text/csv", body=b"a").status_code == 415


def test_controller_consumes_ignores_routes_without_a_body() -> None:
    response = _client(False).get("/csv/", headers={"content-type": "application/json"})

    assert response.status_code == 200


def test_an_empty_body_without_content_type_passes_a_declared_gate() -> None:
    response = _post(_client(True), "/items/import", None, body=b"")

    assert response.status_code == 200


def test_lauren_forwards_route_content_options() -> None:
    from lauren import Lauren

    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None, max_body_size=1000)

    @app.post("/csv", consumes="text/csv", max_body_size=4)
    async def csv(body: Bytes) -> dict:
        return {"size": len(body)}

    client = TestClient(app)

    assert _post(client, "/csv", "text/csv", body=b"a,b").json() == {"size": 3}
    assert _post(client, "/csv", "text/csv", body=b"a,b,c").status_code == 413
    assert _post(client, "/csv", "application/json", body=b"[]").status_code == 415


def test_absurdly_deep_json_is_a_malformed_body() -> None:
    body = b"[" * 50_000 + b"]" * 50_000

    response = _post(_client(False), "/items/", "application/json", body=body)

    assert response.status_code == 422
    assert response.json()["error"]["detail"]["reason"] == "malformed_body"
