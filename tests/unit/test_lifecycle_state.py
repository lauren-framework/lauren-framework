"""The public application lifecycle state: transitions, signals, ``begin_draining()``."""

from __future__ import annotations

import asyncio
import signal
from typing import Any

import pytest

from lauren import (
    AppLifecycleState,
    Lauren,
    LaurenFactory,
    LifecycleStateChanged,
    SignalBus,
    StartupBegin,
    injectable,
    module,
    post_construct,
)
from lauren.exceptions import LifecycleViolationError
from lauren.signals import ShutdownBegin, install_signal_handlers
from lauren.testing import TestClient

S = AppLifecycleState


@module()
class EmptyModule:
    pass


def _recorded(signals: SignalBus) -> list[tuple[AppLifecycleState | None, AppLifecycleState | None, str]]:
    transitions: list[tuple[AppLifecycleState | None, AppLifecycleState | None, str]] = []
    signals.on(LifecycleStateChanged)(lambda e: transitions.append((e.previous, e.current, e.reason)))
    return transitions


def _failing_module() -> type:
    @injectable()
    class Broken:
        @post_construct
        async def boom(self) -> None:
            raise RuntimeError("cannot connect")

    @module(providers=[Broken])
    class BrokenModule:
        pass

    return BrokenModule


def test_a_new_app_is_created() -> None:
    app = LaurenFactory.create(EmptyModule)

    assert app.lifecycle_state is S.CREATED


async def test_startup_moves_through_starting_to_ready() -> None:
    signals = SignalBus()
    transitions = _recorded(signals)
    app = LaurenFactory.create(EmptyModule, signals=signals)

    await app.startup()

    assert app.lifecycle_state is S.READY
    assert transitions == [(S.CREATED, S.STARTING, ""), (S.STARTING, S.READY, "")]


async def test_starting_is_announced_before_startup_begin() -> None:
    signals = SignalBus()
    seen: list[str] = []
    signals.on(LifecycleStateChanged)(lambda e: seen.append(e.current.value))
    signals.on(StartupBegin)(lambda e: seen.append("startup_begin"))
    app = LaurenFactory.create(EmptyModule, signals=signals)

    await app.startup()

    assert seen == ["starting", "startup_begin", "ready"]


async def test_shutdown_drains_then_stops() -> None:
    signals = SignalBus()
    app = LaurenFactory.create(EmptyModule, signals=signals)
    await app.startup()
    transitions = _recorded(signals)

    await app.shutdown()

    assert app.lifecycle_state is S.STOPPED
    assert transitions == [(S.READY, S.DRAINING, "shutdown"), (S.DRAINING, S.STOPPED, "")]


async def test_begin_draining_flips_ready_to_draining_with_a_reason() -> None:
    signals = SignalBus()
    app = LaurenFactory.create(EmptyModule, signals=signals)
    await app.startup()
    transitions = _recorded(signals)

    changed = app.begin_draining(reason="preStop")

    assert changed is True
    assert app.lifecycle_state is S.DRAINING
    assert transitions == [(S.READY, S.DRAINING, "preStop")]


async def test_begin_draining_is_idempotent() -> None:
    signals = SignalBus()
    app = LaurenFactory.create(EmptyModule, signals=signals)
    await app.startup()
    transitions = _recorded(signals)
    app.begin_draining()

    changed = app.begin_draining()

    assert changed is False
    assert len(transitions) == 1


def test_begin_draining_before_startup_does_nothing() -> None:
    app = LaurenFactory.create(EmptyModule)

    changed = app.begin_draining()

    assert changed is False
    assert app.lifecycle_state is S.CREATED


async def test_shutdown_after_begin_draining_only_stops() -> None:
    signals = SignalBus()
    app = LaurenFactory.create(EmptyModule, signals=signals)
    await app.startup()
    app.begin_draining()
    transitions = _recorded(signals)

    await app.shutdown()

    assert transitions == [(S.DRAINING, S.STOPPED, "")]


