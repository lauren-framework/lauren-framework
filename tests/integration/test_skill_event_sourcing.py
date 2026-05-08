"""Integration tests for skill 27: Event Sourcing with Projection Rebuilding.

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Json,
    Path,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient
from lauren.types import Response


# ---------------------------------------------------------------------------
# Domain event
# ---------------------------------------------------------------------------


@dataclass
class DomainEvent:
    aggregate_id: str
    event_type: str
    payload: dict
    timestamp: float = field(default_factory=time.time)
    version: int = 0


# ---------------------------------------------------------------------------
# Event store
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class EventStore:
    def __init__(self) -> None:
        self._events: list[DomainEvent] = []

    def append(self, event: DomainEvent) -> None:
        existing_count = sum(
            1 for e in self._events if e.aggregate_id == event.aggregate_id
        )
        event.version = existing_count + 1
        self._events.append(event)

    def get_events(self, aggregate_id: str) -> list[DomainEvent]:
        return [e for e in self._events if e.aggregate_id == aggregate_id]

    def get_all_events(self, event_type: str | None = None) -> list[DomainEvent]:
        if event_type:
            return [e for e in self._events if e.event_type == event_type]
        return list(self._events)


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------


class OrderAggregate:
    def __init__(self, order_id: str) -> None:
        self.order_id = order_id
        self.status = "new"
        self.items: list[dict] = []
        self.total = 0.0

    def apply(self, event: DomainEvent) -> None:
        if event.event_type == "OrderCreated":
            self.status = "created"
        elif event.event_type == "ItemAdded":
            self.items.append(event.payload)
            self.total += event.payload.get("price", 0.0)
        elif event.event_type == "OrderCompleted":
            self.status = "completed"
        elif event.event_type == "OrderCancelled":
            self.status = "cancelled"

    @classmethod
    def rebuild(cls, order_id: str, events: list[DomainEvent]) -> "OrderAggregate":
        agg = cls(order_id)
        for event in events:
            agg.apply(event)
        return agg


# ---------------------------------------------------------------------------
# Command handler
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class OrderCommandHandler:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    def create_order(self, order_id: str) -> OrderAggregate:
        event = DomainEvent(
            aggregate_id=order_id, event_type="OrderCreated", payload={}
        )
        self._store.append(event)
        return self._load(order_id)

    def add_item(self, order_id: str, name: str, price: float) -> OrderAggregate:
        self._load(order_id)  # validates order exists
        event = DomainEvent(
            aggregate_id=order_id,
            event_type="ItemAdded",
            payload={"name": name, "price": price},
        )
        self._store.append(event)
        return self._load(order_id)

    def complete_order(self, order_id: str) -> OrderAggregate:
        event = DomainEvent(
            aggregate_id=order_id, event_type="OrderCompleted", payload={}
        )
        self._store.append(event)
        return self._load(order_id)

    def _load(self, order_id: str) -> OrderAggregate:
        events = self._store.get_events(order_id)
        if not events:
            raise ValueError(f"Order {order_id} does not exist")
        return OrderAggregate.rebuild(order_id, events)


# ---------------------------------------------------------------------------
# Projection
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class OrderSummaryProjection:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    def build(self) -> list[dict]:
        orders: dict[str, dict] = {}
        for event in self._store.get_all_events():
            aid = event.aggregate_id
            if event.event_type == "OrderCreated":
                orders[aid] = {
                    "id": aid,
                    "status": "created",
                    "item_count": 0,
                    "total": 0.0,
                }
            elif event.event_type == "ItemAdded" and aid in orders:
                orders[aid]["item_count"] += 1
                orders[aid]["total"] += event.payload.get("price", 0.0)
            elif event.event_type == "OrderCompleted" and aid in orders:
                orders[aid]["status"] = "completed"
        return list(orders.values())


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class CreateOrderRequest(BaseModel):
    order_id: str


class AddItemRequest(BaseModel):
    name: str
    price: float


@controller("/orders")
class OrderController:
    def __init__(
        self,
        handler: OrderCommandHandler,
        projection: OrderSummaryProjection,
        store: EventStore,
    ) -> None:
        self._handler = handler
        self._projection = projection
        self._store = store

    @post("/")
    async def create(self, body: Json[CreateOrderRequest]) -> Response:
        order = self._handler.create_order(body.order_id)
        return Response.json({"id": order.order_id, "status": order.status}, status=201)

    @post("/{order_id}/items")
    async def add_item(self, order_id: Path[str], body: Json[AddItemRequest]) -> dict:
        order = self._handler.add_item(order_id, body.name, body.price)
        return {
            "id": order.order_id,
            "total": order.total,
            "item_count": len(order.items),
        }

    @post("/{order_id}/complete")
    async def complete(self, order_id: Path[str]) -> dict:
        order = self._handler.complete_order(order_id)
        return {"id": order.order_id, "status": order.status}

    @get("/summary")
    async def summary(self) -> list:
        return self._projection.build()

    @get("/{order_id}/events")
    async def events(self, order_id: Path[str]) -> list:
        return [
            {"type": e.event_type, "version": e.version, "payload": e.payload}
            for e in self._store.get_events(order_id)
        ]

    @get("/{order_id}/status")
    async def status(self, order_id: Path[str]) -> dict:
        events = self._store.get_events(order_id)
        if not events:
            return {
                "id": order_id,
                "status": "not_found",
                "item_count": 0,
                "total": 0.0,
            }
        order = OrderAggregate.rebuild(order_id, events)
        return {
            "id": order.order_id,
            "status": order.status,
            "item_count": len(order.items),
            "total": order.total,
        }


@module(
    controllers=[OrderController],
    providers=[EventStore, OrderCommandHandler, OrderSummaryProjection],
)
class OrderModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(OrderModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestEventStoreViaHTTP:
    def test_append_event_via_order_creation(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "o1"})
        r = client.get("/orders/o1/events")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_versions_increment_sequentially(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "o2"})
        client.post("/orders/o2/items", json={"name": "A", "price": 1.0})
        client.post("/orders/o2/items", json={"name": "B", "price": 2.0})
        r = client.get("/orders/o2/events")
        versions = [e["version"] for e in r.json()]
        assert versions == [1, 2, 3]

    def test_events_filtered_by_aggregate(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "a"})
        client.post("/orders/", json={"order_id": "b"})
        assert len(client.get("/orders/a/events").json()) == 1
        assert len(client.get("/orders/b/events").json()) == 1

    def test_event_type_on_creation(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "o3"})
        events = client.get("/orders/o3/events").json()
        assert events[0]["type"] == "OrderCreated"


class TestOrderAggregateViaHTTP:
    def test_initial_status_after_creation(self) -> None:
        client = build_app()
        r = client.post("/orders/", json={"order_id": "agg1"})
        assert r.status_code == 201
        assert r.json()["status"] == "created"

    def test_add_item_updates_total(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "agg2"})
        r = client.post("/orders/agg2/items", json={"name": "Widget", "price": 9.99})
        assert r.status_code == 200
        assert abs(r.json()["total"] - 9.99) < 0.001
        assert r.json()["item_count"] == 1

    def test_complete_order_changes_status(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "agg3"})
        r = client.post("/orders/agg3/complete")
        assert r.json()["status"] == "completed"

    def test_rebuild_from_multiple_events(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "agg4"})
        client.post("/orders/agg4/items", json={"name": "A", "price": 5.0})
        client.post("/orders/agg4/items", json={"name": "B", "price": 3.0})
        client.post("/orders/agg4/complete")
        r = client.get("/orders/agg4/status")
        assert r.json()["status"] == "completed"
        assert r.json()["item_count"] == 2
        assert abs(r.json()["total"] - 8.0) < 0.001


class TestEventSourcingIntegration:
    def test_create_order(self) -> None:
        client = build_app()
        r = client.post("/orders/", json={"order_id": "order-1"})
        assert r.status_code == 201
        assert r.json()["status"] == "created"

    def test_add_item(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "order-2"})
        r = client.post("/orders/order-2/items", json={"name": "Widget", "price": 9.99})
        assert r.status_code == 200
        assert r.json()["item_count"] == 1
        assert abs(r.json()["total"] - 9.99) < 0.001

    def test_complete_order(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "order-3"})
        r = client.post("/orders/order-3/complete")
        assert r.json()["status"] == "completed"

    def test_event_log(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "order-4"})
        client.post("/orders/order-4/items", json={"name": "X", "price": 1.0})
        r = client.get("/orders/order-4/events")
        types = [e["type"] for e in r.json()]
        assert "OrderCreated" in types
        assert "ItemAdded" in types

    def test_projection_summary(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "order-5"})
        client.post("/orders/order-5/items", json={"name": "A", "price": 10.0})
        client.post("/orders/order-5/items", json={"name": "B", "price": 5.0})
        r = client.get("/orders/summary")
        summaries = {s["id"]: s for s in r.json()}
        assert "order-5" in summaries
        assert summaries["order-5"]["item_count"] == 2
        assert abs(summaries["order-5"]["total"] - 15.0) < 0.001

    def test_events_have_sequential_versions(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "order-6"})
        client.post("/orders/order-6/items", json={"name": "A", "price": 1.0})
        r = client.get("/orders/order-6/events")
        versions = [e["version"] for e in r.json()]
        assert versions == [1, 2]
