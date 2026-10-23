"""Full-stack tests for development error pages — ``debug=True`` / ``DebugConfig`` (F05)."""

from __future__ import annotations

import asyncio
import logging
import sys
import types
from dataclasses import dataclass
from typing import Any

import pytest

from lauren import (
    DebugConfig,
    Lauren,
    LaurenFactory,
    NotFoundError,
    Request,
    Response,
    controller,
    get,
    middleware,
    module,
    use_guards,
    use_value,
)
from lauren.exceptions import StartupError
from lauren.logging import InMemoryLogger
from lauren.testing import TestClient

HTML = {"accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
JSON = {"accept": "application/json"}


class AllowAll:
    async def can_activate(self, ctx: Any) -> bool:
        return True


@controller("/dbg")
class DebugController:
    @get("/boom")
    @use_guards(AllowAll)
    async def boom(self, q: str = "") -> dict:
        try:
            {}["missing"]
        except KeyError as exc:
            raise RuntimeError("<script>alert('x')</script>") from exc
        return {}

    @get("/missing/{item_id}")
    async def missing(self, item_id: int) -> dict:
        raise NotFoundError("no item", detail={"id": item_id})

    @get("/secret")
    async def secret(self) -> dict:
        # Built at runtime so the values never appear in the source context.
        password = "hun" + "ter2"  # noqa: F841 — a local the debug page must redact
        api_token = "tok" + "-123"  # noqa: F841
        visible = "plain" + "-value"  # noqa: F841
        raise ValueError("locals test")


@module(controllers=[DebugController])
class DebugModule:
    pass


def _client(**kwargs: Any) -> TestClient:
    return TestClient(LaurenFactory.create(DebugModule, **kwargs))


async def _raw_get(
    app: Any, path: str, *, client: tuple[str, int], headers: dict[str, str]
) -> tuple[int, bytes]:
    """Drive the app with a hand-built ASGI scope (custom client address)."""
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await app.startup()
    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"",
        "headers": [(k.encode("latin-1"), v.encode("latin-1")) for k, v in headers.items()],
        "client": client,
        "server": ("127.0.0.1", 8000),
    }
    await app(scope, receive, send)
    return sent[0]["status"], b"".join(m.get("body", b"") for m in sent[1:])


# ---------------------------------------------------------------------------
# Off by default
# ---------------------------------------------------------------------------


def test_debug_off_keeps_the_plain_500_envelope() -> None:
    r = _client().get("/dbg/boom", headers=HTML)

    assert r.status_code == 500
    assert r.json() == {
        "error": {
            "code": "internal_error",
            "message": "internal server error",
            "detail": {"type": "RuntimeError"},
        }
    }


def test_debug_off_never_imports_the_renderer(monkeypatch: pytest.MonkeyPatch) -> None:
    import lauren._asgi as asgi_package

    monkeypatch.delitem(sys.modules, "lauren._asgi._debug", raising=False)
    monkeypatch.delattr(asgi_package, "_debug", raising=False)

    _client().get("/dbg/boom")

    assert "lauren._asgi._debug" not in sys.modules


# ---------------------------------------------------------------------------
# HTML page and JSON member
# ---------------------------------------------------------------------------


def test_browser_gets_an_html_page_with_exception_route_and_handler() -> None:
    r = _client(debug=True).get("/dbg/boom", headers=HTML)

    assert r.status_code == 500
    assert r.header("content-type") == "text/html; charset=utf-8"
    assert r.header("cache-control") == "no-store"
    assert "builtins.RuntimeError" not in r.text and "RuntimeError" in r.text
    assert "GET /dbg/boom" in r.text
    assert "DebugController.boom" in r.text
    assert "KeyError" in r.text  # the __cause__ chain
    assert "AllowAll" in r.text  # the guard chain


def test_html_page_escapes_the_exception_message() -> None:
    r = _client(debug=True).get("/dbg/boom", headers=HTML)

    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text


