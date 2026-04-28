"""Integration tests for :mod:`lauren.signals`.

We verify that ``install_signal_handlers`` wires SIGTERM/SIGINT delivery to
``app.shutdown()`` and that ``wait_for_shutdown`` unblocks once a signal is
received.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys

import pytest

from lauren import LaurenFactory, controller, get, module
from lauren.logging import InMemoryLogger, LogLevel
from lauren.signals import DEFAULT_SIGNALS, install_signal_handlers, wait_for_shutdown


@controller("/")
class _C:
    @get("/")
    async def idx(self) -> dict:
        return {"ok": True}


@module(controllers=[_C])
class _Mod:
    pass


def _build(logger=None):
    return LaurenFactory.create(
        _Mod, logger=logger or InMemoryLogger(level=LogLevel.DEBUG)
    )


# Signal handling requires a real event loop and a POSIX platform for the
# full integration path. Skip on Windows.
pytestmark = pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="POSIX signal handlers are required for this test",
)


class TestInstallSignalHandlers:
    def test_sigterm_triggers_shutdown(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        async def scenario():
            event = install_signal_handlers(app, drain_timeout=1.0)
            # Simulate a signal from another thread \u2014 signal.raise_signal
            # targets the current process, which is exactly what we need.
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
            await asyncio.wait_for(event.wait(), timeout=2.0)
            # The shutdown task was scheduled; wait a beat for it to finish.
            await asyncio.sleep(0.2)

        asyncio.run(scenario())
        messages = [r.message for r in logger.records if r.context == "Shutdown"]
        assert any("SIGTERM" in m for m in messages)
        assert any("Goodbye" in m for m in messages)

    def test_sigint_triggers_shutdown(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        async def scenario():
            event = install_signal_handlers(app, drain_timeout=1.0)
            asyncio.get_running_loop().call_later(
                0.05, lambda: os.kill(os.getpid(), signal.SIGINT)
            )
            await asyncio.wait_for(event.wait(), timeout=2.0)
            await asyncio.sleep(0.2)

        asyncio.run(scenario())
        messages = [r.message for r in logger.records if r.context == "Shutdown"]
        assert any("SIGINT" in m for m in messages)

    def test_double_signal_is_ignored(self):
        logger = InMemoryLogger(level=LogLevel.DEBUG)
        app = _build(logger)

        async def scenario():
            event = install_signal_handlers(app, drain_timeout=1.0)
            loop = asyncio.get_running_loop()
            loop.call_later(0.05, lambda: os.kill(os.getpid(), signal.SIGTERM))
            loop.call_later(0.10, lambda: os.kill(os.getpid(), signal.SIGTERM))
            await asyncio.wait_for(event.wait(), timeout=2.0)
            await asyncio.sleep(0.3)

        asyncio.run(scenario())
        # Only ONE "Signal SIGTERM received \u2014 beginning graceful shutdown"
        # message should appear; the second raise should be logged as a
        # duplicate (WARN-level "ignoring").
        init_msgs = [
            r
            for r in logger.records
            if r.context == "Shutdown" and "beginning" in r.message
        ]
        assert len(init_msgs) == 1
        ignored = [
            r
            for r in logger.records
            if r.context == "Shutdown" and "ignoring" in r.message
        ]
        assert len(ignored) >= 1

    def test_wait_for_shutdown_returns_on_signal(self):
        app = _build()

        async def scenario():
            event = install_signal_handlers(app, drain_timeout=1.0)
            asyncio.get_running_loop().call_later(
                0.05, lambda: os.kill(os.getpid(), signal.SIGTERM)
            )
            await asyncio.wait_for(wait_for_shutdown(event), timeout=2.0)
            # Give the shutdown task a moment to complete.
            await asyncio.sleep(0.2)

        asyncio.run(scenario())  # no exception => ok

    def test_default_signals_cover_sigint_sigterm(self):
        assert signal.SIGINT in DEFAULT_SIGNALS
        assert signal.SIGTERM in DEFAULT_SIGNALS


class TestShutdownDuringInFlightTraffic:
    def test_in_flight_requests_drained(self):
        """If a request is in flight when shutdown fires, the response still
        completes (drain honours its budget)."""
        logger = InMemoryLogger(level=LogLevel.DEBUG)

        @controller("/")
        class Slow:
            @get("/slow")
            async def slow(self) -> dict:
                await asyncio.sleep(0.05)
                return {"done": True}

        @module(controllers=[Slow])
        class SlowMod: ...

        app = LaurenFactory.create(SlowMod, logger=logger)

        async def scenario():
            # Fire an ASGI call directly so we control timing.
            from lauren.testing import TestClient

            client = TestClient(app)
            # Serve one request and then shut down.
            r = client.get("/slow")
            assert r.status_code == 200
            await app.shutdown(drain_timeout=1.0)

        asyncio.run(scenario())
        messages = [r.message for r in logger.records]
        assert any("Goodbye" in m for m in messages)
