---
name: message-queue
description: Decouples producers from consumers in a Lauren application — in-process with the typed application event bus (EventBus + @on_event), across processes with a message broker (RabbitMQ, Kafka) behind an abstract MessageBroker provider, and bridges the two. Use when decoupling producers from consumers, implementing event-driven workflows, or deferring work to other services.
---

> Use `codemap find "EventBus"` / `codemap find "injectable"` to locate the bus and existing provider registrations before adding a broker.

# Event-driven workflows: in-process events and brokers

Two layers, two tools:

| Need | Use |
|---|---|
| React inside **this** process (other modules, read models, mails) | the application event bus: `EventBus` / `EventPublisher` + `@on_event` |
| Hand work to **other** processes or services, durably | a message broker (RabbitMQ, Kafka) behind a `MessageBroker` provider |

Do not hand-roll a string-topic in-memory broker for the first case: the
event bus is typed, DI-aware, validated at startup, ordered, and drained at
shutdown. See `skills/building-lauren-events/` for every option.

## In-process: typed events

```python
from __future__ import annotations

from dataclasses import dataclass

from lauren import injectable, module
from lauren.events import EventBus, EventContext, on_event


@dataclass(frozen=True)
class OrderCreated:
    order_id: str
    items: tuple[str, ...]


@injectable()
class OrderService:
    def __init__(self, events: EventBus) -> None:
        self._events = events

    async def place_order(self, order_id: str, items: list[str]) -> dict:
        order = {"id": order_id, "items": items, "status": "pending"}
        await self._events.emit(OrderCreated(order_id, tuple(items)))
        return order


@injectable()
class OrderProcessor:
    def __init__(self) -> None:
        self.processed: list[str] = []

    @on_event(mode="concurrent")              # runs after emit() returns, as a tracked task
    async def handle(self, event: OrderCreated, ctx: EventContext) -> None:
        self.processed.append(event.order_id)


@module(providers=[OrderService, OrderProcessor])
class OrdersModule:
    pass
```

* Handlers are discovered on every provider at startup — no `subscribe()`
  calls, nothing to unregister: the bus drains (then cancels) running
  handlers during `app.shutdown()`.
* Inline handlers (the default mode) fail the producer when they raise;
  `"concurrent"` / `"background"` handlers are isolated and reported as
  `EventHandlerFailed`.
* In a route handler, inject `EventPublisher` instead so inline handlers
  share the request's DI scope and `"background"` handlers run after the
  response.

Test it without a broker:

```python
async def test_order_is_processed(app: LaurenApp) -> None:
    await app.startup()
    bus = await app.container.resolve(EventBus)
    service = await app.container.resolve(OrderService)
    with bus.recording() as rec:
        await service.place_order("o1", ["book"])
        await bus.drain()
    assert rec.of(OrderCreated)[0].order_id == "o1"
    assert rec.handler_calls(OrderProcessor.handle) == 1
```

## Across processes: a broker provider

```python
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable

from lauren import Scope, injectable, post_construct, pre_destruct


class MessageBroker(ABC):
    @abstractmethod
    async def publish(self, topic: str, message: dict) -> None: ...

    @abstractmethod
    async def consume(self, topic: str, callback: Callable[[dict], Awaitable[None]]) -> None: ...
```

### aio-pika (RabbitMQ)

```python
import json

import aio_pika


@injectable(scope=Scope.SINGLETON, provides=(MessageBroker,))
class RabbitMQBroker(MessageBroker):
    def __init__(self) -> None:
        self._url = "amqp://guest:guest@localhost/"
        self._connection: aio_pika.abc.AbstractRobustConnection | None = None
        self._channel: aio_pika.abc.AbstractChannel | None = None

    @post_construct
    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel()

    @pre_destruct
    async def disconnect(self) -> None:
        if self._connection is not None:
            await self._connection.close()   # also cancels the consumers below

    async def publish(self, topic: str, message: dict) -> None:
        await self._channel.default_exchange.publish(
            aio_pika.Message(body=json.dumps(message).encode()),
            routing_key=topic,
        )

    async def consume(self, topic: str, callback: Callable[[dict], Awaitable[None]]) -> None:
        queue = await self._channel.declare_queue(topic, durable=True)

        async def on_message(message: aio_pika.abc.AbstractIncomingMessage) -> None:
            async with message.process():
                await callback(json.loads(message.body))

        await queue.consume(on_message)
```

## Bridging the two

Forward selected domain events to the broker with an ordinary handler, and
turn incoming broker messages back into domain events — the rest of the app
only ever sees typed events:

```python
@injectable()
class OrderOutbound:
    def __init__(self, broker: MessageBroker) -> None:
        self._broker = broker

    @on_event(mode="background")              # after the response when published in a request
    async def forward(self, event: OrderCreated) -> None:
        await self._broker.publish("orders.created", {"id": event.order_id, "items": list(event.items)})


@injectable()
class OrderInbound:
    def __init__(self, broker: MessageBroker, events: EventBus) -> None:
        self._broker = broker
        self._events = events

    @post_construct
    async def start(self) -> None:
        await self._broker.consume("orders.created", self._on_message)

    async def _on_message(self, message: dict) -> None:
        await self._events.emit(OrderCreated(message["id"], tuple(message["items"])))
```

## Key points

- In-process events are **not durable**: deferred handlers still queued when
  the process dies are lost. Anything that must survive a crash goes through
  the broker (or a transactional outbox).
- `provides=(MessageBroker,)` lets every service declare `broker: MessageBroker`;
  swap implementations in `global_providers` or with `use_class` in tests.
- Consumers are tied to the broker connection: closing it in `@pre_destruct`
  stops them at shutdown.
- Keep broker payloads JSON-serialisable dicts so they are transport-agnostic;
  keep in-process events typed.
