"""Integration tests for OpenTelemetry distributed tracing (Skill 41).

Each test creates a fresh TracingService and TracerProvider to avoid
global-state interference between test cases.
"""

from __future__ import annotations

import asyncio

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from lauren import LaurenFactory, Scope, controller, get, injectable, middleware, module
from lauren.testing import TestClient
from lauren.types import Request, Response


# ---------------------------------------------------------------------------
# TracingService (local provider — no global state mutation)
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class TracingService:
    """Owns the TracerProvider and in-memory exporter."""

    def __init__(self) -> None:
        self._exporter = InMemorySpanExporter()
        self._provider = TracerProvider()
        self._provider.add_span_processor(SimpleSpanProcessor(self._exporter))
        self._tracer = self._provider.get_tracer(__name__)

    def get_tracer(self):
        return self._tracer

    def get_finished_spans(self):
        return self._exporter.get_finished_spans()

    def clear_spans(self) -> None:
        self._exporter.clear()


# ---------------------------------------------------------------------------
# TracingMiddleware
# ---------------------------------------------------------------------------


@middleware()
@injectable(scope=Scope.SINGLETON)
class TracingMiddleware:
    """Opens a span per HTTP request."""

    def __init__(self, tracing: TracingService) -> None:
        self._tracing = tracing

    async def dispatch(self, request: Request, call_next) -> Response:
        tracer = self._tracing.get_tracer()
        span_name = f"{request.method} {request.path}"
        with tracer.start_as_current_span(span_name) as span:
            span.set_attribute("http.method", request.method)
            span.set_attribute("http.target", request.path)
            response = await call_next(request)
            span.set_attribute("http.status_code", response.status)
            return response


# ---------------------------------------------------------------------------
# Application controllers
# ---------------------------------------------------------------------------


@controller("/api")
class ApiController:
    @get("/hello")
    async def hello(self) -> dict:
        return {"message": "hello"}

    @get("/items/{item_id}")
    async def get_item(self) -> dict:
        return {"item": "found"}


@module(
    controllers=[ApiController],
    providers=[TracingService, TracingMiddleware],
)
class TracingAppModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_client_and_service() -> tuple[TestClient, TracingService]:
    """Build a fresh app and return both the client and the TracingService."""
    app = LaurenFactory.create(
        TracingAppModule,
        global_middlewares=[TracingMiddleware],
    )
    # Retrieve the singleton TracingService from DI
    svc: TracingService = asyncio.run(app.container.resolve(TracingService))
    return TestClient(app), svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestOtelTracing:
    def test_span_created_for_request(self):
        client, svc = _build_client_and_service()
        r = client.get("/api/hello")
        assert r.status_code == 200
        spans = svc.get_finished_spans()
        assert len(spans) >= 1

    def test_span_name_includes_method_and_path(self):
        client, svc = _build_client_and_service()
        client.get("/api/hello")
        spans = svc.get_finished_spans()
        names = [s.name for s in spans]
        assert any("GET" in n and "/api/hello" in n for n in names)

    def test_span_has_http_method_attribute(self):
        client, svc = _build_client_and_service()
        client.get("/api/hello")
        spans = svc.get_finished_spans()
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("http.method") == "GET"

    def test_span_has_status_code_attribute(self):
        client, svc = _build_client_and_service()
        client.get("/api/hello")
        spans = svc.get_finished_spans()
        attrs = dict(spans[0].attributes or {})
        assert attrs.get("http.status_code") == 200

    def test_multiple_requests_create_multiple_spans(self):
        client, svc = _build_client_and_service()
        client.get("/api/hello")
        client.get("/api/hello")
        spans = svc.get_finished_spans()
        assert len(spans) >= 2

    def test_clear_spans(self):
        client, svc = _build_client_and_service()
        client.get("/api/hello")
        assert len(svc.get_finished_spans()) >= 1
        svc.clear_spans()
        assert len(svc.get_finished_spans()) == 0

    def test_span_target_attribute(self):
        client, svc = _build_client_and_service()
        client.get("/api/hello")
        spans = svc.get_finished_spans()
        attrs = dict(spans[0].attributes or {})
        assert "/api/hello" in attrs.get("http.target", "")

    def test_get_tracer_returns_tracer(self):
        svc = TracingService()
        tracer = svc.get_tracer()
        assert tracer is not None

    def test_fresh_service_has_no_spans(self):
        svc = TracingService()
        assert svc.get_finished_spans() == ()

    def test_response_body_correct(self):
        client, svc = _build_client_and_service()
        r = client.get("/api/hello")
        assert r.json() == {"message": "hello"}
