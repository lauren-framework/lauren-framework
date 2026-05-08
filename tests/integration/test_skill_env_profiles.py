"""Integration tests for environment profile merging (Skill 19).

Uses APP_ENV env var to select profile. Pure Python — no external deps.
"""

from __future__ import annotations

import os
from typing import Any

from lauren import LaurenFactory, Scope, controller, get, injectable, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Config data
# ---------------------------------------------------------------------------

BASE_CONFIG: dict[str, Any] = {
    "debug": False,
    "log_level": "INFO",
    "database_pool_size": 10,
    "cache_ttl": 300,
}

PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    "development": {
        "debug": True,
        "log_level": "DEBUG",
        "database_pool_size": 2,
    },
    "staging": {
        "log_level": "WARNING",
        "database_pool_size": 5,
    },
    "production": {
        "database_pool_size": 20,
        "cache_ttl": 3600,
    },
}


# ---------------------------------------------------------------------------
# ProfileConfigService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class ProfileConfigService:
    def __init__(self) -> None:
        profile = os.getenv("APP_ENV", "development")
        self._config: dict[str, Any] = {
            **BASE_CONFIG,
            **PROFILE_OVERRIDES.get(profile, {}),
        }
        self._profile = profile

    @property
    def profile(self) -> str:
        return self._profile

    def get(self, key: str, default: Any = None) -> Any:
        return self._config.get(key, default)

    def all(self) -> dict[str, Any]:
        return dict(self._config)


# ---------------------------------------------------------------------------
# Controller + module
# ---------------------------------------------------------------------------


@controller("/profile")
class ProfileController:
    def __init__(self, cfg: ProfileConfigService) -> None:
        self._cfg = cfg

    @get("/")
    async def current(self) -> dict:
        return {"profile": self._cfg.profile, "config": self._cfg.all()}


@module(controllers=[ProfileController], providers=[ProfileConfigService])
class ProfileModule:
    pass


def build_app_for(profile: str) -> TestClient:
    os.environ["APP_ENV"] = profile
    try:
        return TestClient(LaurenFactory.create(ProfileModule))
    finally:
        os.environ.pop("APP_ENV", None)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnvironmentProfiles:
    def setup_method(self):
        os.environ.pop("APP_ENV", None)

    def test_development_profile_enables_debug(self):
        svc = _make_svc("development")
        assert svc.get("debug") is True
        assert svc.get("log_level") == "DEBUG"
        assert svc.get("database_pool_size") == 2

    def test_production_profile_overrides_pool_size(self):
        svc = _make_svc("production")
        assert svc.get("debug") is False
        assert svc.get("database_pool_size") == 20
        assert svc.get("cache_ttl") == 3600

    def test_staging_profile_uses_warning_log_level(self):
        svc = _make_svc("staging")
        assert svc.get("log_level") == "WARNING"
        assert svc.get("database_pool_size") == 5
        # Inherits base cache_ttl
        assert svc.get("cache_ttl") == 300

    def test_unknown_profile_falls_back_to_base(self):
        svc = _make_svc("canary")
        assert svc.get("debug") is False
        assert svc.get("database_pool_size") == 10

    def test_all_returns_merged_dict(self):
        svc = _make_svc("development")
        cfg = svc.all()
        assert "debug" in cfg
        assert "log_level" in cfg
        assert cfg["log_level"] == "DEBUG"

    def test_controller_returns_profile_name(self):
        client = build_app_for("production")
        r = client.get("/profile/")
        assert r.status_code == 200
        data = r.json()
        assert data["profile"] == "production"
        assert data["config"]["cache_ttl"] == 3600


def _make_svc(profile: str) -> ProfileConfigService:
    os.environ["APP_ENV"] = profile
    try:
        return ProfileConfigService()
    finally:
        os.environ.pop("APP_ENV", None)
