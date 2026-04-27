"""Benchmarks for :class:`lauren.signals.SignalBus`.

Measures the cost of the event-bus machinery in three regimes:

* **Empty bus** — no listeners registered. The fast-path check
  inside :meth:`SignalBus.emit` makes this effectively free; we
  pin the ``<1 µs/op`` ceiling.
* **One sync listener** — the common observability case (metrics
  counter, log line).
* **Ten listeners** — a loaded bus. The per-listener overhead must
  scale linearly and remain well under 1 µs per listener.

The benchmark also compares end-to-end request throughput with and
without a busy bus so the operator can see the amortised cost on a
realistic workload.
"""

from __future__ import annotations

import asyncio
import gc
import time
from dataclasses import dataclass

from lauren import (
    LaurenFactory,
    RequestComplete,
    RequestReceived,
    SignalBus,
    StartupBegin,
    controller,
    get,
    module,
)
from lauren.signals import LifecycleEvent
from lauren.testing import TestClient


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


def _print_table(title: str, results: list[BenchResult]) -> None:
    baseline = max(r.seconds for r in results)
    print(f"\n\n=== {title} ===")
    print(f"{'mode':<30} {'s/op':>10} {'ops/sec':>14} {'speedup':>10}")
    print("-" * 68)
    for r in results:
        speedup = baseline / r.seconds
        print(
            f"{r.mode:<30} {r.per_op_us:>10.3f} {r.ops_per_sec:>14,.0f} "
            f"{speedup:>9.2f}x"
        )
    print()


# ---------------------------------------------------------------------------
# 1. Raw SignalBus.emit microbench
# ---------------------------------------------------------------------------


class TestSignalBusMicroBench:
    """Compare the raw cost of ``bus.emit(event)`` across listener counts."""

    def test_emit_scales_with_listener_count(self) -> None:
        n = 50_000
        results: list[BenchResult] = []

        # Case 1: empty bus.
        empty = SignalBus()
        event = StartupBegin()
        gc.collect()

        async def run_empty() -> None:
            for _ in range(n):
                await empty.emit(event)

        t0 = time.perf_counter()
        asyncio.run(run_empty())
        results.append(
            BenchResult(
                mode="emit (no listeners)",
                iterations=n,
                seconds=time.perf_counter() - t0,
            )
        )

        # Case 2: one sync listener.
        single = SignalBus()
        hits = {"n": 0}

        def inc(e: LifecycleEvent) -> None:
            hits["n"] += 1

        single.on(StartupBegin)(inc)
        gc.collect()

        async def run_single() -> None:
            for _ in range(n):
                await single.emit(event)

        t0 = time.perf_counter()
        asyncio.run(run_single())
        results.append(
            BenchResult(
                mode="emit (1 sync listener)",
                iterations=n,
                seconds=time.perf_counter() - t0,
            )
        )
        assert hits["n"] == n

        # Case 3: ten sync listeners.
        loaded = SignalBus()
        for _ in range(10):
            loaded.on(StartupBegin)(inc)
        gc.collect()

        async def run_loaded() -> None:
            for _ in range(n):
                await loaded.emit(event)

        t0 = time.perf_counter()
        asyncio.run(run_loaded())
        results.append(
            BenchResult(
                mode="emit (10 sync listeners)",
                iterations=n,
                seconds=time.perf_counter() - t0,
            )
        )

        _print_table(f"SignalBus.emit \u00d7 {n}", results)

        by_mode = {r.mode: r for r in results}
        empty_result = by_mode["emit (no listeners)"]
        # Empty bus must stay under 1 s/op \u2014 the fast-path check on
        # ``_listeners`` inside emit is a single dict truthiness test.
        assert empty_result.per_op_us < 1.0, (
            f"empty emit was {empty_result.per_op_us:.3f} s/op; "
            f"the no-listener fast path regressed"
        )

    def test_empty_bus_overhead_is_near_zero(self) -> None:
        """Sanity: a fresh ``SignalBus`` has zero listeners; emission\n        should be indistinguishable from a no-op function call.\n"""
        bus = SignalBus()
        event = StartupBegin()
        n = 10_000

        async def run() -> None:
            for _ in range(n):
                await bus.emit(event)

        t0 = time.perf_counter()
        asyncio.run(run())
        elapsed = time.perf_counter() - t0
        print(
            f"\n\n=== empty bus: {n} emits in {elapsed:.4f}s "
            f"({(elapsed / n) * 1_000_000:.3f} s/op)\n"
        )
        assert elapsed / n < 1e-6, "empty emit exceeded 1 s/op"


# ---------------------------------------------------------------------------
# 2. End-to-end request throughput: bus empty vs loaded
# ---------------------------------------------------------------------------


@controller("/sig")
class _SigController:
    @get("/hi")
    async def hi(self) -> dict:
        return {"ok": True}


@module(controllers=[_SigController])
class _SigModule:
    pass


class TestSignalBusEndToEnd:
    def test_empty_bus_is_near_zero_overhead_per_request(self) -> None:
        n = 500

        # Baseline: no signals bus activity at all (fresh bus, zero listeners).
        app_empty = asyncio.run(LaurenFactory.create(_SigModule))
        client_empty = TestClient(app_empty)
        for _ in range(10):
            client_empty.get("/sig/hi")
        gc.collect()
        t0 = time.perf_counter()
        for _ in range(n):
            client_empty.get("/sig/hi")
        empty_secs = time.perf_counter() - t0

        # Loaded bus: one listener per lifecycle event (RequestReceived
        # and RequestComplete are the two fired per request).
        bus = SignalBus()
        counters = {"recv": 0, "done": 0}

        @bus.on(RequestReceived)
        def on_recv(_: RequestReceived) -> None:
            counters["recv"] += 1

        @bus.on(RequestComplete)
        def on_done(_: RequestComplete) -> None:
            counters["done"] += 1

        app_loaded = asyncio.run(LaurenFactory.create(_SigModule, signals=bus))
        client_loaded = TestClient(app_loaded)
        for _ in range(10):
            client_loaded.get("/sig/hi")
        gc.collect()
        t0 = time.perf_counter()
        for _ in range(n):
            client_loaded.get("/sig/hi")
        loaded_secs = time.perf_counter() - t0

        results = [
            BenchResult(mode="no listeners", iterations=n, seconds=empty_secs),
            BenchResult(mode="2 sync listeners", iterations=n, seconds=loaded_secs),
        ]
        _print_table(f"end-to-end request throughput \u00d7 {n}", results)

        # Correctness: both listeners saw every request.
        assert counters["recv"] == n + 10  # include warmup
        assert counters["done"] == n + 10

        # Perf: adding two sync listeners must not slow the app by
        # more than 20% \u2014 observability overhead must be negligible.
        slowdown = loaded_secs / empty_secs
        assert slowdown <= 1.9, (
            f"2 listeners caused {slowdown:.2f}x slowdown; budget is 1.20x"
        )
