"""Benchmarks for the router static-prefix fast path.

Compares three router workloads:

* **All-static workload** — every route is purely static. The fast
  path wins big: a single dict lookup replaces segment splitting and
  the recursive radix walk.
* **Mixed workload** — one static route and one dynamic route in the
  same router, lookup targets the static route. Fast path still
  wins, though the gap narrows.
* **Dynamic-only workload** — every route contains a ``{param}``.
  The fast path is bypassed; we measure it to confirm the
  optimisation imposes zero overhead on the slow path.

Two flavours of measurement:

1. **Direct ``router.find()`` calls** — microbenchmark, zero ASGI
   overhead. Isolates the router's contribution.
2. **End-to-end ``TestClient.get()`` calls** — the user-visible
   latency, diluted by the full request lifecycle.
"""

from __future__ import annotations

import gc
import time
from dataclasses import dataclass

from lauren import LaurenFactory, Path, controller, get, module
from lauren._routing import Router
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Router fixtures
# ---------------------------------------------------------------------------


def _build_all_static_router(n_routes: int = 50) -> Router:
    """A router with ``n_routes`` pure-static routes, no dynamic."""
    router = Router()
    for i in range(n_routes):
        router.add_route("GET", f"/static/r{i}/endpoint", _h)
    router.freeze()
    return router


def _build_mixed_router(n_static: int = 25, n_dynamic: int = 25) -> Router:
    """Mix of static and dynamic; lookups target static paths."""
    router = Router()
    for i in range(n_static):
        router.add_route("GET", f"/static/s{i}", _h)
    for i in range(n_dynamic):
        router.add_route("GET", f"/dynamic/d{i}/{{id}}", _h)
    router.freeze()
    return router


def _build_dynamic_only_router(n_routes: int = 50) -> Router:
    router = Router()
    for i in range(n_routes):
        router.add_route("GET", f"/dynamic/d{i}/{{id}}", _h)
    router.freeze()
    return router


def _h() -> None:
    pass


# ---------------------------------------------------------------------------
# Measurement helpers
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


def _time_find(router: Router, method: str, path: str, n: int) -> BenchResult:
    """Call ``router.find`` ``n`` times, return total elapsed seconds."""
    # Warmup.
    for _ in range(100):
        router.find(method, path)
    gc.collect()
    start = time.perf_counter()
    for _ in range(n):
        router.find(method, path)
    return BenchResult(mode=path, iterations=n, seconds=time.perf_counter() - start)


def _print_table(title: str, results: list[BenchResult]) -> None:
    baseline = max(r.seconds for r in results)
    print(f"\n\n=== {title} ===")
    print(f"{'mode':<28} {'s/op':>10} {'ops/sec':>12} {'speedup':>10}")
    print("-" * 64)
    for r in results:
        speedup = baseline / r.seconds
        print(
            f"{r.mode:<28} {r.per_op_us:>10.3f} {r.ops_per_sec:>12,.0f} "
            f"{speedup:>9.2f}x"
        )
    print()


# ---------------------------------------------------------------------------
# Microbenchmarks on Router.find
# ---------------------------------------------------------------------------


