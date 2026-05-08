"""Integration tests for multi-tenant row-level isolation (Skill 48)."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar

from sqlalchemy import Column, Integer, String, create_engine
from sqlalchemy.orm import DeclarativeBase, Session

from lauren import (
    LaurenFactory,
    Json,
    Scope,
    controller,
    get,
    injectable,
    middleware,
    module,
    post,
)
from lauren.exceptions import UnauthorizedError
from lauren.testing import TestClient
from lauren.types import Request, Response


# ---------------------------------------------------------------------------
# Tenant ContextVar
# ---------------------------------------------------------------------------

tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="")


# ---------------------------------------------------------------------------
# SQLAlchemy model
# ---------------------------------------------------------------------------


class Base(DeclarativeBase):
    pass


class TenantRecord(Base):
    __tablename__ = "records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)


# ---------------------------------------------------------------------------
# TenantMiddleware
# ---------------------------------------------------------------------------


@middleware()
@injectable(scope=Scope.SINGLETON)
class TenantMiddleware:
    async def dispatch(self, request: Request, call_next) -> Response:
        tid = request.headers.get("x-tenant-id", "")
        if tid:
            token = tenant_id_var.set(tid)
            try:
                return await call_next(request)
            finally:
                tenant_id_var.reset(token)
        return await call_next(request)


# ---------------------------------------------------------------------------
# TenantDataService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class TenantDataService:
    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self._engine)

    def _current_tenant(self) -> str:
        tid = tenant_id_var.get("")
        if not tid:
            raise UnauthorizedError("No tenant context")
        return tid

    def create_record(self, name: str, tenant_id: str | None = None) -> dict:
        tid = tenant_id or self._current_tenant()
        with Session(self._engine) as s:
            rec = TenantRecord(tenant_id=tid, name=name)
            s.add(rec)
            s.commit()
            s.refresh(rec)
            return {"id": rec.id, "tenant_id": rec.tenant_id, "name": rec.name}

    def list_records(self, tenant_id: str | None = None) -> list[dict]:
        tid = tenant_id or self._current_tenant()
        with Session(self._engine) as s:
            rows = s.query(TenantRecord).filter_by(tenant_id=tid).all()
            return [
                {"id": r.id, "tenant_id": r.tenant_id, "name": r.name} for r in rows
            ]

    def count_all(self) -> int:
        with Session(self._engine) as s:
            return s.query(TenantRecord).count()


# ---------------------------------------------------------------------------
# Controller + Pydantic model
# ---------------------------------------------------------------------------

from pydantic import BaseModel


class CreateRecordBody(BaseModel):
    name: str


@controller("/records")
class RecordsController:
    def __init__(self, svc: TenantDataService) -> None:
        self._svc = svc

    @get("/")
    async def list_records(self) -> list:
        return self._svc.list_records()

    @post("/")
    async def create_record(self, body: Json[CreateRecordBody]) -> dict:
        return self._svc.create_record(body.name)


@module(
    controllers=[RecordsController],
    providers=[TenantDataService, TenantMiddleware],
)
class TenantModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app():
    return LaurenFactory.create(
        TenantModule,
        global_middlewares=[TenantMiddleware],
    )


def tenant_headers(tid: str) -> dict:
    return {"x-tenant-id": tid}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestMultiTenantIsolation:
    def test_create_record_for_tenant(self):
        app = build_app()
        client = TestClient(app)
        r = client.post(
            "/records/", json={"name": "Widget"}, headers=tenant_headers("tenant-a")
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Widget"
        assert data["tenant_id"] == "tenant-a"

    def test_list_records_returns_only_own_tenant(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(TenantDataService))
        svc.create_record("record-for-a", tenant_id="tenant-a")
        svc.create_record("record-for-b", tenant_id="tenant-b")
        client = TestClient(app)
        r = client.get("/records/", headers=tenant_headers("tenant-a"))
        records = r.json()
        assert all(rec["tenant_id"] == "tenant-a" for rec in records)
        assert not any(rec["name"] == "record-for-b" for rec in records)

    def test_tenants_do_not_see_each_others_data(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(TenantDataService))
        svc.create_record("a-exclusive", tenant_id="tenant-a")
        svc.create_record("b-exclusive", tenant_id="tenant-b")
        client = TestClient(app)
        r_a = client.get("/records/", headers=tenant_headers("tenant-a"))
        r_b = client.get("/records/", headers=tenant_headers("tenant-b"))
        names_a = {rec["name"] for rec in r_a.json()}
        names_b = {rec["name"] for rec in r_b.json()}
        assert "a-exclusive" in names_a
        assert "b-exclusive" not in names_a
        assert "b-exclusive" in names_b
        assert "a-exclusive" not in names_b

    def test_multiple_records_same_tenant(self):
        app = build_app()
        client = TestClient(app)
        client.post(
            "/records/", json={"name": "r1"}, headers=tenant_headers("tenant-x")
        )
        client.post(
            "/records/", json={"name": "r2"}, headers=tenant_headers("tenant-x")
        )
        r = client.get("/records/", headers=tenant_headers("tenant-x"))
        assert len(r.json()) == 2

    def test_shared_database_all_records_stored(self):
        app = build_app()
        svc = asyncio.run(app.container.resolve(TenantDataService))
        svc.create_record("r1", tenant_id="t1")
        svc.create_record("r2", tenant_id="t2")
        svc.create_record("r3", tenant_id="t3")
        # All three records in the same physical table
        assert svc.count_all() == 3

    def test_no_tenant_header_raises_unauthorized(self):
        app = build_app()
        client = TestClient(app)
        r = client.get("/records/")
        assert r.status_code == 401

    def test_tenant_service_create_with_explicit_id(self):
        svc = TenantDataService()
        rec = svc.create_record("explicit", tenant_id="t42")
        assert rec["tenant_id"] == "t42"
        assert rec["name"] == "explicit"

    def test_tenant_service_list_with_explicit_id(self):
        svc = TenantDataService()
        svc.create_record("alpha", tenant_id="ta")
        svc.create_record("beta", tenant_id="tb")
        records = svc.list_records(tenant_id="ta")
        assert len(records) == 1
        assert records[0]["name"] == "alpha"

    def test_contextvar_resets_after_request(self):
        # After a request, the ContextVar should not leak between requests
        app = build_app()
        client = TestClient(app)
        client.post(
            "/records/",
            json={"name": "cross-test"},
            headers=tenant_headers("tenant-leak"),
        )
        # A second request with a different tenant should not see tenant-leak records
        r = client.get("/records/", headers=tenant_headers("tenant-clean"))
        records = r.json()
        assert not any(rec["name"] == "cross-test" for rec in records)
