"""Integration tests for custom Response subclasses.

Covers:
- Subclass returned from a handler is delivered unchanged (correct body, status, content-type)
- with_* builder chain on a subclass preserves headers/status end-to-end
- Extra instance attributes are readable in an interceptor
- Streaming subclass delivers the full body
- Exception handler returning a subclass delivers the correct response
- Subclass with custom content-type is correctly served
"""

from __future__ import annotations

import json


from lauren import (
    LaurenFactory,
    Response,
    controller,
    get,
    module,
    post,
)
from lauren.testing import TestClient
from lauren.types import Headers


# ---------------------------------------------------------------------------
# Shared custom response types
# ---------------------------------------------------------------------------


class JsonApiResponse(Response):
    """JSON:API media type."""

    @classmethod
    def resource(cls, data: dict, *, status: int = 200) -> "JsonApiResponse":
        body = json.dumps({"data": data}, separators=(",", ":")).encode()
        return cls(body, status=status, media_type="application/vnd.api+json")

    @classmethod
    def error_response(cls, title: str, status: int = 400) -> "JsonApiResponse":
        body = json.dumps({"errors": [{"title": title}]}, separators=(",", ":")).encode()
        return cls(body, status=status, media_type="application/vnd.api+json")


class CsvResponse(Response):
    """Streaming CSV response."""

    @classmethod
    def from_rows(cls, rows: list[list[str]]) -> "CsvResponse":
        async def _gen():
            for row in rows:
                yield (",".join(row) + "\n").encode()

        return cls(
            stream=_gen(),
            media_type="text/csv",
            headers=Headers([("content-disposition", 'attachment; filename="data.csv"')]),
        )


class HtmlTemplateResponse(Response):
    """Simple HTML template response that stores template name."""

    def __init__(self, template: str, context: dict, *, status: int = 200) -> None:
        rendered = f"<html><body>{template}: {list(context.values())}</body></html>"
        super().__init__(rendered.encode(), status=status, media_type="text/html; charset=utf-8")
        self.template = template
        self.context = context


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(ctrl: type) -> TestClient:
    @module(controllers=[ctrl])
    class M:
        pass

    return TestClient(LaurenFactory.create(M))


# ---------------------------------------------------------------------------
# Basic subclass passthrough
# ---------------------------------------------------------------------------


class TestSubclassPassthrough:
    def test_jsonapi_body_delivered(self):
        @controller("/api")
        class C:
            @get("/users/{id}")
            async def get_user(self, id: int) -> JsonApiResponse:
                return JsonApiResponse.resource({"id": id, "name": "Alice"})

        r = _build(C).get("/api/users/1")
        assert r.status_code == 200
        payload = json.loads(r.body)
        assert payload == {"data": {"id": 1, "name": "Alice"}}

    def test_jsonapi_content_type(self):
        @controller("/api2")
        class C:
            @get("/")
            async def h(self) -> JsonApiResponse:
                return JsonApiResponse.resource({"id": 1})

        r = _build(C).get("/api2/")
        assert r.header("content-type") == "application/vnd.api+json"

    def test_jsonapi_error_status(self):
        @controller("/apierr")
        class C:
            @get("/")
            async def h(self) -> JsonApiResponse:
                return JsonApiResponse.error_response("Not found", status=404)

        r = _build(C).get("/apierr/")
        assert r.status_code == 404
        payload = json.loads(r.body)
        assert payload["errors"][0]["title"] == "Not found"

    def test_custom_status_via_resource(self):
        @controller("/created")
        class C:
            @post("/")
            async def h(self) -> JsonApiResponse:
                return JsonApiResponse.resource({"id": 99}, status=201)

        r = _build(C).post("/created/")
        assert r.status_code == 201


# ---------------------------------------------------------------------------
# Builder chain on a subclass
# ---------------------------------------------------------------------------


class TestBuilderChainEndToEnd:
    def test_with_header_on_subclass_delivered(self):
        @controller("/hdr")
        class C:
            @get("/")
            async def h(self) -> JsonApiResponse:
                return JsonApiResponse.resource({"id": 1}).with_header("x-trace", "t-001")

        r = _build(C).get("/hdr/")
        assert r.header("x-trace") == "t-001"
        # Content-Type still JSON:API
        assert r.header("content-type") == "application/vnd.api+json"

    def test_chained_builders_all_applied(self):
        @controller("/chain")
        class C:
            @get("/")
            async def h(self) -> JsonApiResponse:
                return (
                    JsonApiResponse.resource({"id": 2})
                    .with_status(202)
                    .with_header("x-a", "alpha")
                    .with_header("x-b", "beta")
                )

        r = _build(C).get("/chain/")
        assert r.status_code == 202
        assert r.header("x-a") == "alpha"
        assert r.header("x-b") == "beta"

    def test_with_cookie_on_subclass(self):
        @controller("/cook")
        class C:
            @get("/")
            async def h(self) -> JsonApiResponse:
                return JsonApiResponse.resource({"id": 3}).with_cookie("token", "abc", http_only=True)

        r = _build(C).get("/cook/")
        assert r.status_code == 200
        cookie = r.header("set-cookie") or ""
        assert "token=abc" in cookie
        assert "HttpOnly" in cookie


