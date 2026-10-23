# Application Events

> Typed, in-process domain events. One module announces that something
> happened — *a user registered* — and other modules react: send a welcome
> mail, write an audit row, update a read model, call a webhook. The
> publisher depends only on the event class; it never imports the modules
> that listen.

Without events, `UserService.register()` has to inject `Mailer`,
`AuditTrail`, `Analytics` and `WebhookService`, and every new side effect
edits the core service and widens its module's `imports`. With events the
service emits `UserRegistered` and each side effect is an `@on_event`
handler in its own module.

Handlers are methods on ordinary providers. Lauren finds them on every
provider while `LaurenFactory.create` runs, validates their signatures there
(a wiring mistake fails startup, not the first request), and compiles a
dispatch table, so emitting an event never reflects.

---

## Quick start

<!-- docs-contract: run -->
```python
from dataclasses import dataclass

from lauren import LaurenFactory, injectable, module
from lauren.events import EventBus, EventContext, on_event


@dataclass(frozen=True)
class UserRegistered:
    user_id: str
    email: str


@injectable()
class UserService:
    def __init__(self, events: EventBus) -> None:
        self._events = events

    async def register(self, user_id: str, email: str) -> None:
        # ... store the user ...
        await self._events.emit(UserRegistered(user_id, email))


@injectable()
class WelcomeMailer:
    def __init__(self) -> None:
        self.outbox: list[str] = []

    @on_event()  # the event class comes from the annotation
    async def send_welcome(self, event: UserRegistered, ctx: EventContext) -> None:
        self.outbox.append(f"welcome {event.email} ({ctx.event_id[:8]})")


@module(providers=[UserService, WelcomeMailer])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
await app.startup()
users = await app.container.resolve(UserService)
await users.register("u1", "ada@example.com")
mailer = await app.container.resolve(WelcomeMailer)
assert mailer.outbox[0].startswith("welcome ada@example.com")
await app.shutdown()
```

There is nothing to enable: every app has an `EventBus`, and any provider
method decorated with `@on_event` is a handler.

---

## Modelling events

Any class is an event. Frozen dataclasses are a good default — immutable,
cheap, and they need no extra dependency:

<!-- docs-contract: fragment -->
```python
@dataclass(frozen=True)
class DomainEvent:
    """Optional common base: a handler on it sees every subclass."""


@dataclass(frozen=True)
class UserRegistered(DomainEvent):
    user_id: str
    email: str


@dataclass(frozen=True)
class OrderPlaced(DomainEvent):
    order_id: str
    total_cents: int
```

Delivery follows the class hierarchy: a handler subscribed to
`DomainEvent` receives `UserRegistered` and `OrderPlaced` too. There are no
string topics or wildcards — a base class is the wildcard, and the type
checker sees it.

Put **values** in an event, never live objects tied to a request: requests
are pooled and reused, request-scoped instances are torn down when the
request ends, and deferred handlers run later than both.

---

## Handlers

`@on_event(event_type=None, *, mode="inline", priority=0, errors=None, enabled=True)`
decorates a method of any provider — an `@injectable` of any scope or a
controller. It must be called with parentheses, it returns the method
unchanged, and stacking it subscribes one method to several classes.

<!-- docs-contract: fragment -->
```python
@injectable()
class AuditTrail:
    def __init__(self, store: AuditStore) -> None:
        self._store = store

    @on_event(priority=100)                      # inferred: DomainEvent and every subclass
    async def record(self, event: DomainEvent, db: DbSession, ctx: EventContext) -> None:
        await db.insert_audit(kind=type(event).__name__, event_id=ctx.event_id)

    @on_event(OrderPlaced)                       # explicit class; the annotation may be a base
    @on_event(UserRegistered)
    async def count(self, event: DomainEvent) -> None:
        self._store.bump(type(event).__name__)
```

* **The event parameter** is the first parameter after `self`. Without an
  explicit class, its annotation is the subscription (a union such as
  `UserRegistered | OrderPlaced` subscribes to each member). With an explicit
  class, the annotation must be that class or a base of it, so the handler
  can accept everything it subscribes to.
