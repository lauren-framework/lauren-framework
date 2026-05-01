"""Integration tests for synchronous handler blocking behaviour.

A sync handler that calls ``time.sleep()`` runs directly on the event-loop
thread unless the framework offloads it to a thread pool.  These tests
measure wall-clock time for concurrent requests to determine whether sync
handlers block the event loop.

Expected (non-blocking) behaviour
----------------------------------
Two concurrent requests to a route that sleeps 0.4 s should complete in
roughly 0.4 s total (parallel), not 0.8 s (serial).

How it works
------------
``httpx.AsyncClient`` with ``ASGITransport`` sends real ASGI calls through
the application.  ``asyncio.gather`` fires both requests at the same time.
If the sync handler blocks the event-loop thread, the second request cannot
start until the first finishes.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient

# Chosen to be long enough to distinguish serial vs parallel but short
# enough to keep the test suite fast.
SLOW = 0.4  # seconds
PARALLEL_THRESHOLD = SLOW * 1.5  # < this → ran in parallel; > this → serialised


# ---------------------------------------------------------------------------
# Test application
# ---------------------------------------------------------------------------


@controller("/")
class SlowController:
    @get("/slow")
    def slow_sync(self) -> dict:
        """Sync handler that blocks for SLOW seconds."""
        time.sleep(SLOW)
        return {"done": True}

    @get("/fast-sync")
    def fast_sync(self) -> dict:
        """Sync handler that returns immediately."""
        return {"fast": True}

    @get("/fast-async")
    async def fast_async(self) -> dict:
        """Async handler that returns immediately (control group)."""
        return {"fast": True}


@module(controllers=[SlowController])
class SlowModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_client() -> tuple[httpx.AsyncClient, object]:
    """Start the app and return an (AsyncClient, app) pair."""
    app = LaurenFactory.create(SlowModule)
    await app.startup()
    transport = httpx.ASGITransport(app=app)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    return client, app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSyncHandlerNonBlocking:
    """Assert that sync handlers do NOT block the event loop.

    Each test fires two requests concurrently and asserts that the total
    wall-clock time is close to a single handler's execution time — i.e.
    they ran in parallel.
    """

    async def test_two_concurrent_slow_sync_requests_run_in_parallel(self):
        """Two concurrent sync-sleep requests must finish in ~SLOW seconds total.

        Failure means the framework is blocking the event loop: the two
        requests serialise and take ~2*SLOW seconds.
        """
        client, app = await _make_client()
        try:
            start = time.monotonic()
            r1, r2 = await asyncio.gather(
                client.get("/slow"),
                client.get("/slow"),
            )
            elapsed = time.monotonic() - start
        finally:
            await client.aclose()
            await app.shutdown()

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert elapsed < PARALLEL_THRESHOLD, (
            f"Two concurrent sync-sleep({SLOW}s) requests took {elapsed:.2f}s "
            f"(threshold {PARALLEL_THRESHOLD:.2f}s).  "
            "This indicates the event loop is being blocked by the sync handler."
        )

    async def test_fast_async_route_not_blocked_by_concurrent_slow_sync(self):
        """A fast async route must respond quickly even while a slow sync handler runs.

        If the sync handler blocks the event loop, the fast async route
        cannot run until the slow one finishes.
        """
        client, app = await _make_client()
        fast_elapsed: float | None = None
        try:
            fast_start = time.monotonic()

            async def timed_fast():
                nonlocal fast_elapsed
                r = await client.get("/fast-async")
                fast_elapsed = time.monotonic() - fast_start
                return r

            slow_r, fast_r = await asyncio.gather(
                client.get("/slow"),
                timed_fast(),
            )
        finally:
            await client.aclose()
            await app.shutdown()

        assert slow_r.status_code == 200
        assert fast_r.status_code == 200
        assert fast_elapsed is not None
        assert fast_elapsed < PARALLEL_THRESHOLD, (
            f"Fast async route took {fast_elapsed:.2f}s while a slow sync route was "
            f"running (threshold {PARALLEL_THRESHOLD:.2f}s).  "
            "The sync handler appears to be blocking the event loop."
        )

    async def test_fast_sync_route_not_blocked_by_concurrent_slow_sync(self):
        """A fast sync route must also not be delayed by a slow concurrent sync handler."""
        client, app = await _make_client()
        fast_elapsed: float | None = None
        try:
            fast_start = time.monotonic()

            async def timed_fast():
                nonlocal fast_elapsed
                r = await client.get("/fast-sync")
                fast_elapsed = time.monotonic() - fast_start
                return r

            slow_r, fast_r = await asyncio.gather(
                client.get("/slow"),
                timed_fast(),
            )
        finally:
            await client.aclose()
            await app.shutdown()

        assert slow_r.status_code == 200
        assert fast_r.status_code == 200
        assert fast_elapsed is not None
        assert fast_elapsed < PARALLEL_THRESHOLD, (
            f"Fast sync route took {fast_elapsed:.2f}s while a slow sync route was "
            f"running (threshold {PARALLEL_THRESHOLD:.2f}s).  "
            "The sync handler appears to be blocking the event loop."
        )


class TestSyncHandlerCorrectness:
    """Return-value and error behaviour must be identical for thread-offloaded handlers."""

    def test_sync_route_still_returns_correct_value(self):
        client = TestClient(LaurenFactory.create(SlowModule))
        r = client.get("/slow")
        assert r.status_code == 200
        assert r.json() == {"done": True}

    def test_fast_sync_route_still_works(self):
        client = TestClient(LaurenFactory.create(SlowModule))
        r = client.get("/fast-sync")
        assert r.json() == {"fast": True}

    def test_fast_async_route_still_works(self):
        client = TestClient(LaurenFactory.create(SlowModule))
        r = client.get("/fast-async")
        assert r.json() == {"fast": True}
