"""An ``@auth(*roles)`` composite across controllers and routes (F23 §13).

One reusable decorator built with ``compose`` attaches an authentication
guard, a roles guard and typed roles; ``IsPublic`` exempts routes; the
principal travels from the guard to handlers through a ``StateKey`` with no
string literals at the call sites.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

import pytest

from lauren import (
    LaurenFactory,
    MetadataKey,
    MetadataSetKey,
    StateKey,
    UnauthorizedError,
    UnresolvableParameterError,
    compose,
    controller,
    get,
    injectable,
    module,
    post,
    provides_state,
    reject_bare_usage,
    use_guards,
)
from lauren.reflect import reflect_composed
from lauren.testing import TestClient
from lauren.types import ExecutionContext

Roles = MetadataSetKey[str]("e2e.roles")
IsPublic = MetadataKey[bool]("e2e.is_public", default=False)


@dataclass(frozen=True)
class Principal:
    name: str
    roles: frozenset[str]


CurrentUser = StateKey[Principal]("e2e.user", on_missing=UnauthorizedError, require_provider=True)
User = Annotated[Principal, CurrentUser]

_TOKENS = {
    "alice": Principal("alice", frozenset({"admin", "auditor"})),
    "bob": Principal("bob", frozenset({"auditor"})),
}


@injectable()
@provides_state(CurrentUser)
class TokenGuard:
    deny_error = UnauthorizedError

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        if ctx.get_metadata(IsPublic):
            return True
        principal = _TOKENS.get(ctx.request.headers.get("authorization", "").removeprefix("Bearer "))
        if principal is None:
            return False
        ctx.request.state[CurrentUser] = principal
        return True


@injectable()
class RolesGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        if ctx.get_metadata(IsPublic):
            return True
        required = ctx.get_metadata(Roles)
        principal = ctx.request.state.get(CurrentUser)
        return principal is not None and set(required) <= principal.roles


def auth(*roles: str) -> Any:
    """Require a token and every listed role."""
    reject_bare_usage("auth", roles[0] if roles else None)
    return compose(use_guards(TokenGuard, RolesGuard), Roles(*roles), name=f"auth{roles!r}")


public = compose(IsPublic(True), name="public")


@auth("auditor")
@controller("/reports")
class ReportsController:
    @get("/")
    async def index(self, user: User) -> dict:
        return {"viewer": user.name}

    @post("/purge")
    @Roles("admin")
    async def purge(self, user: User) -> dict:
        return {"purged_by": user.name}

    @get("/status")
    @public
    async def status(self) -> dict:
        return {"ok": True}


@controller("/admin")
class AdminController:
    @get("/stats")
    @auth("admin")
    async def stats(self, user: User) -> dict:
        return {"admin": user.name}


@module(controllers=[ReportsController, AdminController], providers=[TokenGuard, RolesGuard])
class AppModule:
    pass


@pytest.fixture(scope="module")
def client() -> Any:
    with TestClient(LaurenFactory.create(AppModule)) as c:
        yield c


def _bearer(token: str) -> dict[str, str]:
    return {"authorization": f"Bearer {token}"}


@pytest.mark.parametrize(
    ("method", "path", "token", "status", "body"),
    [
        ("GET", "/reports", "bob", 200, {"viewer": "bob"}),
        ("GET", "/reports", None, 401, None),
        ("POST", "/reports/purge", "bob", 403, None),
        ("POST", "/reports/purge", "alice", 200, {"purged_by": "alice"}),
        ("GET", "/reports/status", None, 200, {"ok": True}),
        ("GET", "/admin/stats", "bob", 403, None),
        ("GET", "/admin/stats", "alice", 200, {"admin": "alice"}),
    ],
)
def test_composite_policy_across_controllers_and_routes(
    client: Any, method: str, path: str, token: str | None, status: int, body: Any
) -> None:
    r = client.request(method, path, headers=_bearer(token) if token else {})

    assert r.status_code == status
    if body is not None:
        assert r.json() == body


def test_composition_provenance_is_reflected() -> None:
    assert [c.name for c in reflect_composed(ReportsController)] == ["auth('auditor',)"]
    assert [c.name for c in reflect_composed(AdminController.stats)] == ["auth('admin',)"]
    assert reflect_composed(ReportsController.status)[0].decorators == ("e2e.is_public(True)",)


def test_route_without_the_composite_fails_startup() -> None:
    @controller("/leak")
    class Leaky:
        @get("/")
        async def h(self, user: User) -> dict:
            return {}

    @module(controllers=[Leaky], providers=[TokenGuard])
    class LeakyModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(LeakyModule)

    assert info.value.detail["providers"] == ["TokenGuard"]
