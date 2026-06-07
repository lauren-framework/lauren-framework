"""Phase 1 e2e tests: full app lifecycle with and without pydantic installed."""

from __future__ import annotations

import dataclasses

import pytest

from lauren import Json, LaurenFactory, controller, get, module, post
from lauren.testing import TestClient


@dataclasses.dataclass
class Payload:
    value: str


@controller("/api")
class ApiController:
    @get("/health")
    async def health(self) -> dict:
        return {"healthy": True}

    @post("/echo")
    async def echo(self, body: Json[Payload]) -> dict:
        return {"echoed": body.value}


@module(controllers=[ApiController])
class ApiModule:
    pass


@pytest.fixture(scope="module")
def client():
    return TestClient(LaurenFactory.create(ApiModule))


class TestHealthEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_body_is_healthy(self, client):
        assert client.get("/api/health").json() == {"healthy": True}


class TestEchoEndpoint:
    def test_echo_happy_path(self, client):
        resp = client.post("/api/echo", json={"value": "hello"})
        assert resp.status_code == 200
        assert resp.json() == {"echoed": "hello"}

    def test_echo_missing_required_returns_422(self, client):
        resp = client.post("/api/echo", json={})
        assert resp.status_code == 422

    def test_echo_extra_fields_ignored(self, client):
        resp = client.post("/api/echo", json={"value": "world", "extra": "ignored"})
        assert resp.status_code == 200
        assert resp.json()["echoed"] == "world"
