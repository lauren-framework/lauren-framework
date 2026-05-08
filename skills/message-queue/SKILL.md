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
        # For RabbitMQ, wire consumers via @post_construct instead
        raise NotImplementedError("Use on_startup consumer registration")
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

## Consumer registration at startup

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

## Module wiring

```python
from lauren import module

@module(providers=[InMemoryBroker, OrderService, OrderProcessor])
class MessagingModule:
    pass
```

## Key points

- `provides=(MessageBroker,)` on `InMemoryBroker` means any service that declares `broker: MessageBroker` receives the in-memory instance.
- For production, replace `InMemoryBroker` with a `RabbitMQBroker` or `KafkaProducerBroker` in `global_providers`.
- Subscribers registered in `@post_construct` are automatically torn down when `@pre_destruct` runs at shutdown.
- Keep message payloads JSON-serialisable dicts so they are transport-agnostic.
