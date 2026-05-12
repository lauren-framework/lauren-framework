"""Benchmarks for the :class:`UploadFile` extractor.

Compares two scenarios:

* **single small upload** \u2014 a 4 KiB file uploaded many times. Covers
  the dispatch-overhead case (parsing cost per request is low, so
  throughput is bounded by framework overhead).
* **large single upload** \u2014 one 2 MiB file uploaded a moderate number
  of times. Surfaces parser throughput rather than dispatch cost.

The parser is O(body_size) with a small constant, so we expect
throughput to scale smoothly with payload size. The benchmark also
confirms that the SHA-256 of every received file matches the
sha-of-the-bytes-we-sent \u2014 a regression in the CRLF scanner would
silently corrupt data.
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass

from lauren import LaurenFactory, UploadFile, controller, module, post
from lauren.testing import TestClient


@controller("/bench")
class _BenchController:
    @post("/single")
    async def single(self, file: UploadFile) -> dict:
        data = await file.read()
        return {"size": len(data), "sha": hashlib.sha256(data).hexdigest()}


@module(controllers=[_BenchController])
class _BenchModule:
    pass


def _multipart_body(
    payload: bytes,
    filename: str = "x.bin",
    boundary: str = "----Bench",
) -> tuple[bytes, str]:
    delim = f"--{boundary}".encode()
    body = b"\r\n".join(
        [
            delim,
            f'Content-Disposition: form-data; name="file"; filename="{filename}"'.encode(),
            b"Content-Type: application/octet-stream",
            b"",
            payload,
            f"--{boundary}--".encode(),
            b"",
        ]
    )
    return body, f"multipart/form-data; boundary={boundary}"


@dataclass
class BenchResult:
    mode: str
    iterations: int
    seconds: float
    payload_size: int

    @property
    def per_op_ms(self) -> float:
        return (self.seconds / self.iterations) * 1000

    @property
    def throughput_mib_s(self) -> float:
        return (self.iterations * self.payload_size) / (self.seconds * 1024 * 1024)


def _print_table(title: str, results: list[BenchResult]) -> None:
    print(f"\n\n=== {title} ===")
    print(f"{'mode':<26} {'ms/req':>10} {'req/sec':>12} {'MiB/sec':>10}")
    print("-" * 62)
    for r in results:
        rps = r.iterations / r.seconds
        print(f"{r.mode:<26} {r.per_op_ms:>10.3f} {rps:>12,.0f} {r.throughput_mib_s:>10.2f}")
    print()


class TestUploadFileBench:
    """Dispatch throughput and parser throughput for UploadFile."""

    def test_small_upload_dispatch_throughput(self) -> None:
        """4 KiB payload \u00d7 200 requests \u2014 measures per-request dispatch cost."""
        payload = os.urandom(4 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        body, ct = _multipart_body(payload)
        n = 200

        app = LaurenFactory.create(_BenchModule)
        client = TestClient(app)
        for _ in range(10):
            client.post("/bench/single", content=body, headers={"content-type": ct})
        start = time.perf_counter()
        for _ in range(n):
            r = client.post("/bench/single", content=body, headers={"content-type": ct})
            assert r.status_code == 200
            # Sanity: bytes survived byte-for-byte.
            assert r.json()["sha"] == expected_sha
        secs = time.perf_counter() - start
        results = [
            BenchResult(
                mode="4 KiB \u00d7 200 req",
                iterations=n,
                seconds=secs,
                payload_size=len(payload),
            )
        ]
        _print_table("UploadFile dispatch (small files)", results)
        assert secs > 0

    def test_large_upload_parser_throughput(self) -> None:
        """2 MiB payload \u00d7 20 requests \u2014 measures parser throughput."""
        payload = os.urandom(2 * 1024 * 1024)
        expected_sha = hashlib.sha256(payload).hexdigest()
        body, ct = _multipart_body(payload)
        n = 20

        app = LaurenFactory.create(_BenchModule, max_body_size=len(body) + 4096)
        client = TestClient(app)
        for _ in range(2):
            client.post("/bench/single", content=body, headers={"content-type": ct})
        start = time.perf_counter()
        for _ in range(n):
            r = client.post("/bench/single", content=body, headers={"content-type": ct})
            assert r.status_code == 200
            assert r.json()["sha"] == expected_sha
        secs = time.perf_counter() - start
        results = [
            BenchResult(
                mode="2 MiB \u00d7 20 req",
                iterations=n,
                seconds=secs,
                payload_size=len(payload),
            )
        ]
        _print_table("UploadFile parser (large files)", results)
        # Throughput must clear some minimal threshold \u2014 10 MiB/s is
        # trivial for a pure-Python linear scanner on modern hardware
        # and gives plenty of headroom for noisy CI.
        assert results[0].throughput_mib_s > 10.0, (
            f"parser throughput collapsed to {results[0].throughput_mib_s:.2f} MiB/s"
        )
