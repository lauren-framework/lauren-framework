---
name: event-sourcing
description: Implements event sourcing with an append-only EventStore and aggregate rebuilding in a Lauren application. Use when you need a complete audit trail, temporal queries, or the ability to replay events to rebuild state.
---

> Use `codemap find "EventStore"` to check if an event store is already defined before creating one.

# Event Sourcing with Projection Rebuilding

The pattern stores domain events as an immutable append-only log. Aggregates rebuild their state by replaying events rather than loading a single snapshot row.

## Core types

```python
from __future__ import annotations
import time
from dataclasses import dataclass, field

@dataclass
class DomainEvent:
    aggregate_id: str
    event_type: str
    payload: dict
    timestamp: float = field(default_factory=time.time)
    version: int = 0
```

## Event store

The store is the single append point. After appending it publishes the event
on the application event bus, so read models update incrementally instead of
re-reading the whole log (`EventBus` is injectable everywhere; see
`skills/building-lauren-events/`).

```python
from lauren import injectable, Scope
from lauren.events import EventBus

@injectable(scope=Scope.SINGLETON)
class EventStore:
    def __init__(self, bus: EventBus) -> None:
        self._events: list[DomainEvent] = []
        self._bus = bus

    async def append(self, event: DomainEvent) -> None:
        event.version = len([e for e in self._events if e.aggregate_id == event.aggregate_id]) + 1
        self._events.append(event)
        await self._bus.emit(event)  # inline projections run now; a failing one fails the append

    def get_events(self, aggregate_id: str) -> list[DomainEvent]:
        return [e for e in self._events if e.aggregate_id == aggregate_id]

    def get_all_events(self, event_type: str | None = None) -> list[DomainEvent]:
        if event_type:
            return [e for e in self._events if e.event_type == event_type]
        return list(self._events)
```

## Aggregate with event sourcing

```python
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
```

## Command handler / domain service

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class OrderCommandHandler:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    async def create_order(self, order_id: str) -> OrderAggregate:
        event = DomainEvent(aggregate_id=order_id, event_type="OrderCreated", payload={})
        await self._store.append(event)
        return self._load(order_id)

    async def add_item(self, order_id: str, name: str, price: float) -> OrderAggregate:
        self._load(order_id)  # validates order exists
        event = DomainEvent(
            aggregate_id=order_id,
            event_type="ItemAdded",
            payload={"name": name, "price": price},
        )
        await self._store.append(event)
        return self._load(order_id)

    async def complete_order(self, order_id: str) -> OrderAggregate:
        event = DomainEvent(aggregate_id=order_id, event_type="OrderCompleted", payload={})
        await self._store.append(event)
        return self._load(order_id)

    def _load(self, order_id: str) -> OrderAggregate:
        events = self._store.get_events(order_id)
        if not events:
            raise ValueError(f"Order {order_id} does not exist")
        return OrderAggregate.rebuild(order_id, events)
```

## Projections as event handlers

The read model is updated by an `@on_event` handler for every appended event,
and can still be rebuilt from scratch by replaying the log (after a schema
change, or on startup for a persistent store):

```python
from lauren.events import on_event

@injectable(scope=Scope.SINGLETON)
class OrderSummaryProjection:
    """Read model kept up to date from the event stream."""

    def __init__(self, store: EventStore) -> None:
        self._store = store
        self._orders: dict[str, dict] = {}

    @on_event()  # inline: the read model is consistent when append() returns
    async def apply(self, event: DomainEvent) -> None:
        agg_id = event.aggregate_id
        if event.event_type == "OrderCreated":
            self._orders[agg_id] = {"id": agg_id, "status": "created", "item_count": 0, "total": 0.0}
        elif event.event_type == "ItemAdded" and agg_id in self._orders:
            self._orders[agg_id]["item_count"] += 1
            self._orders[agg_id]["total"] += event.payload.get("price", 0.0)
        elif event.event_type == "OrderCompleted" and agg_id in self._orders:
            self._orders[agg_id]["status"] = "completed"

    async def rebuild(self) -> None:
        self._orders.clear()
        for event in self._store.get_all_events():
            await self.apply(event)

    def build(self) -> list[dict]:
        return list(self._orders.values())
```

Use `@on_event(mode="concurrent")` for projections that may lag (search
indexes, analytics): they run after `append()` returns and their failures are
isolated (logged, signalled as `EventHandlerFailed`) instead of failing the
command.

## Controller

```python
from lauren import controller, get, post, module, Path, Json
from lauren.types import Response
from pydantic import BaseModel

class CreateOrderRequest(BaseModel):
    order_id: str

class AddItemRequest(BaseModel):
    name: str
    price: float

@controller("/orders")
class OrderController:
    def __init__(self, handler: OrderCommandHandler, projection: OrderSummaryProjection) -> None:
        self._handler = handler
        self._projection = projection

    @post("/")
    async def create(self, body: Json[CreateOrderRequest]) -> Response:
        order = await self._handler.create_order(body.order_id)
        return Response.json({"id": order.order_id, "status": order.status}, status=201)

    @post("/{order_id}/items")
    async def add_item(self, order_id: Path[str], body: Json[AddItemRequest]) -> dict:
        order = await self._handler.add_item(order_id, body.name, body.price)
        return {"id": order.order_id, "total": order.total, "items": order.items}

    @get("/summary")
    async def summary(self) -> list:
        return self._projection.build()

@module(
    controllers=[OrderController],
    providers=[EventStore, OrderCommandHandler, OrderSummaryProjection],
)
class OrderModule:
    pass
```

## Key points

- `EventStore.append` is the only mutation point — never modify past events.
- Aggregates are pure in-memory objects; they have no awareness of the store.
- Projections are `@on_event` handlers: inline ones are consistent when `append()` returns, `"concurrent"` ones catch up asynchronously. Keep `rebuild()` for replays.
- Handlers subscribed to `DomainEvent` see every event; with one class per event type (`OrderCreated(DomainEvent)`, …) a projection can subscribe to exactly the types it needs.
- The event bus is in-process: other services consume the log through a broker (`skills/message-queue/`).
- For persistence, replace the in-memory list with an append-only DB table (PostgreSQL `INSERT` only, no `UPDATE`/`DELETE`).
