---
name: rbac-engine
description: Implements Role-Based Access Control (RBAC) in Lauren using a guard and route metadata. Use when different user roles (admin, editor, viewer) need different endpoint permissions.
---

> Use `codemap find "set_metadata"` to locate metadata helpers before reading.

# Role-Based Access Control (RBAC) Policy Engine

## Overview

RBAC maps roles to permission sets. A single `RBACGuard` reads the required
permission from route metadata (set via `@set_metadata`) and the user's role
from request state (populated by an upstream middleware or guard).

## Core Pattern

```python
from __future__ import annotations

from lauren import (
    ExecutionContext,
    Scope,
    controller,
    get,
    injectable,
    module,
    set_metadata,
    use_guards,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError

# Role → permission mapping (extend as needed)
ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin":  {"read", "write", "delete"},
    "editor": {"read", "write"},
    "viewer": {"read"},
}

PERMISSION_KEY = "required_permission"
ROLE_HEADER = "x-role"


@injectable(scope=Scope.SINGLETON)
class RBACGuard:
    """Validates that the request's role has the required permission."""

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        required = ctx.get_metadata(PERMISSION_KEY, "")
        if not required:
            return True  # no permission declared → open endpoint

        role = ctx.request.headers.get(ROLE_HEADER, "")
        if not role:
            raise UnauthorizedError("Missing role header")

        perms = ROLE_PERMISSIONS.get(role, set())
        if required not in perms:
            raise ForbiddenError(
                f"Role '{role}' lacks permission '{required}'",
                detail={"required": required, "role": role},
            )
        return True


@use_guards(RBACGuard)
@controller("/api")
class ResourceController:
    @get("/items")
    @set_metadata(PERMISSION_KEY, "read")
    async def list_items(self) -> dict:
        return {"items": ["a", "b", "c"]}

    @get("/items/manage")
    @set_metadata(PERMISSION_KEY, "write")
    async def manage_items(self) -> dict:
        return {"status": "managed"}

    @get("/items/delete")
    @set_metadata(PERMISSION_KEY, "delete")
    async def delete_items(self) -> dict:
        return {"status": "deleted"}


@module(controllers=[ResourceController], providers=[RBACGuard])
class RBACModule:
    pass
```

## How the Role Reaches the Guard

For real applications the role comes from a decoded JWT or session. For
simplicity the examples above use a custom `X-Role` header. Replace the
`ctx.request.headers.get(ROLE_HEADER, "")` lookup with
`ctx.request.state.get("role", "")` once an upstream guard has stored it.

## Key Points

- `@set_metadata(PERMISSION_KEY, "read")` attaches metadata at the *route level*.
  `ctx.get_metadata()` reads it inside the guard.
- Returning `False` from `can_activate` → 403. Raising `ForbiddenError` → 403 with a body.
- `ROLE_PERMISSIONS` is a plain dict — swap it for a database-backed lookup in production.
- Guards are `SINGLETON` by default when declared with `@injectable(scope=Scope.SINGLETON)`.
  They can also be plain classes (non-injectable) and Lauren will instantiate them per request.