async def test_requests_are_still_admitted_while_draining_until_shutdown_begins() -> None:
    app = LaurenFactory.create(EmptyModule)
    await app.startup()
    app.begin_draining()
    admitted_while_draining = app._running

    shutdown = asyncio.create_task(app.shutdown())
    await asyncio.sleep(0)
    admitted_during_shutdown = app._running
    await shutdown

    assert admitted_while_draining is True
    assert admitted_during_shutdown is False
    assert app._running is False


def test_running_and_started_are_false_before_startup() -> None:
    app = LaurenFactory.create(EmptyModule)

    assert (app._running, app._started) == (False, False)


async def test_a_failing_post_construct_leaves_the_app_failed() -> None:
    signals = SignalBus()
    transitions = _recorded(signals)
    app = LaurenFactory.create(_failing_module(), signals=signals)

    with pytest.raises(RuntimeError):
        await app.startup()

    assert app.lifecycle_state is S.FAILED
    assert transitions[-1][:2] == (S.STARTING, S.FAILED)
    assert "cannot connect" in transitions[-1][2]


async def test_a_cancelled_startup_is_failed() -> None:
    signals = SignalBus()
    transitions = _recorded(signals)

    @injectable()
    class Slow:
        @post_construct
        async def warm(self) -> None:
            raise asyncio.CancelledError

    @module(providers=[Slow])
    class SlowModule:
        pass

    app = LaurenFactory.create(SlowModule, signals=signals)

    with pytest.raises(asyncio.CancelledError):
        await app.startup()

    assert app.lifecycle_state is S.FAILED
    assert transitions[-1] == (S.STARTING, S.FAILED, "CancelledError")


async def test_a_failed_startup_can_be_retried() -> None:
    signals = SignalBus()
    attempts: list[int] = []

    @injectable()
    class Flaky:
        @post_construct
        async def connect(self) -> None:
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("not yet")

    @module(providers=[Flaky])
    class FlakyModule:
        pass

    app = LaurenFactory.create(FlakyModule, signals=signals)
    with pytest.raises(RuntimeError):
        await app.startup()
    transitions = _recorded(signals)

    await app.startup()

    assert app.lifecycle_state is S.READY
    assert transitions == [(S.FAILED, S.STARTING, ""), (S.STARTING, S.READY, "")]


async def test_a_failed_app_cannot_start_once_shutdown_began() -> None:
    app = LaurenFactory.create(_failing_module())
    with pytest.raises(RuntimeError):
        await app.startup()
    await app.shutdown()

    with pytest.raises(LifecycleViolationError, match="after shutdown") as info:
        await app.startup()

    assert info.value.detail == {"lifecycle_state": "failed"}


async def test_a_failed_app_stays_failed_after_shutdown() -> None:
    app = LaurenFactory.create(_failing_module())
    with pytest.raises(RuntimeError):
        await app.startup()

    await app.shutdown()

    assert app.lifecycle_state is S.FAILED


async def test_stopped_is_terminal() -> None:
    app = LaurenFactory.create(EmptyModule)
    await app.startup()
    await app.shutdown()

    with pytest.raises(LifecycleViolationError, match="after shutdown") as info:
        await app.startup()

    assert info.value.detail == {"lifecycle_state": "stopped"}
    assert app.lifecycle_state is S.STOPPED


async def test_startup_while_ready_is_called_twice() -> None:
    app = LaurenFactory.create(EmptyModule)
    await app.startup()

    with pytest.raises(LifecycleViolationError, match="called twice") as info:
        await app.startup()

    assert info.value.detail == {"lifecycle_state": "ready"}


async def test_a_second_startup_returns_quietly_without_strict_lifecycle() -> None:
    app = LaurenFactory.create(EmptyModule, strict_lifecycle=False)
    await app.startup()

    await app.startup()

    assert app.lifecycle_state is S.READY


async def test_shutting_down_a_never_started_app_stops_it() -> None:
    signals = SignalBus()
    transitions = _recorded(signals)
    app = LaurenFactory.create(EmptyModule, signals=signals)

    await app.shutdown()

    assert transitions == [(S.CREATED, S.STOPPED, "")]


