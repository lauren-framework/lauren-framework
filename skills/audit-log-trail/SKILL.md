---
name: audit-log-trail
description: Implements an immutable append-only audit log for tracking mutations in a Lauren application. Use when you need a tamper-evident record of who changed what and when — for compliance (SOC 2, HIPAA, GDPR), debugging, or data lineage.
---

> Use `codemap find "AuditLogService"` to locate any existing audit instrumentation before adding a new one.

# Audit Log Trail (Immutable Mutation Tracking)

The pattern uses a single `AuditLogService` singleton. Every mutation operation in your domain calls `record(...)`. The list is append-only — there is no `update` or `delete` method.

## Data model

```python
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any

@dataclass
class AuditEntry:
    entry_id: str          # deterministic SHA-256 prefix
    timestamp: float       # unix epoch (time.time())
    user_id: str
    action: str            # e.g. "create", "update", "delete"
    resource_type: str     # e.g. "user", "order", "invoice"
    resource_id: str
    changes: dict[str, Any]
```

## AuditLogService

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class AuditLogService:
    """Append-only audit log. Never exposes a mutation surface to callers."""

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
        self._entries.append(entry)   # append-only
        return entry

    def get_by_user(self, user_id: str) -> list[AuditEntry]:
        return [e for e in self._entries if e.user_id == user_id]

    def get_by_resource(
        self, resource_type: str, resource_id: str
    ) -> list[AuditEntry]:
        return [
            e for e in self._entries
            if e.resource_type == resource_type and e.resource_id == resource_id
        ]

    def get_all(self) -> list[AuditEntry]:
        return list(self._entries)  # return a copy — callers cannot mutate the log
```

## AuditController (query endpoints)

```python
from lauren import controller, get, Path, module

@controller("/audit")
class AuditController:
    def __init__(self, log: AuditLogService) -> None:
        self._log = log

    @get("/")
    async def list_all(self) -> list[dict]:
        return [vars(e) for e in self._log.get_all()]

    @get("/user/{user_id}")
    async def by_user(self, user_id: Path[str]) -> list[dict]:
        return [vars(e) for e in self._log.get_by_user(user_id)]

    @get("/resource/{resource_type}/{resource_id}")
    async def by_resource(
        self,
        resource_type: Path[str],
        resource_id: Path[str],
    ) -> list[dict]:
        return [
            vars(e)
            for e in self._log.get_by_resource(resource_type, resource_id)
        ]

@module(controllers=[AuditController], providers=[AuditLogService])
class AuditModule:
    pass
```

## Injecting into domain services

```python
@injectable(scope=Scope.SINGLETON)
class UserService:
    def __init__(self, audit: AuditLogService) -> None:
        self._audit = audit
        self._users: dict[str, dict] = {}

    def update_email(self, actor_id: str, user_id: str, new_email: str) -> None:
        old = self._users.get(user_id, {})
        self._users[user_id] = {**old, "email": new_email}
        self._audit.record(
            user_id=actor_id,
            action="update",
            resource_type="user",
            resource_id=user_id,
            changes={"email": {"from": old.get("email"), "to": new_email}},
        )
```

## Security note

`entry_id` is a truncated SHA-256 hash derived from user, action, resource and timestamp. For a production-grade immutable log, persist entries to an append-only store (e.g., PostgreSQL `INSERT` with no `UPDATE`/`DELETE` grants, an event-sourcing stream, or Amazon QLDB).

## Testing

```python
def test_record_creates_entry():
    svc = AuditLogService()
    entry = svc.record("u1", "create", "order", "o1", {"amount": 100})
    assert entry.user_id == "u1"
    assert entry.action == "create"
    assert len(svc.get_all()) == 1

def test_log_is_append_only():
    svc = AuditLogService()
    svc.record("u1", "create", "order", "o1", {})
    svc.record("u1", "update", "order", "o1", {"amount": 200})
    assert len(svc.get_all()) == 2
```
