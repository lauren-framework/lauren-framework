"""Integration tests for GDPR data subject request handling (Skill 46)."""

from __future__ import annotations

import asyncio
import json as jsonlib

from lauren import (
    LaurenFactory,
    Path,
    Scope,
    controller,
    delete,
    get,
    injectable,
    module,
)
from lauren.testing import TestClient
from lauren.types import Response


# ---------------------------------------------------------------------------
# UserDataRegistry
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class UserDataRegistry:
    def __init__(self) -> None:
        self._profiles: dict[str, dict] = {}
        self._orders: dict[str, list] = {}

    def register_profile(self, user_id: str, data: dict) -> None:
        self._profiles[user_id] = data

    def register_order(self, user_id: str, order: dict) -> None:
        self._orders.setdefault(user_id, []).append(order)

    def export_user(self, user_id: str) -> dict:
        return {
            "user_id": user_id,
            "profile": self._profiles.get(user_id, {}),
            "orders": self._orders.get(user_id, []),
        }

    def delete_user(self, user_id: str) -> dict[str, int]:
        deleted: dict[str, int] = {
            "profiles": 1 if user_id in self._profiles else 0,
            "orders": len(self._orders.get(user_id, [])),
        }
        self._profiles.pop(user_id, None)
        self._orders.pop(user_id, None)
        return deleted

    def has_profile(self, user_id: str) -> bool:
        return user_id in self._profiles


# ---------------------------------------------------------------------------
# DataSubjectService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class DataSubjectService:
    def __init__(self, registry: UserDataRegistry) -> None:
        self._registry = registry

    def export(self, user_id: str) -> bytes:
        data = self._registry.export_user(user_id)
        return jsonlib.dumps(data, indent=2).encode("utf-8")

    def delete(self, user_id: str) -> dict:
        return self._registry.delete_user(user_id)


# ---------------------------------------------------------------------------
# GDPRController
# ---------------------------------------------------------------------------


@controller("/gdpr")
class GDPRController:
    def __init__(self, svc: DataSubjectService) -> None:
        self._svc = svc

    @get("/export/{user_id}")
    async def export_data(self, user_id: Path[str]) -> Response:
        data = self._svc.export(user_id)
        return Response(body=data, media_type="application/json").with_header(
            "Content-Disposition", f"attachment; filename=user_{user_id}_data.json"
        )

    @delete("/delete/{user_id}")
    async def delete_data(self, user_id: Path[str]) -> dict:
        result = self._svc.delete(user_id)
        return {"user_id": user_id, "deleted": result}


@module(
    controllers=[GDPRController],
    providers=[DataSubjectService, UserDataRegistry],
)
class GDPRModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app():
    return LaurenFactory.create(GDPRModule)


def build_client_and_registry():
    app = build_app()
    registry = asyncio.run(app.container.resolve(UserDataRegistry))
    return TestClient(app), registry


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGDPRExport:
    def test_export_returns_200(self):
        client, _ = build_client_and_registry()
        r = client.get("/gdpr/export/user1")
        assert r.status_code == 200

    def test_export_contains_user_id(self):
        client, _ = build_client_and_registry()
        r = client.get("/gdpr/export/user1")
        data = jsonlib.loads(r.body)
        assert data["user_id"] == "user1"

    def test_export_contains_profile(self):
        client, registry = build_client_and_registry()
        registry.register_profile("alice", {"name": "Alice", "email": "alice@example.com"})
        r = client.get("/gdpr/export/alice")
        data = jsonlib.loads(r.body)
        assert data["profile"]["name"] == "Alice"
        assert data["profile"]["email"] == "alice@example.com"

    def test_export_contains_orders(self):
        client, registry = build_client_and_registry()
        registry.register_profile("bob", {"name": "Bob"})
        registry.register_order("bob", {"id": "o1", "amount": 99})
        registry.register_order("bob", {"id": "o2", "amount": 49})
        r = client.get("/gdpr/export/bob")
        data = jsonlib.loads(r.body)
        assert len(data["orders"]) == 2

    def test_export_empty_user_returns_empty_data(self):
        client, _ = build_client_and_registry()
        r = client.get("/gdpr/export/nonexistent")
        data = jsonlib.loads(r.body)
        assert data["profile"] == {}
        assert data["orders"] == []

    def test_export_content_disposition_header(self):
        client, _ = build_client_and_registry()
        r = client.get("/gdpr/export/user42")
        disposition = r.header("content-disposition") or ""
        assert "user_user42_data.json" in disposition

    def test_export_is_valid_json(self):
        client, registry = build_client_and_registry()
        registry.register_profile("carol", {"name": "Carol"})
        r = client.get("/gdpr/export/carol")
        parsed = jsonlib.loads(r.body)
        assert isinstance(parsed, dict)


class TestGDPRDelete:
    def test_delete_returns_200(self):
        client, _ = build_client_and_registry()
        r = client.delete("/gdpr/delete/user1")
        assert r.status_code == 200

    def test_delete_returns_user_id(self):
        client, _ = build_client_and_registry()
        r = client.delete("/gdpr/delete/alice")
        assert r.json()["user_id"] == "alice"

    def test_delete_removes_profile(self):
        client, registry = build_client_and_registry()
        registry.register_profile("alice", {"name": "Alice"})
        client.delete("/gdpr/delete/alice")
        assert not registry.has_profile("alice")

    def test_delete_returns_deleted_counts(self):
        client, registry = build_client_and_registry()
        registry.register_profile("bob", {"name": "Bob"})
        registry.register_order("bob", {"id": "o1"})
        registry.register_order("bob", {"id": "o2"})
        r = client.delete("/gdpr/delete/bob")
        deleted = r.json()["deleted"]
        assert deleted["profiles"] == 1
        assert deleted["orders"] == 2

    def test_delete_nonexistent_user_returns_zeros(self):
        client, _ = build_client_and_registry()
        r = client.delete("/gdpr/delete/ghost")
        deleted = r.json()["deleted"]
        assert deleted["profiles"] == 0
        assert deleted["orders"] == 0

    def test_delete_then_export_returns_empty(self):
        client, registry = build_client_and_registry()
        registry.register_profile("carol", {"name": "Carol", "email": "carol@x.com"})
        client.delete("/gdpr/delete/carol")
        r = client.get("/gdpr/export/carol")
        data = jsonlib.loads(r.body)
        assert data["profile"] == {}

    def test_data_subject_service_export_bytes(self):
        app = build_app()
        registry = asyncio.run(app.container.resolve(UserDataRegistry))
        svc = asyncio.run(app.container.resolve(DataSubjectService))
        registry.register_profile("dave", {"ssn": "123-45-6789"})
        result = svc.export("dave")
        assert isinstance(result, bytes)
        parsed = jsonlib.loads(result)
        assert parsed["profile"]["ssn"] == "123-45-6789"