def test_json_client_gets_the_envelope_plus_a_debug_member() -> None:
    r = _client(debug=True).get("/dbg/boom", headers=JSON)

    body = r.json()
    assert r.status_code == 500
    assert body["error"]["code"] == "internal_error"
    assert body["debug"]["exception"]["type"] == "RuntimeError"
    assert body["debug"]["chain"][0]["relation"] == "cause"
    assert body["debug"]["route"]["path_template"] == "/dbg/boom"
    assert body["debug"]["route"]["guards"][-1].endswith("AllowAll")
    assert {"name": "q", "source": "query"} in body["debug"]["route"]["parameters"]
    assert body["debug"]["frames"][-1]["function"].endswith("DebugController.boom")
    assert r.header("cache-control") == "no-store"


def test_json_debug_member_rides_on_rfc9457_documents() -> None:
    r = _client(debug=True, error_format="rfc9457").get("/dbg/boom")

    assert r.json()["code"] == "internal_error"
    assert r.json()["debug"]["environment"]["error_format"] == "rfc9457"


def test_source_context_and_max_frames_are_honoured() -> None:
    r = _client(debug=DebugConfig(source_context=0, max_frames=1)).get("/dbg/boom", headers=JSON)

    frames = r.json()["debug"]["frames"]
    assert len(frames) == 1
    assert len(frames[0]["context"]) == 1 and frames[0]["context"][0]["current"] is True


def test_routing_miss_reports_no_route_on_the_debug_page() -> None:
    @middleware()
    class Exploding:
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            raise RuntimeError("before routing")

    r = _client(debug=True, global_middlewares=[Exploding]).get("/nowhere", headers=HTML)

    assert r.status_code == 500
    assert "did not match a route" in r.text


def test_global_middleware_failure_after_routing_shows_the_route() -> None:
    @middleware()
    class AfterHook:
        async def dispatch(self, request: Request, call_next: Any) -> Response:
            await call_next(request)
            raise RuntimeError("after the handler")

    r = _client(debug=True, global_middlewares=[AfterHook]).get("/dbg/missing/3", headers=JSON)

    assert r.json()["debug"]["route"]["path_template"] == "/dbg/missing/{item_id}"
    assert r.json()["debug"]["route"]["middlewares"][-1].endswith("AfterHook")


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_sensitive_headers_and_query_values_are_redacted() -> None:
    headers = {**HTML, "authorization": "Bearer s3cr3t", "cookie": "sid=c00kie", "x-request-id": "rid-1"}

    r = _client(debug=True).get("/dbg/boom?token=t0ken&access_token=at0k&q=visible", headers=headers)

    for secret in ("s3cr3t", "c00kie", "t0ken", "at0k"):
        assert secret not in r.text
    assert "rid-1" in r.text and "visible" in r.text
    assert "[redacted]" in r.text


def test_json_report_redacts_too() -> None:
    r = _client(debug=True).get("/dbg/boom?password=pw1", headers={**JSON, "x-api-key": "k3y"})

    report = r.json()["debug"]["request"]
    assert report["query"] == {"password": ["[redacted]"]}
    assert ["x-api-key", "[redacted]"] in report["headers"]


def test_locals_are_hidden_by_default() -> None:
    r = _client(debug=True).get("/dbg/secret", headers=JSON)

    assert "locals" not in r.json()["debug"]["frames"][-1]
    assert "plain-value" not in r.text


def test_include_locals_shows_values_but_redacts_secret_names() -> None:
    r = _client(debug=DebugConfig(include_locals=True)).get("/dbg/secret", headers=JSON)

    local_vars = r.json()["debug"]["frames"][-1]["locals"]
    assert local_vars["visible"] == "'plain-value'"
    assert local_vars["password"] == "[redacted]"
    assert local_vars["api_token"] == "[redacted]"
    assert "hunter2" not in r.text and "tok-123" not in r.text


def test_include_locals_renders_in_html() -> None:
    r = _client(debug=DebugConfig(include_locals=True)).get("/dbg/secret", headers=HTML)

    assert "plain-value" in r.text and "hunter2" not in r.text


