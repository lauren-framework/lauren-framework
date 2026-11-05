---
name: building-lauren-events
description: Decouples Lauren modules with typed in-process domain events. Covers event classes, @on_event handlers (inference, base-class subscriptions, priorities, inline/concurrent/background modes, propagate/isolate errors), publishing with EventBus (singleton) vs EventPublisher (request-scoped), EventContext, after-commit emission via UnitOfWork, startup validation errors, shutdown draining, and testing with recording()/drain()/disabled(). Use when one module must react to something another module did (welcome mail, audit row, read model, webhook) without importing it.
---

> Use `codemap find "EventBus"` (or `"on_event"`) to locate the bus before reading —
> it lives in `lauren/_events/` and is re-exported from `lauren.events`.

# Lauren Application Events

Publisher depends on the **event class** only; every reaction is an
`@on_event` method on a provider in its own module. Handlers are discovered
and validated in `LaurenFactory.create`. Imports:

```python
from lauren.events import (
    EmitResult, EventBus, EventContext, EventPublisher, EventsModule,
    TransactionPhase, UnitOfWork, on_event,
)
```

`EventBus`, `EventPublisher`, `on_event`, `EventContext`, `EmitResult` and
`EventHandlerFailed` are also exported from `lauren`.

## 1. Define events

```python
from dataclasses import dataclass


@dataclass(frozen=True)
class DomainEvent:
    """Optional base: a handler on it receives every subclass."""


@dataclass(frozen=True)
class UserRegistered(DomainEvent):
    user_id: str
    email: str
```

Plain values only — never the `Request` or a request-scoped instance.

## 2. Handle them

```python
from lauren import injectable


@injectable()
class WelcomeMailer:
    def __init__(self, mail: MailClient) -> None:
        self._mail = mail

    @on_event(mode="background")                  # class inferred from the annotation
    async def send_welcome(self, event: UserRegistered) -> None:
        await self._mail.send(event.email, template="welcome")


@injectable()
class AuditTrail:
    @on_event(priority=100)                       # inline (default): part of the unit of work
    async def record(self, event: DomainEvent, db: DbSession, ctx: EventContext) -> None:
        await db.insert_audit(kind=type(event).__name__, event_id=ctx.event_id)
```

| Option | Values | Notes |
|---|---|---|
| `event_type` | class or `None` | `None` infers from the first parameter's annotation (unions → each member) |
| `mode` | `"inline"` / `"concurrent"` / `"background"` | background = after the HTTP response via `EventPublisher`, else like concurrent |
| `priority` | int, default 0 | higher first; ties → discovery order |
| `errors` | `"propagate"` / `"isolate"` | inline default propagate; deferred modes always isolate |
| `enabled` | bool | `False` = validated, never called |

Handler parameters: the event first, then `EventContext` (any name) and
dependencies visible from the provider's own module. Sync `def` handlers
run in a worker thread. Stack `@on_event` to subscribe to several classes.

## 3. Publish

```python
from lauren import controller, injectable, post


@injectable()                                     # singleton: no request
class UserService:
    def __init__(self, events: EventBus) -> None:
        self._events = events

    async def import_user(self, user_id: str, email: str) -> EmitResult:
        return await self._events.emit(UserRegistered(user_id, email))


@controller("/users")
class UserController:
    @post("/")
    async def register(self, body: Json[RegisterUser], events: EventPublisher, db: DbSession) -> Any:
        user = await create_user(db, body)
        await events.emit(UserRegistered(user.id, user.email))   # AuditTrail.record gets the same `db`
        return {"id": user.id}, 201
```

| Inject | Inline handlers' request-scoped deps | Background handlers |
|---|---|---|
| `EventBus` (singleton) | one fresh `OperationScope` per `emit()` | tracked task (like concurrent) |
| `EventPublisher` (request) | the current request / WebSocket connection scope | after the response, each in a fresh `OperationScope` |

`emit(event, *, wait=False) -> EmitResult(event, handled, scheduled, errors)`.
A `"propagate"` inline failure stops the remaining inline handlers, schedules
no deferred handler and re-raises. `wait=True` awaits the deferred tasks too.

### After commit / from sync code

```python
await events.emit(OrderPlaced(order.id), after=TransactionPhase.COMMIT)  # dropped on rollback


@post("/import")
def import_users(self, events: EventPublisher) -> dict:          # sync handler, worker thread
    return {"handled": events.emit_threadsafe(UsersImported(10)).result().handled}
```

`after=` needs a provider implementing `UnitOfWork` (`on_commit(cb)`,
`on_rollback(cb)`, async callbacks) — usually the request-scoped DB session.

## 4. Startup errors

All raised by `LaurenFactory.create` as `UnresolvableParameterError`,
`detail={"handler": "Class.method", "param": ..., "reason": ...}`:

| `reason` | Fix |
|---|---|
| `event_type_mismatch` | annotate the event parameter with the subscribed class or a base |
| `missing_event_parameter` | put the event first and annotate it (or pass `@on_event(Type)`) |
| `invalid_event_type` | annotate with a class, not `Any` / a generic |
| `unresolvable_dependency` | register / export the dependency in the handler's module (`Request` is never injectable) |

`@on_event` misuse (bare decorator, bad mode, `errors="propagate"` on a
deferred mode) raises `DecoratorUsageError` at class definition.

## 5. Lifecycle

* During startup, inline delivery only reaches singletons already built
  (`LifecycleViolationError`, `reason="app_not_ready"` otherwise); deferred
  handlers wait for READY.
* `shutdown()` awaits running handler tasks after the request drain, then
  cancels the rest (`EventHandlerFailed` with `CancelledError`); `emit()` after
  shutdown raises `LifecycleViolationError`.
* Re-emitting deeper than `max_depth` (16) raises `RecursionError`.
* Tune with `@module(imports=[EventsModule.for_root(max_depth=16, drain_timeout=None)])`.

## 6. Test

```python
async def test_registration_mails(app: LaurenApp) -> None:
    await app.startup()
    bus = await app.container.resolve(EventBus)
    with bus.recording() as rec:
        await TestClient(app).arequest("POST", "/users/", json={"email": "a@b.c"})
        await bus.drain()
    assert rec.of(UserRegistered)[0].email == "a@b.c"
    assert rec.handler_calls(WelcomeMailer.send_welcome) == 1
    assert rec.failures == []


async def test_service_publishes() -> None:
    bus = EventBus()                                   # bare bus: no handlers, still records
    with bus.recording() as rec:
        await UserService(bus).import_user("u1", "a@b.c")
    assert rec.of(UserRegistered)
```

* `with bus.disabled(WelcomeMailer.send_welcome):` mutes a handler.
* `bus.handlers_for(UserRegistered)` → handler names in dispatch order.
* The sync `TestClient` runs each request in its own loop: `"concurrent"`
  handlers started by a request are cancelled with it — use `wait=True`,
  `"background"`, or async tests with `arequest` + `drain()`.

## Pitfalls

- ❌ `SignalBus` / `LifecycleEvent` subclasses for domain events — that bus is framework telemetry; listeners get no DI and errors are swallowed.
- ❌ `Request` or request-scoped instances inside an event.
- ❌ `EventPublisher` in a singleton, job or `app.scope()` block — use `EventBus`.
- ❌ Expecting deferred-handler failures to fail the request — make the handler `"inline"`.
- ❌ Treating events as durable — in-process only; deferred work is lost on a crash.