* **Other parameters** are an `EventContext` (any name) or dependencies the
  provider's **own module** can resolve, exactly like constructor
  dependencies. Discovery is app-wide, but it never widens visibility.
* **Sync handlers** (`def`) run in a worker thread.
* **Inheritance** follows the method rule: a subclass provider inherits a
  decorated method; overriding it without the decorator removes the handler.
* `enabled=False` keeps the handler validated at startup but never calls it —
  handy behind a feature flag.

`EventContext` carries `event_id` (unique per `emit()`, useful for
idempotency keys), `emitted_at` (UTC), `depth` (0 for a top-level emission,
+1 each time a handler re-emits), `mode`, `in_request`, and `log` — the app
logger with `context="Event:<EventType>"`.

---

## Delivery modes, ordering and errors

| `mode` | Runs | Default `errors` |
|---|---|---|
| `"inline"` (default) | inside `emit()`, one after another | `"propagate"` |
| `"concurrent"` | as a tracked task, after every inline handler succeeded | `"isolate"` |
| `"background"` | after the HTTP response when published through `EventPublisher` in a request; otherwise like `"concurrent"` | `"isolate"` |

**Ordering.** Handlers run by `priority` (higher first), then in discovery
order (the order providers are registered, then method order). Deferred
handlers start in the same order and then run concurrently; background
handlers on a request's queue run one after another.

**Errors.**

* `"propagate"` — the exception stops the remaining inline handlers,
  **schedules no deferred handler**, and re-raises from `emit()`. Inline
  handlers are part of the publisher's unit of work: if an audit write fails,
  the registration fails and no welcome mail goes out.
* `"isolate"` — the exception is logged, signalled as
  `EventHandlerFailed` on the app's `SignalBus`, and listed in
  `EmitResult.errors`; the other handlers still run.

`errors="propagate"` with a deferred mode is rejected when the class is
defined: by the time a deferred handler fails, `emit()` has returned.

`emit()` returns an `EmitResult(event, handled, scheduled, errors)`:

<!-- docs-contract: fragment -->
```python
result = await events.emit(OrderPlaced("o1", 4200), wait=True)
result.handled      # handlers that finished without error during the call
result.scheduled    # deferred handlers queued or started
result.errors       # EventHandlerFailed records of isolated failures
result.ok           # not errors
```

`wait=True` also awaits the deferred tasks the call started — useful in
scripts, commands and tests.

---

## Publishing: `EventBus` or `EventPublisher`

Two injectables, so that whether an emission joins the current request is
visible in the type a component asks for:

| Inject | Scope | Inline handlers resolve request-scoped deps in… | `"background"` handlers run… |
|---|---|---|---|
| `EventBus` | singleton (one per app) | one fresh `OperationScope` per `emit()` | as tracked tasks (like `"concurrent"`) |
| `EventPublisher` | request | **the current request's scope** — same `DbSession` as the controller | after the response, on the request's `BackgroundTasks` queue, each in a fresh `OperationScope` |

<!-- docs-contract: fragment -->
```python
@controller("/users")
class UserController:
    def __init__(self, users: UserService) -> None:
        self._users = users

    @post("/")
    async def register(self, body: Json[RegisterUser], events: EventPublisher, db: DbSession) -> Any:
        user = await self._users.create(db, body)
        await events.emit(UserRegistered(user.id, user.email))  # AuditTrail.record gets this `db`
        return {"id": user.id}, 201
```

Every deferred handler call gets its own `OperationScope`
([`app.scope()`](dependency-injection.md)), whose request-scoped instances are
torn down (`@pre_destruct`, then `aclose()`) when the handler returns — a
deferred handler never touches a request scope that has already closed.

`EventPublisher` is also available in WebSocket hooks (inline handlers share
the connection's scope; `"background"` behaves like `"concurrent"` since there
is no response) and inside `@on_event` handlers, where it joins the handler's
own scope. Anywhere else — a singleton, a job, an `app.scope()` block — inject
`EventBus`: resolving `EventPublisher` there raises
`LifecycleViolationError`, and a singleton that depends on it fails startup
with `DIScopeViolationError`.

### Sync handlers