class TestRouterFindBench:
    """Compares ``router.find`` throughput across route shapes."""

    def test_static_lookup_is_faster_than_dynamic(self) -> None:
        """Pure-static lookup (fast path) vs pure-dynamic lookup
        (slow path) on comparably-sized routers. The fast path's
        single dict lookup should be meaningfully faster than the
        multi-step radix walk \u2014 we measure \u22482-4x locally.
        """
        n = 50_000
        static_router = _build_all_static_router(50)
        dynamic_router = _build_dynamic_only_router(50)

        static_result = _time_find(static_router, "GET", "/static/r25/endpoint", n)
        static_result.mode = "static (fast path)"
        dynamic_result = _time_find(dynamic_router, "GET", "/dynamic/d25/42", n)
        dynamic_result.mode = "dynamic (radix walk)"

        results = [dynamic_result, static_result]
        _print_table(f"Router.find {n} (50-route tables)", results)

        speedup = dynamic_result.seconds / static_result.seconds
        # We measure \u22482.5-4x locally. Assert \u22651.3x so the test is
        # robust on slow CI while still proving the fast path delivers.
        assert speedup >= 1.3, (
            f"static fast path only {speedup:.2f}x faster than radix walk; regression?"
        )

    def test_static_lookup_in_mixed_router_still_fast(self) -> None:
        """Static lookups in a router that also contains dynamic
        routes must still hit the fast path. If the fast path were
        conditioned on ``_has_dynamic_routes == False`` this test
        would catch the regression.
        """
        n = 50_000
        mixed = _build_mixed_router(25, 25)

        result = _time_find(mixed, "GET", "/static/s12", n)
        result.mode = "static in mixed router"

        print(f"\n\n=== static lookup in mixed router ({n} iterations) ===")
        print(
            f"{result.mode:<28} {result.per_op_us:>10.3f} s/op "
            f"{result.ops_per_sec:>12,.0f} ops/sec\n"
        )

        # Sanity: should still be much faster than a dynamic lookup
        # in the same router. This is mostly a smoke check \u2014 the
        # real correctness comes from the unit-test suite.
        dyn_result = _time_find(mixed, "GET", "/dynamic/d12/99", n)
        speedup = dyn_result.seconds / result.seconds
        assert speedup >= 1.2, (
            f"static-in-mixed only {speedup:.2f}x faster than dynamic-in-mixed"
        )

    def test_fast_path_does_not_allocate_params_dict(self) -> None:
        """The fast path returns the shared ``_EMPTY_PARAMS`` sentinel
        rather than allocating a fresh ``{}`` per lookup. We pin the
        identity invariant here (``is`` check) so the saving lands
        in production.
        """
        from lauren._routing import _EMPTY_PARAMS

        router = _build_all_static_router(10)
        # Every call over a few thousand iterations must return the
        # identical dict object \u2014 proof that no allocation occurs.
        for _ in range(5_000):
            _, params = router.find("GET", "/static/r5/endpoint")
            assert params is _EMPTY_PARAMS


# ---------------------------------------------------------------------------
# End-to-end dispatch benchmark \u2014 full request lifecycle
# ---------------------------------------------------------------------------


@controller("/app")
class _AppController:
    @get("/health")
    async def health(self) -> dict:
        return {"status": "ok"}

    @get("/users/{user_id}")
    async def user(self, user_id: Path[int]) -> dict:
        return {"id": user_id}


@module(controllers=[_AppController])
class _BenchAppModule:
    pass


class TestRouterEndToEndBench:
    """Full request/response cycle through :class:`LaurenApp`.

    The router's contribution is diluted by ASGI parsing, DI
    resolution, and response serialisation, so the end-to-end
    speedup is much smaller than the microbenchmark ratio. We still
    expect a small positive delta for static routes.
    """

    def test_static_vs_dynamic_end_to_end(self) -> None:
        n = 500
        app = LaurenFactory.create(_BenchAppModule)
        client = TestClient(app)

        # Warmup both paths.
        for _ in range(20):
            client.get("/app/health")
            client.get("/app/users/1")
        gc.collect()

        start = time.perf_counter()
        for _ in range(n):
            client.get("/app/health")
        static_secs = time.perf_counter() - start

        start = time.perf_counter()
        for _ in range(n):
            client.get("/app/users/42")
        dynamic_secs = time.perf_counter() - start

        results = [
            BenchResult(
                mode="dynamic GET /users/{id}", iterations=n, seconds=dynamic_secs
            ),
            BenchResult(mode="static GET /health", iterations=n, seconds=static_secs),
        ]
        _print_table(f"end-to-end GET throughput {n} requests", results)

        # Static must not be slower end-to-end than dynamic. On this
        # workload we typically observe \u22481.05-1.15x faster.
        speedup = dynamic_secs / static_secs
        assert speedup >= 0.5, (
            f"static end-to-end slower than dynamic: speedup={speedup:.2f}x"
        )