# ---------------------------------------------------------------------------
# Who may see it
# ---------------------------------------------------------------------------


def test_remote_client_gets_the_plain_500() -> None:
    app = LaurenFactory.create(DebugModule, debug=True)

    status, body = asyncio.run(_raw_get(app, "/dbg/boom", client=("203.0.113.5", 4000), headers=HTML))

    assert status == 500
    assert b"<html" not in body
    assert b'"internal_error"' in body and b'"debug"' not in body


def test_ipv6_loopback_client_gets_the_page() -> None:
    app = LaurenFactory.create(DebugModule, debug=True)

    status, body = asyncio.run(_raw_get(app, "/dbg/boom", client=("::1", 4000), headers=HTML))

    assert status == 500 and b"<html" in body


def test_allow_remote_serves_every_client() -> None:
    app = LaurenFactory.create(DebugModule, debug=DebugConfig(allow_remote=True))

    _, body = asyncio.run(_raw_get(app, "/dbg/boom", client=("203.0.113.5", 4000), headers=HTML))

    assert b"<html" in body


# ---------------------------------------------------------------------------
# include_4xx
# ---------------------------------------------------------------------------


def test_include_4xx_adds_raised_at_to_http_errors() -> None:
    r = _client(debug=DebugConfig(include_4xx=True)).get("/dbg/missing/9")

    body = r.json()
    assert r.status_code == 404
    assert body["error"]["code"] == "not_found"
    assert "test_debug_error_pages.py" in body["debug"]["raised_at"]
    assert body["debug"]["traceback"][-1] == body["debug"]["raised_at"]
    assert r.header("cache-control") == "no-store"


def test_http_errors_carry_no_debug_member_without_include_4xx() -> None:
    assert "debug" not in _client(debug=True).get("/dbg/missing/9").json()


def test_include_4xx_is_loopback_only_too() -> None:
    app = LaurenFactory.create(DebugModule, debug=DebugConfig(include_4xx=True))

    status, body = asyncio.run(_raw_get(app, "/dbg/missing/9", client=("203.0.113.5", 4000), headers=JSON))

    assert status == 404 and b'"debug"' not in body


def test_include_4xx_renderer_failure_still_sends_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from lauren._asgi import _debug

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(_debug, "raised_at", broken)
    log = InMemoryLogger()

    r = _client(debug=DebugConfig(include_4xx=True), logger=log).get("/dbg/missing/9")

    assert r.status_code == 404 and "debug" not in r.json()
    assert any(record.extra.get("origin") == "debug_renderer" for record in log.records)


# ---------------------------------------------------------------------------
# Safety rails
# ---------------------------------------------------------------------------


def test_renderer_failure_falls_back_to_the_plain_500_and_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    from lauren._asgi import _debug

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("renderer bug")

    monkeypatch.setattr(_debug, "collect", broken)
    log = InMemoryLogger()

    r = _client(debug=True, logger=log).get("/dbg/boom", headers=HTML)

    assert r.status_code == 500
    assert r.json()["error"]["code"] == "internal_error"
    failures = [rec for rec in log.records if rec.extra.get("origin") == "debug_renderer"]
    assert failures and failures[0].extra["cause"] == "RuntimeError"


def test_debug_renderer_is_a_no_op_when_debug_is_off() -> None:
    app = LaurenFactory.create(DebugModule)

    assert (
        app._debug_error_response(ValueError("x"), request=Request(method="GET", path="/"), encoder=None)
        is None
    )


def test_create_logs_a_warning_when_debug_is_on(caplog: pytest.LogCaptureFixture) -> None:
    log = InMemoryLogger()

    with caplog.at_level(logging.WARNING, logger="lauren"):
        LaurenFactory.create(DebugModule, debug=True, logger=log)

    assert any("never use in production" in r.getMessage() for r in caplog.records)
    assert any("never use in production" in rec.message for rec in log.records)


