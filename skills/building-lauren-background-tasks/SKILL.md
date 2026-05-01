---
name: building-lauren-background-tasks
description: Adds fire-and-forget background tasks to Lauren handlers. Covers BackgroundTasks extractor, add_task, TaskHandle, sync/async callables, signal subscription (BackgroundTaskStarted/Complete/Failed), graceful shutdown participation, and the request-scoped DI constraint. Use when deferring work (emails, webhooks, audit writes) to after the HTTP response is sent.
---

# Lauren Background Tasks

## Basic usage

Declare `tasks: BackgroundTasks` as a handler parameter. Lauren injects a fresh
instance per request and runs queued tasks **after** the HTTP response is sent.

```python
from lauren import BackgroundTasks

@post("/orders")
async def create(self, body: Json[Order], tasks: BackgroundTasks) -> dict:
    order = await self._repo.create(body)
    tasks.add_task(notify_warehouse, order_id=order.id)
    tasks.add_task(send_confirmation_email, email=order.email)
    return {"id": order.id}, 201
```

Import: `from lauren import BackgroundTasks, TaskHandle`.
No module registration needed — `BackgroundTasks` is an extractor, not an injectable.

## TaskHandle

`add_task` returns a `TaskHandle` you can include in the response:

```python
handle = tasks.add_task(process_file, file_id=upload.id)
return {"job_id": handle.task_id}   # client can poll for status elsewhere
```

`TaskHandle.status` cycles: `"pending"` → `"running"` → `"done"` | `"failed"`.

## Sync callables are offloaded automatically

```python
def send_email(to: str, subject: str) -> None:  # plain sync function
    import smtplib
    smtplib.SMTP("localhost").sendmail(...)

tasks.add_task(send_email, "alice@x.com", "Welcome!")
# → runs in anyio.to_thread.run_sync, never blocks the event loop
```

## Task failures are isolated

If one task raises, the exception is caught, logged, and a `BackgroundTaskFailed`
signal is emitted. Subsequent tasks **always** run.

## Execution order

Tasks run in the order they were added, sequentially (not concurrently).

## Signal subscription

```python
from lauren import BackgroundTaskStarted, BackgroundTaskComplete, BackgroundTaskFailed

@post_construct
async def setup(self) -> None:
    self._app.signals.on(BackgroundTaskFailed)(self._on_failure)

async def _on_failure(self, event: BackgroundTaskFailed) -> None:
    await self._alerts.send(f"Task {event.task_id} failed: {event.error}")
```

Signal fields:
- `BackgroundTaskStarted`: `task_id`, `func`
- `BackgroundTaskComplete`: `task_id`, `func`, `duration_s`
- `BackgroundTaskFailed`: `task_id`, `func`, `error`

## Graceful shutdown

Tasks run in the same `asyncio.Task` as the request, which is tracked in
`LaurenApp._in_flight`. On SIGTERM/SIGINT the drain phase waits for all in-flight
tasks (including background tasks) to complete before teardown proceeds.

## ❌ Do NOT pass Scope.REQUEST instances

```python
# BAD — request-scoped session is torn down before tasks run
@post("/items")
async def create(self, session: Depends[DbSession], tasks: BackgroundTasks) -> dict:
    tasks.add_task(do_work, session)   # ← session is invalid by the time task runs
    return {}

# GOOD — pass the plain value, not the session
tasks.add_task(do_work, item_id=item.id)   # resolve deps in the task function itself
```

`Scope.SINGLETON` services (e.g. a mailer or event-bus) are safe to pass.

## Testing

`TestClient` runs background tasks synchronously before returning, so side effects
are directly assertable:

```python
results = []

def test_task_ran(client):
    results.clear()
    client.post("/orders", json={...})
    assert results == [...]  # task already ran
```
