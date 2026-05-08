# Background Tasks

Fire-and-forget work that runs after the HTTP response has been sent.

### `BackgroundTasks`

```python
class BackgroundTasks()
```

Collect tasks during a handler; they run after the response is sent.

Declare as a handler parameter::

    @post("/users")
    async def create(self, body: Json[CreateUser], tasks: BackgroundTasks):
        user = await self._repo.create(body)
        tasks.add_task(send_welcome_email, user.email)
        return user, 201

Sync functions are offloaded to ``anyio.to_thread.run_sync``
automatically so they never block the event loop. Exceptions are
caught, logged, and emitted as :class:`~lauren.signals.BackgroundTaskFailed`
signals. All tasks run in order regardless of individual failures.

Tasks run in the same ``asyncio.Task`` as the request so they
participate in the graceful-shutdown drain automatically.

.. warning::

    Do **not** pass ``Scope.REQUEST`` DI instances as args/kwargs —
    they are torn down after the handler returns, before tasks run.
    Capture plain values (IDs, strings) instead.
    ``Scope.SINGLETON`` services are safe to pass.

#### `BackgroundTasks.add_task`

```python
def add_task(self, func: Callable[..., Any], args: Any = (), kwargs: Any = {}) -> TaskHandle
```

Enqueue *func* to run after the response is sent.

Returns a :class:`TaskHandle` whose :attr:`~TaskHandle.task_id`
can be included in the response body so clients can track status.

### `TaskHandle`

```python
class TaskHandle(task_id: str, status: str = 'pending')
```

A handle returned by :meth:`BackgroundTasks.add_task`.

Tracks the lifecycle of a single background task. The
:attr:`task_id` is a random hex string that callers may include in
response bodies so clients can poll for completion status elsewhere.
:attr:`status` progresses ``"pending"`` → ``"running"`` →
``"done"`` | ``"failed"``.

