"""Unit tests for :mod:`lauren._arena`.

These tests exercise the arena in isolation (no ASGI app, no
dispatcher) to prove the core invariants:

* A lease yields a :class:`RequestAllocation` with empty containers.
* Release clears every container before returning it to the pool.
* The pool is LIFO \u2014 the most recently released bundle is reused
  first.
* The configured ``capacity`` is a hard cap; bundles released when
  the pool is full are dropped.
* Stats counters (``hits`` / ``misses`` / ``drops`` / ``in_flight``)
  track the lease lifecycle accurately.
* The Request-instance pool reuses objects via :meth:`Request.reset`
  and respects the same capacity / statistics discipline.
* Concurrent leases do not interfere with each other under asyncio.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren._arena import RequestAllocation, RequestArena
from lauren.types import AppState, ClientInfo, Headers, Request, ServerInfo


# ---------------------------------------------------------------------------
# Bundle pool \u2014 acquire / release / clear semantics
# ---------------------------------------------------------------------------


async def _noop_receive() -> dict[str, Any]:
    return {"type": "http.request", "body": b"", "more_body": False}


def _fresh_arena(capacity: int = 4) -> RequestArena:
    return RequestArena(capacity=capacity)


async def test_lease_yields_empty_allocation() -> None:
    arena = _fresh_arena()
    async with arena.lease() as alloc:
        assert isinstance(alloc, RequestAllocation)
        assert alloc.request_cache == {}
        assert alloc.framework_values == {}
        assert alloc.kwargs == {}
        assert alloc.scratch == {}


async def test_lease_clears_containers_on_exit() -> None:
    arena = _fresh_arena()
    async with arena.lease() as alloc:
        alloc.request_cache[int] = 42
        alloc.framework_values[str] = "value"
        alloc.kwargs["x"] = 1
        alloc.scratch["tmp"] = object()
    # After release the bundle is back in the pool with empty fields.
    assert arena.pooled() == 1
    async with arena.lease() as alloc2:
        assert alloc2.request_cache == {}
        assert alloc2.framework_values == {}
        assert alloc2.kwargs == {}
        assert alloc2.scratch == {}


async def test_lease_reuses_most_recent_allocation_lifo() -> None:
    arena = _fresh_arena()
    seen: list[int] = []
    async with arena.lease() as a:
        seen.append(id(a))
    async with arena.lease() as b:
        # Single-entry pool \u2014 same object reused.
        assert id(b) == seen[0]


async def test_lease_respects_capacity_drops_extra_bundles() -> None:
    arena = _fresh_arena(capacity=2)
    holders: list[RequestAllocation] = []
    # Acquire three simultaneously \u2014 nothing is released yet.
    async with arena.lease() as a:
        async with arena.lease() as b:
            async with arena.lease() as c:
                holders.extend([a, b, c])
                assert arena.pooled() == 0
    # All three released; only two can fit, the third is dropped.
    assert arena.pooled() == 2
    assert arena.stats.drops == 1


async def test_stats_counters_track_lifecycle() -> None:
    arena = _fresh_arena(capacity=2)
    assert arena.stats.hits == 0
    assert arena.stats.misses == 0
    assert arena.stats.in_flight == 0

    async with arena.lease():
        assert arena.stats.misses == 1
        assert arena.stats.in_flight == 1
    assert arena.stats.in_flight == 0

    async with arena.lease():
        # Second lease hits the pool.
        assert arena.stats.hits == 1
        assert arena.stats.in_flight == 1
    assert arena.stats.in_flight == 0


async def test_clear_empties_pools_but_keeps_stats() -> None:
    arena = _fresh_arena()
    async with arena.lease():
        pass
    assert arena.pooled() == 1
    assert arena.stats.misses == 1
    arena.clear()
    assert arena.pooled() == 0
    # Stats are preserved \u2014 operators running long-term benchmarks
    # should not lose their counters when clearing.
    assert arena.stats.misses == 1


async def test_exception_inside_lease_still_releases_bundle() -> None:
    arena = _fresh_arena()

    class _Boom(RuntimeError):
        pass

    with pytest.raises(_Boom):
        async with arena.lease() as alloc:
            alloc.request_cache[int] = 1
            raise _Boom()
    # The bundle still returned to the pool and was cleared.
    assert arena.pooled() == 1
    async with arena.lease() as alloc2:
        assert alloc2.request_cache == {}


async def test_capacity_zero_disables_pooling() -> None:
    arena = RequestArena(capacity=0)
    async with arena.lease():
        pass
    # Nothing pooled, everything dropped on release.
    assert arena.pooled() == 0
    assert arena.stats.drops == 1


async def test_negative_capacity_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        RequestArena(capacity=-1)


# ---------------------------------------------------------------------------
# Concurrent leases \u2014 pool integrity under asyncio interleaving
# ---------------------------------------------------------------------------


async def test_concurrent_leases_do_not_share_bundles() -> None:
    """Two coroutines leasing simultaneously must receive distinct bundles.

    A single bundle shared across two tasks would cause silent data
    corruption \u2014 one task's ``kwargs`` would overwrite the other's.
    """
    arena = _fresh_arena(capacity=8)
    ids_seen: list[int] = []
    barrier = asyncio.Event()
    observed_concurrency = asyncio.Event()

    async def worker(tag: str) -> None:
        async with arena.lease() as alloc:
            ids_seen.append(id(alloc))
            alloc.kwargs["tag"] = tag
            # Hand over to the scheduler so the other worker can also
            # acquire its own bundle \u2014 both must be in-flight
            # simultaneously to stress the acquire path.
            observed_concurrency.set()
            await barrier.wait()
            # Verify no one else mutated this lease's kwargs.
            assert alloc.kwargs["tag"] == tag

    task1 = asyncio.create_task(worker("A"))
    task2 = asyncio.create_task(worker("B"))
    await observed_concurrency.wait()
    barrier.set()
    await asyncio.gather(task1, task2)

    # Two distinct allocations observed, two misses.
    assert len(set(ids_seen)) == 2
    assert arena.stats.misses == 2


async def test_many_sequential_leases_reuse_same_bundle() -> None:
    """Sequential, non-overlapping leases should hit the free list."""
    arena = _fresh_arena(capacity=1)
    prev_id: int | None = None
    for _ in range(50):
        async with arena.lease() as alloc:
            if prev_id is not None:
                assert id(alloc) == prev_id
            prev_id = id(alloc)
    assert arena.stats.misses == 1  # only the very first lease missed
    assert arena.stats.hits == 49


# ---------------------------------------------------------------------------
# Request-object pool
# ---------------------------------------------------------------------------


def _request_kwargs(path: str = "/x") -> dict[str, Any]:
    return dict(
        method="GET",
        path=path,
        raw_query_string=b"",
        headers=Headers(),
        client=ClientInfo(None, None),
        server=ServerInfo(None, None),
        receive=_noop_receive,
        app_state=AppState(),
        max_body_size=1_048_576,
    )


def test_acquire_request_builds_fresh_when_pool_empty() -> None:
    arena = _fresh_arena()
    req = arena.acquire_request(Request, **_request_kwargs("/a"))
    assert isinstance(req, Request)
    assert req.path == "/a"
    assert arena.stats.request_misses == 1
    assert arena.stats.request_hits == 0


def test_release_and_reacquire_request_reuses_instance() -> None:
    arena = _fresh_arena()
    req = arena.acquire_request(Request, **_request_kwargs("/a"))
    first_id = id(req)
    arena.release_request(req)
    assert arena.pooled_requests() == 1
    req2 = arena.acquire_request(Request, **_request_kwargs("/b"))
    # Pooled instance reused \u2014 same object, re-initialised.
    assert id(req2) == first_id
    assert req2.path == "/b"
    assert arena.stats.request_hits == 1


def test_reset_clears_previous_request_state() -> None:
    """Reusing a Request must never leak state from the prior request.

    Specifically: path_params, query_params cache, cookies cache, and
    body cache all have to be cleared; otherwise a second request with
    different inputs would see the first request's values.
    """
    arena = _fresh_arena()
    req = arena.acquire_request(
        Request,
        method="GET",
        path="/first",
        raw_query_string=b"a=1&b=2",
        headers=Headers([("cookie", "session=abc")]),
        client=ClientInfo(None, None),
        server=ServerInfo(None, None),
        receive=_noop_receive,
        app_state=AppState(),
        max_body_size=1_048_576,
    )
    req._path_params["user_id"] = "42"
    # Force lazy caches to populate.
    _ = req.query_params
    _ = req.cookies
    arena.release_request(req)

    req2 = arena.acquire_request(
        Request,
        method="POST",
        path="/second",
        raw_query_string=b"",
        headers=Headers(),
        client=ClientInfo(None, None),
        server=ServerInfo(None, None),
        receive=_noop_receive,
        app_state=AppState(),
        max_body_size=1_048_576,
    )
    assert id(req2) == id(req)
    assert req2.method == "POST"
    assert req2.path == "/second"
    assert req2.path_params == {}
    assert req2.query_params == {}
    assert req2.cookies == {}
    assert req2._body is None
    assert req2._matched_route is None


def test_request_pool_drops_at_capacity() -> None:
    arena = _fresh_arena(capacity=2)
    first = arena.acquire_request(Request, **_request_kwargs())
    second = arena.acquire_request(Request, **_request_kwargs())
    third = arena.acquire_request(Request, **_request_kwargs())
    arena.release_request(first)
    arena.release_request(second)
    arena.release_request(third)  # pool is full \u2014 drop
    assert arena.pooled_requests() == 2
    assert arena.stats.request_drops == 1


def test_capacity_property_is_readonly() -> None:
    arena = RequestArena(capacity=17)
    assert arena.capacity == 17
    with pytest.raises(AttributeError):
        arena.capacity = 99  # type: ignore[misc]
