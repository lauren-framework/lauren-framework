"""Integration tests for skill 28: Transactional Email Service.

All tests go through the TestClient / HTTP layer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

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
class EmailMessage:
    to: list[str]
    subject: str
    body: str
    html_body: str = ""
    from_addr: str = "noreply@example.com"
    reply_to: str = ""


class EmailBackend(ABC):
    @abstractmethod
    async def send(self, message: EmailMessage) -> None: ...


# ---------------------------------------------------------------------------
# In-memory backend
# ---------------------------------------------------------------------------


class InMemoryEmailBackend(EmailBackend):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)

    def find(self, to: str) -> list[EmailMessage]:
        return [m for m in self.sent if to in m.to]

    def clear(self) -> None:
        self.sent.clear()


# ---------------------------------------------------------------------------
# Email service
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class EmailService:
    def __init__(self, backend: EmailBackend) -> None:
        self._backend = backend

    async def send_welcome_email(self, to_addr: str, username: str) -> None:
        await self._backend.send(
            EmailMessage(
                to=[to_addr],
                subject=f"Welcome, {username}!",
                body=f"Hi {username}, welcome to our platform.",
                html_body=f"<h1>Welcome {username}</h1>",
            )
        )

    async def send_password_reset(self, to_addr: str, reset_link: str) -> None:
        await self._backend.send(
            EmailMessage(
                to=[to_addr],
                subject="Reset your password",
                body=f"Click here to reset your password: {reset_link}",
                html_body=f'<p>Click <a href="{reset_link}">here</a> to reset.</p>',
            )
        )

    async def send_order_confirmation(
        self, to_addr: str, order_id: str, total: float
    ) -> None:
        await self._backend.send(
            EmailMessage(
                to=[to_addr],
                subject=f"Order #{order_id} confirmed",
                body=f"Your order #{order_id} for ${total:.2f} has been confirmed.",
            )
        )


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class SendWelcomeRequest(BaseModel):
    to: str
    username: str


class SendResetRequest(BaseModel):
    to: str
    reset_link: str


class SendOrderRequest(BaseModel):
    to: str
    order_id: str
    total: float


_test_backend = InMemoryEmailBackend()


@controller("/email")
class EmailController:
    def __init__(self, service: EmailService) -> None:
        self._service = service

    @post("/welcome")
    async def welcome(self, body: Json[SendWelcomeRequest]) -> dict:
        await self._service.send_welcome_email(body.to, body.username)
        return {"sent": True}

    @post("/reset")
    async def reset(self, body: Json[SendResetRequest]) -> dict:
        await self._service.send_password_reset(body.to, body.reset_link)
        return {"sent": True}

    @post("/order-confirmation")
    async def order_confirmation(self, body: Json[SendOrderRequest]) -> dict:
        await self._service.send_order_confirmation(body.to, body.order_id, body.total)
        return {"sent": True}

    @get("/sent-count")
    async def sent_count(self) -> dict:
        return {"count": len(_test_backend.sent)}

    @get("/inbox/{address}")
    async def inbox(self, address: Path[str]) -> list:
        return [
            {
                "to": m.to,
                "subject": m.subject,
                "body": m.body,
                "html_body": m.html_body,
                "from_addr": m.from_addr,
            }
            for m in _test_backend.find(address)
        ]

    @http_delete("/inbox")
    async def clear_inbox(self) -> dict:
        _test_backend.clear()
        return {"cleared": True}


# ---------------------------------------------------------------------------
# Module wiring using use_value
# ---------------------------------------------------------------------------


@module(
    controllers=[EmailController],
    providers=[
        use_value(provide=EmailBackend, value=_test_backend),
        EmailService,
    ],
)
class EmailModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    _test_backend.clear()
    return TestClient(LaurenFactory.create(EmailModule))


# ---------------------------------------------------------------------------
# Tests — all through TestClient
# ---------------------------------------------------------------------------


class TestEmailBackendViaClient:
    def test_send_stores_message(self) -> None:
        client = build_app()
        client.post("/email/welcome", json={"to": "x@y.com", "username": "X"})
        r = client.get("/email/inbox/x@y.com")
        assert r.status_code == 200
        assert len(r.json()) == 1

    def test_find_by_recipient(self) -> None:
        client = build_app()
        client.post(
            "/email/welcome", json={"to": "alice@example.com", "username": "Alice"}
        )
        client.post("/email/welcome", json={"to": "bob@example.com", "username": "Bob"})
        assert len(client.get("/email/inbox/alice@example.com").json()) == 1
        assert len(client.get("/email/inbox/bob@example.com").json()) == 1
        assert len(client.get("/email/inbox/charlie@example.com").json()) == 0

    def test_clear_empties_inbox(self) -> None:
        client = build_app()
        client.post("/email/welcome", json={"to": "a@b.com", "username": "A"})
        client.delete("/email/inbox")
        r = client.get("/email/sent-count")
        assert r.json()["count"] == 0


class TestEmailServiceViaClient:
    def test_welcome_email_subject_contains_username(self) -> None:
        client = build_app()
        client.post(
            "/email/welcome", json={"to": "alice@example.com", "username": "Alice"}
        )
        msgs = client.get("/email/inbox/alice@example.com").json()
        assert len(msgs) == 1
        assert "Alice" in msgs[0]["subject"]

    def test_welcome_email_has_html_body(self) -> None:
        client = build_app()
        client.post(
            "/email/welcome", json={"to": "alice@example.com", "username": "Alice"}
        )
        msgs = client.get("/email/inbox/alice@example.com").json()
        assert msgs[0]["html_body"] != ""

    def test_password_reset_includes_link(self) -> None:
        client = build_app()
        link = "https://example.com/reset/abc"
        client.post("/email/reset", json={"to": "bob@example.com", "reset_link": link})
        msgs = client.get("/email/inbox/bob@example.com").json()
        assert link in msgs[0]["body"]

    def test_order_confirmation_includes_order_id(self) -> None:
        client = build_app()
        client.post(
            "/email/order-confirmation",
            json={"to": "carol@example.com", "order_id": "ORD-99", "total": 49.95},
        )
        msgs = client.get("/email/inbox/carol@example.com").json()
        assert "ORD-99" in msgs[0]["subject"]

    def test_from_addr_is_noreply_by_default(self) -> None:
        client = build_app()
        client.post(
            "/email/welcome", json={"to": "user@example.com", "username": "User"}
        )
        msgs = client.get("/email/inbox/user@example.com").json()
        assert msgs[0]["from_addr"] == "noreply@example.com"


class TestEmailIntegration:
    def test_welcome_endpoint_returns_sent(self) -> None:
        client = build_app()
        r = client.post(
            "/email/welcome", json={"to": "alice@example.com", "username": "Alice"}
        )
        assert r.status_code == 200
        assert r.json()["sent"] is True

    def test_welcome_email_is_stored_in_backend(self) -> None:
        client = build_app()
        client.post(
            "/email/welcome", json={"to": "alice@example.com", "username": "Alice"}
        )
        assert len(_test_backend.find("alice@example.com")) == 1

    def test_reset_endpoint(self) -> None:
        client = build_app()
        r = client.post(
            "/email/reset",
            json={"to": "bob@example.com", "reset_link": "https://x.com/r/tok"},
        )
        assert r.status_code == 200
        assert _test_backend.find("bob@example.com")[0].subject == "Reset your password"

    def test_order_confirmation_endpoint(self) -> None:
        client = build_app()
        r = client.post(
            "/email/order-confirmation",
            json={"to": "carol@example.com", "order_id": "ORD-1", "total": 29.99},
        )
        assert r.status_code == 200
        msgs = _test_backend.find("carol@example.com")
        assert len(msgs) == 1
        assert "ORD-1" in msgs[0].subject

    def test_multiple_emails_accumulate(self) -> None:
        client = build_app()
        client.post("/email/welcome", json={"to": "a@example.com", "username": "A"})
        client.post("/email/welcome", json={"to": "b@example.com", "username": "B"})
        assert len(_test_backend.sent) == 2
