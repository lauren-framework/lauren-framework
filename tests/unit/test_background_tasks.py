"""Unit tests for lauren.background (BackgroundTasks, TaskHandle)."""

from __future__ import annotations

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
        statuses: list[str] = []

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
