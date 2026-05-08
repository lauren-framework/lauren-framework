"""Integration tests for the centralized config service (Skill 16).

Uses pydantic.BaseModel + from_env() pattern (no pydantic-settings dep needed).
"""

from __future__ import annotations

import os

from pydantic import BaseModel

from lauren import LaurenFactory, Path, Scope, controller, get, injectable, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# AppConfig + ConfigService
# ---------------------------------------------------------------------------


class AppConfig(BaseModel):
    database_url: str = "sqlite:///:memory:"
    secret_key: str = "default-secret"
    debug: bool = False
    api_prefix: str = "/api/v1"

    @classmethod
    def from_env(cls) -> "AppConfig":
        return cls(
            database_url=os.getenv("DATABASE_URL", "sqlite:///:memory:"),
            secret_key=os.getenv("SECRET_KEY", "default-secret"),
            debug=os.getenv("DEBUG", "false").lower() == "true",
            api_prefix=os.getenv("API_PREFIX", "/api/v1"),
        )


@injectable(scope=Scope.SINGLETON)
class ConfigService:
    def __init__(self) -> None:
        self._config = AppConfig.from_env()

    @property
    def config(self) -> AppConfig:
        return self._config

    def get(self, key: str, default=None):
        return getattr(self._config, key, default)


# ---------------------------------------------------------------------------
# Controller that exposes config values
# ---------------------------------------------------------------------------


@controller("/config")
class ConfigController:
    def __init__(self, cfg: ConfigService) -> None:
        self._cfg = cfg

    @get("/")
    async def all_config(self) -> dict:
        return self._cfg.config.model_dump()

    @get("/{key}")
    async def get_key(self, key: Path[str]) -> dict:
        return {"key": key, "value": self._cfg.get(key)}


@module(controllers=[ConfigController], providers=[ConfigService])
class ConfigModule:
    pass


def build_app():
    return TestClient(LaurenFactory.create(ConfigModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestConfigService:
    def test_default_values(self):
        # Ensure no interfering env vars
        for k in ("DATABASE_URL", "SECRET_KEY", "DEBUG", "API_PREFIX"):
            os.environ.pop(k, None)

        svc = ConfigService()
        assert svc.config.database_url == "sqlite:///:memory:"
        assert svc.config.secret_key == "default-secret"
        assert svc.config.debug is False
        assert svc.config.api_prefix == "/api/v1"

    def test_env_var_overrides_default(self):
        os.environ["SECRET_KEY"] = "super-secret"
        os.environ["DEBUG"] = "true"
        try:
            svc = ConfigService()
            assert svc.config.secret_key == "super-secret"
            assert svc.config.debug is True
        finally:
            os.environ.pop("SECRET_KEY", None)
            os.environ.pop("DEBUG", None)

    def test_get_method_returns_attribute(self):
        for k in ("DATABASE_URL", "SECRET_KEY", "DEBUG", "API_PREFIX"):
            os.environ.pop(k, None)
        svc = ConfigService()
        assert svc.get("api_prefix") == "/api/v1"

    def test_get_method_returns_default_for_missing_key(self):
        svc = ConfigService()
        assert svc.get("nonexistent_key", "fallback") == "fallback"

    def test_config_endpoint_returns_all_keys(self):
        for k in ("DATABASE_URL", "SECRET_KEY", "DEBUG", "API_PREFIX"):
            os.environ.pop(k, None)
        client = build_app()
        r = client.get("/config/")
        assert r.status_code == 200
        data = r.json()
        assert "database_url" in data
        assert "secret_key" in data
        assert "debug" in data