`emit()` is async. A sync route handler (running in a worker thread) uses
`emit_threadsafe`, which schedules the emission on the app's event loop:

<!-- docs-contract: fragment -->
```python
@post("/import")
def import_users(self, events: EventPublisher) -> dict:
    result = events.emit_threadsafe(UsersImported(count=10)).result()  # wait before returning
    return {"handled": result.handled}
```

Call `.result()` before the handler returns: the request scope ends with it.

### After the transaction commits

`EventPublisher.emit(event, after=TransactionPhase.COMMIT)` holds the whole
emission until the request's unit of work commits, and drops it on rollback
— the event is never seen for data that was never committed. Core defines
the contract only: a database integration registers a provider implementing
the `UnitOfWork` protocol (`on_commit(callback)`, `on_rollback(callback)`,
async callbacks), for example its request-scoped session.

<!-- docs-contract: fragment -->
```python
await events.emit(OrderPlaced(order.id, order.total_cents), after=TransactionPhase.COMMIT)
```

When the unit of work commits, the emission is delivered like
`EventBus.emit` (fresh operation scopes). Without a `UnitOfWork` provider the
call raises `LifecycleViolationError` (`detail["reason"] == "no_unit_of_work"`).

---

## Decoupling two modules

The users module publishes; the notifications module listens. Neither imports
the other:

<!-- docs-contract: run -->
```python
from lauren import Scope, controller, post
from lauren.events import EventPublisher
from lauren.testing import TestClient


@dataclass(frozen=True)
class AccountOpened:
    account_id: str
    email: str


@controller("/accounts")
class AccountsController:
    @post("/")
    async def open(self, events: EventPublisher) -> dict:
        await events.emit(AccountOpened("acc-1", "grace@example.com"))
        return {"id": "acc-1"}


@module(controllers=[AccountsController])
class AccountsModule:
    pass


@injectable(scope=Scope.REQUEST)
class SmtpConnection:
    async def aclose(self) -> None:  # closed after each handler call
        pass


@injectable()
class Notifications:
    def __init__(self) -> None:
        self.sent: list[str] = []

    @on_event(mode="background")  # after the response
    async def greet(self, event: AccountOpened, smtp: SmtpConnection) -> None:
        self.sent.append(event.email)


@module(providers=[SmtpConnection, Notifications])
class NotificationsModule:
    pass


@module(imports=[AccountsModule, NotificationsModule])
class BankModule:
    pass


bank = LaurenFactory.create(BankModule)
await bank.startup()
response = await TestClient(bank).arequest("POST", "/accounts/")
assert response.json() == {"id": "acc-1"}
assert (await bank.container.resolve(Notifications)).sent == ["grace@example.com"]
await bank.shutdown()
```

---

## Startup validation

Every handler is checked inside `LaurenFactory.create`. Wiring mistakes raise
`UnresolvableParameterError` with
`detail = {"handler": "Class.method", "param": ..., "reason": ...}`:

| `reason` | Cause |
|---|---|
| `"event_type_mismatch"` | The event parameter's annotation is narrower than the subscribed class (`@on_event(UserRegistered)` on `event: AdminRegistered`) |
| `"missing_event_parameter"` | No event parameter, `EventContext` in its place, or no annotation to infer the class from |
| `"invalid_event_type"` | The annotation is not a class (or a union of classes) |
| `"unresolvable_dependency"` | Another parameter is neither `EventContext` nor visible from the provider's module (`Request` included) |

Bad decorator arguments raise `DecoratorUsageError` when the class is defined.
A verbose startup log lists every event class with its handlers, modes and
priorities.

---

## Lifecycle

* **Startup.** While `@post_construct` hooks run, an emission reaches inline
  handlers only on singletons that are already constructed (otherwise
  `LifecycleViolationError`, `reason="app_not_ready"`); deferred handlers are
  queued and start once the app is ready, so they never race their own
  dependencies.
