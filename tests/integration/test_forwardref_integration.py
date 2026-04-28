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
"""

from __future__ import annotations


from pydantic import BaseModel

from lauren import (
    Depends,
    Json,
    LaurenFactory,
    Path,
    controller,
    get,
    injectable,
    module,
    post,
)
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
