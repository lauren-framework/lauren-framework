"""Unit tests for lauren.background (BackgroundTasks, TaskHandle)."""

from __future__ import annotations

import asyncio
import concurrent.futures
import dataclasses
import pickle
import threading
from typing import Any
from unittest.mock import MagicMock

import pytest

from lauren.background import BackgroundTasks, TaskHandle, _BG_TASKS_ATTR
from lauren.signals import (
    BackgroundTaskComplete,
    BackgroundTaskFailed,
    BackgroundTaskStarted,
    SignalBus,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_bus() -> SignalBus:
    return SignalBus()


async def _run(bg: BackgroundTasks, *, bus: SignalBus | None = None) -> None:
    if bus is None:
        bus = _make_bus()
    logger = MagicMock()
    await bg._run(signals=bus, logger=logger)


# ---------------------------------------------------------------------------
# TestTaskHandle
# ---------------------------------------------------------------------------


class TestTaskHandle:
    def test_default_status_is_pending(self) -> None:
        h = TaskHandle(task_id="abc")
        assert h.status == "pending"

    def test_task_id_is_non_empty_string(self) -> None:
        h = TaskHandle(task_id="xyz123")
        assert isinstance(h.task_id, str)
        assert len(h.task_id) > 0

    def test_status_mutable(self) -> None:
        h = TaskHandle(task_id="abc")
        h.status = "done"
        assert h.status == "done"


# ---------------------------------------------------------------------------
# TestBackgroundTasksQueue
# ---------------------------------------------------------------------------


class TestBackgroundTasksQueue:
    def test_empty_has_no_tasks(self) -> None:
        bg = BackgroundTasks()
        assert not bg._has_tasks()

    def test_add_task_returns_handle(self) -> None:
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: None)
        assert isinstance(handle, TaskHandle)

    def test_add_task_has_tasks_becomes_true(self) -> None:
        bg = BackgroundTasks()
        bg.add_task(lambda: None)
        assert bg._has_tasks()

    def test_add_multiple_tasks(self) -> None:
        bg = BackgroundTasks()
        bg.add_task(lambda: None)
        bg.add_task(lambda: None)
        bg.add_task(lambda: None)
        assert len(bg._queue) == 3

    def test_add_task_unique_ids(self) -> None:
        bg = BackgroundTasks()
        ids = {bg.add_task(lambda: None).task_id for _ in range(10)}
        assert len(ids) == 10  # all unique


# ---------------------------------------------------------------------------
# TestBackgroundTasksRun
# ---------------------------------------------------------------------------


class TestBackgroundTasksRun:
    @pytest.mark.asyncio
    async def test_async_task_runs(self) -> None:
        results: list[int] = []

        async def work() -> None:
            results.append(1)

        bg = BackgroundTasks()
        bg.add_task(work)
        await _run(bg)
        assert results == [1]

    @pytest.mark.asyncio
    async def test_sync_task_runs_without_blocking(self) -> None:
        results: list[int] = []

        def work() -> None:
            results.append(42)

        bg = BackgroundTasks()
        bg.add_task(work)
        await _run(bg)
        assert results == [42]

    @pytest.mark.asyncio
    async def test_tasks_run_in_order(self) -> None:
        order: list[int] = []

        async def a() -> None:
            order.append(1)

        async def b() -> None:
            order.append(2)

        async def c() -> None:
            order.append(3)

        bg = BackgroundTasks()
        bg.add_task(a)
        bg.add_task(b)
        bg.add_task(c)
        await _run(bg)
        assert order == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_handle_status_transitions_pending_running_done(self) -> None:
        async def work() -> None:
            pass

        bg = BackgroundTasks()
        handle = bg.add_task(work)
        assert handle.status == "pending"
        await _run(bg)
        assert handle.status == "done"

    @pytest.mark.asyncio
    async def test_handle_status_transitions_pending_running_failed(self) -> None:
        async def boom() -> None:
            raise ValueError("oops")

        bg = BackgroundTasks()
        handle = bg.add_task(boom)
        await _run(bg)
        assert handle.status == "failed"

    @pytest.mark.asyncio
    async def test_failing_task_does_not_stop_subsequent_tasks(self) -> None:
        results: list[int] = []

        async def bad() -> None:
            raise RuntimeError("fail")

        async def good() -> None:
            results.append(99)

        bg = BackgroundTasks()
        bg.add_task(bad)
        bg.add_task(good)
        await _run(bg)
        assert results == [99]

    @pytest.mark.asyncio
    async def test_exception_in_task_caught_not_raised(self) -> None:
        async def boom() -> None:
            raise ValueError("should not propagate")

        bg = BackgroundTasks()
        bg.add_task(boom)
        # Must not raise
        await _run(bg)

    @pytest.mark.asyncio
    async def test_empty_run_is_noop(self) -> None:
        bg = BackgroundTasks()
        await _run(bg)  # no tasks, must not error

    @pytest.mark.asyncio
    async def test_sync_task_receives_args_and_kwargs(self) -> None:
        received: list[Any] = []

        def work(a: int, *, b: str) -> None:
            received.append((a, b))

        bg = BackgroundTasks()
        bg.add_task(work, 7, b="hello")
        await _run(bg)
        assert received == [(7, "hello")]

    @pytest.mark.asyncio
    async def test_async_task_receives_args_and_kwargs(self) -> None:
        received: list[Any] = []

        async def work(x: int, y: int) -> None:
            received.append(x + y)

        bg = BackgroundTasks()
        bg.add_task(work, 3, 4)
        await _run(bg)
        assert received == [7]


