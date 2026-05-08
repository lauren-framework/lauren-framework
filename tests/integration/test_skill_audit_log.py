"""Integration tests for the audit log trail pattern (Skill 43)."""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from lauren import LaurenFactory, Path, Scope, controller, get, injectable, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class AuditEntry:
    entry_id: str
    timestamp: float
    user_id: str
    action: str
    resource_type: str
    resource_id: str
    changes: dict[str, Any]


# ---------------------------------------------------------------------------
# AuditLogService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class AuditLogService:
    def __init__(self) -> None:
        self._entries: list[AuditEntry] = []

    def record(
        self,
        user_id: str,
        action: str,
        resource_type: str,
        resource_id: str,
        changes: dict,
    ) -> AuditEntry:
        ts = time.time()
        entry_id = hashlib.sha256(
            f"{user_id}{action}{resource_id}{ts}".encode()
        ).hexdigest()[:16]
        entry = AuditEntry(
            entry_id=entry_id,
            timestamp=ts,
            user_id=user_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            changes=changes,
        )
        self._entries.append(entry)
        return entry

    def get_by_user(self, user_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.user_id == user_id]

    def get_by_resource(self, resource_type: str, resource_id: str) -> list[AuditEntry]:
        return [
            e
            for e in self._entries
            if e.resource_type == resource_type and e.resource_id == resource_id
        ]

    def get_all(self) -> list[AuditEntry]:
        return list(self._entries)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


@controller("/audit")
class AuditController:
    def __init__(self, log: AuditLogService) -> None:
        self._log = log

    @get("/")
    async def list_all(self) -> list:
        return [
            {
                "entry_id": e.entry_id,
                "user_id": e.user_id,
                "action": e.action,
                "resource_type": e.resource_type,
                "resource_id": e.resource_id,
                "changes": e.changes,
            }
            for e in self._log.get_all()
        ]

    @get("/user/{user_id}")
    async def by_user(self, user_id: Path[str]) -> list:
        return [
            {
                "entry_id": e.entry_id,
                "user_id": e.user_id,
                "action": e.action,
                "resource_type": e.resource_type,
                "resource_id": e.resource_id,
            }
            for e in self._log.get_by_user(user_id)
        ]

    @get("/resource/{resource_type}/{resource_id}")
    async def by_resource(
        self,
        resource_type: Path[str],
        resource_id: Path[str],
    ) -> list:
        return [
            {
                "entry_id": e.entry_id,
                "action": e.action,
            }
            for e in self._log.get_by_resource(resource_type, resource_id)
        ]


@module(controllers=[AuditController], providers=[AuditLogService])
class AuditModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app():
    return LaurenFactory.create(AuditModule)


def build_client_and_service():
    app = build_app()
    svc = asyncio.run(app.container.resolve(AuditLogService))
    return TestClient(app), svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestAuditLog:
    def test_record_creates_entry(self):
        svc = AuditLogService()
        entry = svc.record("u1", "create", "order", "o1", {"amount": 100})
        assert entry.user_id == "u1"
        assert entry.action == "create"
        assert entry.resource_type == "order"
        assert entry.resource_id == "o1"
        assert entry.changes == {"amount": 100}

    def test_record_returns_unique_entry_id(self):
        svc = AuditLogService()
        e1 = svc.record("u1", "create", "order", "o1", {})
        e2 = svc.record("u1", "update", "order", "o1", {})
        assert e1.entry_id != e2.entry_id

    def test_log_is_append_only(self):
        svc = AuditLogService()
        svc.record("u1", "create", "order", "o1", {})
        svc.record("u1", "update", "order", "o1", {"amount": 200})
        assert len(svc.get_all()) == 2

    def test_get_all_returns_copy(self):
        svc = AuditLogService()
        svc.record("u1", "create", "order", "o1", {})
        all_entries = svc.get_all()
        all_entries.clear()
        assert len(svc.get_all()) == 1  # original unchanged

    def test_get_by_user_filters_correctly(self):
        svc = AuditLogService()
        svc.record("user_a", "create", "order", "o1", {})
        svc.record("user_b", "create", "order", "o2", {})
        svc.record("user_a", "update", "order", "o1", {})
        assert len(svc.get_by_user("user_a")) == 2
        assert len(svc.get_by_user("user_b")) == 1

    def test_get_by_resource_filters_correctly(self):
        svc = AuditLogService()
        svc.record("u1", "create", "order", "o1", {})
        svc.record("u1", "create", "order", "o2", {})
        svc.record("u1", "update", "order", "o1", {"amount": 50})
        result = svc.get_by_resource("order", "o1")
        assert len(result) == 2
        assert all(e.resource_id == "o1" for e in result)

    def test_get_all_empty_when_no_records(self):
        svc = AuditLogService()
        assert svc.get_all() == []

    def test_get_by_user_empty_for_unknown_user(self):
        svc = AuditLogService()
        svc.record("u1", "create", "order", "o1", {})
        assert svc.get_by_user("unknown") == []

    def test_entry_has_timestamp(self):
        svc = AuditLogService()
        before = time.time()
        entry = svc.record("u1", "create", "order", "o1", {})
        after = time.time()
        assert before <= entry.timestamp <= after

    def test_api_list_all_empty(self):
        client, _ = build_client_and_service()
        r = client.get("/audit/")
        assert r.status_code == 200
        assert r.json() == []

    def test_api_list_all_after_record(self):
        client, svc = build_client_and_service()
        svc.record("u1", "create", "order", "o1", {"amount": 100})
        r = client.get("/audit/")
        data = r.json()
        assert len(data) == 1
        assert data[0]["user_id"] == "u1"
        assert data[0]["action"] == "create"

    def test_api_by_user(self):
        client, svc = build_client_and_service()
        svc.record("alice", "create", "doc", "d1", {})
        svc.record("bob", "create", "doc", "d2", {})
        r = client.get("/audit/user/alice")
        data = r.json()
        assert len(data) == 1
        assert data[0]["user_id"] == "alice"

    def test_api_by_resource(self):
        client, svc = build_client_and_service()
        svc.record("u1", "create", "order", "o1", {})
        svc.record("u2", "update", "order", "o1", {})
        svc.record("u3", "create", "order", "o2", {})
        r = client.get("/audit/resource/order/o1")
        data = r.json()
        assert len(data) == 2

    def test_changes_dict_preserved(self):
        svc = AuditLogService()
        changes = {"email": {"from": "old@x.com", "to": "new@x.com"}}
        entry = svc.record("u1", "update", "user", "u99", changes)
        assert entry.changes == changes
