"""Integration tests for @openapi_security → OpenAPI schema generation.

Each test boots a real LaurenApp via LaurenFactory.create() and inspects
the generated OpenAPI document at /openapi.json (or via generate_openapi()).

Coverage grid
-------------
Schema-level tests (verify op["security"] in generated document)
  1.  Single guard with @openapi_security → security field present.
  2.  Guard without @openapi_security → no security field on operation.
  3.  Explicit @controller(security=[...]) takes precedence over guard.
  4.  Two guards → AND merge into one dict.
  5.  Single guard with two requirements → OR preserved in output.
  6.  Controller-level guard applies security to ALL routes on controller.
  7.  Route-level @use_guards appends to controller guards (AND merge).
  8.  Unguarded routes have no ``security`` key.
  9.  Guard-derived security co-exists with securitySchemes component.
  10. securitySchemes registered via openapi_security_schemes appear.
  11. OAuth2 guard with scopes — scopes are preserved in output.
  12. Mixed: some routes guarded, some not — per-route correctness.

Runtime tests (guards still enforce can_activate at request time)
  13. can_activate returning True → 200, security field in schema.
  14. can_activate returning False / raising ForbiddenError → 403.
"""

# No ``from __future__ import annotations`` — DI resolves live class objects.

import pytest

from lauren import (
    ExecutionContext,
    ForbiddenError,
    LaurenFactory,
    controller,
    get,
    module,
    openapi_security,
    use_guards,
)
from lauren._asgi._openapi import generate_openapi
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Shared guard stubs
# ---------------------------------------------------------------------------


class AlwaysAllow:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


class AlwaysDeny:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        raise ForbiddenError("denied")


# ---------------------------------------------------------------------------
# Scenario 1 — single guard with @openapi_security
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class BearerGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(BearerGuard)
@controller("/secured")
class SecuredController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[SecuredController])
class SecuredApp: ...


@pytest.mark.asyncio
async def test_single_guard_security_appears_in_schema() -> None:
    app = LaurenFactory.create(SecuredApp)
    doc = generate_openapi(app)
    op = doc["paths"]["/secured"]["get"]
    assert "security" in op
    assert op["security"] == [{"BearerAuth": []}]


# ---------------------------------------------------------------------------
# Scenario 2 — guard without @openapi_security → no security field
# ---------------------------------------------------------------------------


class PlainGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(PlainGuard)
@controller("/plain")
class PlainController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[PlainController])
class PlainApp: ...


@pytest.mark.asyncio
async def test_guard_without_security_meta_no_schema_entry() -> None:
    app = LaurenFactory.create(PlainApp)
    doc = generate_openapi(app)
    op = doc["paths"]["/plain"]["get"]
    assert "security" not in op


# ---------------------------------------------------------------------------
# Scenario 3 — explicit @controller(security=[...]) takes precedence
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class OverriddenGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(OverriddenGuard)
@controller("/explicit", security=[{"ApiKey": ["read"]}])
class ExplicitSecurityController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[ExplicitSecurityController])
class ExplicitApp: ...


@pytest.mark.asyncio
async def test_explicit_controller_security_takes_precedence() -> None:
    app = LaurenFactory.create(ExplicitApp)
    doc = generate_openapi(app)
    op = doc["paths"]["/explicit"]["get"]
    assert op["security"] == [{"ApiKey": ["read"]}]


# ---------------------------------------------------------------------------
# Scenario 4 — two guards → AND merge
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class AndGuard1:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@openapi_security({"TenantHeader": []})
class AndGuard2:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(AndGuard1, AndGuard2)
@controller("/and-merge")
class AndMergeController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[AndMergeController])
class AndMergeApp: ...


@pytest.mark.asyncio
async def test_two_guards_and_merge() -> None:
    app = LaurenFactory.create(AndMergeApp)
    doc = generate_openapi(app)
    op = doc["paths"]["/and-merge"]["get"]
    assert "security" in op
    assert len(op["security"]) == 1
    merged = op["security"][0]
    assert "BearerAuth" in merged
    assert "TenantHeader" in merged


