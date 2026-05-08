"""Integration tests for the RBAC policy engine skill."""

from __future__ import annotations


from lauren import (
    ExecutionContext,
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    set_metadata,
    use_guards,
)
from lauren.exceptions import ForbiddenError, UnauthorizedError
from lauren.testing import TestClient

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"read", "write", "delete"},
    "editor": {"read", "write"},
    "viewer": {"read"},
}

PERMISSION_KEY = "required_permission"
ROLE_HEADER = "x-role"


@injectable(scope=Scope.SINGLETON)
class RBACGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        required = ctx.get_metadata(PERMISSION_KEY, "")
        if not required:
            return True

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

    @get("/items/nuke")
    @set_metadata(PERMISSION_KEY, "delete")
    async def delete_items(self) -> dict:
        return {"status": "deleted"}

    @get("/open")
    async def open_endpoint(self) -> dict:
        return {"open": True}


@module(controllers=[ResourceController], providers=[RBACGuard])
class RBACModule:
    pass


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(RBACModule))


class TestRBAC:
    def test_admin_can_read(self):
        client = build_app()
        r = client.get("/api/items", headers={"x-role": "admin"})
        assert r.status_code == 200
        assert r.json()["items"] == ["a", "b", "c"]

    def test_admin_can_write(self):
        client = build_app()
        r = client.get("/api/items/manage", headers={"x-role": "admin"})
        assert r.status_code == 200

    def test_viewer_can_read(self):
        client = build_app()
        r = client.get("/api/items", headers={"x-role": "viewer"})
        assert r.status_code == 200

    def test_viewer_cannot_write(self):
        client = build_app()
        r = client.get("/api/items/manage", headers={"x-role": "viewer"})
        assert r.status_code == 403

    def test_editor_cannot_delete(self):
        client = build_app()
        r = client.get("/api/items/nuke", headers={"x-role": "editor"})
        assert r.status_code == 403

    def test_no_role_on_protected_endpoint_returns_401(self):
        client = build_app()
        r = client.get("/api/items")
        assert r.status_code == 401

    def test_open_endpoint_accessible_without_role(self):
        client = build_app()
        r = client.get("/api/open")
        assert r.status_code == 200
        assert r.json()["open"] is True
