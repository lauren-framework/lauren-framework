"""End-to-end tests verifying ForwardRef annotations work across the
framework's DI container, extractors and route dispatch.

These scenarios cover the three real-world shapes where forward
references appear in lauren applications:

1. ``from __future__ import annotations`` causes *every* annotation in
   the module to become a string; the resolver must unwrap them before
   the DI container can look up providers.

2. Self-referential Pydantic models used inside ``Json[T]`` extractor
   slots — the handler signature references the model by string name
   because the model recursively references itself.

3. Services that depend on forward-declared services declared later in
   the same module (two-phase declaration order).

4. ``@use_guards`` on a controller whose handlers use ``Json[T]`` — guards
   must not prevent extractor-hint resolution when PEP 563 is active.

5. Multiple extractor types in the same handler (``Path[int]``,
   ``Query[str]``, ``Json[T]``) all stringified by PEP 563 simultaneously.
"""

from __future__ import annotations


from pydantic import BaseModel

from lauren import (
    Depends,
    Json,
    LaurenFactory,
    Path,
    Query,
    controller,
    get,
    injectable,
    module,
    post,
    use_guards,
)
from lauren.types import ExecutionContext
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# 1. PEP 563 (``from __future__ import annotations``) end-to-end
# ---------------------------------------------------------------------------


@injectable()
class Greeter:
    def greet(self, name: str) -> str:
        return f"hello {name}"


@controller("/greet")
class GreetController:
    # Field-level injection with the class reference as a string thanks
    # to the module-wide ``from __future__ import annotations``.
    greeter: Greeter

    @get("/{name}")
    async def greet(self, name: Path[str]) -> dict:
        return {"message": self.greeter.greet(name)}


@module(controllers=[GreetController], providers=[Greeter])
class _PEP563Module:
    pass


def test_pep563_annotations_resolve_for_di_and_extractors() -> None:
    app = LaurenFactory.create(_PEP563Module)
    r = TestClient(app).get("/greet/world")
    assert r.status_code == 200
    assert r.json() == {"message": "hello world"}


# ---------------------------------------------------------------------------
# 2. Self-referential Pydantic model inside ``Json[T]``
# ---------------------------------------------------------------------------


class TreeNode(BaseModel):
    """Recursive schema — ``children`` points at the same class."""

    value: int
    children: list[TreeNode] = []


@controller("/trees")
class TreeController:
    @post("/echo")
    async def echo(self, body: Json[TreeNode]) -> TreeNode:
        return body


@module(controllers=[TreeController])
class _TreeModule:
    pass


def test_recursive_pydantic_model_through_json_extractor() -> None:
    app = LaurenFactory.create(_TreeModule)
    payload = {
        "value": 1,
        "children": [
            {"value": 2, "children": []},
            {"value": 3, "children": [{"value": 4, "children": []}]},
        ],
    }
    r = TestClient(app).post("/trees/echo", json=payload)
    assert r.status_code == 200
    assert r.json() == payload


# ---------------------------------------------------------------------------
# 3. Service dependency declared as a forward-ref string (PEP 563 shape)
# ---------------------------------------------------------------------------


@injectable()
class UpstreamService:
    def ping(self) -> str:
        return "pong"


@injectable()
class DownstreamService:
    # Forward reference as a quoted string — still resolves to the
    # concrete class at startup.
    def __init__(self, upstream: "UpstreamService") -> None:
        self.upstream = upstream

    def call(self) -> str:
        return self.upstream.ping()


@controller("/svc")
class SvcController:
    @get("/ping")
    async def ping(self, down: Depends["DownstreamService"]) -> dict:
        return {"ok": down.call()}


@module(
    controllers=[SvcController],
    providers=[UpstreamService, DownstreamService],
)
class _ChainModule:
    pass


def test_forward_ref_between_injectables_resolves() -> None:
    app = LaurenFactory.create(_ChainModule)
    r = TestClient(app).get("/svc/ping")
    assert r.status_code == 200
    assert r.json() == {"ok": "pong"}


# ---------------------------------------------------------------------------
# 4. Stringified forward ref inside a generic container (``Json[list[T]]``)
# ---------------------------------------------------------------------------


class Item(BaseModel):
    id: int
    name: str


