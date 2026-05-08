---
name: push-notifications
description: Adds push notification dispatch to a Lauren application using an abstract PushBackend. Use when sending Firebase Cloud Messaging (FCM), Apple Push Notification service (APNs), or Web Push notifications to mobile or browser clients.
---

> Use `codemap find "PushNotificationService"` to check if a push service is already wired.

# Push Notification Dispatch (FCM / APNs / Web Push)

The pattern mirrors the email service pattern: an abstract `PushBackend` with an in-memory implementation for tests and real transport adapters for production.

## Core types

```python
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

@dataclass
class PushNotification:
    device_token: str
    title: str
    body: str
    data: dict = field(default_factory=dict)
    badge: int = 0
    sound: str = "default"

class PushBackend(ABC):
    @abstractmethod
    async def send(self, notification: PushNotification) -> bool: ...

    @abstractmethod
    async def send_batch(self, notifications: list[PushNotification]) -> list[bool]: ...
```

## In-memory backend (tests / development)

```python
class InMemoryPushBackend(PushBackend):
    def __init__(self) -> None:
        self.sent: list[PushNotification] = []
        self.failed_tokens: set[str] = set()  # simulate delivery failures

    async def send(self, notification: PushNotification) -> bool:
        if notification.device_token in self.failed_tokens:
            return False
        self.sent.append(notification)
        return True

    async def send_batch(self, notifications: list[PushNotification]) -> list[bool]:
        return [await self.send(n) for n in notifications]

    def find(self, device_token: str) -> list[PushNotification]:
        return [n for n in self.sent if n.device_token == device_token]
```

## FCM HTTP v1 backend

```python
import httpx

class FCMPushBackend(PushBackend):
    """Firebase Cloud Messaging HTTP v1 API."""

    def __init__(self, project_id: str, service_account_token: str) -> None:
        self._project_id = project_id
        self._token = service_account_token
        self._url = f"https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"

    async def send(self, notification: PushNotification) -> bool:
        payload = {
            "message": {
                "token": notification.device_token,
                "notification": {
                    "title": notification.title,
                    "body": notification.body,
                },
                "data": {str(k): str(v) for k, v in notification.data.items()},
                "android": {"priority": "high"},
                "apns": {
                    "payload": {
                        "aps": {
                            "sound": notification.sound,
                            "badge": notification.badge,
                        }
                    }
                },
            }
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                self._url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            return r.status_code == 200

    async def send_batch(self, notifications: list[PushNotification]) -> list[bool]:
        import asyncio
        results = await asyncio.gather(*[self.send(n) for n in notifications])
        return list(results)
```

## APNs backend

```python
class APNsPushBackend(PushBackend):
    """Apple Push Notification service HTTP/2 backend.
    Requires httpx[http2] and a valid APNs certificate.
    """

    def __init__(self, team_id: str, key_id: str, private_key_pem: str, bundle_id: str) -> None:
        self._team_id = team_id
        self._key_id = key_id
        self._private_key_pem = private_key_pem
        self._bundle_id = bundle_id

    def _make_jwt(self) -> str:
        import time
        import jwt  # pip install PyJWT
        return jwt.encode(
            {"iss": self._team_id, "iat": int(time.time())},
            self._private_key_pem,
            algorithm="ES256",
            headers={"kid": self._key_id},
        )

    async def send(self, notification: PushNotification) -> bool:
        async with httpx.AsyncClient(http2=True) as client:
            r = await client.post(
                f"https://api.push.apple.com/3/device/{notification.device_token}",
                json={
                    "aps": {
                        "alert": {"title": notification.title, "body": notification.body},
                        "sound": notification.sound,
                        "badge": notification.badge,
                    },
                    **notification.data,
                },
                headers={
                    "authorization": f"bearer {self._make_jwt()}",
                    "apns-topic": self._bundle_id,
                },
            )
            return r.status_code == 200

    async def send_batch(self, notifications: list[PushNotification]) -> list[bool]:
        import asyncio
        return list(await asyncio.gather(*[self.send(n) for n in notifications]))
```

## Push notification service

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class PushNotificationService:
    def __init__(self, backend: PushBackend) -> None:
        self._backend = backend

    async def send_to_device(self, device_token: str, title: str, body: str, data: dict | None = None) -> bool:
        notification = PushNotification(
            device_token=device_token,
            title=title,
            body=body,
            data=data or {},
        )
        return await self._backend.send(notification)

    async def notify_all(self, tokens: list[str], title: str, body: str, data: dict | None = None) -> dict:
        notifications = [
            PushNotification(device_token=t, title=title, body=body, data=data or {})
            for t in tokens
        ]
        results = await self._backend.send_batch(notifications)
        return {
            "total": len(tokens),
            "delivered": sum(results),
            "failed": len(tokens) - sum(results),
        }
```

## Module wiring

```python
from lauren import module
from lauren import use_value

backend = InMemoryPushBackend()

@module(providers=[
    use_value(provide=PushBackend, value=backend),
    PushNotificationService,
])
class PushModule:
    pass
```

## Key points

- Device tokens expire; implement a token rotation strategy by subscribing to FCM/APNs unregister callbacks.
- For FCM, use a service account with `google-auth` for token refresh, not a hardcoded Bearer token.
- `send_batch` should be preferred over calling `send` in a loop — most backends support batching natively.
- Store device tokens in a `DeviceRegistry` singleton alongside user IDs to enable user-level targeting.