# ---------------------------------------------------------------------------
# Extra instance attributes visible in interceptor
# ---------------------------------------------------------------------------


class TestExtraAttributesInInterceptor:
    def test_template_name_readable_in_interceptor(self):
        seen_templates: list[str] = []

        from lauren import injectable
        from lauren.types import ExecutionContext, CallHandler
        from lauren.decorators import use_interceptors

        @injectable()
        class TemplateLogger:
            async def intercept(self, ctx: ExecutionContext, next_handler: CallHandler):
                result = await next_handler.handle()
                if isinstance(result, HtmlTemplateResponse):
                    seen_templates.append(result.template)
                return result

        @controller("/tmpl")
        @use_interceptors(TemplateLogger)
        class C:
            @get("/")
            async def h(self) -> HtmlTemplateResponse:
                return HtmlTemplateResponse("index.html", {"user": "Alice"})

        @module(controllers=[C], providers=[TemplateLogger])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M)).get("/tmpl/")
        assert r.status_code == 200
        assert "text/html" in (r.header("content-type") or "")
        assert seen_templates == ["index.html"]


# ---------------------------------------------------------------------------
# Streaming subclass
# ---------------------------------------------------------------------------


class TestStreamingSubclass:
    def test_csv_body_correct(self):
        @controller("/csv")
        class C:
            @get("/")
            async def h(self) -> CsvResponse:
                return CsvResponse.from_rows([["name", "score"], ["Alice", "95"], ["Bob", "87"]])

        r = _build(C).get("/csv/")
        assert r.status_code == 200
        assert r.body == b"name,score\nAlice,95\nBob,87\n"

    def test_csv_content_type(self):
        @controller("/csvct")
        class C:
            @get("/")
            async def h(self) -> CsvResponse:
                return CsvResponse.from_rows([["a", "b"]])

        r = _build(C).get("/csvct/")
        assert r.header("content-type") == "text/csv"

    def test_csv_content_disposition(self):
        @controller("/csvcd")
        class C:
            @get("/")
            async def h(self) -> CsvResponse:
                return CsvResponse.from_rows([["x"]])

        r = _build(C).get("/csvcd/")
        assert "attachment" in (r.header("content-disposition") or "")


# ---------------------------------------------------------------------------
# Subclass returned from exception handler
# ---------------------------------------------------------------------------


class TestSubclassFromExceptionHandler:
    def test_exception_handler_returns_jsonapi_subclass(self):
        from lauren import exception_handler

        @exception_handler(LookupError)
        class LookupHandler:
            async def catch(self, exc: LookupError, request) -> JsonApiResponse:
                return JsonApiResponse.error_response(str(exc), status=404)

        @controller("/ehtest")
        class C:
            @get("/")
            async def h(self) -> JsonApiResponse:
                raise LookupError("item missing")

        @module(controllers=[C])
        class M:
            pass

        r = TestClient(LaurenFactory.create(M, global_exception_handlers=[LookupHandler])).get("/ehtest/")
        assert r.status_code == 404
        assert r.header("content-type") == "application/vnd.api+json"
        payload = json.loads(r.body)
        assert payload["errors"][0]["title"] == "item missing"


# ---------------------------------------------------------------------------
# HtmlTemplateResponse end-to-end
# ---------------------------------------------------------------------------


class TestHtmlTemplateResponseEndToEnd:
    def test_html_body_rendered(self):
        @controller("/html")
        class C:
            @get("/")
            async def h(self) -> HtmlTemplateResponse:
                return HtmlTemplateResponse("hello", {"name": "World"})

        r = _build(C).get("/html/")
        assert r.status_code == 200
        assert b"<html>" in r.body
        assert b"World" in r.body

    def test_html_content_type(self):
        @controller("/htmlct")
        class C:
            @get("/")
            async def h(self) -> HtmlTemplateResponse:
                return HtmlTemplateResponse("t", {})

        r = _build(C).get("/htmlct/")
        assert "text/html" in (r.header("content-type") or "")