# ---------------------------------------------------------------------------
# Scenario 5 — single guard with two requirements (OR preserved)
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []}, {"ApiKey": []})
class OrGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(OrGuard)
@controller("/or-scheme")
class OrController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[OrController])
class OrApp: ...


@pytest.mark.asyncio
async def test_single_guard_multiple_requirements_or_preserved() -> None:
    app = LaurenFactory.create(OrApp)
    doc = generate_openapi(app)
    op = doc["paths"]["/or-scheme"]["get"]
    assert op["security"] == [{"BearerAuth": []}, {"ApiKey": []}]


# ---------------------------------------------------------------------------
# Scenario 6 — controller-level guard applies to all routes
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class CtrlLevelGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(CtrlLevelGuard)
@controller("/multi")
class MultiRouteController:
    @get("/a")
    async def route_a(self) -> dict:
        return {"route": "a"}

    @get("/b")
    async def route_b(self) -> dict:
        return {"route": "b"}

    @get("/c")
    async def route_c(self) -> dict:
        return {"route": "c"}


@module(controllers=[MultiRouteController])
class MultiRouteApp: ...


@pytest.mark.asyncio
async def test_controller_level_guard_security_on_all_routes() -> None:
    app = LaurenFactory.create(MultiRouteApp)
    doc = generate_openapi(app)
    for path in ("/multi/a", "/multi/b", "/multi/c"):
        op = doc["paths"][path]["get"]
        assert op.get("security") == [{"BearerAuth": []}], f"missing on {path}"


# ---------------------------------------------------------------------------
# Scenario 7 — route-level guard in addition to controller guard → AND merge
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class RouteCtrlGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@openapi_security({"OtpCode": []})
class RouteMethodGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(RouteCtrlGuard)
@controller("/route-level")
class RouteLevelController:
    @get("/open")
    async def open_route(self) -> dict:
        return {"guarded": False}

    @get("/sensitive")
    @use_guards(RouteMethodGuard)
    async def sensitive_route(self) -> dict:
        return {"guarded": True}


@module(controllers=[RouteLevelController])
class RouteLevelApp: ...


@pytest.mark.asyncio
async def test_route_level_guard_adds_to_and_merge() -> None:
    app = LaurenFactory.create(RouteLevelApp)
    doc = generate_openapi(app)
    # /route-level/open — only ctrl guard
    open_op = doc["paths"]["/route-level/open"]["get"]
    assert open_op.get("security") == [{"BearerAuth": []}]
    # /route-level/sensitive — ctrl guard + route guard → AND merge
    sens_op = doc["paths"]["/route-level/sensitive"]["get"]
    assert "security" in sens_op
    assert len(sens_op["security"]) == 1
    merged = sens_op["security"][0]
    assert "BearerAuth" in merged
    assert "OtpCode" in merged


# ---------------------------------------------------------------------------
# Scenario 8 — unguarded routes have no security key
# ---------------------------------------------------------------------------


@controller("/public")
class PublicController:
    @get("/")
    async def index(self) -> dict:
        return {"public": True}


@module(controllers=[PublicController])
class PublicApp: ...


@pytest.mark.asyncio
async def test_unguarded_routes_have_no_security() -> None:
    app = LaurenFactory.create(PublicApp)
    doc = generate_openapi(app)
    op = doc["paths"]["/public"]["get"]
    assert "security" not in op


# ---------------------------------------------------------------------------
# Scenario 9 + 10 — securitySchemes component is registered and present
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class SchemeGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(SchemeGuard)
@controller("/scheme-test")
class SchemeTestController:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[SchemeTestController])
class SchemeTestApp: ...


BEARER_SCHEME = {
    "type": "http",
    "scheme": "bearer",
    "bearerFormat": "JWT",
}


@pytest.mark.asyncio
async def test_security_schemes_component_registered() -> None:
    app = LaurenFactory.create(
        SchemeTestApp,
        openapi_security_schemes={"BearerAuth": BEARER_SCHEME},
    )
    doc = generate_openapi(app)
    assert "securitySchemes" in doc["components"]
    assert doc["components"]["securitySchemes"]["BearerAuth"] == BEARER_SCHEME


