"""BackgroundTasks — fire-and-forget tasks executed after the response is sent."""

from __future__ import annotations

import asyncio
import concurrent.futures
import inspect
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import anyio.to_thread

_BG_TASKS_ATTR = "_lauren_bg_tasks"  # private key on request.state


_TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})


def _current_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:  # no running loop (e.g. a sync handler's worker thread)
        return None


@dataclass
class TaskHandle:
    """A handle returned by :meth:`BackgroundTasks.add_task`.

    Tracks the lifecycle of a single background task. The
    :attr:`task_id` is a random hex string that callers may include in
    response bodies so clients can poll for completion status elsewhere.
    :attr:`status` progresses ``"pending"`` → ``"running"`` →
    ``"done"`` | ``"failed"`` | ``"cancelled"``.

    Background tasks run only *after* the response has been sent, so
    :meth:`result` cannot be awaited by the request that queued the task
    (that would deadlock — it raises :class:`RuntimeError` instead); await
    it from another coroutine, e.g. a WebSocket handler or a test.

    Only ``task_id`` and ``status`` are dataclass fields, so returning a
    handle from a handler serializes to ``{"task_id": ..., "status": ...}``
    and handles pickle; the waiting machinery is runtime-only state.
    """

    task_id: str
    status: str = "pending"  # "pending" | "running" | "done" | "failed" | "cancelled"

    def __post_init__(self) -> None:
        self._finished = asyncio.Event()
        self._value: Any = None
        self._error: BaseException | None = None
        self._owner: asyncio.Task[Any] | None = None
        #: The loop of the first coroutine awaiting :meth:`result`, so a
        #: settle from a worker thread (``cancel()`` in a sync handler) can
        #: wake it safely.
        self._waiter_loop: asyncio.AbstractEventLoop | None = None
        if self.status in _TERMINAL_STATUSES:
            self._finished.set()

    def __getstate__(self) -> dict[str, Any]:
        return {"task_id": self.task_id, "status": self.status}

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.task_id = state["task_id"]
        self.status = state["status"]
        self.__post_init__()

    @property
    def done(self) -> bool:
        """``True`` once the task has finished, failed, or been cancelled."""
        return self.status in _TERMINAL_STATUSES

    def cancel(self) -> bool:
        """Cancel the task if it has not started; return whether it was cancelled.

        A ``"pending"`` task is marked ``"cancelled"`` and skipped when the
        queue runs. A task that is running or finished cannot be cancelled
        (``False``) — background tasks share the request's ``asyncio.Task``.
        """
        if self.status != "pending":
            return False
        self._settle("cancelled")
        return True

    async def result(self) -> Any:
        """Wait for the task and return its return value.

        Raises the task's exception if it failed, and
        :class:`concurrent.futures.CancelledError` (an ordinary
        ``Exception``, unlike :class:`asyncio.CancelledError`) if it was
        cancelled.
        """
        if not self.done and self._owner is not None and _current_task() is self._owner:
            raise RuntimeError(
                f"TaskHandle.result() for background task {self.task_id} was awaited by the "
                "request that queued it; background tasks only run after the response is "
                "sent, so this would never complete. Await it from another coroutine."
            )
        if self._waiter_loop is None:
            self._waiter_loop = asyncio.get_running_loop()
        if not self.done:
            await self._finished.wait()
        if self.status == "cancelled":
            raise concurrent.futures.CancelledError(f"background task {self.task_id} was cancelled")
        if self._error is not None:
            raise self._error
        return self._value

    def _settle(self, status: str, *, value: Any = None, error: BaseException | None = None) -> None:
        self._value = value
        self._error = error
        self.status = status
        # ``asyncio.Event`` is not thread-safe: wake a waiter on another
        # thread's loop through that loop.
        loop = self._waiter_loop
        if loop is None or loop.is_closed():
            self._finished.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._finished.set()
        else:
            loop.call_soon_threadsafe(self._finished.set)


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
        self._queue: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any], TaskHandle]] = []

    def add_task(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> TaskHandle:
        """Enqueue *func* to run after the response is sent.

        Returns a :class:`TaskHandle` whose :attr:`~TaskHandle.task_id`
        can be included in the response body so clients can track status.
        """
        handle = TaskHandle(task_id=uuid.uuid4().hex)
        handle._owner = _current_task()
        self._queue.append((func, args, kwargs, handle))
        return handle

    def _has_tasks(self) -> bool:
        return bool(self._queue)

    def _cancel_pending(self) -> None:
        """Mark every handle that never ran ``"cancelled"`` — the request ended first."""
        for _func, _args, _kwargs, handle in self._queue:
            if not handle.done:
                handle._settle("cancelled")

    async def _run(self, *, signals: Any, logger: Any) -> None:
        """Execute all queued tasks.

        Errors are logged and emitted as signals; the remaining tasks still
        run. Handles cancelled with :meth:`TaskHandle.cancel` are skipped. If
        the run itself is cancelled (shutdown, client disconnect), the
        interrupted and remaining handles are marked ``"cancelled"`` — none is
        left pending — and the cancellation propagates.
        """
        from .signals import (
            BackgroundTaskComplete,
            BackgroundTaskFailed,
            BackgroundTaskStarted,
        )

        index = 0
        try:
            for index, (func, args, kwargs, handle) in enumerate(self._queue):
                if handle.status == "cancelled":
                    continue
                handle.status = "running"
                t0 = time.perf_counter()
                func_name = getattr(func, "__qualname__", repr(func))
                try:
                    await signals.emit(BackgroundTaskStarted(task_id=handle.task_id, func=func))
                    if inspect.iscoroutinefunction(func):
                        value = await func(*args, **kwargs)
                    else:
                        _a, _kw = args, kwargs
                        value = await anyio.to_thread.run_sync(lambda: func(*_a, **_kw))
                    handle._settle("done", value=value)
                    await signals.emit(
                        BackgroundTaskComplete(
                            task_id=handle.task_id,
                            func=func,
                            duration_s=time.perf_counter() - t0,
                        )
                    )
                except Exception as exc:
                    handle._settle("failed", error=exc)
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
        except asyncio.CancelledError:
            remaining = [h for _f, _a, _k, h in self._queue[index:] if not h.done]
            for handle in remaining:
                handle._settle("cancelled")
            logger.warn(
                f"Background tasks cancelled ({len(remaining)} not completed)",
                context="BackgroundTasks",
            )
            raise
