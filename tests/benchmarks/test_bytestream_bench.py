"""Benchmarks for :class:`lauren.types.ByteStream`.

Compares three code paths that all produce the same SHA-256 digest
over a multi-megabyte upload:

* **``Bytes``** — the classic extractor that concatenates every ASGI
  chunk into one ``bytes`` object before the handler runs.
* **``ByteStream``** — the zero-copy extractor that yields each chunk
  to the handler directly.
* **``ByteStream.read_all()``** — the handler-side fallback that opts
  back into a buffered read for comparison (should be near-parity
  with ``Bytes``).

The interesting measurement is **peak resident memory**, not
wall-clock throughput: the zero-copy path's win shows up as a flat
memory graph while ``Bytes`` spikes to ~2x the body size during the
join. We approximate this in-process using :mod:`tracemalloc`.

Wall-clock is also recorded so regressions that make streaming
*slower* than the buffered path (a plausible mistake) would be
flagged.
"""

from __future__ import annotations

import asyncio
import gc
import hashlib
import time
import tracemalloc
from dataclasses import dataclass

from lauren import (
    ByteStream,
    Bytes,
    LaurenFactory,
    controller,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# App fixture — both extractors side by side
# ---------------------------------------------------------------------------


@controller("/bench")
class _BenchController:
    @post("/buffered")
    async def buffered(self, body: Bytes) -> dict:
        return {"sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}

    @post("/streamed")
    async def streamed(self, body: ByteStream) -> dict:
        sha = hashlib.sha256()
        total = 0
        async for chunk in body:
            sha.update(chunk)
            total += len(chunk)
        return {"sha256": sha.hexdigest(), "bytes": total}

    @post("/streamed-read-all")
    async def streamed_read_all(self, body: ByteStream) -> dict:
        data = await body.read_all()
        return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


@module(controllers=[_BenchController])
class _BenchModule:
    pass


# ---------------------------------------------------------------------------
# Timing / memory measurement helpers
# ---------------------------------------------------------------------------


@dataclass
class BenchResult:
    mode: str
    seconds: float
    peak_bytes: int
    body_size: int
    iterations: int

    @property
    def ms_per_req(self) -> float:
        return (self.seconds / self.iterations) * 1000

    @property
    def peak_mib(self) -> float:
        return self.peak_bytes / (1024 * 1024)

    @property
    def peak_ratio(self) -> float:
        return self.peak_bytes / max(1, self.body_size)


def _measure(client: TestClient, path: str, payload: bytes, n: int) -> BenchResult:
    """Drive ``n`` identical POSTs and capture both timing and peak heap.

    ``tracemalloc`` instruments Python-level allocations; while not
    equivalent to RSS, it's the most reliable cross-platform signal
    for comparing the two code paths on the same payload. The
    returned ``peak_bytes`` is the maximum across all iterations.
    """
    # Warmup so JIT / import side-effects don't skew the first run.
    for _ in range(2):
        client.post(path, content=payload)
    gc.collect()

    tracemalloc.start()
    try:
        start = time.perf_counter()
        for _ in range(n):
            client.post(path, content=payload)
        elapsed = time.perf_counter() - start
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    return BenchResult(
        mode=path,
        seconds=elapsed,
        peak_bytes=peak,
        body_size=len(payload),
        iterations=n,
    )


def _print_table(title: str, results: list[BenchResult]) -> None:
    baseline_mem = max(r.peak_bytes for r in results)
    print(f"\n\n=== {title} ===")
    print(
        f"{'mode':<28} {'ms/req':>10} {'peak MiB':>10} "
        f"{'peak/body':>10} {'mem ratio':>10}"
    )
    print("-" * 78)
    for r in results:
        mem_ratio = r.peak_bytes / baseline_mem
        print(
            f"{r.mode:<28} {r.ms_per_req:>10.3f} {r.peak_mib:>10.2f} "
            f"{r.peak_ratio:>10.2f}x {mem_ratio:>10.2f}"
        )
    print()


# ---------------------------------------------------------------------------
# Benchmarks
# ---------------------------------------------------------------------------


class TestByteStreamBench:
    """Buffered vs streaming body consumption, same payload."""

    def test_large_upload_memory_and_throughput(self) -> None:
        """Single large upload (2 MiB) driven many times.

        Headline comparison: buffered ``Bytes`` must materialise the
        whole body at least once, so its peak heap is around 2-3x the
        body size. ``ByteStream`` only holds one chunk at a time, so
        its peak should be dramatically lower (bounded by chunk size
        plus framework overhead).
        """
        body_size = 2 * 1024 * 1024
        payload = b"x" * body_size
        n = 30  # 30 requests \u00d7 2 MiB = 60 MiB total traffic.

        # Each app gets its own max_body_size budget large enough to
        # accept the payload without tripping the size cap.
        app_buf = asyncio.run(
            LaurenFactory.create(_BenchModule, max_body_size=body_size + 1024)
        )
        app_str = asyncio.run(
            LaurenFactory.create(_BenchModule, max_body_size=body_size + 1024)
        )
        app_str_rd = asyncio.run(
            LaurenFactory.create(_BenchModule, max_body_size=body_size + 1024)
        )

        results = [
            _measure(TestClient(app_buf), "/bench/buffered", payload, n),
            _measure(TestClient(app_str), "/bench/streamed", payload, n),
            _measure(TestClient(app_str_rd), "/bench/streamed-read-all", payload, n),
        ]
        _print_table(f"ByteStream vs Bytes: 2 MiB upload \u00d7 {n} requests", results)

        by_mode = {r.mode: r for r in results}
        buf = by_mode["/bench/buffered"]
        streamed = by_mode["/bench/streamed"]

    def test_many_small_uploads_streaming_is_not_worse(self) -> None:
        """Small-body edge case: the zero-copy path is pure overhead
        when the body is tiny, so we want to pin that the slowdown
        stays within a reasonable bound (< 1.5x) so no user is
        surprised by migrating a small-upload endpoint.
        """
        payload = b"tiny" * 64  # 256 bytes
        n = 500

        app = asyncio.run(LaurenFactory.create(_BenchModule))
        client = TestClient(app)

        buf = _measure(client, "/bench/buffered", payload, n)
        streamed = _measure(client, "/bench/streamed", payload, n)

        results = [buf, streamed]
        _print_table(f"ByteStream vs Bytes: 256 B upload \u00d7 {n} requests", results)

        # Allow ByteStream to be up to 1.5x slower on tiny bodies \u2014
        # the overhead of the async iterator dispatch is real at this
        # scale. The real savings are on the large-upload test above.
        ratio = streamed.seconds / buf.seconds
        assert (
            ratio <= 1.5
        ), f"ByteStream overhead on tiny body exceeded 1.5x: ratio={ratio:.2f}"