# ---------------------------------------------------------------------------
# TestBackgroundTasksSignals
# ---------------------------------------------------------------------------


class TestBackgroundTasksSignals:
    @pytest.mark.asyncio
    async def test_started_signal_emitted(self) -> None:
        bus = _make_bus()
        events: list[BackgroundTaskStarted] = []
        bus.on(BackgroundTaskStarted)(events.append)

        async def work() -> None:
            pass

        bg = BackgroundTasks()
        bg.add_task(work)
        await _run(bg, bus=bus)
        assert len(events) == 1
        assert events[0].func is work

    @pytest.mark.asyncio
    async def test_complete_signal_emitted_with_duration(self) -> None:
        bus = _make_bus()
        events: list[BackgroundTaskComplete] = []
        bus.on(BackgroundTaskComplete)(events.append)

        async def work() -> None:
            pass

        bg = BackgroundTasks()
        bg.add_task(work)
        await _run(bg, bus=bus)
        assert len(events) == 1
        assert events[0].duration_s >= 0.0

    @pytest.mark.asyncio
    async def test_failed_signal_emitted_on_exception(self) -> None:
        bus = _make_bus()
        failures: list[BackgroundTaskFailed] = []
        bus.on(BackgroundTaskFailed)(failures.append)

        async def boom() -> None:
            raise ValueError("expected")

        bg = BackgroundTasks()
        bg.add_task(boom)
        await _run(bg, bus=bus)
        assert len(failures) == 1
        assert isinstance(failures[0].error, ValueError)

    @pytest.mark.asyncio
    async def test_no_complete_signal_on_failure(self) -> None:
        bus = _make_bus()
        complete_events: list[BackgroundTaskComplete] = []
        bus.on(BackgroundTaskComplete)(complete_events.append)

        async def boom() -> None:
            raise RuntimeError("fail")

        bg = BackgroundTasks()
        bg.add_task(boom)
        await _run(bg, bus=bus)
        assert complete_events == []

    @pytest.mark.asyncio
    async def test_signal_task_id_matches_handle(self) -> None:
        bus = _make_bus()
        started_ids: list[str] = []
        bus.on(BackgroundTaskStarted)(lambda e: started_ids.append(e.task_id))

        async def work() -> None:
            pass

        bg = BackgroundTasks()
        handle = bg.add_task(work)
        await _run(bg, bus=bus)
        assert started_ids == [handle.task_id]


# ---------------------------------------------------------------------------
# TestBackgroundTasksAttr
# ---------------------------------------------------------------------------


class TestBackgroundTasksAttr:
    def test_bg_tasks_attr_constant_value(self) -> None:
        assert _BG_TASKS_ATTR == "_lauren_bg_tasks"


# ---------------------------------------------------------------------------
# TaskHandle surface: done / cancel() / result()
# ---------------------------------------------------------------------------


