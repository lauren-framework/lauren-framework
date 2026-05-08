---
name: gdpr-data-requests
description: Implements GDPR Article 15 (data export) and Article 17 (right to erasure) request handling in a Lauren application. Use when you need structured /gdpr/export and /gdpr/delete endpoints that aggregate PII from multiple data stores and return or purge it on request.
---

> Use `codemap find "DataSubjectService"` to check for an existing GDPR handler before adding one.

# GDPR Data Subject Request Handler (Export / Delete)

The pattern uses three components:

1. **`UserDataRegistry`** — owns all in-memory (or DB-backed) user data stores.
2. **`DataSubjectService`** — orchestrates export and delete across all stores.
3. **`GDPRController`** — exposes the HTTP surface: `GET /gdpr/export/{user_id}` and `DELETE /gdpr/delete/{user_id}`.

## UserDataRegistry

```python
from __future__ import annotations

import json
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class UserDataRegistry:
    """Central registry for all data stores that hold user PII.

    In production, replace in-memory dicts with repository classes that
    wrap your ORM. Export and delete each via the same interface.
    """

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
```

## DataSubjectService

```python
@injectable(scope=Scope.SINGLETON)
class DataSubjectService:
    def __init__(self, registry: UserDataRegistry) -> None:
        self._registry = registry

    def export(self, user_id: str) -> bytes:
        """Return a JSON dump of all PII for user_id."""
        data = self._registry.export_user(user_id)
        return json.dumps(data, indent=2).encode("utf-8")

    def delete(self, user_id: str) -> dict:
        """Erase all PII for user_id. Returns deleted-record counts."""
        return self._registry.delete_user(user_id)
```

## GDPRController

```python
from lauren import controller, get, delete, module, Path
from lauren.types import Response

@controller("/gdpr")
class GDPRController:
    def __init__(self, svc: DataSubjectService) -> None:
        self._svc = svc

    @get("/export/{user_id}")
    async def export_data(self, user_id: Path[str]) -> Response:
        data = self._svc.export(user_id)
        return (
            Response(body=data, media_type="application/json")
            .with_header("Content-Disposition", f"attachment; filename=user_{user_id}_data.json")
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
```

## Compliance notes

- **Export** must include all personal data across all systems (Art. 15). Extend `UserDataRegistry` with more stores as needed.
- **Delete** must remove or anonymise data, not just soft-delete (Art. 17). Log the deletion event via `AuditLogService` for accountability.
- **Access control**: wrap both endpoints with an authentication guard. The subject must authenticate, or an admin must present proof of identity.
- **Response time**: GDPR mandates completion within 30 days. For large datasets, generate the export asynchronously and send a download link via email.

## Testing

```python
def test_export_contains_profile():
    app = LaurenFactory.create(GDPRModule)
    registry = app.container.resolve(UserDataRegistry)
    registry.register_profile("u1", {"name": "Alice", "email": "alice@example.com"})
    client = TestClient(app)
    r = client.get("/gdpr/export/u1")
    data = json.loads(r.body)
    assert data["profile"]["name"] == "Alice"

def test_delete_removes_all_data():
    app = LaurenFactory.create(GDPRModule)
    registry = app.container.resolve(UserDataRegistry)
    registry.register_profile("u1", {"name": "Alice"})
    registry.register_order("u1", {"id": "o1", "amount": 50})
    client = TestClient(app)
    r = client.delete("/gdpr/delete/u1")
    assert r.json()["deleted"]["profiles"] == 1
    assert registry.export_user("u1")["profile"] == {}
```