* **Shutdown.** After in-flight requests drain, `shutdown()` awaits running
  handler tasks with what is left of `drain_timeout`, then cancels the rest;
  each cancelled handler is reported as `EventHandlerFailed` with an
  `asyncio.CancelledError`. Emitting still works during shutdown (from an
  `on_shutdown` callback, say), and handlers started that late are cancelled
  when shutdown completes. After that, `emit()` raises
  `LifecycleViolationError` (`reason="app_stopped"`).
* **Re-entrancy.** Handlers may emit further events. A chain deeper than
  `max_depth` (default 16) raises `RecursionError` naming the chain of event
  classes — almost always a handler that re-emits the event it handles.

Tune both with the optional `EventsModule`:

<!-- docs-contract: fragment -->
```python
from lauren.events import EventsModule


@module(imports=[EventsModule.for_root(max_depth=8, drain_timeout=5.0)])
class AppModule:
    pass
```

`drain_timeout=None` (the default) uses what is left of
`shutdown(drain_timeout=...)`. Events are in-process and **not durable**:
deferred work still queued when the process dies is lost. Use a durable job
queue for work that must survive a crash.

---

## Testing

<!-- docs-contract: fragment -->
```python
async def test_registration_sends_a_welcome_mail(app: LaurenApp) -> None:
    bus = await app.container.resolve(EventBus)
    client = TestClient(app)

    with bus.recording() as rec:
        await client.arequest("POST", "/users/", json={"email": "a@example.com"})
        await bus.drain()  # wait for concurrent handlers

    assert rec.of(UserRegistered)[0].email == "a@example.com"
    assert rec.handler_calls(WelcomeMailer.send_welcome) == 1
    assert rec.failures == []
```

* `bus.recording()` captures `events` (in emission order), `calls`
  (`"Class.method"` per finished handler call) and `failures`; recordings are
  per app and may nest.
* `await bus.drain(timeout=None)` waits until no deferred handler task is
  running (`TimeoutError` past the timeout).
* `with bus.disabled(WelcomeMailer.send_welcome):` mutes handlers (by method or
  `"Class.method"` name) while the block runs.
* `bus.handlers_for(UserRegistered)` lists handler names in dispatch order.
* Unit-testing a service that publishes? Pass it a bare `EventBus()` — it has
  no handlers — and assert on `bus.recording()`.

!!! tip "Concurrent handlers in tests"
    `TestClient` keeps one event loop for the client's lifetime, so a
    `"concurrent"` handler a request starts keeps running after the response.
    Call `bus.drain()` (or emit with `wait=True`) before asserting on its side
    effects; in an async test, `AsyncTestClient` runs the app on the test's own
    loop.

---

## Signals vs events

`SignalBus` and `EventBus` look alike but serve different purposes:

| | `SignalBus` | `EventBus` |
|---|---|---|
| Purpose | Framework lifecycle telemetry | Application domain events |
| Event types | `LifecycleEvent` subclasses | Any class |
| Listeners | Plain callables, imperative or a process-global default bus | DI-discovered methods, validated at startup |
| Errors | Always logged and swallowed | `propagate` (inline default) or `isolate` |
| Modes | Sequential inline | Inline / concurrent / background |
| Scope | Default bus copied into every app | Per app only |

Use [signals](signals.md) for metrics, tracing and readiness flags about the
framework itself; use events for what happens in your domain. Handler
failures are themselves a signal — subscribe to `EventHandlerFailed` to alert
on them.

---

## Pitfalls

* ❌ Subclassing `LifecycleEvent` and emitting domain events on the
  `SignalBus` — listeners cannot receive dependencies, errors are swallowed,
  and every `LifecycleEvent` firehose listener sees them.
* ❌ Putting the `Request` (or a request-scoped instance) inside an event —
  copy the values the handler needs.
* ❌ Expecting a `"concurrent"` handler's failure to fail the request — it
  runs after `emit()` returned; make it `"inline"` if it must.
* ❌ Injecting `EventPublisher` into a singleton or a job — inject `EventBus`.
* ❌ Treating events as durable delivery — they are in-process.

## See also

* [Signals & Lifecycle Events](signals.md)
* [Background Tasks](background-tasks.md)
* [Dependency Injection](dependency-injection.md) — scopes and `app.scope()`
* [API reference: Events](../reference/events.md)