class TestTaskHandleSurface:
    @pytest.mark.parametrize(
        "status,done",
        [("pending", False), ("running", False), ("done", True), ("failed", True), ("cancelled", True)],
    )
    def test_done_reflects_terminal_status(self, status: str, done: bool) -> None:
        handle = TaskHandle(task_id="t", status=status)
        assert handle.done is done

    async def test_result_returns_the_async_task_value(self) -> None:
        async def compute(x: int) -> int:
            return x * 2

        bg = BackgroundTasks()
        handle = bg.add_task(compute, 21)
        await _run(bg)
        assert await handle.result() == 42

    async def test_result_returns_the_sync_task_value(self) -> None:
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: "sync-value")
        await _run(bg)
        assert await handle.result() == "sync-value"

    async def test_result_raises_the_task_exception(self) -> None:
        async def boom() -> None:
            raise ValueError("bad input")

        bg = BackgroundTasks()
        handle = bg.add_task(boom)
        await _run(bg)
        with pytest.raises(ValueError, match="bad input"):
            await handle.result()

    async def test_cancel_skips_a_pending_task(self) -> None:
        calls: list[str] = []
        bg = BackgroundTasks()
        skipped = bg.add_task(lambda: calls.append("skipped"))
        kept = bg.add_task(lambda: calls.append("kept"))

        assert skipped.cancel() is True
        await _run(bg)

        assert calls == ["kept"]
        assert (skipped.status, kept.status) == ("cancelled", "done")
        # An ordinary Exception (not asyncio.CancelledError), so app-level
        # error handling still sees it when another request awaits the handle.
        with pytest.raises(concurrent.futures.CancelledError):
            await skipped.result()

    @pytest.mark.parametrize("status", ["running", "done", "failed", "cancelled"])
    def test_cancel_after_start_is_refused(self, status: str) -> None:
        handle = TaskHandle(task_id="t", status=status)
        assert handle.cancel() is False
        assert handle.status == status

    async def test_awaiting_result_in_the_queueing_task_raises(self) -> None:
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: None)
        with pytest.raises(RuntimeError, match="would never complete"):
            await handle.result()

    async def test_result_from_another_task_waits_for_the_run(self) -> None:
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: "later")

        waiter = asyncio.create_task(handle.result())
        await asyncio.sleep(0)
        assert not waiter.done()

        await _run(bg)
        assert await asyncio.wait_for(waiter, 1) == "later"

    async def test_cancelled_run_leaves_no_handle_pending(self) -> None:
        started = asyncio.Event()

        async def slow() -> None:
            started.set()
            await asyncio.sleep(10)

        bg = BackgroundTasks()
        first = bg.add_task(slow)
        second = bg.add_task(lambda: None)

        runner = asyncio.create_task(_run(bg))
        await started.wait()
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

        assert (first.status, second.status) == ("cancelled", "cancelled")
        assert first.done and second.done

    def test_only_public_fields_serialize(self) -> None:
        # Regression: runtime-only state (an asyncio.Event, the owning Task)
        # leaked into dataclasses.asdict, so returning a handle from a handler
        # failed with "cannot pickle '_asyncio.Task'".
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: None)

        assert dataclasses.asdict(handle) == {"task_id": handle.task_id, "status": "pending"}
        assert repr(handle) == f"TaskHandle(task_id={handle.task_id!r}, status='pending')"

    async def test_handles_pickle(self) -> None:
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: "value")
        await _run(bg)

        restored = pickle.loads(pickle.dumps(handle))

        assert (restored.task_id, restored.status, restored.done) == (handle.task_id, "done", True)

    def test_cancel_pending_settles_handles_that_never_ran(self) -> None:
        bg = BackgroundTasks()
        first, second = bg.add_task(lambda: None), bg.add_task(lambda: None)

        bg._cancel_pending()

        assert (first.status, second.status) == ("cancelled", "cancelled")

    async def test_cancel_from_a_worker_thread_wakes_the_waiter(self) -> None:
        # Regression: Event.set() from a thread did not wake the loop.
        bg = BackgroundTasks()
        handle = bg.add_task(lambda: None)
        waiter = asyncio.create_task(handle.result())
        await asyncio.sleep(0)

        threading.Thread(target=handle.cancel).start()

        with pytest.raises(concurrent.futures.CancelledError):
            await asyncio.wait_for(waiter, timeout=1.0)
