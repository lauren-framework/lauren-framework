"""Integration tests for dynamic configuration hot-reload (Skill 20).

Tests verify that POST /admin/config updates values visible via GET /admin/config.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from lauren import Json, LaurenFactory, Scope, controller, get, injectable, module
from lauren import post
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# DynamicConfigService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class DynamicConfigService:
    def __init__(self) -> None:
        self._config: dict = {
            "maintenance_mode": False,
            "max_request_size": 10_485_760,
            "allowed_origins": "*",
        }
        self._lock = asyncio.Lock()

    async def update(self, key: str, value) -> None:
        async with self._lock:
            self._config[key] = value

    def get(self, key: str, default=None):
        return self._config.get(key, default)

    def all(self) -> dict:
        return dict(self._config)


# ---------------------------------------------------------------------------
# Request body + controller
# ---------------------------------------------------------------------------


class ConfigUpdateBody(BaseModel):
    key: str
    value: str | int | bool | float


@controller("/admin/config")
class ConfigController:
    def __init__(self, cfg: DynamicConfigService) -> None:
        self._cfg = cfg

    @get("/")
    async def get_config(self) -> dict:
        return self._cfg.all()

    @post("/")
    async def update_config(self, body: Json[ConfigUpdateBody]) -> dict:
        await self._cfg.update(body.key, body.value)
        return {"updated": body.key, "value": body.value}


@module(controllers=[ConfigController], providers=[DynamicConfigService])
class ConfigModule:
    pass


def build_app():
    return TestClient(LaurenFactory.create(ConfigModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestDynamicConfigReload:
    def test_get_config_returns_defaults(self):
        client = build_app()
        r = client.get("/admin/config/")
        assert r.status_code == 200
        data = r.json()
        assert data["maintenance_mode"] is False
        assert data["max_request_size"] == 10_485_760

    def test_update_string_value(self):
        client = build_app()
        r = client.post(
            "/admin/config/",
            json={"key": "allowed_origins", "value": "https://myapp.com"},
        )
        assert r.status_code == 200
        assert r.json()["updated"] == "allowed_origins"
        # Verify GET reflects the change
        r2 = client.get("/admin/config/")
        assert r2.json()["allowed_origins"] == "https://myapp.com"

    def test_update_boolean_value(self):
        client = build_app()
        client.post("/admin/config/", json={"key": "maintenance_mode", "value": True})
        r = client.get("/admin/config/")
        assert r.json()["maintenance_mode"] is True

    def test_update_integer_value(self):
        client = build_app()
        client.post("/admin/config/", json={"key": "max_request_size", "value": 1024})
        r = client.get("/admin/config/")
        assert r.json()["max_request_size"] == 1024

    def test_multiple_updates_accumulate(self):
        client = build_app()
        client.post("/admin/config/", json={"key": "maintenance_mode", "value": True})
        client.post(
            "/admin/config/", json={"key": "allowed_origins", "value": "localhost"}
        )
        r = client.get("/admin/config/")
        cfg = r.json()
        assert cfg["maintenance_mode"] is True
        assert cfg["allowed_origins"] == "localhost"

    def test_returns_copy_not_reference(self):
        """all() must return a copy so mutations don't affect internal state."""
        svc = DynamicConfigService()
        snapshot = svc.all()
        snapshot["maintenance_mode"] = True
        assert svc.get("maintenance_mode") is False
