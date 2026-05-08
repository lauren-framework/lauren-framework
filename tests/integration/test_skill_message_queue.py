"""Integration tests for skill 26: Message Queue Producer & Consumer.

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Callable

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
    post_construct,
)
from lauren.testing import TestClient
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Abstract interface
# ---------------------------------------------------------------------------


class MessageBroker(ABC):
    @abstractmethod
    async def publish(self, topic: str, message: dict) -> None: ...

    @abstractmethod
    def subscribe(self, topic: str, callback: Callable) -> None: ...


# ---------------------------------------------------------------------------
# In-memory broker
# ---------------------------------------------------------------------------


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

    def topic_count(self, topic: str) -> int:
        return len(self._messages[topic])


# ---------------------------------------------------------------------------
# Producer service
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class OrderService:
    def __init__(self, broker: MessageBroker) -> None:
        self._broker = broker

    async def place_order(self, order_id: str, items: list[dict]) -> dict:
        order = {"id": order_id, "items": items, "status": "pending"}
        await self._broker.publish("orders.created", order)
        return order

    async def cancel_order(self, order_id: str) -> dict:
        event = {"id": order_id, "status": "cancelled"}
        await self._broker.publish("orders.cancelled", event)
        return event


# ---------------------------------------------------------------------------
# Consumer service
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class OrderProcessor:
    def __init__(self, broker: MessageBroker) -> None:
        self._broker = broker
        self._processed: list[dict] = []
        self._cancelled: list[str] = []

    @post_construct
    async def start(self) -> None:
        self._broker.subscribe("orders.created", self._handle_created)
        self._broker.subscribe("orders.cancelled", self._handle_cancelled)

    async def _handle_created(self, message: dict) -> None:
        self._processed.append({"id": message["id"], "status": "processed"})

    async def _handle_cancelled(self, message: dict) -> None:
        self._cancelled.append(message["id"])

    def get_processed(self) -> list[dict]:
        return list(self._processed)

    def get_cancelled(self) -> list[str]:
        return list(self._cancelled)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class PlaceOrderRequest(BaseModel):
    order_id: str
    items: list[dict] = []


@controller("/orders")
class OrderController:
    def __init__(
        self,
        service: OrderService,
        processor: OrderProcessor,
        broker: MessageBroker,
    ) -> None:
        self._service = service
        self._processor = processor
        self._broker = broker

    @post("/")
    async def place_order(self, body: Json[PlaceOrderRequest]) -> dict:
        return await self._service.place_order(body.order_id, body.items)

    @post("/{order_id}/cancel")
    async def cancel_order(self, order_id: Path[str]) -> dict:
        return await self._service.cancel_order(order_id)

    @get("/processed")
    async def processed(self) -> list:
        return self._processor.get_processed()

    @get("/cancelled")
    async def cancelled(self) -> list:
        return self._processor.get_cancelled()

    @get("/broker/{topic}/messages")
    async def broker_messages(self, topic: Path[str]) -> dict:
        return {
            "topic": topic,
            "count": self._broker.topic_count(topic),
            "messages": self._broker.get_messages(topic),
        }


@module(
    controllers=[OrderController],
    providers=[InMemoryBroker, OrderService, OrderProcessor],
)
class MessagingModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(MessagingModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestMessageBrokerViaHTTP:
    def test_publish_stores_message_in_broker(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "ord-broker-1"})
        r = client.get("/orders/broker/orders.created/messages")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] == 1
        assert data["messages"][0]["id"] == "ord-broker-1"

    def test_multiple_publishes_accumulate(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "b1"})
        client.post("/orders/", json={"order_id": "b2"})
        r = client.get("/orders/broker/orders.created/messages")
        assert r.json()["count"] == 2

    def test_cancel_publishes_to_different_topic(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "b3"})
        client.post("/orders/b3/cancel")
        r = client.get("/orders/broker/orders.cancelled/messages")
        assert r.json()["count"] == 1

    def test_empty_topic_returns_zero_count(self) -> None:
        client = build_app()
        r = client.get("/orders/broker/nonexistent.topic/messages")
        assert r.json()["count"] == 0

    def test_different_topics_are_isolated(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "iso-1"})
        created_r = client.get("/orders/broker/orders.created/messages")
        cancelled_r = client.get("/orders/broker/orders.cancelled/messages")
        assert created_r.json()["count"] == 1
        assert cancelled_r.json()["count"] == 0


class TestMessageQueueIntegration:
    def test_place_order_publishes_event(self) -> None:
        client = build_app()
        r = client.post("/orders/", json={"order_id": "ord-1", "items": [{"sku": "A"}]})
        assert r.status_code == 200
        assert r.json()["id"] == "ord-1"

    def test_consumer_processes_order(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "ord-2"})
        r = client.get("/orders/processed")
        processed_ids = [p["id"] for p in r.json()]
        assert "ord-2" in processed_ids

    def test_cancel_order_triggers_consumer(self) -> None:
        client = build_app()
        client.post("/orders/", json={"order_id": "ord-3"})
        client.post("/orders/ord-3/cancel")
        r = client.get("/orders/cancelled")
        assert "ord-3" in r.json()

    def test_multiple_orders_all_processed(self) -> None:
        client = build_app()
        for i in range(5):
            client.post("/orders/", json={"order_id": f"ord-{i}"})
        r = client.get("/orders/processed")
        assert len(r.json()) == 5
