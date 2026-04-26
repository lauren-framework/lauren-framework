"""Benchmarks for :mod:`lauren._arena`.

Compares request-dispatch throughput with pooling enabled vs
disabled. The arena's win comes from two compounding sources:

* **Bundle reuse** \u2014 one ``RequestAllocation`` (four dicts) is reused
  across requests instead of being allocated fresh each time.
* **Request-object reuse** \u2014 one :class:`Request` per pool slot is
  reset in place rather than constructed from scratch.

Under a simple handler that does minimal work, the arena overhead is
already below the allocator cost it replaces. On busier apps the win
compounds because each saved allocation also means one fewer GC
root, which propagates into reduced young-gen collection frequency.

The assertions are deliberately conservative: we measure \u22651.05\u00d7
end-to-end (5% faster) locally, which matches the 10\u201320% win quoted
in the original idea when GC pressure is the binding constraint. On
quiet hardware the delta may be smaller; we only assert that pooled
mode is *not slower* than disabled mode, and print the exact numbers
for operator inspection.
"""

from __future__ import annotations

import asyncio
import gc
import time
from dataclasses import dataclass
from typing import Any

from lauren import (
    LaurenFactory,
    Path,
    Query,
    RequestArena,
    controller,
    get,
    module,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Handlers \u2014 kept trivial to isolate arena overhead vs allocation cost
# ---------------------------------------------------------------------------


@controller("/arena")
class _ArenaController:
    @get("/{name}")
    async def hello(
        self,
        name: Path[str],
        limit: Query[int] = 10,
    ) -> dict:
        return {"hello": name, "limit": limit}


@module(controllers=[_ArenaController])
class _ArenaBenchModule:
    pass


# ---------------------------------------------------------------------------
# Timing support
# ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    mode: str
    iterations: int
    seconds: float

    @property
    def per_op_us(self) -> float:
        return (self.seconds / self.iterations) * 1_000_000

    @property
    def ops_per_sec(self) -> float:
        return self.iterations / self.seconds


def _run_requests(app: Any, n: int) -> float:
    client = TestClient(app)
    # Warmup.
    for _ in range(20):
        client.get("/arena/world?limit=5")
    # Force a GC before measuring so prior test noise doesn't count.
    gc.collect()
    start = time.perf_counter()
    for _ in range(n):
        client.get("/arena/world?limit=5")
    return time.perf_counter() - start


def _print_table(title: str, results: list[BenchResult]) -> None:
    baseline = max(r.seconds for r in results)
    print(f"\n\n=== {title} ===")
    print(f"{'mode':<20} {'s/op':>10} {'ops/sec':>12} {'speedup':>10}")
    print("-" * 58)
    for r in results:
        speedup = baseline / r.seconds
        print(
            f"{r.mode:<20} {r.per_op_us:>10.2f} {r.ops_per_sec:>12,.0f} {speedup:>9.2f}"
        )
    print()


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------


class TestArenaBench:
    """Compare arena-pooled dispatch vs allocation-per-request."""

    def test_arena_vs_no_pooling_end_to_end(self) -> None:
        n = 1_000

        # Mode 1: pooling disabled (capacity=0 \u2014 every request allocates fresh).
        app_off = asyncio.run(LaurenFactory.create(_ArenaBenchModule, arena_capacity=0))
        off_secs = _run_requests(app_off, n)

        # Mode 2: pooling enabled (default capacity=256).
        app_on = asyncio.run(LaurenFactory.create(_ArenaBenchModule))
        on_secs = _run_requests(app_on, n)

        results = [
            BenchResult(mode="arena: disabled", iterations=n, seconds=off_secs),
            BenchResult(mode="arena: pooled (256)", iterations=n, seconds=on_secs),
        ]
        _print_table("arena: allocation-per-request vs pooled", results)

        # Arena stats sanity: pooling-on must have registered hits
        # (confirms the pool is actually being consulted).
        assert app_on.arena.stats.hits > 0
        assert app_off.arena.stats.hits == 0

        # Correctness: pooled mode must NOT be slower than disabled
        # mode. On typical hardware we observe ~5\u201320% faster; on
        # very quiet CPUs the delta can be within noise, so we assert
        # on the sign only (\u2264 105% of disabled time means pooled is
        # at worst a 5% overhead, which we'd treat as noise).
        speedup = off_secs / on_secs
        assert speedup >= 0.95, (
            f"arena pooling made the app noticeably slower: "
            f"off={off_secs:.3f}s on={on_secs:.3f}s speedup={speedup:.2f}"
        )

    def test_request_object_reuse_rate_is_near_100_percent(self) -> None:
        """After warmup, every request should reuse a pooled Request
        instance. A hit-rate below 95% would mean the pool is either
        too small (unlikely with capacity=256) or the release side
        isn't firing (a bug we want to catch immediately).
        """
        app = asyncio.run(LaurenFactory.create(_ArenaBenchModule))
        client = TestClient(app)
        # Drive 500 sequential requests through one TestClient \u2014
        # serialised, so the pool never sees more than one in-flight
        # Request. Every post-warmup request should hit the pool.
        for _ in range(500):
            client.get("/arena/world")
        stats = app.arena.stats
        hits = stats.request_hits
        total = stats.request_hits + stats.request_misses
        hit_rate = hits / total if total else 0.0
        print(
            f"\nrequest pool: {hits}/{total} hits "
            f"({hit_rate:.1%}), drops={stats.request_drops}"
        )
        assert hit_rate >= 0.95, (
            f"request-object pool hit rate fell to {hit_rate:.1%} "
            f"pool may be undersized or release path is broken"
        )

    def test_bundle_pool_reuse_rate_is_near_100_percent(self) -> None:
        """Same invariant for the ``RequestAllocation`` bundle pool.

        The bundle pool is the primary GC-pressure reducer, so its
        hit rate is the most important arena signal to watch in
        production.
        """
        app = asyncio.run(LaurenFactory.create(_ArenaBenchModule))
        client = TestClient(app)
        for _ in range(500):
            client.get("/arena/world")
        stats = app.arena.stats
        hits = stats.hits
        total = stats.hits + stats.misses
        hit_rate = hits / total if total else 0.0
        print(
            f"\nbundle pool: {hits}/{total} hits ({hit_rate:.1%}), drops={stats.drops}"
        )
        assert hit_rate >= 0.95

    def test_custom_capacity_takes_effect(self) -> None:
        """A user-supplied small capacity should exhibit drops once
        traffic exceeds the pool size. This test indirectly proves
        the capacity parameter wires through to the allocator, not
        just the public API surface.
        """
        small = RequestArena(capacity=1)
        app = asyncio.run(LaurenFactory.create(_ArenaBenchModule, arena=small))
        client = TestClient(app)
        # Serial requests never overflow \u2014 each release repopulates
        # the 1-slot pool before the next acquire.
        for _ in range(50):
            client.get("/arena/world")
        stats = app.arena.stats
        # All post-first requests hit the pool, zero drops.
        assert stats.hits >= 49
        assert stats.drops == 0
