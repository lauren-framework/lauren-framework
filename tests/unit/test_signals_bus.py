"""Unit tests for :class:`lauren.signals.SignalBus`.

Exercises the event bus in isolation (no ASGI app, no dispatcher) to
pin the behavioural contract that the framework's lifecycle-event
plumbing relies on:

* Listener registration is idempotent; re-registering the same
  callable is a no-op.
* ``off()`` returns a clean bool and never raises.
* ``emit`` walks the MRO so base-class listeners see every subclass
  event.
* Listener errors are swallowed and logged, never propagated.
* Async listeners are awaited sequentially in registration order.
* Snapshot semantics: mutating the listener set *during* emit does
  not affect the in-flight iteration.
"""

from __future__ import annotations

import asyncio


from lauren.signals import (
    LifecycleEvent,
    RequestComplete,
    RequestReceived,
    ShutdownBegin,
    SignalBus,
    StartupBegin,
    StartupComplete,
)


# ---------------------------------------------------------------------------
# Registration + idempotency
# ---------------------------------------------------------------------------


async def test_emit_with_no_listeners_is_a_no_op() -> None:
    bus = SignalBus()
    await bus.emit(StartupBegin())  # must not raise


async def test_listener_receives_its_event() -> None:
    bus = SignalBus()
    seen: list[LifecycleEvent] = []

    @bus.on(StartupComplete)
    def listener(event: StartupComplete) -> None:
        seen.append(event)

    evt = StartupComplete(duration_s=0.5)
    await bus.emit(evt)
    assert seen == [evt]


async def test_registering_same_listener_twice_is_idempotent() -> None:
    bus = SignalBus()
    counter = {"n": 0}

    def listener(event: LifecycleEvent) -> None:
        counter["n"] += 1

    bus.on(StartupBegin)(listener)
    bus.on(StartupBegin)(listener)
    await bus.emit(StartupBegin())
    assert counter["n"] == 1


async def test_off_removes_registered_listener() -> None:
    bus = SignalBus()
    calls: list[str] = []

    def listener(event: LifecycleEvent) -> None:
        calls.append("hit")

    bus.on(StartupBegin)(listener)
    assert bus.off(StartupBegin, listener) is True
    await bus.emit(StartupBegin())
    assert calls == []


async def test_off_on_missing_listener_returns_false() -> None:
    bus = SignalBus()
    assert bus.off(StartupBegin, lambda e: None) is False


async def test_clear_drops_every_listener() -> None:
    bus = SignalBus()
    bus.on(StartupBegin)(lambda e: None)
    bus.on(RequestComplete)(lambda e: None)
    assert bus.listener_count(StartupBegin) == 1
    bus.clear()
    assert bus.listener_count(StartupBegin) == 0
    assert bus.listener_count(RequestComplete) == 0


# ---------------------------------------------------------------------------
# MRO dispatch — base-class listeners see every subclass event
# ---------------------------------------------------------------------------


async def test_base_class_listener_catches_every_subclass() -> None:
    """A listener registered on :class:`LifecycleEvent` must receive
    every subclass event, making it a firehose suitable for tracing.
    """
    bus = SignalBus()
    seen: list[type] = []

    @bus.on(LifecycleEvent)
    def trace(event: LifecycleEvent) -> None:
        seen.append(type(event))

    await bus.emit(StartupBegin())
    await bus.emit(RequestReceived(request=None))
    await bus.emit(ShutdownBegin())

    assert seen == [StartupBegin, RequestReceived, ShutdownBegin]


async def test_specific_listener_does_not_receive_sibling_events() -> None:
    """A listener on ``StartupBegin`` must not fire on ``ShutdownBegin``."""
    bus = SignalBus()
    seen: list[type] = []

    @bus.on(StartupBegin)
    def on_start(event: StartupBegin) -> None:
        seen.append(type(event))

    await bus.emit(ShutdownBegin())
    assert seen == []


async def test_specific_listener_does_not_receive_parent_plain_event() -> None:
    """A listener on ``StartupComplete`` should not catch a bare
    :class:`LifecycleEvent` (which is the base class)."""
    bus = SignalBus()
    seen = []
    bus.on(StartupComplete)(lambda e: seen.append(e))

    await bus.emit(LifecycleEvent())  # bare base class
    assert seen == []


# ---------------------------------------------------------------------------
# Async listeners are awaited sequentially in registration order
# ---------------------------------------------------------------------------


