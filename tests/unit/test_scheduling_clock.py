"""Unit tests for the scheduler clocks and the scheduling thread helpers."""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest

from lauren._scheduling._clock import MANUAL_CLOCK_EPOCH, wait_event_or_sleep
from lauren._scheduling._threads import run_in_daemon_thread, run_sync_abandonable
from lauren.scheduling import Clock, ManualClock, SystemClock


def test_manual_clock_defaults_to_a_fixed_epoch() -> None:
    clock = ManualClock()

    assert clock.now() == MANUAL_CLOCK_EPOCH
    assert clock.monotonic() == 0.0


def test_naive_start_is_read_as_utc() -> None:
    clock = ManualClock(datetime(2026, 5, 1, 12, 0))

    assert clock.now() == datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)


def test_both_clocks_satisfy_the_protocol() -> None:
    assert isinstance(ManualClock(), Clock)
    assert isinstance(SystemClock(), Clock)


async def test_advance_moves_wall_and_monotonic_time_together() -> None:
    clock = ManualClock()

    await clock.advance(seconds=30, minutes=1, hours=1, days=1)

    expected = 30 + 60 + 3600 + 86400
    assert clock.monotonic() == expected
    assert clock.now() - MANUAL_CLOCK_EPOCH == timedelta(seconds=expected)


async def test_advance_wakes_sleepers_in_deadline_order_at_their_deadline() -> None:
    clock = ManualClock()
    woke: list[tuple[str, float]] = []

    async def sleeper(label: str, seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append((label, clock.monotonic()))

    tasks = [asyncio.create_task(sleeper("late", 20)), asyncio.create_task(sleeper("early", 5))]
    await clock.advance(30)

    assert woke == [("early", 5.0), ("late", 20.0)]
    await asyncio.gather(*tasks)


async def test_sleepers_beyond_the_window_keep_sleeping() -> None:
    clock = ManualClock()
    task = asyncio.create_task(clock.sleep(100))

    await clock.advance(50)

    assert not task.done()
    assert clock.sleepers == 1
    await clock.advance(50)
    assert task.done()


async def test_a_sleeper_that_sleeps_again_is_woken_again_inside_the_window() -> None:
    clock = ManualClock()
    ticks: list[float] = []

    async def ticker() -> None:
        for _ in range(3):
            await clock.sleep(10)
            ticks.append(clock.monotonic())

    task = asyncio.create_task(ticker())
    await clock.advance(35)

    assert ticks == [10.0, 20.0, 30.0]
    await task


async def test_cancelled_sleepers_are_dropped() -> None:
    clock = ManualClock()
    task = asyncio.create_task(clock.sleep(10))
    await clock.advance()
    task.cancel()

    await clock.advance(20)

    assert task.cancelled()
    assert clock.sleepers == 0


async def test_zero_and_negative_sleeps_only_yield() -> None:
    clock = ManualClock()

    await clock.sleep(0)
    await clock.sleep(-1)

    assert clock.sleepers == 0


async def test_advance_refuses_to_go_backwards() -> None:
    with pytest.raises(ValueError, match="backwards"):
        await ManualClock().advance(-1)


def test_repr_shows_the_time() -> None:
    assert "2000-01-01" in repr(ManualClock())
    assert repr(SystemClock()) == "SystemClock()"


async def test_system_clock_reads_real_time() -> None:
    clock = SystemClock()

    before = clock.monotonic()
    await clock.sleep(0)

    assert clock.monotonic() >= before
    assert clock.now().tzinfo is timezone.utc


async def test_wait_event_or_sleep_returns_true_when_the_event_wins() -> None:
    clock = ManualClock()
    event = asyncio.Event()
    task = asyncio.create_task(wait_event_or_sleep(clock, event, 60))
    await clock.advance()

    event.set()

    assert await task is True


async def test_wait_event_or_sleep_returns_false_when_the_sleep_runs_out() -> None:
    clock = ManualClock()
    task = asyncio.create_task(wait_event_or_sleep(clock, asyncio.Event(), 60))

    await clock.advance(60)

    assert await task is False


async def test_wait_event_or_sleep_without_timeout_waits_for_the_event() -> None:
    event = asyncio.Event()
    task = asyncio.create_task(wait_event_or_sleep(ManualClock(), event, None))
    await asyncio.sleep(0)

    event.set()

    assert await task is True
    assert await wait_event_or_sleep(ManualClock(), event, None) is True


async def test_daemon_thread_returns_the_result_on_a_named_thread() -> None:
    def work() -> str:
        return threading.current_thread().name

    assert await run_in_daemon_thread(work, name="lauren-hosted:test") == "lauren-hosted:test"


async def test_daemon_thread_forwards_exceptions() -> None:
    def boom() -> None:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await run_in_daemon_thread(boom, name="t")


async def test_cancelling_a_daemon_thread_call_abandons_the_thread() -> None:
    release = threading.Event()
    task = asyncio.create_task(run_in_daemon_thread(release.wait, name="t"))
    await asyncio.sleep(0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()


async def test_run_sync_abandonable_runs_in_the_worker_pool() -> None:
    assert await run_sync_abandonable(lambda: threading.current_thread() is not threading.main_thread())


async def test_jump_wakes_overdue_sleepers_together_after_the_jump() -> None:
    clock = ManualClock()
    woke: list[float] = []

    async def sleeper(seconds: float) -> None:
        await clock.sleep(seconds)
        woke.append(clock.monotonic())

    tasks = [asyncio.create_task(sleeper(5)), asyncio.create_task(sleeper(20))]
    await clock.jump(minutes=1)

    assert woke == [60.0, 60.0]
    await asyncio.gather(*tasks)


async def test_jump_refuses_to_go_backwards() -> None:
    with pytest.raises(ValueError, match="backwards"):
        await ManualClock().jump(-1)


async def test_settle_falls_back_to_fixed_rounds_without_a_ready_queue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import types

    from lauren._scheduling import _clock

    fake = types.SimpleNamespace(sleep=asyncio.sleep, get_running_loop=lambda: object())
    monkeypatch.setattr(_clock, "asyncio", fake)

    await _clock._settle()  # returns after the fallback rounds


async def test_advance_stops_waiting_for_a_blocked_sync_job(monkeypatch: pytest.MonkeyPatch) -> None:
    from lauren._scheduling import _clock

    monkeypatch.setattr(_clock, "_THREAD_WAIT", 0.01)
    clock = ManualClock()
    release = threading.Event()
    blocked = asyncio.ensure_future(run_sync_abandonable(lambda: release.wait(5)))
    clock._track_offloaded(blocked)

    await clock.advance(1)
    release.set()
    await blocked

    assert clock.monotonic() == 1.0