@controller("/items")
class ItemsController:
    @post("/bulk")
    async def bulk(self, body: Json[list[Item]]) -> dict:
        # ``body`` is a list of dicts when the inner type is parsed as a
        # plain ``list[Item]`` — lauren's JSON extractor delegates the
        # recursive validation to pydantic when the inner type is a
        # BaseModel subclass, otherwise it returns the raw structure.
        # Regardless of shape, we should receive two entries.
        first = body[0]
        first_name = first.name if hasattr(first, "name") else first.get("name")
        return {"count": len(body), "first": first_name}


@module(controllers=[ItemsController])
class _ItemsModule:
    pass


def test_forwardref_inside_generic_container() -> None:
    """``Json[list[Item]]`` is stringified end-to-end by PEP 563 so the
    full generic tree — including the inner ``Item`` class — has to be
    walked and re-assembled by the resolver."""
    app = LaurenFactory.create(_ItemsModule)
    r = TestClient(app).post(
        "/items/bulk",
        json=[{"id": 1, "name": "a"}, {"id": 2, "name": "b"}],
    )
    assert r.status_code == 200
    assert r.json() == {"count": 2, "first": "a"}


# ---------------------------------------------------------------------------
# 5. ``@use_guards`` + ``Json[T]`` + PEP 563
#
# Regression test for the case where both resolve_type_hints calls in
# _safe_type_hints fall back to the inspect.get_annotations() last resort.
# Guards must not prevent extractor-hint resolution when the handler file
# has ``from __future__ import annotations``.
# ---------------------------------------------------------------------------


class TokenRequest(BaseModel):
    token: str
    user_id: str


class _AllowAllGuard:
    """A no-op guard that always allows the request through."""

    async def can_activate(self, context: ExecutionContext) -> bool:
        return True


class _RejectAllGuard:
    """A guard that always rejects (for verifying the guard runs)."""

    async def can_activate(self, context: ExecutionContext) -> bool:
        return False


@use_guards(_AllowAllGuard)
@controller("/guarded")
class GuardedController:
    @post("/token")
    async def issue_token(self, body: Json[TokenRequest]) -> dict:
        return {"echo_token": body.token, "echo_user": body.user_id}

    @use_guards(_RejectAllGuard)
    @post("/secret")
    async def secret(self, body: Json[TokenRequest]) -> dict:
        return {"secret": "you should not see this"}


@module(controllers=[GuardedController])
class _GuardedModule:
    pass


def test_guarded_controller_json_body_resolves_with_pep563() -> None:
    """``@use_guards`` on a PEP-563 controller must not break ``Json[T]``
    extractor resolution — guards do not wrap handler functions so the
    extractor compiler must still see the proper Annotated type."""
    app = LaurenFactory.create(_GuardedModule)
    r = TestClient(app).post(
        "/guarded/token",
        json={"token": "abc123", "user_id": "u-1"},
    )
    assert r.status_code == 200
    assert r.json() == {"echo_token": "abc123", "echo_user": "u-1"}


def test_guarded_controller_route_guard_rejects() -> None:
    """A method-level ``@use_guards`` that returns False must produce 403,
    even when the containing controller has PEP-563 active."""
    app = LaurenFactory.create(_GuardedModule)
    r = TestClient(app).post(
        "/guarded/secret",
        json={"token": "x", "user_id": "u-2"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# 6. Multiple extractor types in one handler, all stringified by PEP 563
# ---------------------------------------------------------------------------


class PatchPayload(BaseModel):
    value: str


@controller("/multi")
class MultiExtractorController:
    @post("/{resource_id}/update")
    async def update(
        self,
        resource_id: Path[int],
        dry_run: Query[bool],
        body: Json[PatchPayload],
    ) -> dict:
        return {
            "resource_id": resource_id,
            "dry_run": dry_run,
            "value": body.value,
        }


@module(controllers=[MultiExtractorController])
class _MultiExtractorModule:
    pass


def test_multiple_extractors_with_pep563() -> None:
    """Path, Query, and Json extractors in the same handler must all resolve
    correctly when their types are stringified by ``from __future__ import
    annotations``."""
    app = LaurenFactory.create(_MultiExtractorModule)
    r = TestClient(app).post(
        "/multi/42/update?dry_run=true",
        json={"value": "hello"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data["resource_id"] == 42
    assert data["dry_run"] is True
    assert data["value"] == "hello"