async def test_the_state_changed_event_carries_the_app() -> None:
    signals = SignalBus()
    apps: list[Any] = []
    signals.on(LifecycleStateChanged)(lambda e: apps.append(e.app))
    app = LaurenFactory.create(EmptyModule, signals=signals)

    await app.startup()

    assert apps == [app, app]


# ---------------------------------------------------------------------------
# Lauren (imperative API) parity
# ---------------------------------------------------------------------------


def test_lauren_reports_created_before_it_compiles() -> None:
    app = Lauren()

    assert app.lifecycle_state is S.CREATED
    assert app.begin_draining() is False


def test_lauren_forwards_the_compiled_state_and_draining() -> None:
    app = Lauren()

    @app.get("/")
    async def index() -> dict:
        return {}

    TestClient(app).get("/")

    assert app.lifecycle_state is S.READY
    assert app.begin_draining(reason="test") is True
    assert app.lifecycle_state is S.DRAINING


# ---------------------------------------------------------------------------
# install_signal_handlers (driven through a stand-in loop: cross-platform)
# ---------------------------------------------------------------------------


class _Loop:
    """Records what ``install_signal_handlers`` asks of the event loop."""

    def __init__(self) -> None:
        self.handlers: dict[int, Any] = {}
        self.later: list[tuple[float, Any]] = []
        self.tasks: list[Any] = []

    def add_signal_handler(self, sig: int, callback: Any, *args: Any) -> None:
        self.handlers[sig] = lambda: callback(*args)

    def call_later(self, delay: float, callback: Any) -> None:
        self.later.append((delay, callback))

    def create_task(self, coro: Any) -> Any:
        coro.close()  # never awaited here; the scheduling is what we assert
        task = _Task()
        self.tasks.append(task)
        return task


class _Task:
    def add_done_callback(self, callback: Any) -> None:
        pass


async def test_a_signal_drains_first_and_delays_the_shutdown() -> None:
    app = LaurenFactory.create(EmptyModule)
    await app.startup()
    loop = _Loop()
    install_signal_handlers(app, signals=(signal.SIGTERM,), shutdown_delay=5.0, loop=loop)  # type: ignore[arg-type]

    loop.handlers[signal.SIGTERM]()

    assert app.lifecycle_state is S.DRAINING
    assert [delay for delay, _ in loop.later] == [5.0]
    assert loop.tasks == []


async def test_the_delayed_shutdown_is_scheduled_when_the_delay_elapses() -> None:
    app = LaurenFactory.create(EmptyModule)
    await app.startup()
    loop = _Loop()
    install_signal_handlers(app, signals=(signal.SIGTERM,), shutdown_delay=5.0, loop=loop)  # type: ignore[arg-type]
    loop.handlers[signal.SIGTERM]()

    loop.later[0][1]()

    assert len(loop.tasks) == 1


async def test_without_a_delay_the_shutdown_is_scheduled_at_once() -> None:
    app = LaurenFactory.create(EmptyModule)
    await app.startup()
    loop = _Loop()
    install_signal_handlers(app, signals=(signal.SIGTERM,), loop=loop)  # type: ignore[arg-type]

    loop.handlers[signal.SIGTERM]()

    assert app.lifecycle_state is S.DRAINING
    assert (loop.later, len(loop.tasks)) == ([], 1)


@pytest.mark.parametrize("delay", [-1, "5", True, None])
def test_an_invalid_shutdown_delay_is_rejected(delay: Any) -> None:
    app = LaurenFactory.create(EmptyModule)

    with pytest.raises(ValueError, match="shutdown_delay"):
        install_signal_handlers(app, shutdown_delay=delay, loop=_Loop())  # type: ignore[arg-type]


async def test_shutdown_begin_is_not_emitted_by_draining() -> None:
    signals = SignalBus()
    began: list[ShutdownBegin] = []
    signals.on(ShutdownBegin)(began.append)
    app = LaurenFactory.create(EmptyModule, signals=signals)
    await app.startup()

    app.begin_draining()

    assert began == []
