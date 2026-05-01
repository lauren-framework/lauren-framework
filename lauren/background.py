"""BackgroundTasks — fire-and-forget tasks executed after the response is sent."""

from __future__ import annotations

import inspect
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import anyio.to_thread

_BG_TASKS_ATTR = "_lauren_bg_tasks"  # private key on request.state


@dataclass
class TaskHandle:
    """A handle returned by :meth:`BackgroundTasks.add_task`.

    Tracks the lifecycle of a single background task. The
    :attr:`task_id` is a random hex string that callers may include in
    response bodies so clients can poll for completion status elsewhere.
    :attr:`status` progresses ``"pending"`` → ``"running"`` →
    ``"done"`` | ``"failed"``.
    """

    task_id: str
    status: str = "pending"  # "pending" | "running" | "done" | "failed"


class BackgroundTasks:
    """Collect tasks during a handler; they run after the response is sent.

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
    """

    def __init__(self) -> None:
        self._queue: list[
            tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], TaskHandle]
        ] = []

    def add_task(
        self, func: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> TaskHandle:
        """Enqueue *func* to run after the response is sent.

        Returns a :class:`TaskHandle` whose :attr:`~TaskHandle.task_id`
        can be included in the response body so clients can track status.
        """
        handle = TaskHandle(task_id=uuid.uuid4().hex)
        self._queue.append((func, args, kwargs, handle))
        return handle

    def _has_tasks(self) -> bool:
        return bool(self._queue)

    async def _run(self, *, signals: Any, logger: Any) -> None:
        """Execute all queued tasks.

        Errors are logged and emitted as signals; all tasks always run
        regardless of individual failures.
        """
        from .signals import (
            BackgroundTaskComplete,
            BackgroundTaskFailed,
            BackgroundTaskStarted,
        )

        for func, args, kwargs, handle in self._queue:
            handle.status = "running"
            t0 = time.perf_counter()
            func_name = getattr(func, "__qualname__", repr(func))
            try:
                await signals.emit(
                    BackgroundTaskStarted(task_id=handle.task_id, func=func)
                )
                if inspect.iscoroutinefunction(func):
                    await func(*args, **kwargs)
                else:
                    _a, _kw = args, kwargs
                    await anyio.to_thread.run_sync(lambda: func(*_a, **_kw))
                handle.status = "done"
                await signals.emit(
                    BackgroundTaskComplete(
                        task_id=handle.task_id,
                        func=func,
                        duration_s=time.perf_counter() - t0,
                    )
                )
            except Exception as exc:
                handle.status = "failed"
                logger.error(
                    f"Background task {func_name!r} failed: {exc}",
                    context="BackgroundTasks",
                )
                await signals.emit(
                    BackgroundTaskFailed(
                        task_id=handle.task_id,
                        func=func,
                        error=exc,
                    )
                )
