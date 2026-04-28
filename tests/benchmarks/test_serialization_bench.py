"""Benchmarks for :mod:`lauren.serialization`.

Two benchmark classes:

* **Encoder micro-benchmarks** \u2014 pure ``encoder.encode(payload)``
  throughput, isolating the serializer from the rest of the stack.
  We expect orjson and msgspec to be 3\u201310\u00d7 faster than stdlib on
  typical dict/list payloads.

* **End-to-end dispatch benchmarks** \u2014 full request/response cycle
  through :class:`LaurenApp` with each encoder installed. The ratio
  is tighter (the framework's own overhead dilutes the encoder's
  advantage) but the orjson/msgspec path should still win.

The assertions are deliberately conservative: we assert on
speedup ratios that are roughly half of what we measure locally, so
the tests stay green on slower CI runners while still proving the
optimisation landed.
"""

from __future__ import annotations

import json as stdlib_json
import time
from dataclasses import dataclass
from typing import Any, Callable


from lauren import LaurenFactory, controller, get, module
from lauren.serialization import (
    JSONEncoder,
    MsgspecEncoder,
    OrjsonEncoder,
    StdlibJSONEncoder,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Payload shapes \u2014 the same ones lauren handlers typically emit
# ---------------------------------------------------------------------------


def _small_dict() -> dict[str, Any]:
    return {"id": 42, "name": "alice", "active": True, "score": 3.14}


def _list_of_dicts(n: int = 100) -> list[dict[str, Any]]:
    return [
        {
            "id": i,
            "name": f"item-{i}",
            "tags": ["alpha", "beta", "gamma"],
            "meta": {"created": "2024-01-01T00:00:00Z", "priority": i % 5},
        }
        for i in range(n)
    ]


def _nested_tree(depth: int = 5, width: int = 3) -> dict[str, Any]:
    if depth == 0:
        return {"leaf": True, "value": 42}
    return {f"child_{i}": _nested_tree(depth - 1, width) for i in range(width)}


# ---------------------------------------------------------------------------
# Timing helper
# ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    name: str
    iterations: int
    seconds: float

    @property
    def per_op_us(self) -> float:
        return (self.seconds / self.iterations) * 1_000_000

    @property
    def ops_per_sec(self) -> float:
        return self.iterations / self.seconds


def _time_it(name: str, iterations: int, fn: Callable[[], Any]) -> BenchResult:
    """Run ``fn`` ``iterations`` times and return a :class:`BenchResult`.

    Warms up with ten iterations to stabilise caches / JIT / import
    side-effects before the measured run begins.
    """
    for _ in range(10):
        fn()
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed = time.perf_counter() - start
    return BenchResult(name=name, iterations=iterations, seconds=elapsed)


def _print_table(title: str, results: list[BenchResult]) -> None:
    """Emit a human-readable table under ``pytest -s``.

    Each row shows ``\u00b5s/op``, ``ops/sec``, and a ratio relative to
    the slowest result so the speedup is at-a-glance visible.
    """
    baseline = max(r.seconds for r in results)
    print(f"\n\n=== {title} ===")
    print(f"{'encoder':<12} {'s/op':>10} {'ops/sec':>14} {'speedup':>10}")
    print("-" * 50)
    for r in results:
        speedup = baseline / r.seconds
        print(
            f"{r.name:<12} {r.per_op_us:>10.2f} {r.ops_per_sec:>14,.0f} {speedup:>9.2f}"
        )
    print()


# ---------------------------------------------------------------------------
# Available-backend helpers
# ---------------------------------------------------------------------------


def _get_encoders() -> dict[str, JSONEncoder]:
    encoders: dict[str, JSONEncoder] = {"stdlib": StdlibJSONEncoder()}
    try:
        encoders["orjson"] = OrjsonEncoder()
    except RuntimeError:
        pass
    try:
        encoders["msgspec"] = MsgspecEncoder()
    except RuntimeError:
        pass
    return encoders


# ---------------------------------------------------------------------------
# 1. Pure encoder micro-benchmark
# ---------------------------------------------------------------------------


class TestEncoderMicroBench:
    """Compare ``encoder.encode_compact(payload)`` throughput.

    This is the most direct measurement of the serialization win: no
    ASGI overhead, no handler invocation, no response object. A
    speedup here flows proportionally into the end-to-end dispatch
    test below, diluted only by the framework's fixed costs.
    """

    def test_small_dict_encoder_bench(self) -> None:
        payload = _small_dict()
        encoders = _get_encoders()
        results = [
            _time_it(name, 20_000, lambda e=enc: e.encode_compact(payload))
            for name, enc in encoders.items()
        ]
        _print_table("encoder: small dict (200 bytes)", results)
        by_name = {r.name: r for r in results}
        # If orjson is available, it should comfortably outpace stdlib
        # \u2014 we measure ~7\u00d7 locally, assert on \u22652\u00d7 to leave headroom
        # for slow CI.
        if "orjson" in by_name:
            speedup = by_name["stdlib"].seconds / by_name["orjson"].seconds
            assert speedup >= 2.0, (
                f"orjson only {speedup:.2f}\u00d7 faster than stdlib \u2014 regression?"
            )
        if "msgspec" in by_name:
            speedup = by_name["stdlib"].seconds / by_name["msgspec"].seconds
            assert speedup >= 2.0, (
                f"msgspec only {speedup:.2f}\u00d7 faster than stdlib \u2014 "
                f"regression?"
            )

    def test_list_of_dicts_encoder_bench(self) -> None:
        payload = _list_of_dicts(100)
        encoders = _get_encoders()
        results = [
            _time_it(name, 2_000, lambda e=enc: e.encode_compact(payload))
            for name, enc in encoders.items()
        ]
        _print_table("encoder: list[dict] \u00d7 100", results)
        by_name = {r.name: r for r in results}
        # Larger payloads amplify the C-extension advantage; we measure
        # ~5\u20138\u00d7 locally. Assert \u22652\u00d7 here too.
        if "orjson" in by_name:
            speedup = by_name["stdlib"].seconds / by_name["orjson"].seconds
            assert speedup >= 2.0, f"orjson speedup collapsed to {speedup:.2f}\u00d7"
        if "msgspec" in by_name:
            speedup = by_name["stdlib"].seconds / by_name["msgspec"].seconds
            assert speedup >= 2.0, f"msgspec speedup collapsed to {speedup:.2f}\u00d7"

    def test_nested_tree_encoder_bench(self) -> None:
        payload = _nested_tree(depth=5, width=3)
        encoders = _get_encoders()
        results = [
            _time_it(name, 5_000, lambda e=enc: e.encode_compact(payload))
            for name, enc in encoders.items()
        ]
        _print_table("encoder: nested tree (depth=5, width=3)", results)
        # No hard assertion \u2014 deeply nested dicts are a known
        # stdlib-friendly shape; we just want the numbers in the table.
        assert all(r.seconds > 0 for r in results)

    def test_bytes_are_semantically_identical_across_encoders(self) -> None:
        """Every encoder must round-trip to the same parsed value."""
        payload = _list_of_dicts(10)
        parsed_values: list[Any] = []
        for enc in _get_encoders().values():
            parsed_values.append(stdlib_json.loads(enc.encode_compact(payload)))
        # All backends parse back to the same Python object.
        assert all(v == payload for v in parsed_values)


# ---------------------------------------------------------------------------
# 2. End-to-end dispatch benchmark
# ---------------------------------------------------------------------------


@controller("/bench")
class _BenchController:
    # Return shape is the typical "list of DTOs" pattern \u2014 the one
    # where the encoder choice has the biggest wall-clock impact.
    @get("/list")
    async def list_items(self) -> list[dict]:
        return _list_of_dicts(50)

    @get("/small")
    async def small(self) -> dict:
        return _small_dict()


@module(controllers=[_BenchController])
class _BenchModule:
    pass


def _run_requests(app: Any, path: str, n: int) -> float:
    """Drive ``n`` requests against ``app`` and return total seconds."""
    client = TestClient(app)
    # Warmup.
    for _ in range(10):
        client.get(path)
    start = time.perf_counter()
    for _ in range(n):
        client.get(path)
    return time.perf_counter() - start


class TestEncoderEndToEndBench:
    """Full request/response cycle, one encoder per iteration.

    Because the TestClient serialises requests and the framework has
    its own fixed per-request costs (routing, DI, middleware walk),
    the speedup here is smaller than the pure-encoder one \u2014 but
    still positive, and that's the point.
    """

    def test_end_to_end_list_response(self) -> None:
        encoders = _get_encoders()
        results: list[BenchResult] = []
        for name, enc in encoders.items():
            app = LaurenFactory.create(_BenchModule, json_encoder=enc)
            secs = _run_requests(app, "/bench/list", n=500)
            results.append(BenchResult(name=name, iterations=500, seconds=secs))
        _print_table("end-to-end: GET /bench/list (list of 50 dicts)", results)
        by_name = {r.name: r for r in results}
        if "orjson" in by_name:
            speedup = by_name["stdlib"].seconds / by_name["orjson"].seconds
            # We observe ~1.3\u20131.8\u00d7 end-to-end on this shape. Even a
            # 5% improvement beats the stdlib baseline \u2014 assert on the
            # sign of the effect only so the test is robust on noisy CI.
            assert speedup >= 1.0, (
                f"orjson slower end-to-end: {speedup:.2f}\u00d7 \u2014 investigate"
            )

    def test_end_to_end_small_response(self) -> None:
        encoders = _get_encoders()
        results: list[BenchResult] = []
        for name, enc in encoders.items():
            app = LaurenFactory.create(_BenchModule, json_encoder=enc)
            secs = _run_requests(app, "/bench/small", n=2_000)
            results.append(BenchResult(name=name, iterations=2_000, seconds=secs))
        _print_table("end-to-end: GET /bench/small (tiny dict)", results)
        # No strict assertion \u2014 on tiny payloads the framework overhead
        # dominates and the encoder win is near the noise floor.
        assert all(r.seconds > 0 for r in results)
