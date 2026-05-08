---
name: webhook-dispatcher
description: Implements an outbound webhook dispatcher with HMAC-SHA256 request signing, retry logic with exponential backoff, and delivery status tracking. Use when notifying external systems of events via HTTP POST callbacks.
---

> Use `codemap find "WebhookDispatcher"` to check if a dispatcher is already registered.

# Outbound Webhook Dispatcher with Retry & Signatures

The dispatcher sends signed HTTP POST requests to registered callback URLs and retries on failure with exponential backoff.

## Delivery record

```python
from __future__ import annotations
import time
from dataclasses import dataclass, field

@dataclass
class WebhookDelivery:
    webhook_id: str
    url: str
    event: str
    payload: dict
    attempts: int = 0
    status: str = "pending"   # pending | delivered | failed
    last_error: str = ""
    delivered_at: float | None = None
```

## Dispatcher

```python
import asyncio
import hashlib
import hmac
import json
import uuid

import httpx

from lauren import injectable, Scope

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
        body = json.dumps({
            "event": event,
            "data": payload,
            "timestamp": int(time.time()),
        }).encode()
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
                await asyncio.sleep(2 ** attempt)  # 1s, 2s backoff

        delivery.status = "failed"
        return delivery

    def get_deliveries(self, event: str | None = None) -> list[WebhookDelivery]:
        if event:
            return [d for d in self._deliveries if d.event == event]
        return list(self._deliveries)
```

## Subscriber registry

```python
from dataclasses import dataclass, field

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
```

## Fan-out service

```python
import asyncio

@injectable(scope=Scope.SINGLETON)
class WebhookService:
    def __init__(self, dispatcher: WebhookDispatcher, registry: WebhookRegistry) -> None:
        self._dispatcher = dispatcher
        self._registry = registry

    async def emit(self, event: str, payload: dict) -> list[WebhookDelivery]:
        subscribers = self._registry.get_subscribers(event)
        deliveries = await asyncio.gather(
            *[self._dispatcher.dispatch(sub.url, event, payload) for sub in subscribers],
            return_exceptions=False,
        )
        return list(deliveries)
```

## Signature verification (receiver side)

```python
import hmac, hashlib

def verify_webhook(body: bytes, signature_header: str, secret: str) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

## Module wiring

```python
from lauren import module

@module(providers=[WebhookDispatcher, WebhookRegistry, WebhookService])
class WebhookModule:
    pass
```

## Key points

- Always use `hmac.compare_digest` for signature comparison to prevent timing attacks.
- The `X-Webhook-Signature: sha256=<hex>` header mirrors the GitHub/Stripe convention.
- Use `BackgroundTasks` to fire webhooks without blocking the HTTP response: return the response first, then dispatch.
- For high-volume production use, move delivery to a task queue (e.g., Celery or ARQ) and store `WebhookDelivery` rows in a database.
- Exponential backoff: attempt 0 is immediate, attempt 1 waits 1s, attempt 2 waits 2s (2^0, 2^1).
