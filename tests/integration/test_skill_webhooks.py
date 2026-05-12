"""Integration tests for skill 30: Outbound Webhook Dispatcher."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch


from lauren import (
    LaurenFactory,
    Json,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient
from pydantic import BaseModel


# ---------------------------------------------------------------------------
# Delivery record
# ---------------------------------------------------------------------------


@dataclass
class WebhookDelivery:
    webhook_id: str
    url: str
    event: str
    payload: dict
    attempts: int = 0
    status: str = "pending"
    last_error: str = ""
    delivered_at: float | None = None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class WebhookDispatcher:
    def __init__(self, secret: str = "webhook-secret", timeout: float = 10.0) -> None:
        self._secret = secret
        self._timeout = timeout
        self._deliveries: list[WebhookDelivery] = []

    def _sign(self, payload_bytes: bytes) -> str:
        return hmac.new(self._secret.encode(), payload_bytes, hashlib.sha256).hexdigest()

    async def dispatch(
        self,
        url: str,
        event: str,
        payload: dict,
        max_retries: int = 3,
    ) -> WebhookDelivery:
        body = json.dumps({"event": event, "data": payload, "timestamp": int(time.time())}).encode()
        signature = self._sign(body)
        delivery = WebhookDelivery(
            webhook_id=str(uuid.uuid4()),
            url=url,
            event=event,
            payload=payload,
        )
        self._deliveries.append(delivery)

        for attempt in range(max_retries):
            delivery.attempts += 1
            try:
                import httpx

                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    r = await client.post(
                        url,
                        content=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-Webhook-Signature": f"sha256={signature}",
                            "X-Webhook-Event": event,
                            "X-Webhook-Delivery": delivery.webhook_id,
                        },
                    )
                if r.status_code < 400:
                    delivery.status = "delivered"
                    delivery.delivered_at = time.time()
                    return delivery
                delivery.last_error = f"HTTP {r.status_code}"
            except Exception as exc:
                delivery.last_error = str(exc)

            if attempt < max_retries - 1:
                await asyncio.sleep(0)  # yield; no real sleep in tests

        delivery.status = "failed"
        return delivery

    def get_deliveries(self, event: str | None = None) -> list[WebhookDelivery]:
        if event:
            return [d for d in self._deliveries if d.event == event]
        return list(self._deliveries)


# ---------------------------------------------------------------------------
# Subscriber registry
# ---------------------------------------------------------------------------


@dataclass
class WebhookSubscription:
    id: str
    url: str
    events: list[str]


@injectable(scope=Scope.SINGLETON)
class WebhookRegistry:
    def __init__(self) -> None:
        self._subscriptions: list[WebhookSubscription] = []

    def register(self, url: str, events: list[str]) -> WebhookSubscription:
        sub = WebhookSubscription(id=str(uuid.uuid4()), url=url, events=events)
        self._subscriptions.append(sub)
        return sub

    def get_subscribers(self, event: str) -> list[WebhookSubscription]:
        return [s for s in self._subscriptions if event in s.events]

    def all_subscriptions(self) -> list[WebhookSubscription]:
        return list(self._subscriptions)


# ---------------------------------------------------------------------------
# Fan-out service
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class WebhookService:
    def __init__(self, dispatcher: WebhookDispatcher, registry: WebhookRegistry) -> None:
        self._dispatcher = dispatcher
        self._registry = registry

    async def emit(self, event: str, payload: dict) -> list[WebhookDelivery]:
        subscribers = self._registry.get_subscribers(event)
        deliveries = await asyncio.gather(
            *[self._dispatcher.dispatch(sub.url, event, payload) for sub in subscribers]
        )
        return list(deliveries)


# ---------------------------------------------------------------------------
# Signature verifier (helper for receiver side)
# ---------------------------------------------------------------------------


def verify_webhook_signature(body: bytes, signature_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class RegisterWebhookRequest(BaseModel):
    url: str
    events: list[str]


class EmitEventRequest(BaseModel):
    event: str
    payload: dict = {}


@controller("/webhooks")
class WebhookController:
    def __init__(
        self,
        service: WebhookService,
        registry: WebhookRegistry,
        dispatcher: WebhookDispatcher,
    ) -> None:
        self._service = service
        self._registry = registry
        self._dispatcher = dispatcher

    @post("/subscriptions")
    async def register(self, body: Json[RegisterWebhookRequest]) -> dict:
        sub = self._registry.register(body.url, body.events)
        return {"id": sub.id, "url": sub.url, "events": sub.events}

    @post("/emit")
    async def emit(self, body: Json[EmitEventRequest]) -> dict:
        deliveries = await self._service.emit(body.event, body.payload)
        return {
            "dispatched": len(deliveries),
            "delivered": sum(1 for d in deliveries if d.status == "delivered"),
            "failed": sum(1 for d in deliveries if d.status == "failed"),
        }

    @get("/deliveries")
    async def deliveries(self) -> list:
        return [
            {
                "webhook_id": d.webhook_id,
                "url": d.url,
                "event": d.event,
                "status": d.status,
                "attempts": d.attempts,
            }
            for d in self._dispatcher.get_deliveries()
        ]


@module(
    controllers=[WebhookController],
    providers=[WebhookDispatcher, WebhookRegistry, WebhookService],
)
class WebhookModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(WebhookModule))


# ---------------------------------------------------------------------------
# Unit tests — signature and delivery logic
# ---------------------------------------------------------------------------


class TestWebhookSignature:
    def test_sign_produces_deterministic_hex(self) -> None:
        dispatcher = WebhookDispatcher(secret="test-secret")
        payload = b'{"event": "test"}'
        sig1 = dispatcher._sign(payload)
        sig2 = dispatcher._sign(payload)
        assert sig1 == sig2
        assert len(sig1) == 64  # SHA-256 hex digest

    def test_verify_valid_signature(self) -> None:
        secret = "my-secret"
        body = b'{"event": "order.created"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(body, f"sha256={sig}", secret) is True

    def test_verify_invalid_signature(self) -> None:
        secret = "my-secret"
        body = b'{"event": "order.created"}'
        assert verify_webhook_signature(body, "sha256=invalid", secret) is False

    def test_verify_tampered_body(self) -> None:
        secret = "my-secret"
        original = b'{"event": "order.created"}'
        tampered = b'{"event": "order.deleted"}'
        sig = hmac.new(secret.encode(), original, hashlib.sha256).hexdigest()
        assert verify_webhook_signature(tampered, f"sha256={sig}", secret) is False


class TestWebhookRegistry:
    def test_register_subscription(self) -> None:
        registry = WebhookRegistry()
        sub = registry.register("https://example.com/hook", ["order.created"])
        assert sub.url == "https://example.com/hook"
        assert "order.created" in sub.events

    def test_get_subscribers_for_event(self) -> None:
        registry = WebhookRegistry()
        registry.register("https://a.com/hook", ["order.created", "order.updated"])
        registry.register("https://b.com/hook", ["user.created"])
        subs = registry.get_subscribers("order.created")
        assert len(subs) == 1
        assert subs[0].url == "https://a.com/hook"

    def test_no_subscribers_for_unknown_event(self) -> None:
        registry = WebhookRegistry()
        assert registry.get_subscribers("unknown.event") == []

    def test_multiple_subscribers_for_same_event(self) -> None:
        registry = WebhookRegistry()
        registry.register("https://a.com/hook", ["ping"])
        registry.register("https://b.com/hook", ["ping"])
        assert len(registry.get_subscribers("ping")) == 2


class TestWebhookDelivery:
    def test_successful_delivery(self) -> None:
        dispatcher = WebhookDispatcher()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            delivery = asyncio.run(
                dispatcher.dispatch("https://example.com/hook", "test.event", {"key": "val"})
            )

        assert delivery.status == "delivered"
        assert delivery.attempts == 1
        assert delivery.delivered_at is not None

    def test_failed_delivery_after_retries(self) -> None:
        dispatcher = WebhookDispatcher()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            delivery = asyncio.run(
                dispatcher.dispatch("https://example.com/hook", "test.event", {}, max_retries=3)
            )

        assert delivery.status == "failed"
        assert delivery.attempts == 3
        assert "connection refused" in delivery.last_error

    def test_retries_on_http_error(self) -> None:
        dispatcher = WebhookDispatcher()

        mock_response = MagicMock()
        mock_response.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            delivery = asyncio.run(
                dispatcher.dispatch("https://example.com/hook", "test.event", {}, max_retries=2)
            )

        assert delivery.status == "failed"
        assert delivery.attempts == 2
        assert "HTTP 500" in delivery.last_error

    def test_delivery_recorded_in_dispatcher(self) -> None:
        dispatcher = WebhookDispatcher()

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            asyncio.run(dispatcher.dispatch("https://example.com/hook", "order.created", {}))

        deliveries = dispatcher.get_deliveries("order.created")
        assert len(deliveries) == 1


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


class TestWebhookIntegration:
    def test_register_subscription(self) -> None:
        client = build_app()
        r = client.post(
            "/webhooks/subscriptions",
            json={"url": "https://example.com/hook", "events": ["order.created"]},
        )
        assert r.status_code == 200
        assert r.json()["url"] == "https://example.com/hook"

    def test_emit_dispatches_to_subscribers(self) -> None:
        client = build_app()
        client.post(
            "/webhooks/subscriptions",
            json={"url": "https://example.com/hook", "events": ["order.placed"]},
        )

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_http):
            r = client.post(
                "/webhooks/emit",
                json={"event": "order.placed", "payload": {"order_id": "123"}},
            )

        assert r.status_code == 200
        assert r.json()["dispatched"] == 1
        assert r.json()["delivered"] == 1

    def test_deliveries_endpoint(self) -> None:
        client = build_app()
        client.post(
            "/webhooks/subscriptions",
            json={"url": "https://example.com/hook", "events": ["ping"]},
        )

        mock_response = MagicMock()
        mock_response.status_code = 200

        mock_http = AsyncMock()
        mock_http.post = AsyncMock(return_value=mock_response)
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_http):
            client.post("/webhooks/emit", json={"event": "ping", "payload": {}})

        r = client.get("/webhooks/deliveries")
        assert r.status_code == 200
        deliveries = r.json()
        assert len(deliveries) == 1
        assert deliveries[0]["event"] == "ping"
        assert deliveries[0]["status"] == "delivered"

    def test_no_subscribers_means_zero_dispatched(self) -> None:
        client = build_app()
        r = client.post("/webhooks/emit", json={"event": "ghost.event", "payload": {}})
        assert r.json()["dispatched"] == 0
