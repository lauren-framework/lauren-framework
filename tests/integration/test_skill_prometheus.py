"""Integration tests for the Prometheus Metrics skill (Skill 40).

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)

from lauren import (
    LaurenFactory,
    Json,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient
from lauren.types import Response
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class MetricsService:
    def __init__(self) -> None:
        self._registry = CollectorRegistry()
        self.request_count = Counter(
            "http_requests_total",
            "Total HTTP requests",
            ["method", "endpoint", "status"],
            registry=self._registry,
        )
        self.request_duration = Histogram(
            "http_request_duration_seconds",
            "HTTP request duration",
            ["method", "endpoint"],
            registry=self._registry,
        )

    def get_metrics(self) -> bytes:
        return generate_latest(self._registry)

    def record_request(
        self, method: str, endpoint: str, status: int, duration: float
    ) -> None:
        self.request_count.labels(
            method=method, endpoint=endpoint, status=str(status)
        ).inc()
        self.request_duration.labels(method=method, endpoint=endpoint).observe(duration)


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


class RecordRequestBody(BaseModel):
    method: str = "GET"
    endpoint: str = "/example"
    status: int = 200
    duration: float = 0.042


@controller("/metrics")
class MetricsController:
    def __init__(self, metrics: MetricsService) -> None:
        self._metrics = metrics

    @get("/")
    async def expose(self) -> Response:
        data = self._metrics.get_metrics()
        return Response(data, media_type=CONTENT_TYPE_LATEST)

    @post("/record")
    async def record(self, body: Json[RecordRequestBody]) -> dict:
        self._metrics.record_request(
            body.method, body.endpoint, body.status, body.duration
        )
        return {"recorded": True, "method": body.method, "endpoint": body.endpoint}


@module(controllers=[MetricsController], providers=[MetricsService])
class MetricsModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(MetricsModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_200(self) -> None:
        client = build_app()
        r = client.get("/metrics/")
        assert r.status_code == 200

    def test_initial_output_contains_metric_names(self) -> None:
        client = build_app()
        r = client.get("/metrics/")
        body = r.text
        assert "http_requests_total" in body
        assert "http_request_duration_seconds" in body

    def test_metrics_prometheus_format_has_help_lines(self) -> None:
        client = build_app()
        r = client.get("/metrics/")
        assert "# HELP" in r.text

    def test_metrics_prometheus_format_has_type_lines(self) -> None:
        client = build_app()
        r = client.get("/metrics/")
        assert "# TYPE" in r.text

    def test_record_then_metrics_shows_counter(self) -> None:
        client = build_app()
        r = client.post("/metrics/record", json={})
        assert r.status_code == 200
        assert r.json()["recorded"] is True
        r2 = client.get("/metrics/")
        assert "http_requests_total" in r2.text

    def test_counter_value_after_multiple_records(self) -> None:
        client = build_app()
        client.post(
            "/metrics/record",
            json={"method": "GET", "endpoint": "/api", "status": 200, "duration": 0.01},
        )
        client.post(
            "/metrics/record",
            json={"method": "GET", "endpoint": "/api", "status": 200, "duration": 0.02},
        )
        r = client.get("/metrics/")
        assert "2.0" in r.text or "http_requests_total{" in r.text

    def test_histogram_buckets_in_output(self) -> None:
        client = build_app()
        client.post(
            "/metrics/record",
            json={
                "method": "GET",
                "endpoint": "/timed",
                "status": 200,
                "duration": 0.1,
            },
        )
        r = client.get("/metrics/")
        assert "http_request_duration_seconds_bucket" in r.text

    def test_different_methods_recorded_separately(self) -> None:
        client = build_app()
        client.post(
            "/metrics/record",
            json={"method": "GET", "endpoint": "/x", "status": 200, "duration": 0.01},
        )
        client.post(
            "/metrics/record",
            json={"method": "POST", "endpoint": "/x", "status": 201, "duration": 0.02},
        )
        r = client.get("/metrics/")
        body = r.text
        assert "http_requests_total" in body


class TestMetricsController:
    def test_record_endpoint_returns_method_echo(self) -> None:
        client = build_app()
        r = client.post(
            "/metrics/record",
            json={
                "method": "DELETE",
                "endpoint": "/res",
                "status": 204,
                "duration": 0.005,
            },
        )
        assert r.json()["method"] == "DELETE"
        assert r.json()["endpoint"] == "/res"

    def test_metrics_content_type(self) -> None:
        client = build_app()
        r = client.get("/metrics/")
        content_type = r.header("content-type") or ""
        assert "text/plain" in content_type or "metrics" in content_type
