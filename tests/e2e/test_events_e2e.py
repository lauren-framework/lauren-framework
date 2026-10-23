"""End-to-end test for the application event bus across modules.

A users module publishes ``UserRegistered`` without knowing who listens. An
audit module writes an audit row inline, inside the same request-scoped
"transaction" the controller used; a notifications module sends a welcome
mail in the background, after the response. The users module imports
neither of them. Driven through the synchronous ``TestClient``: background
handlers run on the request's ``BackgroundTasks`` queue, so they finish
before the client returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from lauren import Json, LaurenFactory, Scope, controller, injectable, module, post, use_value
from lauren.events import EventContext, EventPublisher, on_event
from lauren.testing import TestClient


@dataclass(frozen=True)
class UserRegistered:
    user_id: str
    email: str


@dataclass
class RegisterUser:
    email: str


@dataclass
class World:
    """Everything the app did, in order (shared by the providers below)."""

    log: list[str] = field(default_factory=list)
    audit_rows: list[dict[str, Any]] = field(default_factory=list)
    mails: list[str] = field(default_factory=list)
    fail_audit: bool = False


@injectable(scope=Scope.REQUEST)
class Transaction:
    """A request-scoped unit of work, committed when the request scope closes."""

    def __init__(self, world: World) -> None:
        self.world = world
        self.id = len(world.log)

    async def aclose(self) -> None:
        self.world.log.append(f"commit tx{self.id}")


@controller("/users")
class UsersController:
    def __init__(self, world: World) -> None:
        self.world = world

    @post("/")
    async def register(self, body: Json[RegisterUser], tx: Transaction, events: EventPublisher) -> Any:
        user_id = f"u{len(self.world.audit_rows) + 1}"
        self.world.log.append(f"insert {user_id} in tx{tx.id}")
        await events.emit(UserRegistered(user_id, body.email))
        return {"id": user_id}, 201


@injectable()
class AuditTrail:
    def __init__(self, world: World) -> None:
        self.world = world

    @on_event(priority=100)
    async def record(self, event: UserRegistered, tx: Transaction, ctx: EventContext) -> None:
        if self.world.fail_audit:
            raise RuntimeError("audit store unavailable")
        self.world.audit_rows.append({"user": event.user_id, "tx": tx.id, "event_id": ctx.event_id})
        self.world.log.append(f"audit {event.user_id} in tx{tx.id}")


@injectable()
class WelcomeMailer:
    def __init__(self, world: World) -> None:
        self.world = world

    @on_event(mode="background")
    async def send_welcome(self, event: UserRegistered, ctx: EventContext) -> None:
        self.world.mails.append(event.email)
        self.world.log.append(f"mail {event.email} ({ctx.mode})")


def build(world: World) -> Any:
    @module(controllers=[UsersController], providers=[Transaction], exports=[Transaction])
    class UsersModule:
        pass

    @module(imports=[UsersModule], providers=[AuditTrail])
    class AuditModule:
        pass

    @module(providers=[WelcomeMailer])
    class NotificationsModule:
        pass

    @module(imports=[UsersModule, AuditModule, NotificationsModule])
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, global_providers=[use_value(provide=World, value=world)])


@pytest.fixture
def world() -> World:
    return World()


def test_registration_audits_inline_and_mails_after_the_response(world: World) -> None:
    client = TestClient(build(world))

    response = client.post("/users/", json={"email": "ada@example.com"})

    assert response.status_code == 201
    assert response.json() == {"id": "u1"}
    assert world.log == [
        "insert u1 in tx0",
        "audit u1 in tx0",
        "commit tx0",
        "mail ada@example.com (background)",
    ]
    assert world.audit_rows[0]["tx"] == 0
    assert len(world.audit_rows[0]["event_id"]) == 32


def test_an_audit_failure_fails_the_request_and_sends_no_mail(world: World) -> None:
    world.fail_audit = True
    client = TestClient(build(world))

    response = client.post("/users/", json={"email": "bob@example.com"})

    assert response.status_code == 500
    assert world.mails == []
    assert world.audit_rows == []


def test_each_request_gets_its_own_transaction(world: World) -> None:
    client = TestClient(build(world))

    client.post("/users/", json={"email": "a@example.com"})
    client.post("/users/", json={"email": "b@example.com"})

    assert [row["tx"] for row in world.audit_rows] == [0, 4]
    assert world.mails == ["a@example.com", "b@example.com"]
