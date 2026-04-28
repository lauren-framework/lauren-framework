"""Benchmarks for the RFC 7807 error envelope vs the classic envelope.

The wire shape has measurable differences:

* **default envelope** \u2014 a nested ``{\"error\": {...}}`` dict produced
  by ``err.to_payload()``. Minimal construction cost.
* **rfc7807 envelope** \u2014 a flat 5-field dict plus an optional
  ``errors`` extension and a custom content-type header.

The two encoders must produce comparable throughput; the RFC 7807
path does a handful of extra dict lookups (problem_type, title,
http-status lookup) so it's a touch slower, but not meaningfully so.

The benchmark also compares request-path throughput when most
responses are errors \u2014 a realistic shape for API gateways that
reject malformed input at the edge.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from lauren import LaurenFactory, controller, get, module
from lauren.exceptions import HTTPError
from lauren.testing import TestClient


class _BadRequest(HTTPError):
    status_code = 400
    code = "bad_request"


@controller("/err")
class _ErrController:
    @get("/")
    async def fail(self) -> dict:
        raise _BadRequest("invalid input", detail={"field": "x", "reason": "bad"})


@module(controllers=[_ErrController])
class _ErrModule:
    pass


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
    print(f"{'mode':<22} {'s/req':>10} {'req/sec':>12} {'ratio':>10}")
    print("-" * 58)
    for r in results:
        ratio = r.seconds / baseline
        print(
            f"{r.mode:<22} {r.per_op_us:>10.2f} {r.ops_per_sec:>12,.0f} {ratio:>9.2f}x"
        )
    print()


def _drive(app: object, n: int) -> float:
    client = TestClient(app)
    for _ in range(20):
        client.get("/err/")
    start = time.perf_counter()
    for _ in range(n):
        client.get("/err/")
    return time.perf_counter() - start


class TestRfc7807Bench:
    """Classic envelope vs RFC 7807 Problem Details throughput."""

    def test_error_envelope_throughput(self) -> None:
        n = 500
        app_default = LaurenFactory.create(_ErrModule)
        app_rfc = LaurenFactory.create(_ErrModule, error_format="rfc7807")
        default_secs = _drive(app_default, n)
        rfc_secs = _drive(app_rfc, n)
        results = [
            BenchResult(mode="default envelope", iterations=n, seconds=default_secs),
            BenchResult(mode="rfc7807 envelope", iterations=n, seconds=rfc_secs),
        ]
        _print_table("error envelope dispatch throughput", results)

        # RFC 7807 must not be meaningfully slower. We allow 1.5x
        # overhead to cover noise; in practice we observe near-parity.
        ratio = rfc_secs / default_secs
        assert ratio <= 1.5, f"RFC 7807 path too slow: {ratio:.2f}x vs default envelope"