async def test_async_listeners_run_in_registration_order() -> None:
    bus = SignalBus()
    order: list[str] = []

    @bus.on(StartupBegin)
    async def first(event: LifecycleEvent) -> None:
        await asyncio.sleep(0)
        order.append("first")

    @bus.on(StartupBegin)
    async def second(event: LifecycleEvent) -> None:
        order.append("second")

    await bus.emit(StartupBegin())
    assert order == ["first", "second"]


async def test_sync_and_async_listeners_coexist() -> None:
    bus = SignalBus()
    order: list[str] = []

    @bus.on(StartupBegin)
    def sync_listener(event: LifecycleEvent) -> None:
        order.append("sync")

    @bus.on(StartupBegin)
    async def async_listener(event: LifecycleEvent) -> None:
        order.append("async")

    await bus.emit(StartupBegin())
    assert order == ["sync", "async"]


# ---------------------------------------------------------------------------
# Listener-error isolation
# ---------------------------------------------------------------------------


async def test_listener_exception_does_not_break_other_listeners() -> None:
    bus = SignalBus()
    observations: list[str] = []

    @bus.on(StartupBegin)
    def bad(event: LifecycleEvent) -> None:
        raise RuntimeError("kaboom")

    @bus.on(StartupBegin)
    def good(event: LifecycleEvent) -> None:
        observations.append("good")

    # Must not raise \u2014 observability must not break the app.
    await bus.emit(StartupBegin())
    assert observations == ["good"]


async def test_async_listener_exception_is_swallowed() -> None:
    bus = SignalBus()
    reached_second = {"yes": False}

    @bus.on(StartupBegin)
    async def bad(event: LifecycleEvent) -> None:
        raise ValueError("nope")

    @bus.on(StartupBegin)
    async def ok(event: LifecycleEvent) -> None:
        reached_second["yes"] = True

    await bus.emit(StartupBegin())
    assert reached_second["yes"] is True


# ---------------------------------------------------------------------------
# Snapshot semantics \u2014 self-modifying listeners don't corrupt emit
# ---------------------------------------------------------------------------


async def test_listener_registering_sibling_during_emit_is_safe() -> None:
    """A listener that registers a second listener mid-emit should
    not see the new listener fire until the *next* emit. Otherwise
    a misbehaving listener could create an unbounded dispatch loop
    simply by re-registering itself.
    """
    bus = SignalBus()
    call_log: list[str] = []

    def late_listener(event: LifecycleEvent) -> None:
        call_log.append("late")

    @bus.on(StartupBegin)
    def registrar(event: LifecycleEvent) -> None:
        call_log.append("registrar")
        bus.on(StartupBegin)(late_listener)

    await bus.emit(StartupBegin())
    # Only registrar fires on this emit; late is queued for next.
    assert call_log == ["registrar"]
    await bus.emit(StartupBegin())
    assert call_log == ["registrar", "registrar", "late"]


async def test_listener_count_walks_mro_correctly() -> None:
    bus = SignalBus()
    bus.on(LifecycleEvent)(lambda e: None)
    bus.on(StartupBegin)(lambda e: None)
    bus.on(StartupBegin)(lambda e: None)
    # StartupBegin inherits one MRO listener and has two direct ones.
    assert bus.listener_count(StartupBegin) == 3
    # LifecycleEvent only has its direct listener.
    assert bus.listener_count(LifecycleEvent) == 1


# ---------------------------------------------------------------------------
# emit_sync \u2014 scheduling async listeners onto the current loop
# ---------------------------------------------------------------------------


async def test_emit_sync_schedules_async_listener_on_running_loop() -> None:
    bus = SignalBus()
    done = asyncio.Event()

    @bus.on(StartupBegin)
    async def later(event: LifecycleEvent) -> None:
        done.set()

    bus.emit_sync(StartupBegin())
    await asyncio.wait_for(done.wait(), timeout=1.0)
    assert done.is_set()


def test_emit_sync_without_running_loop_drops_async_listener() -> None:
    """Called from a sync context with no loop, async listeners are\n    dropped (with their coroutines closed to avoid the\n    'never awaited' warning) rather than raising or creating a\n    new loop."""
    bus = SignalBus()

    async def al(event: LifecycleEvent) -> None:
        pass

    bus.on(StartupBegin)(al)
    # Must not raise.
    bus.emit_sync(StartupBegin())
