"""Per-request arena allocator — pooled ephemeral objects.

Motivation
----------

Every HTTP request in lauren allocates a handful of short-lived
container objects:

* a ``request_cache`` dict keyed by provider class,
* a ``framework_values`` dict keyed by ``Request`` type,
* a ``kwargs`` dict assembled from extractor outputs,
* various small lists / tuples used while walking the handler plan.

Under sustained load these objects churn the garbage collector —
especially the young generation — which measurably drags on request
latency. The arena pre-allocates small *free lists* of each shape and
lends them to the ASGI dispatcher at request entry, reclaiming them
via a ``finally`` block at request end.

Design
------

The arena is **per-application**, not global: two ``LaurenApp``
instances running in the same process (a common pattern when mounting
admin sub-apps) do not share pools, so their lifetimes stay
independent. Each pool is bounded by a configurable ``capacity`` so
the arena's memory footprint is predictable even under traffic
bursts — any object returned when the pool is full is discarded for
the GC to reclaim on its own schedule.

Acquire / release discipline is surfaced through
:meth:`RequestArena.lease` — an async context manager — which hands
back a fresh :class:`RequestAllocation` bundle to the caller and
returns every pooled slot to the arena on exit. The dispatcher never
calls ``acquire_*`` / ``release_*`` directly; the lease manages the
full lifecycle so a mid-flight exception cannot leak an allocation.

Safety
------

The arena is **single-threaded under asyncio**: ASGI servers dispatch
each request on a cooperative coroutine and leases do not cross
``await`` boundaries without releasing. Tests cover concurrent leases
by awaiting inside the lease block to prove the pool remains correct
when many coroutines interleave.

Every dict lent out is ``.clear()``-ed on release so pooled objects
never leak user data across requests — this is a correctness
invariant, not just hygiene, and is verified by the test suite.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator


# ---------------------------------------------------------------------------
# Leased allocation bundle
# ---------------------------------------------------------------------------


@dataclass
class RequestAllocation:
    """Handle to a lease of pooled containers.

    The dispatcher receives one of these per request and reads its
    fields directly — no indirection, no attribute lookups on the
    arena itself inside the hot path. The :class:`RequestArena` is
    responsible for populating these fields at lease time and reading
    them back for reuse at release time.

    Every field is a plain mutable container that has been ``clear()``
    -ed before the request sees it.
    """

    #: Cache of request-scoped DI instances, keyed by provider class.
    #: Ownership transfers to the dispatcher for the duration of the
    #: lease; the arena reclaims it in :meth:`RequestArena._release`.
    request_cache: dict[type, Any] = field(default_factory=dict)

    #: Type-keyed map of runtime-supplied dependencies (``Request``,
    #: ``WebSocket``, etc.). Kept separate from ``request_cache`` so
    #: that the DI container can short-circuit a lookup before walking
    #: its provider graph.
    framework_values: dict[type, Any] = field(default_factory=dict)

    #: kwargs dict handed to the handler. Built by the extractor loop
    #: from compiled ``_Extraction`` entries.
    kwargs: dict[str, Any] = field(default_factory=dict)

    #: Working scratch dict for ephemeral mapping needs inside
    #: middleware / guard dispatch (unused by default but claimed by
    #: the same lease so a middleware doesn't have to build its own).
    scratch: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# The arena itself
# ---------------------------------------------------------------------------


class RequestArena:
    """Pool of reusable per-request containers.

    Instances are cheap to create; a ``LaurenApp`` owns exactly one.
    The arena exposes a single user-facing entry point —
    :meth:`lease` — which yields a ready-to-use
    :class:`RequestAllocation`.

    Parameters
    ----------
    capacity:
        Maximum number of cached bundles the arena will keep. Once full,
        released bundles are dropped for the GC to collect. Smaller apps
        (or tests) can set this to ``0`` to disable pooling entirely — a
        useful debug configuration that makes allocation patterns
        identical to a classical request-per-alloc design. Defaults to
        ``256`` which comfortably covers burst concurrency for typical
        workloads without pinning much memory.

    Attributes
    ----------
    capacity:
        The configured cap, exposed read-only for introspection.
    """

    __slots__ = ("_capacity", "_pool", "_request_pool", "_stats")

    def __init__(self, capacity: int = 256) -> None:
        if capacity < 0:
            raise ValueError("arena capacity must be non-negative")
        self._capacity = capacity
        # ``list.pop`` from the tail is O(1); we treat the pool as a
        # LIFO stack so the hottest bundle is reused first — that
        # improves cache locality because the most recently released
        # dict's underlying hash table is still warm.
        self._pool: list[RequestAllocation] = []
        # Separate pool for :class:`Request` objects. Kept distinct
        # from the bundle pool because a bundle may be leased for work
        # that does not involve a Request (e.g. WebSocket dispatch),
        # and a Request may outlive its bundle in background-task
        # scenarios.
        self._request_pool: list[Any] = []
        # Lightweight counters — opt-in introspection for operators
        # tuning ``capacity``. Updated only inside acquire / release
        # which are already hot, so the cost is negligible.
        self._stats = _ArenaStats()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def capacity(self) -> int:
        """Configured upper bound on pooled allocations."""
        return self._capacity

    @property
    def stats(self) -> "_ArenaStats":
        """Live counters — ``hits``, ``misses``, ``drops``, ``in_flight``.

        Useful for deciding whether to bump the ``capacity`` setting.
        The stats object is mutable; callers should treat it as
        read-only.
        """
        return self._stats

    def pooled(self) -> int:
        """Number of bundles currently idle in the pool."""
        return len(self._pool)

    @asynccontextmanager
    async def lease(self) -> AsyncIterator[RequestAllocation]:
        """Acquire a :class:`RequestAllocation` for the current request.

        The bundle is guaranteed to have empty container fields. On
        exit (normal or exceptional) every container is cleared and
        the bundle returns to the pool — or is dropped if the pool is
        already at capacity.

        Use as a standard ``async with`` block::

            async with arena.lease() as alloc:
                ...  # use alloc.request_cache, alloc.kwargs, etc.
        """
        alloc = self._acquire()
        try:
            yield alloc
        finally:
            self._release(alloc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _acquire(self) -> RequestAllocation:
        """Pop a bundle off the pool or create a fresh one."""
        if self._pool:
            alloc = self._pool.pop()
            self._stats.hits += 1
        else:
            alloc = RequestAllocation()
            self._stats.misses += 1
        self._stats.in_flight += 1
        return alloc

    def _release(self, alloc: RequestAllocation) -> None:
        """Clear and return a bundle to the pool.

        Clearing is done by the arena, not the caller, so that the
        dispatcher cannot forget. If the pool is full the bundle is
        dropped — the Python GC reclaims it on its normal schedule.
        """
        alloc.request_cache.clear()
        alloc.framework_values.clear()
        alloc.kwargs.clear()
        alloc.scratch.clear()
        self._stats.in_flight = max(0, self._stats.in_flight - 1)
        if len(self._pool) < self._capacity:
            self._pool.append(alloc)
        else:
            self._stats.drops += 1

    # ------------------------------------------------------------------
    # Test / diagnostic helpers
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Empty every pool immediately.

        Primarily useful in tests that want deterministic starting
        state. Does not reset :attr:`stats` so long-running benchmarks
        can still observe the cumulative counters.
        """
        self._pool.clear()
        self._request_pool.clear()

    # ------------------------------------------------------------------
    # Request-object pool
    # ------------------------------------------------------------------
    #
    # The Request class supports in-place reset via :meth:`Request.reset`,
    # which lets the dispatcher reuse the same object across requests.
    # The saving is small per-call (one ``Request.__init__`` plus a
    # handful of dict allocations) but it compounds under sustained
    # load. Like the bundle pool, this one is bounded by ``capacity``.

    def acquire_request(self, factory: Any, **kwargs: Any) -> Any:
        """Return a :class:`lauren.types.Request`, reusing a pooled
        instance when available.

        ``factory`` is the callable used to build a fresh Request when
        the pool is empty. It is passed the full ``**kwargs`` bundle
        verbatim — the caller chooses which arguments the Request
        constructor takes, so the arena stays decoupled from the
        Request's exact signature.

        Pooled instances are re-initialised via
        :meth:`Request.reset(**kwargs)` so every attribute set by
        ``__init__`` is fresh.
        """
        if self._request_pool:
            req = self._request_pool.pop()
            req.reset(**kwargs)
            self._stats.request_hits += 1
            return req
        self._stats.request_misses += 1
        return factory(**kwargs)

    def release_request(self, req: Any) -> None:
        """Return a :class:`Request` to the pool.

        No clearing is performed here — reuse passes through
        :meth:`Request.reset`, which does a full re-initialisation.
        When the pool is already at capacity the instance is dropped
        for the GC to collect normally.
        """
        if len(self._request_pool) < self._capacity:
            self._request_pool.append(req)
        else:
            self._stats.request_drops += 1

    def pooled_requests(self) -> int:
        """Number of Request instances currently idle in the pool."""
        return len(self._request_pool)


@dataclass
class _ArenaStats:
    """Cumulative usage counters for a :class:`RequestArena`.

    Bundle counters (``RequestAllocation``):

    * ``hits``       — bundle leases served from the free list.
    * ``misses``     — bundle leases that needed a fresh allocation.
    * ``drops``      — bundles discarded because the pool was full.
    * ``in_flight``  — bundles currently held by active leases.

    Request-object counters:

    * ``request_hits``    — Request reuses (pool had an idle instance).
    * ``request_misses``  — Request allocations (pool was empty).
    * ``request_drops``   — Request instances dropped (pool was full).
    """

    hits: int = 0
    misses: int = 0
    drops: int = 0
    in_flight: int = 0
    request_hits: int = 0
    request_misses: int = 0
    request_drops: int = 0


__all__ = [
    "RequestAllocation",
    "RequestArena",
]