def test_lauren_debug_flag_is_forwarded_to_the_compiled_app() -> None:
    app = Lauren(debug=True, openapi_url=None, docs_url=None, redoc_url=None)

    @app.get("/crash")
    async def crash() -> dict:
        raise RuntimeError("from Lauren")

    r = TestClient(app).get("/crash", headers=HTML)

    assert r.status_code == 500 and "from Lauren" in r.text


def test_lauren_debug_property_reports_a_debug_config_as_enabled() -> None:
    assert Lauren(debug=DebugConfig()).debug is True
    assert Lauren().debug is False


@pytest.mark.parametrize(
    "debug,field",
    [
        ("yes", None),
        (1, None),
        (DebugConfig(max_frames=0), "max_frames"),
        (DebugConfig(source_context=-1), "source_context"),
        (DebugConfig(include_locals="no"), "include_locals"),  # type: ignore[arg-type]
        (DebugConfig(redact_headers="authorization"), "redact_headers"),  # type: ignore[arg-type]
        (DebugConfig(redact_query_keys=(1,)), "redact_query_keys"),  # type: ignore[arg-type]
    ],
    ids=["string", "int", "max-frames", "source-context", "locals-type", "headers-str", "query-keys-type"],
)
def test_invalid_debug_values_fail_startup(debug: Any, field: str | None) -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(DebugModule, debug=debug)

    assert info.value.detail["reason"] == "invalid_debug_config"
    if field is not None:
        assert info.value.detail["field"] == field


@dataclass(frozen=True)
class _FakeProfile:
    name: str
    is_production: bool


@pytest.fixture
def fake_config_module(monkeypatch: pytest.MonkeyPatch) -> type:
    """Stand-in for the typed-configuration module's ``Profile`` (``lauren.config``)."""
    module_obj = types.ModuleType("lauren.config")
    module_obj.Profile = _FakeProfile  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "lauren.config", module_obj)
    return _FakeProfile


def test_debug_in_a_production_profile_fails_startup(fake_config_module: type) -> None:
    profile = fake_config_module(name="production", is_production=True)

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(
            DebugModule, debug=True, global_providers=[use_value(provide=fake_config_module, value=profile)]
        )

    assert info.value.detail == {"reason": "debug_in_production", "profile": "production"}


def test_debug_in_a_development_profile_is_allowed(fake_config_module: type) -> None:
    profile = fake_config_module(name="development", is_production=False)

    app = LaurenFactory.create(
        DebugModule, debug=True, global_providers=[use_value(provide=fake_config_module, value=profile)]
    )

    assert app._debug is not None


def test_production_profile_check_needs_a_registered_profile(fake_config_module: type) -> None:
    assert LaurenFactory.create(DebugModule, debug=True)._debug is not None


def test_config_module_without_a_profile_class_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "lauren.config", types.ModuleType("lauren.config"))

    assert LaurenFactory.create(DebugModule, debug=True)._debug is not None


# ---------------------------------------------------------------------------
# The last-resort fallback in __call__
# ---------------------------------------------------------------------------


def test_error_escaping_handle_gets_the_debug_page(monkeypatch: pytest.MonkeyPatch) -> None:
    app = LaurenFactory.create(DebugModule, debug=True)

    async def broken_handle(request: Request) -> Response:
        raise RuntimeError("escaped handle")

    monkeypatch.setattr(app, "handle", broken_handle)

    status, body = asyncio.run(_raw_get(app, "/dbg/boom", client=("127.0.0.1", 1), headers=HTML))

    assert status == 500 and b"escaped handle" in body


def test_error_escaping_handle_without_debug_is_the_plain_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    app = LaurenFactory.create(DebugModule, error_format="rfc9457")

    async def broken_handle(request: Request) -> Response:
        raise RuntimeError("escaped handle")

    monkeypatch.setattr(app, "handle", broken_handle)

    status, body = asyncio.run(_raw_get(app, "/dbg/boom", client=("127.0.0.1", 1), headers=HTML))

    assert status == 500 and b'"instance":"/dbg/boom"' in body
