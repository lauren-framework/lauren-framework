"""The persistent event loop behind :class:`~lauren.testing.TestClient`.

A :class:`Portal` owns **one asyncio event loop for the client's lifetime**.
Startup, every request, background tasks, WebSocket sessions and shutdown all
run on it, so objects an app binds to its loop at startup (``asyncio.Event`` /
``Queue`` / ``Lock``, connection pools, tasks spawned in ``@post_construct``)
keep working across requests.

Who runs the loop:

* **A client call made from synchronous code runs the loop in the calling
  thread** until the call is done — exactly the thread 1.x ran each request
  in. Handlers therefore see the test thread's thread-local state (an
  in-memory SQLite connection, ``threading.local``) and a copy of its
  context variables, as before.
* **Between calls a daemon "idle" thread keeps the loop running**, so
  background tasks, timers and server-side WebSocket / SSE producers make
  progress while the test is busy elsewhere.
* A call made from inside another running event loop (a sync client used in
  an ``async def`` test) cannot run a second loop in that thread; it hands
  the work to whichever thread is running the client's loop and waits.

Only one thread runs the loop at a time; a caller takes the loop over from
the idle thread and hands it back afterwards. The idle thread also performs
the final teardown (cancel leftover tasks, close the loop) when the portal is
stopped — explicitly, or by ``weakref.finalize`` once the client is
garbage-collected.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from typing import Any, Awaitable, Callable, TypeVar

T = TypeVar("T")

#: How long :meth:`Portal.stop` waits for leftover tasks and the idle thread.
_STOP_TIMEOUT_S = 10.0


async def _invoke(fn: Callable[[], Awaitable[T]]) -> T:
    return await fn()


def _running_loop() -> asyncio.AbstractEventLoop | None:
    """The event loop running in the current thread, or ``None``."""
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _cancel_leftover_tasks(loop: asyncio.AbstractEventLoop) -> list[str]:
    """Cancel every task still pending on the stopped ``loop``; return their names.

    Runs with the loop *stopped*: Python 3.14 tracks tasks per thread, and
    ``asyncio.all_tasks()`` called inside a running loop misses tasks another
    thread created while it was running the loop.
    """
    # Let tasks the app already cancelled (e.g. in ``@pre_destruct``) finish first.
    loop.run_until_complete(asyncio.sleep(0))
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        loop.run_until_complete(asyncio.wait(tasks, timeout=_STOP_TIMEOUT_S))
    return [t.get_name() for t in tasks]


class Portal:
    """One event loop, run by the calling thread during calls and by a daemon thread in between."""

    def __init__(
        self, *, name: str, loop_factory: Callable[[], asyncio.AbstractEventLoop] | None = None
    ) -> None:
        self._loop_factory = loop_factory or asyncio.new_event_loop
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cond = threading.Condition()
        self._driver: threading.Thread | None = None
        self._waiting = 0
        self._closing = False
        self._finished = False
        self._leftovers: list[str] = []
        self._wake: asyncio.Event | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self.thread = threading.Thread(target=self._idle_main, name=name, daemon=True)
        self.thread.start()
        self._ready.wait()
        if self._error is not None:
            raise self._error

    # -- state -----------------------------------------------------------------

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        assert self._loop is not None
        return self._loop

    @property
    def running(self) -> bool:
        """``True`` while the portal accepts work."""
        return not self._closing and self._loop is not None and not self._loop.is_closed()

    def in_loop(self) -> bool:
        """Whether the current thread is executing inside this portal's loop."""
        return self._loop is not None and _running_loop() is self._loop

    # -- calls -------------------------------------------------------------------

    def call(self, fn: Callable[[], Awaitable[T]]) -> T:
        """Run ``fn()`` on the portal's loop and return its result (blocking)."""
        if self.in_loop():
            raise RuntimeError(
                "a TestClient method was called from inside the app's own event loop "
                "(a handler or listener calling the client?); it would deadlock"
            )
        if _running_loop() is None and threading.current_thread() is not self.thread:
            return self._drive(fn)
        future = self.submit(fn)
        try:
            return future.result()
        except BaseException:
            if not future.done():
                future.cancel()
            raise

    def submit(self, fn: Callable[[], Awaitable[T]]) -> concurrent.futures.Future[T]:
        """Schedule ``fn()`` on the portal's loop; return a thread-safe future."""
        if not self.running:
            raise RuntimeError("the TestClient is closed (its event loop has stopped)")
        return asyncio.run_coroutine_threadsafe(_invoke(fn), self.loop)

    def _drive(self, fn: Callable[[], Awaitable[T]]) -> T:
        self._acquire()
        loop = self.loop
        try:
            # Created here, so the task copies this thread's context variables.
            task = loop.create_task(_invoke(fn))
            try:
                return loop.run_until_complete(task)
            except BaseException:
                if not task.done():
                    task.cancel()  # finished by the next thread to run the loop
                raise
        finally:
            self._release()

    def _acquire(self) -> None:
        """Take the loop over (from the idle thread or another caller)."""
        with self._cond:
            if not self.running:
                raise RuntimeError("the TestClient is closed (its event loop has stopped)")
            self._waiting += 1
            try:
                while self._driver is not None:
                    if self._driver is self.thread:
                        self._poke()
                    self._cond.wait()
                    if not self.running:
                        raise RuntimeError("the TestClient is closed (its event loop has stopped)")
                self._driver = threading.current_thread()
            finally:
                self._waiting -= 1

    def _release(self) -> None:
        with self._cond:
            self._driver = None
            self._cond.notify_all()

    def _poke(self) -> None:
        """Ask the idle thread to stop running the loop (thread-safe)."""
        assert self._loop is not None and self._wake is not None
        try:
            self._loop.call_soon_threadsafe(self._wake.set)
        except RuntimeError:  # loop closed concurrently
            pass

    # -- teardown ------------------------------------------------------------------

    def stop(self) -> list[str]:
        """Cancel leftover tasks, close the loop and end the idle thread.

        Returns the names of tasks that were still running and had to be
        cancelled. Safe to call more than once and from inside the loop (the
        idle thread then finishes the teardown once the loop is free).
        """
        with self._cond:
            if self._closing:
                return []
            self._closing = True
            if self._driver is self.thread:
                self._poke()
            self._cond.notify_all()
        if threading.current_thread() is not self.thread and not self.in_loop():
            self.thread.join(timeout=_STOP_TIMEOUT_S)
        return list(self._leftovers)

    def _idle_main(self) -> None:
        try:
            loop = self._loop_factory()
        except BaseException as exc:  # surfaced to the constructor's caller
            self._error = exc
            self._ready.set()
            return
        self._loop = loop
        self._wake = asyncio.Event()
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            while True:
                with self._cond:
                    self._cond.wait_for(
                        lambda: self._driver is None and (self._waiting == 0 or self._closing)
                    )
                    self._driver = self.thread
                    if self._closing:
                        break
                try:
                    self._wake.clear()
                    loop.run_until_complete(self._wake.wait())
                finally:
                    self._release()
            # Closing: this thread holds the loop for the final teardown.
            try:
                self._leftovers = _cancel_leftover_tasks(loop)
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.run_until_complete(loop.shutdown_default_executor())
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        finally:
            with self._cond:
                self._finished = True
                self._driver = None
                self._cond.notify_all()


def stop_portal(portal: Any) -> None:
    """``weakref.finalize`` callback: stop a portal whose client was collected."""
    portal.stop()


__all__ = ["Portal", "stop_portal"]
