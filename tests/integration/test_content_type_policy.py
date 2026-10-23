"""``ContentConfig(strict_content_type=...)`` — the JSON body content-type gate (D03 §5.3).

A JSON body parameter checks ``Content-Type`` before any body byte is read:
strict mode answers 415, "warn" mode accepts and logs once per route and media
type, ``False`` does nothing. Apps with cookie sessions default to "warn".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    Bytes,
    ContentConfig,
    Json,
    Lauren,
    LaurenFactory,
    Request,
    Response,
    controller,
    exception_handler,
    post,
    module,
    use_guards,
)
from lauren.exceptions import StartupError, UnsupportedMediaTypeError
from lauren.logging import InMemoryLogger, LogLevel
from lauren.testing import TestClient


@dataclass
class Item:
    name: str


class DenyAll:
    async def can_activate(self, ctx: Any) -> bool:
        return False


@controller("/items")
class Items:
    @post("/")
    async def create(self, body: Json[Item]) -> dict:
        return {"created": body.name}

    @post("/optional")
    async def optional(self, body: Json[Item] = None) -> dict:  # type: ignore[assignment]
        return {"created": body.name if body is not None else None}

    @post("/raw")
    async def raw(self, body: Bytes) -> dict:
        return {"size": len(body)}

    @post("/locked")
    @use_guards(DenyAll)
    async def locked(self, body: Json[Item]) -> dict:
        return {"created": body.name}


@module(controllers=[Items])
class ItemsModule:
    pass


def _client(strict: Any, **kwargs: Any) -> TestClient:
    content = kwargs.pop("content", ContentConfig(strict_content_type=strict))
    return TestClient(LaurenFactory.create(ItemsModule, content=content, **kwargs))


def _post(
    client: TestClient, content_type: str | None, body: bytes = b'{"name": "x"}', url: str = "/items/"
) -> Any:
    headers = {"content-type": content_type} if content_type is not None else {}
    return client.post(url, content=body, headers=headers)


_ACCEPTED = [
    "application/json",
    "application/json; charset=utf-8",
    "application/json; charset=UTF-8",
    'application/json; charset="utf-8"',
    "APPLICATION/JSON",
    "application/merge-patch+json",
    "application/vnd.api+json",
]

_REJECTED = [
    "text/plain",
    "text/plain; charset=utf-8",
    "application/x-www-form-urlencoded",
    "multipart/form-data; boundary=xyz",
    "text/json",
    "application/json; charset=latin-1",
]


@pytest.mark.parametrize("content_type", _ACCEPTED)
def test_strict_mode_accepts_json_media_types(content_type: str) -> None:
    response = _post(_client(True), content_type)

    assert response.status_code == 200
    assert response.json() == {"created": "x"}


@pytest.mark.parametrize("content_type", _REJECTED)
def test_strict_mode_rejects_other_media_types_with_415(content_type: str) -> None:
    response = _post(_client(True), content_type)

    assert response.status_code == 415
    assert response.json() == {
        "error": {
            "code": "unsupported_media_type",
            "message": response.json()["error"]["message"],
            "detail": {"content_type": content_type, "supported": ["application/json", "application/*+json"]},
        }
    }


def test_strict_mode_rejects_a_body_without_content_type() -> None:
    response = _post(_client(True), None)

    assert response.status_code == 415
    assert response.json()["error"]["detail"]["content_type"] is None


def test_strict_mode_leaves_an_empty_body_to_the_extractor() -> None:
    client = _client(True)

    missing = _post(client, None, body=b"")
    optional = _post(client, None, body=b"", url="/items/optional")

    assert missing.status_code == 422
    assert missing.json()["error"]["message"] == "missing JSON body"
    assert optional.status_code == 200 and optional.json() == {"created": None}


def test_strict_mode_does_not_gate_raw_body_parameters() -> None:
    response = _post(_client(True), "text/plain", url="/items/raw")

    assert response.status_code == 200


def test_guards_run_before_the_content_type_gate() -> None:
    response = _post(_client(True), "text/plain", url="/items/locked")

    assert response.status_code == 403


def test_415_can_be_claimed_by_an_exception_handler() -> None:
    @exception_handler(UnsupportedMediaTypeError)
    def to_400(exc: Exception, request: Request) -> Response:
        return Response.json({"error": "use JSON"}, status=400)

    response = _post(_client(True, global_exception_handlers=[to_400]), "text/plain")

    assert response.status_code == 400
    assert response.json() == {"error": "use JSON"}


@pytest.mark.parametrize("strict", [False, None])
def test_no_check_without_strict_mode(strict: Any) -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)
    response = _post(_client(strict, logger=log), "text/plain")

    assert response.status_code == 200
    assert not [r for r in log.records if r.context == "ContentPolicy"]


def test_warn_mode_accepts_and_logs_once_per_route_and_media_type() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)
    client = _client("warn", logger=log)

    statuses = [
        _post(client, "text/plain").status_code,
        _post(client, "text/plain; charset=utf-8").status_code,
        _post(client, "application/x-www-form-urlencoded").status_code,
        _post(client, "application/json").status_code,
    ]

    assert statuses == [200, 200, 200, 200]
    warnings = [r for r in log.records if r.context == "ContentPolicy"]
    assert [(r.level, r.extra["content_type"]) for r in warnings] == [
        (LogLevel.WARN, "text/plain"),
        (LogLevel.WARN, "application/x-www-form-urlencoded"),
    ]
    assert warnings[0].extra["path"] == "/items" and warnings[0].extra["method"] == "POST"


def test_warn_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import lauren._asgi as asgi

    monkeypatch.setattr(asgi, "_CONTENT_TYPE_WARN_LIMIT", 2)
    log = InMemoryLogger(level=LogLevel.DEBUG)
    client = _client("warn", logger=log)

    for i in range(5):
        assert _post(client, f"text/x-{i}").status_code == 200

    assert len([r for r in log.records if r.context == "ContentPolicy"]) == 2


@pytest.mark.parametrize("bad", ["yes", 1, 0, "strict"])
def test_invalid_strict_content_type_fails_startup(bad: Any) -> None:
    with pytest.raises(StartupError) as excinfo:
        _client(bad)

    assert excinfo.value.detail["reason"] == "invalid_content_config"


def test_content_must_be_a_content_config() -> None:
    with pytest.raises(StartupError) as excinfo:
        _client(None, content={"strict_content_type": True})

    assert excinfo.value.detail == {"reason": "invalid_content_config", "got": "dict"}


def test_lauren_forwards_content_config() -> None:
    app = Lauren(
        content=ContentConfig(strict_content_type=True), openapi_url=None, docs_url=None, redoc_url=None
    )

    @app.post("/echo")
    async def echo(body: Json[Item]) -> dict:
        return {"name": body.name}

    client = TestClient(app)

    assert (
        client.post("/echo", content=b'{"name": "x"}', headers={"content-type": "text/plain"}).status_code
        == 415
    )
    assert client.post("/echo", json={"name": "x"}).status_code == 200


def test_warn_memory_is_per_route(monkeypatch: pytest.MonkeyPatch) -> None:
    # Regression: one shared first-come memory let junk on one route silence
    # every other route.
    import lauren._asgi as asgi

    monkeypatch.setattr(asgi, "_CONTENT_TYPE_WARN_LIMIT", 2)
    log = InMemoryLogger(level=LogLevel.DEBUG)
    client = _client("warn", logger=log)

    for i in range(5):
        _post(client, f"text/x-{i}")
    _post(client, "text/plain", url="/items/locked")  # guard denies, never gated
    _post(client, "text/plain", url="/items/optional")

    paths = [r.extra["path"] for r in log.records if r.context == "ContentPolicy"]
    assert paths == ["/items", "/items", "/items/optional"]


def test_strict_gate_runs_before_interceptors() -> None:
    from lauren import CallHandler

    calls: list[str] = []

    class Recorder:
        async def intercept(self, ctx: Any, call_handler: CallHandler) -> Any:
            calls.append("interceptor")
            return await call_handler.handle()

    client = _client(True, global_interceptors=[Recorder])

    rejected = _post(client, "text/plain")
    accepted = _post(client, "application/json")

    assert (rejected.status_code, accepted.status_code) == (415, 200)
    assert calls == ["interceptor"]