@pytest.mark.asyncio
async def test_guard_security_and_scheme_component_together() -> None:
    """op["security"] references BearerAuth which appears in securitySchemes."""
    app = LaurenFactory.create(
        SchemeTestApp,
        openapi_security_schemes={"BearerAuth": BEARER_SCHEME},
    )
    doc = generate_openapi(app)
    op = doc["paths"]["/scheme-test"]["get"]
    assert op["security"] == [{"BearerAuth": []}]
    assert "BearerAuth" in doc["components"]["securitySchemes"]


# ---------------------------------------------------------------------------
# Scenario 11 — OAuth2 guard with scopes preserved
# ---------------------------------------------------------------------------


@openapi_security({"OAuth2": ["read:items", "write:items"]})
class OAuth2Guard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(OAuth2Guard)
@controller("/oauth")
class OAuth2Controller:
    @get("/")
    async def index(self) -> dict:
        return {"ok": True}


@module(controllers=[OAuth2Controller])
class OAuth2App: ...


@pytest.mark.asyncio
async def test_oauth2_scopes_preserved_in_schema() -> None:
    app = LaurenFactory.create(OAuth2App)
    doc = generate_openapi(app)
    op = doc["paths"]["/oauth"]["get"]
    assert op["security"] == [{"OAuth2": ["read:items", "write:items"]}]


# ---------------------------------------------------------------------------
# Scenario 12 — mixed: guarded and unguarded routes in same module
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class MixedGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@controller("/mixed-ctrl")
class MixedController:
    @get("/public")
    async def public_route(self) -> dict:
        return {"public": True}

    @get("/private")
    @use_guards(MixedGuard)
    async def private_route(self) -> dict:
        return {"private": True}


@module(controllers=[MixedController])
class MixedApp: ...


@pytest.mark.asyncio
async def test_mixed_guarded_and_unguarded_routes() -> None:
    app = LaurenFactory.create(MixedApp)
    doc = generate_openapi(app)
    # Public route has no security.
    pub = doc["paths"]["/mixed-ctrl/public"]["get"]
    assert "security" not in pub
    # Private route has security.
    priv = doc["paths"]["/mixed-ctrl/private"]["get"]
    assert priv.get("security") == [{"BearerAuth": []}]


# ---------------------------------------------------------------------------
# Scenario 13 — can_activate=True still returns 200 with security in schema
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class RuntimeAllowGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@use_guards(RuntimeAllowGuard)
@controller("/runtime-allow")
class RuntimeAllowController:
    @get("/")
    async def index(self) -> dict:
        return {"allowed": True}


@module(controllers=[RuntimeAllowController])
class RuntimeAllowApp: ...


@pytest.mark.asyncio
async def test_guard_allows_request_and_schema_has_security() -> None:
    app = LaurenFactory.create(RuntimeAllowApp)
    r = TestClient(app).get("/runtime-allow/")
    assert r.status_code == 200
    assert r.json() == {"allowed": True}
    doc = generate_openapi(app)
    assert doc["paths"]["/runtime-allow"]["get"]["security"] == [{"BearerAuth": []}]


# ---------------------------------------------------------------------------
# Scenario 14 — can_activate raises ForbiddenError → 403, schema unchanged
# ---------------------------------------------------------------------------


@openapi_security({"BearerAuth": []})
class RuntimeDenyGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        raise ForbiddenError("no access")


@use_guards(RuntimeDenyGuard)
@controller("/runtime-deny")
class RuntimeDenyController:
    @get("/")
    async def index(self) -> dict:
        return {}  # unreachable


@module(controllers=[RuntimeDenyController])
class RuntimeDenyApp: ...


@pytest.mark.asyncio
async def test_guard_denies_request_and_schema_still_has_security() -> None:
    app = LaurenFactory.create(RuntimeDenyApp)
    r = TestClient(app).get("/runtime-deny/")
    assert r.status_code == 403
    doc = generate_openapi(app)
    # Schema describes the *intent* regardless of runtime outcome.
    assert doc["paths"]["/runtime-deny"]["get"]["security"] == [{"BearerAuth": []}]
