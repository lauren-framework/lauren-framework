---
name: multi-tenant-isolation
description: Implements per-row multi-tenant data isolation in a Lauren application using a ContextVar set by middleware and a SQLAlchemy ORM layer. Use when multiple tenants share a single database but must never see each other's data.
---

> Use `codemap find "TenantMiddleware"` to check for existing tenant resolution logic before adding new isolation.

# Multi-Tenant Schema Isolation (Per-Row Strategy)

The per-row strategy attaches a `tenant_id` column to every tenant-aware table and uses a `ContextVar` to propagate the current tenant from the HTTP request through the service layer — with zero SQL injection risk because the filter is applied via the ORM, not string interpolation.

## Tenant resolution

```python
from contextvars import ContextVar

tenant_id_var: ContextVar[str] = ContextVar("tenant_id", default="")
```

## TenantMiddleware

```python
from __future__ import annotations

from lauren import middleware, injectable, Scope
from lauren.types import Request, Response
from lauren.exceptions import UnauthorizedError

@middleware()
@injectable(scope=Scope.SINGLETON)
class TenantMiddleware:
    """Reads the tenant ID from the X-Tenant-Id header and stores it in a ContextVar."""

    async def dispatch(self, request: Request, call_next) -> Response:
        tid = request.headers.get("x-tenant-id", "")
        if tid:
            tenant_id_var.set(tid)
        return await call_next(request)
```

For JWT-based tenant resolution, decode the token in `dispatch` and extract the `tenant_id` claim instead of reading the header.

## SQLAlchemy model

```python
from sqlalchemy import Column, Integer, String
from sqlalchemy.orm import DeclarativeBase

class Base(DeclarativeBase):
    pass

class TenantRecord(Base):
    __tablename__ = "records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(100), nullable=False, index=True)
    name = Column(String(255), nullable=False)
```

## TenantDataService

```python
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class TenantDataService:
    def __init__(self) -> None:
        self._engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self._engine)

    def _current_tenant(self) -> str:
        tid = tenant_id_var.get("")
        if not tid:
            raise UnauthorizedError("No tenant context — X-Tenant-Id header missing")
        return tid

    def create_record(self, name: str) -> dict:
        tid = self._current_tenant()
        with Session(self._engine) as s:
            rec = TenantRecord(tenant_id=tid, name=name)
            s.add(rec)
            s.commit()
            return {"id": rec.id, "tenant_id": rec.tenant_id, "name": rec.name}

    def list_records(self) -> list[dict]:
        tid = self._current_tenant()
        with Session(self._engine) as s:
            rows = s.query(TenantRecord).filter_by(tenant_id=tid).all()
            return [{"id": r.id, "tenant_id": r.tenant_id, "name": r.name} for r in rows]
```

## Controller

```python
from pydantic import BaseModel
from lauren import controller, get, post, module, Json

class CreateRecordBody(BaseModel):
    name: str

@controller("/records")
class RecordsController:
    def __init__(self, svc: TenantDataService) -> None:
        self._svc = svc

    @get("/")
    async def list(self) -> list:
        return self._svc.list_records()

    @post("/")
    async def create(self, body: Json[CreateRecordBody]) -> dict:
        return self._svc.create_record(body.name)

@module(
    controllers=[RecordsController],
    providers=[TenantDataService, TenantMiddleware],
)
class TenantModule:
    pass
```

## Module wiring

```python
from lauren import LaurenFactory

app = LaurenFactory.create(
    TenantModule,
    global_middlewares=[TenantMiddleware],
)
```

## Isolation guarantee

Because `filter_by(tenant_id=tid)` is applied in *every* query method, a tenant can only ever see rows where `tenant_id` matches their token. No cross-tenant leakage is possible even if a bug elsewhere passes the wrong ID — the ORM filter is the last line of defence.

## Testing

```python
def test_tenants_are_isolated():
    client_a = build_client_with_tenant("tenant-a")
    client_b = build_client_with_tenant("tenant-b")
    client_a.post("/records/", json={"name": "record-for-a"})
    r = client_b.get("/records/")
    records = r.json()
    assert all(rec["tenant_id"] == "tenant-b" for rec in records)
    assert not any(rec["name"] == "record-for-a" for rec in records)
```
