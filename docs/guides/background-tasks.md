# Background Tasks

Return an HTTP response immediately and defer work — emails, webhooks, audit writes,
cache invalidation — to run **after the client has received the response**.

---

## Minimal example

```python
from lauren import BackgroundTasks, controller, post, Json
from pydantic import BaseModel

class CreateUser(BaseModel):
    email: str

@controller("/users")
class UsersController:
    @post("/")
    async def create(self, body: Json[CreateUser], tasks: BackgroundTasks) -> dict:
        user = await self._repo.create(body)
        tasks.add_task(send_welcome_email, user.email)
        return {"id": user.id}, 201
```

Declare `tasks: BackgroundTasks` as a handler parameter. Lauren detects this at
compile time (like `request: Request`) and injects a fresh instance per request.

---

## add_task and TaskHandle

```python
handle: TaskHandle = tasks.add_task(func, *args, **kwargs)
```

`add_task` enqueues `func` to run after `_send_response` completes. It returns a
`TaskHandle`:

| Field | Type | Description |
|---|---|---|
| `task_id` | `str` | Random hex string, unique per task |
| `status` | `str` | `"pending"` → `"running"` → `"done"` \| `"failed"` |

You can include `task_id` in the response body so clients can poll elsewhere:

```python
@post("/orders")
async def create(self, body: Json[Order], tasks: BackgroundTasks) -> dict:
    order = await self._repo.create(body)
    handle = tasks.add_task(notify_warehouse, order_id=order.id)
    return {"id": order.id, "job_id": handle.task_id}, 201
```

---

## Sync vs async callables

Both work. Sync callables are offloaded to `anyio.to_thread.run_sync` automatically
so they never block the event loop:

```python
# Async — awaited directly
async def send_email(to: str) -> None:
    await mailer.send(to, ...)

# Sync — offloaded to thread pool
def update_crm(customer_id: int) -> None:
    crm_sdk.update(customer_id, ...)   # blocking I/O

@post("/")
async def create(self, tasks: BackgroundTasks) -> dict:
    tasks.add_task(send_email, "alice@example.com")
    tasks.add_task(update_crm, customer_id=42)
    return {}
```

---

## Execution order

Tasks run **in the order they were added** and execute sequentially (not concurrently).
This is intentional: most post-response work has natural sequencing (notify after persist).

---

## Error isolation

If a task raises, the exception is:

1. Logged at `ERROR` level with `context="BackgroundTasks"`.
2. Emitted as a `BackgroundTaskFailed` signal (see [Signals](#signals) below).
3. **Does not** affect the HTTP response status.
4. **Does not** stop subsequent tasks from running.

```python
tasks.add_task(may_fail)       # fails
tasks.add_task(always_runs)    # still runs
```

---

## Signals

Three lifecycle signals are emitted per task:

| Signal | Fields | Description |
|---|---|---|
| `BackgroundTaskStarted` | `task_id`, `func` | Just before execution begins |
| `BackgroundTaskComplete` | `task_id`, `func`, `duration_s` | After successful completion |
| `BackgroundTaskFailed` | `task_id`, `func`, `error` | After an exception is raised |

Subscribe via the app's `SignalBus`:

```python
from lauren import BackgroundTaskFailed, post_construct

@injectable(scope=Scope.SINGLETON)
class AlertService:
    def __init__(self, app: LaurenApp) -> None:
        self._app = app

    @post_construct
    async def setup(self) -> None:
        self._app.signals.on(BackgroundTaskFailed)(self._on_failure)

    async def _on_failure(self, event: BackgroundTaskFailed) -> None:
        await self._pagerduty.alert(
            f"Background task {event.task_id!r} failed: {event.error}"
        )
```

---

## Graceful shutdown

Tasks run in the **same `asyncio.Task`** as the request that queued them. That task
is registered in `LaurenApp._in_flight` until `release_request` returns — which only
happens after all background tasks complete. On `SIGTERM` / `SIGINT`, the drain
phase waits for all in-flight tasks (including queued background work) before teardown.

---

## DI guidance

### Safe: pass plain values and `Scope.SINGLETON` services

```python
# GOOD — plain value captured at handler time
tasks.add_task(process_order, order_id=order.id)

# GOOD — singleton service is never torn down
tasks.add_task(lambda: self._mailer.send(email))
```

### Unsafe: `Scope.REQUEST` instances

```python
# BAD — db_session is torn down when the request scope exits, before the task runs
@post("/")
async def create(self, session: Depends[DbSession], tasks: BackgroundTasks) -> dict:
    tasks.add_task(do_work, session)   # ← session is invalid by run time
    return {}

# GOOD — capture the plain value instead
tasks.add_task(do_work, item_id=item.id)
```

---

## Testing background tasks

`TestClient` runs background tasks synchronously in the same event loop before the
request call returns, so side effects are directly assertable:

```python
from lauren import BackgroundTasks, LaurenFactory, controller, module, post
from lauren.testing import TestClient

results: list[str] = []

async def notify(email: str) -> None:
    results.append(email)

@controller("/users")
class UsersController:
    @post("/")
    async def create(self, tasks: BackgroundTasks) -> dict:
        handle = tasks.add_task(notify, "alice@example.com")
        return {"task_id": handle.task_id}

@module(controllers=[UsersController])
class AppModule: pass

client = TestClient(LaurenFactory.create(AppModule))

def test_task_ran():
    results.clear()
    resp = client.post("/users")
    assert resp.status_code == 200
    assert results == ["alice@example.com"]   # task already ran

def test_task_id_in_response():
    resp = client.post("/users")
    assert resp.json()["task_id"]             # non-empty
```

For signal-based assertions:

```python
from lauren import BackgroundTaskFailed

failures: list[BackgroundTaskFailed] = []

def test_signal_on_failure(app, client):
    app.signals.on(BackgroundTaskFailed)(failures.append)
    client.post("/path-that-triggers-bad-task")
    assert len(failures) == 1
```

---

## Comparison with FastAPI BackgroundTasks

| Feature | FastAPI | Lauren |
|---|---|---|
| Add tasks | `add_task(fn, *args)` | `add_task(fn, *args, **kwargs)` |
| Sync offload | ❌ blocks event loop | ✅ `anyio.to_thread.run_sync` |
| TaskHandle / task_id | ❌ | ✅ |
| Signals (started/complete/failed) | ❌ | ✅ |
| Graceful shutdown drain | ❌ | ✅ |
| DI container integration | ❌ | ✅ (singleton services safe to pass) |
| Error isolation (one task fails → others run) | ✅ | ✅ |

---

## Non-goals

Background tasks run in the **current process**. For deferred/scheduled/persistent
task queues see the planned `lauren-tasks` companion package which will add:

- `delay=`, `run_at=` scheduling
- Recurring tasks (`cron=`, `every=`)
- Broker adapters (Redis, SQLAlchemy)
- Cross-process distribution
- Task result persistence
