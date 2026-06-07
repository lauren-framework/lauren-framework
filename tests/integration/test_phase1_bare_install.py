"""Phase 1 integration tests: pydantic is optional at install time."""

from __future__ import annotations

import sys

import pytest


def test_pydantic_absence_does_not_break_app_startup(monkeypatch):
    """An App with no pydantic body params must start even when pydantic is absent."""
    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]

    # Clear cached imports of the validation module so the monkeypatch takes effect
    for key in list(sys.modules):
        if "lauren._validation" in key:
            del sys.modules[key]

    from lauren import LaurenFactory, controller, get, module

    @controller("/ping")
    class PingController:
        @get("/")
        async def ping(self) -> dict:
            return {"ok": True}

    @module(controllers=[PingController])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)
    assert app is not None


def test_pydantic_absence_allows_plain_json_response(monkeypatch):
    """Routes returning plain dicts work without pydantic."""
    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]

    for key in list(sys.modules):
        if "lauren._validation" in key:
            del sys.modules[key]

    from lauren import LaurenFactory, controller, get, module
    from lauren.testing import TestClient

    @controller("/health")
    class HealthController:
        @get("/")
        async def health(self) -> dict:
            return {"status": "ok"}

    @module(controllers=[HealthController])
    class HealthModule:
        pass

    client = TestClient(LaurenFactory.create(HealthModule))
    resp = client.get("/health/")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_pyproject_optional_extras_declared():
    """Verify that pydantic / msgspec / full extras are present in package metadata."""
    import importlib.metadata as meta

    try:
        dist = meta.distribution("lauren")
    except meta.PackageNotFoundError:
        pytest.skip("Package not installed as a distribution — running from source")

    extras = dist.metadata.get_all("Provides-Extra") or []
    assert "pydantic" in extras, f"'pydantic' extra missing; found: {extras}"
    assert "msgspec" in extras, f"'msgspec' extra missing; found: {extras}"
    assert "full" in extras, f"'full' extra missing; found: {extras}"
