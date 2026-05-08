"""Integration tests for skill 29: Push Notification Dispatch.

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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
    use_value,
)
from lauren import delete as http_delete
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Core types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryPushBackend(PushBackend):
    def __init__(self) -> None:
        self.sent: list[PushNotification] = []
        self.failed_tokens: set[str] = set()

    async def send(self, notification: PushNotification) -> bool:
        if notification.device_token in self.failed_tokens:
            return False
        self.sent.append(notification)
        return True

    async def send_batch(self, notifications: list[PushNotification]) -> list[bool]:
        return [await self.send(n) for n in notifications]

    def find(self, device_token: str) -> list[PushNotification]:
        return [n for n in self.sent if n.device_token == device_token]

    def clear(self) -> None:
        self.sent.clear()
        self.failed_tokens.clear()


# ---------------------------------------------------------------------------
# Push notification service
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class PushNotificationService:
    def __init__(self, backend: PushBackend) -> None:
        self._backend = backend

    async def send_to_device(
        self,
        device_token: str,
        title: str,
        body: str,
        data: dict | None = None,
    ) -> bool:
        notification = PushNotification(
            device_token=device_token,
            title=title,
            body=body,
            data=data or {},
        )
        return await self._backend.send(notification)

    async def notify_all(
        self, tokens: list[str], title: str, body: str, data: dict | None = None
    ) -> dict:
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


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SendToDeviceRequest(BaseModel):
    device_token: str
    title: str
    body: str
    data: dict = {}


class NotifyAllRequest(BaseModel):
    tokens: list[str]
    title: str
    body: str
    data: dict = {}


class MarkFailedRequest(BaseModel):
    device_token: str


_test_backend = InMemoryPushBackend()


@controller("/push")
class PushController:
    def __init__(self, service: PushNotificationService) -> None:
        self._service = service

    @post("/send")
    async def send(self, body: Json[SendToDeviceRequest]) -> dict:
        delivered = await self._service.send_to_device(
            body.device_token, body.title, body.body, body.data
        )
        return {"delivered": delivered}

    @post("/broadcast")
    async def broadcast(self, body: Json[NotifyAllRequest]) -> dict:
        return await self._service.notify_all(
            body.tokens, body.title, body.body, body.data
        )

    @get("/sent-count")
    async def sent_count(self) -> dict:
        return {"count": len(_test_backend.sent)}

    @get("/inbox/{device_token}")
    async def inbox(self, device_token: Path[str]) -> list:
        return [
            {
                "device_token": n.device_token,
                "title": n.title,
                "body": n.body,
                "data": n.data,
                "badge": n.badge,
                "sound": n.sound,
            }
            for n in _test_backend.find(device_token)
        ]

    @post("/mark-failed")
    async def mark_failed(self, body: Json[MarkFailedRequest]) -> dict:
        _test_backend.failed_tokens.add(body.device_token)
        return {"marked_failed": body.device_token}

    @http_delete("/inbox")
    async def clear_inbox(self) -> dict:
        _test_backend.clear()
        return {"cleared": True}


# ---------------------------------------------------------------------------
# Module
# ---------------------------------------------------------------------------


@module(
    controllers=[PushController],
    providers=[
        use_value(provide=PushBackend, value=_test_backend),
        PushNotificationService,
    ],
)
class PushModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    _test_backend.clear()
    return TestClient(LaurenFactory.create(PushModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestPushBackendViaClient:
    def test_send_returns_true(self) -> None:
        client = build_app()
        r = client.post(
            "/push/send", json={"device_token": "t1", "title": "Hi", "body": "Body"}
        )
        assert r.json()["delivered"] is True

    def test_send_stores_notification(self) -> None:
        client = build_app()
        client.post(
            "/push/send", json={"device_token": "t1", "title": "Hi", "body": "Body"}
        )
        r = client.get("/push/inbox/t1")
        assert len(r.json()) == 1

    def test_failed_token_returns_false(self) -> None:
        client = build_app()
        client.post("/push/mark-failed", json={"device_token": "bad-token"})
        r = client.post(
            "/push/send", json={"device_token": "bad-token", "title": "Hi", "body": ""}
        )
        assert r.json()["delivered"] is False
        assert len(client.get("/push/inbox/bad-token").json()) == 0

    def test_find_by_token(self) -> None:
        client = build_app()
        client.post("/push/send", json={"device_token": "a", "title": "A", "body": ""})
        client.post("/push/send", json={"device_token": "b", "title": "B", "body": ""})
        assert len(client.get("/push/inbox/a").json()) == 1
        assert len(client.get("/push/inbox/b").json()) == 1
        assert len(client.get("/push/inbox/c").json()) == 0

    def test_clear(self) -> None:
        client = build_app()
        client.post("/push/send", json={"device_token": "t", "title": "T", "body": ""})
        client.delete("/push/inbox")
        assert client.get("/push/sent-count").json()["count"] == 0


class TestPushNotificationServiceViaClient:
    def test_send_to_device_success(self) -> None:
        client = build_app()
        r = client.post(
            "/push/send",
            json={"device_token": "tok", "title": "Hello", "body": "World"},
        )
        assert r.json()["delivered"] is True
        assert len(client.get("/push/inbox/tok").json()) == 1

    def test_send_to_device_failure(self) -> None:
        client = build_app()
        client.post("/push/mark-failed", json={"device_token": "dead-token"})
        r = client.post(
            "/push/send",
            json={"device_token": "dead-token", "title": "Hi", "body": "Body"},
        )
        assert r.json()["delivered"] is False

    def test_notify_all_summary(self) -> None:
        client = build_app()
        client.post("/push/mark-failed", json={"device_token": "bad"})
        r = client.post(
            "/push/broadcast",
            json={
                "tokens": ["good1", "good2", "bad"],
                "title": "Alert",
                "body": "Message",
            },
        )
        assert r.json()["total"] == 3
        assert r.json()["delivered"] == 2
        assert r.json()["failed"] == 1

    def test_data_is_stored_in_notification(self) -> None:
        client = build_app()
        client.post(
            "/push/send",
            json={
                "device_token": "tok",
                "title": "Hi",
                "body": "Body",
                "data": {"order_id": "123"},
            },
        )
        msgs = client.get("/push/inbox/tok").json()
        assert msgs[0]["data"] == {"order_id": "123"}

    def test_notification_defaults(self) -> None:
        client = build_app()
        client.post(
            "/push/send", json={"device_token": "d1", "title": "T", "body": "B"}
        )
        msg = client.get("/push/inbox/d1").json()[0]
        assert msg["badge"] == 0
        assert msg["sound"] == "default"


class TestPushIntegration:
    def test_send_endpoint_delivers(self) -> None:
        client = build_app()
        r = client.post(
            "/push/send",
            json={"device_token": "device-1", "title": "Hello", "body": "World"},
        )
        assert r.status_code == 200
        assert r.json()["delivered"] is True

    def test_broadcast_endpoint(self) -> None:
        client = build_app()
        r = client.post(
            "/push/broadcast",
            json={
                "tokens": ["t1", "t2", "t3"],
                "title": "Announcement",
                "body": "Big news",
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["total"] == 3
        assert data["delivered"] == 3
        assert data["failed"] == 0

    def test_broadcast_with_failed_tokens(self) -> None:
        client = build_app()
        client.post("/push/mark-failed", json={"device_token": "broken-token"})
        r = client.post(
            "/push/broadcast",
            json={
                "tokens": ["good-token", "broken-token"],
                "title": "Alert",
                "body": "News",
            },
        )
        data = r.json()
        assert data["delivered"] == 1
        assert data["failed"] == 1

    def test_sent_count_endpoint(self) -> None:
        client = build_app()
        client.post("/push/send", json={"device_token": "t", "title": "T", "body": "B"})
        r = client.get("/push/sent-count")
        assert r.json()["count"] == 1
