"""End-to-end tests for lifecycle event emission.

These tests build real :class:`LaurenApp` instances via
:meth:`LaurenFactory.create` and verify that:

* ``StartupBegin`` / ``StartupComplete`` fire in order during
  ``app.startup()``.
* ``RequestReceived`` and ``RequestComplete`` fire for every HTTP
  request, with ``RequestComplete`` carrying the duration, final
  status, and any captured exception.
* A listener that raises does not break the request path.
* The per-app bus isolates listeners across multiple apps in the
  same process.
* An explicit :class:`SignalBus` passed via ``LaurenFactory.create``
  overrides the default-bus seeding behaviour.
"""

from __future__ import annotations

import asyncio
from typing import Any


from lauren import (
    LaurenFactory,
    RequestComplete,
    RequestReceived,
    SignalBus,
    StartupBegin,
    StartupComplete,
    controller,
    get,
    module,
)
from lauren.exceptions import HTTPError
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Baseline \u2014 every lifecycle event fires in order
# ---------------------------------------------------------------------------


@controller("/ping")
class _PingController:
    @get("/")
    async def ping(self) -> dict:
        return {"ok": True}


@module(controllers=[_PingController])
class _PingModule:
    pass


def test_startup_events_fire_in_order() -> None:
    events: list[str] = []
    bus = SignalBus()

    @bus.on(StartupBegin)
    def on_begin(_: StartupBegin) -> None:
        events.append("begin")

    @bus.on(StartupComplete)
    def on_complete(event: StartupComplete) -> None:
        events.append(f"complete:{event.duration_s >= 0}")

    app = asyncio.run(LaurenFactory.create(_PingModule, signals=bus))
    assert events == ["begin", "complete:True"]
    assert app.signals is bus


def test_request_events_fire_for_every_call() -> None:
    received: list[str] = []
    completed: list[tuple[str, int]] = []
    bus = SignalBus()

    @bus.on(RequestReceived)
    def on_recv(event: RequestReceived) -> None:
        received.append(event.request.path)

    @bus.on(RequestComplete)
    def on_done(event: RequestComplete) -> None:
        completed.append((event.request.path, event.status))

    app = asyncio.run(LaurenFactory.create(_PingModule, signals=bus))
    client = TestClient(app)
    client.get("/ping/")
    client.get("/ping/")

    assert received == ["/ping/", "/ping/"]
    assert completed == [("/ping/", 200), ("/ping/", 200)]


def test_request_complete_carries_duration() -> None:
    durations: list[float] = []
    bus = SignalBus()

    @bus.on(RequestComplete)
    def on_done(event: RequestComplete) -> None:
        durations.append(event.duration_s)

    app = asyncio.run(LaurenFactory.create(_PingModule, signals=bus))
    TestClient(app).get("/ping/")
    assert len(durations) == 1
    # Duration must be non-negative and typically sub-second for a
    # no-op handler on a test client.
    assert 0.0 <= durations[0] < 1.0


# ---------------------------------------------------------------------------
# Error paths \u2014 RequestComplete still fires with captured_error set
# ---------------------------------------------------------------------------


@controller("/boom")
class _BoomController:
    @get("/")
    async def boom(self) -> dict:
        class _Teapot(HTTPError):
            status_code = 418
            code = "teapot"

        raise _Teapot("short and stout", detail={"x": 1})


@module(controllers=[_BoomController])
class _BoomModule:
    pass


def test_request_complete_captures_http_error() -> None:
    captured: list[Any] = []
    bus = SignalBus()

    @bus.on(RequestComplete)
    def on_done(event: RequestComplete) -> None:
        captured.append(
            (event.status, type(event.error).__name__ if event.error else None)
        )

    app = asyncio.run(LaurenFactory.create(_BoomModule, signals=bus))
    r = TestClient(app).get("/boom/")
    assert r.status_code == 418
    assert len(captured) == 1
    status, err_type = captured[0]
    assert status == 418
    assert err_type == "_Teapot"


def test_request_complete_fires_for_route_not_found() -> None:
    bus = SignalBus()
    captured: list[tuple[int, str | None]] = []

    @bus.on(RequestComplete)
    def on_done(event: RequestComplete) -> None:
        err = type(event.error).__name__ if event.error else None
        captured.append((event.status, err))

    app = asyncio.run(LaurenFactory.create(_PingModule, signals=bus))
    TestClient(app).get("/nonexistent")
    assert captured == [(404, "RouteNotFoundError")]


# ---------------------------------------------------------------------------
# Listener isolation \u2014 a broken listener doesn't break requests
# ---------------------------------------------------------------------------


def test_broken_listener_does_not_affect_response() -> None:
    bus = SignalBus()

    @bus.on(RequestReceived)
    def misbehaving(event: RequestReceived) -> None:
        raise RuntimeError("observability outage")

    app = asyncio.run(LaurenFactory.create(_PingModule, signals=bus))
    r = TestClient(app).get("/ping/")
    # Despite the broken listener, the client gets its normal response.
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Multi-app isolation
# ---------------------------------------------------------------------------


def test_each_app_owns_its_own_bus_by_default() -> None:
    """Two apps in the same process must not share listeners.

    If the framework accidentally installed a process-wide bus on
    every app, registering a listener on one app's bus would leak
    to the sibling. We pin the opposite invariant here.
    """
    app_a = asyncio.run(LaurenFactory.create(_PingModule))
    app_b = asyncio.run(LaurenFactory.create(_PingModule))
    assert app_a.signals is not app_b.signals

    a_hits: list[str] = []

    @app_a.signals.on(RequestComplete)
    def on_done(_: RequestComplete) -> None:
        a_hits.append("a")

    TestClient(app_a).get("/ping/")
    TestClient(app_b).get("/ping/")
    # Only app_a's bus saw the event.
    assert a_hits == ["a"]


def test_explicit_bus_overrides_default() -> None:
    """When the user passes ``signals=bus``, the framework must NOT
    seed it with the process-wide default listeners."""
    from lauren.signals import get_default_bus

    # Sanity: the default bus is clean at this point.
    get_default_bus().clear()
    default_hits: list[str] = []
    get_default_bus().on(RequestComplete)(lambda e: default_hits.append("d"))

    explicit = SignalBus()
    explicit_hits: list[str] = []
    explicit.on(RequestComplete)(lambda e: explicit_hits.append("e"))

    app = asyncio.run(LaurenFactory.create(_PingModule, signals=explicit))
    TestClient(app).get("/ping/")

    assert explicit_hits == ["e"]
    # Default-bus listener must NOT have fired \u2014 the user explicitly
    # chose a bus and we honour that choice.
    assert default_hits == []

    # Cleanup so other tests see a clean default bus.
    get_default_bus().clear()


# ---------------------------------------------------------------------------
# Module-level @on() decorator seeds per-app buses
# ---------------------------------------------------------------------------


def test_module_level_on_decorator_seeds_new_apps() -> None:
    """Users who decorate with ``@on(RequestComplete)`` at module
    load time expect their listener to fire on every app built
    afterwards. The framework copies the default bus's listeners
    into each new app's bus at construction time.
    """
    from lauren.signals import get_default_bus, on

    get_default_bus().clear()
    seen: list[int] = []

    @on(RequestComplete)
    def module_level(event: RequestComplete) -> None:
        seen.append(event.status)

    try:
        app = asyncio.run(LaurenFactory.create(_PingModule))
        TestClient(app).get("/ping/")
        assert seen == [200]
    finally:
        get_default_bus().clear()
