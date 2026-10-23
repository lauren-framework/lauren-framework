---
name: message-queue
description: Integrates a message broker (RabbitMQ, Kafka, or in-memory) into a Lauren application using an abstract MessageBroker provider. Use when decoupling producers from consumers, implementing event-driven workflows, or deferring background work.
---

> Use `codemap find "injectable"` to locate existing provider registrations before adding a new broker.

# RabbitMQ / Kafka Producer & Consumer Setup

The pattern wraps the broker behind an abstract interface so the in-memory implementation can be used in tests and the real transport plugged in for production.

## Abstract interface

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable

class MessageBroker(ABC):
    @abstractmethod
    async def publish(self, topic: str, message: dict) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, callback: Callable) -> None: ...
```

## In-memory broker (tests / development)

```python
from collections import defaultdict
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON, provides=(MessageBroker,))
class InMemoryBroker(MessageBroker):
    def __init__(self) -> None:
        self._handlers: dict[str, list[Callable]] = defaultdict(list)
        self._messages: dict[str, list] = defaultdict(list)

    async def publish(self, topic: str, message: dict) -> None:
        self._messages[topic].append(message)
        for handler in self._handlers[topic]:
            await handler(message)

    def subscribe(self, topic: str, callback: Callable) -> None:
        self._handlers[topic].append(callback)

    def get_messages(self, topic: str) -> list:
        return list(self._messages[topic])
```

## aio-pika (RabbitMQ) broker

```python
import aio_pika
import json

@injectable(scope=Scope.SINGLETON, provides=(MessageBroker,))
class RabbitMQBroker(MessageBroker):
    def __init__(self) -> None:
        self._url = "amqp://guest:guest@localhost/"
        self._connection: aio_pika.Connection | None = None

    @post_construct
    async def connect(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)

    @pre_destruct
    async def disconnect(self) -> None:
        if self._connection:
            await self._connection.close()

    async def publish(self, topic: str, message: dict) -> None:
        async with self._connection.channel() as channel:
            await channel.default_exchange.publish(
                aio_pika.Message(body=json.dumps(message).encode()),
                routing_key=topic,
            )

    def subscribe(self, topic: str, callback: Callable) -> None:
        # For RabbitMQ, consume from a @hosted_service instead (see below)
        raise NotImplementedError("consume from a @hosted_service")

    async def consume(self, topic: str):
        async with self._connection.channel() as channel:
            queue = await channel.declare_queue(topic, durable=True)
            async with queue.iterator() as messages:
                async for message in messages:
                    async with message.process():
                        yield json.loads(message.body)
```

## Producer service

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class OrderService:
    def __init__(self, broker: MessageBroker) -> None:
        self._broker = broker

    async def place_order(self, order_id: str, items: list[dict]) -> dict:
        order = {"id": order_id, "items": items, "status": "pending"}
        await self._broker.publish("orders.created", order)
        return order
```

## In-process subscribers

For the in-memory broker a subscription made in `@post_construct` is enough —
nothing runs in the background:

```python
from lauren import injectable, Scope, post_construct

@injectable(scope=Scope.SINGLETON)
class OrderProcessor:
    def __init__(self, broker: MessageBroker) -> None:
        self._broker = broker
        self._processed: list[dict] = []

    @post_construct
    async def start(self) -> None:
        self._broker.subscribe("orders.created", self._handle_order)

    async def _handle_order(self, message: dict) -> None:
        # Business logic here
        self._processed.append({"id": message["id"], "status": "processed"})

    def get_processed(self) -> list[dict]:
        return list(self._processed)
```

## Long-running consumers: `@hosted_service`

A real broker consumer is a loop. Make it a hosted service — never
`asyncio.create_task(...)` in `@post_construct`. The framework starts it after
every `@post_construct` (so `RabbitMQBroker.connect` already ran), restarts it
with backoff when it crashes, and stops it **before** the broker's
`@pre_destruct` closes the connection:

```python
from lauren import Scope, injectable
from lauren.scheduling import ServiceContext, hosted_service


@injectable(scope=Scope.REQUEST)
class OrderRepository:
    async def aclose(self) -> None: ...       # e.g. release the DB session


@hosted_service(restart="on-failure", max_restarts=10, backoff=(1.0, 60.0), stop_timeout=15.0)
class OrderConsumer:
    def __init__(self, broker: RabbitMQBroker) -> None:
        self._broker = broker

    async def run(self, ctx: ServiceContext) -> None:
        async for message in self._broker.consume("orders.created"):
            async with ctx.scope() as scope:              # one unit of work per message
                repo = await scope.resolve(OrderRepository)
                ctx.log.info("order processed", id=message["id"], repo=type(repo).__name__)
            if ctx.stop_requested:
                break
```

Shutdown cancels the service after `stop_timeout` if it is blocked waiting for
the next message, so the loop does not need its own timeout. Tests build the
app with `LaurenFactory.create(..., hosted_services=False)` and call the
handler directly, or drive `await app.startup()` / `await app.shutdown()` in
one event loop.

## Module wiring

```python
from lauren import module

@module(providers=[InMemoryBroker, OrderService, OrderProcessor])
class MessagingModule:
    pass


@module(providers=[RabbitMQBroker, OrderService, OrderConsumer, OrderRepository])
class ProductionMessagingModule:
    pass
```

## Key points

- `provides=(MessageBroker,)` on `InMemoryBroker` means any service that declares `broker: MessageBroker` receives the in-memory instance.
- For production, replace `InMemoryBroker` with a `RabbitMQBroker` or `KafkaProducerBroker` in `global_providers`.
- In-memory subscriptions made in `@post_construct` need no teardown; broker consumers are `@hosted_service` loops, stopped before `@pre_destruct`.
- Keep message payloads JSON-serialisable dicts so they are transport-agnostic.
