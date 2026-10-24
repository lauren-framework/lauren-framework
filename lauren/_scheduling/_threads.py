"""Running sync jobs and sync hosted services off the event loop (private).

Threads cannot be cancelled. A sync job that exceeds its ``timeout``, or a
sync hosted service that ignores a stop request past ``stop_timeout``, is
*abandoned*: the awaiting coroutine returns at once and the thread finishes
on its own. Both helpers below are built for that.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from typing import Any

import anyio.to_thread


async def run_sync_abandonable(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` in anyio's worker pool; cancelling the caller abandons the thread."""
    try:
        call = anyio.to_thread.run_sync(fn, abandon_on_cancel=True)
    except TypeError:  # pragma: no cover - anyio 4.0 spells it ``cancellable``
        call = anyio.to_thread.run_sync(fn, cancellable=True)
    return await call


async def run_in_daemon_thread(fn: Callable[[], Any], *, name: str) -> Any:
    """Run a long-lived ``fn`` on its own daemon thread and await its result.

    Hosted services can run for the app's whole lifetime, so they do not
    occupy a slot of the shared worker pool sync handlers use. Cancelling the
    caller abandons the thread (a daemon thread never blocks interpreter
    exit). The current context variables are copied into the thread.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[Any] = loop.create_future()
    context = contextvars.copy_context()

    def _settle(outcome: Callable[[], None]) -> None:
        if not future.done():
            outcome()

    def _runner() -> None:
        try:
            result = context.run(fn)
        except BaseException as exc:  # noqa: BLE001 - forwarded to the awaiting coroutine
            report = functools.partial(_settle, functools.partial(future.set_exception, exc))
        else:
            report = functools.partial(_settle, functools.partial(future.set_result, result))
        try:
            loop.call_soon_threadsafe(report)
        except RuntimeError:  # the loop closed while the thread was running
            pass

    threading.Thread(target=_runner, name=name, daemon=True).start()
    return await future


__all__ = ["run_in_daemon_thread", "run_sync_abandonable"]
