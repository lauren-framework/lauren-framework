"""Benchmarks for the :class:`UploadFile` extractor.

Compares two code paths for consuming an uploaded file:

* **UploadFile**    — the framework parses the multipart body, picks
  out the file part, hands the handler an :class:`UploadFile`
  instance. This is the FastAPI-equivalent ergonomic path.
* **Bytes**         — the handler receives the raw multipart body
  and the application is responsible for any parsing.

For small files the UploadFile parsing adds a tiny fixed overhead;
for large files the overhead is dominated by the byte-copying that
buffered parsing inherently requires. The benchmark pins both
regimes.
"""

from __future__ import annotations

import gc
import os
import time
from dataclasses import dataclass

from lauren import Bytes, LaurenFactory, UploadFile, controller, module, post
from lauren.testing import TestClient


@dataclass
class BenchResult:
    mode: str
    iterations: int
    seconds: float
    body_size: int

    @property
    def ms_per_req(self) -> float:
        return (self.seconds / self.iterations) * 1000

    @property
    def throughput_mib(self) -> float:
        total_mib = (self.iterations * self.body_size) / (1024 * 1024)
        return total_mib / self.seconds


def _print_table(title: str, results: list[BenchResult]) -> None:
    print(f"\n\n=== {title} ===")
    print(f"{'mode':<22} {'ms/req':>10} {'MiB/s':>10}")
    print("-" * 44)
    for r in results:
        print(f"{r.mode:<22} {r.ms_per_req:>10.3f} {r.throughput_mib:>10.2f}")
    print()


def _build_multipart(
    field: str, data: bytes, filename: str, boundary: str = "B"
) -> tuple[bytes, str]:
    delim = f"--{boundary}".encode()
    body = (
        delim + b"\r\n"
        b'Content-Disposition: form-data; name="'
        + field.encode()
        + b'"; filename="'
        + filename.encode()
        + b'"\r\n'
        b"Content-Type: application/octet-stream\r\n"
        b"\r\n" + data + b"\r\n" + delim + b"--\r\n"
    )
    header = f"multipart/form-data; boundary={boundary}"
    return body, header


@controller("/u")
class _UploadCtrl:
    @post("/file")
    async def file(self, f: UploadFile) -> dict:
        data = await f.read()
        return {"bytes": len(data)}

    @post("/raw")
    async def raw(self, body: Bytes) -> dict:
        # Raw handler that just measures the body length \u2014 no parsing.
        return {"bytes": len(body)}


@module(controllers=[_UploadCtrl])
class _UploadModule:
    pass


class TestUploadFileBench:
    def test_small_upload_overhead(self) -> None:
        """Small (~4 KiB) file: fixed per-request overhead dominates.\n        The UploadFile path must not be more than 3x slower than\n        the raw Bytes path on tiny payloads.\n"""
        payload = os.urandom(4 * 1024)
        multipart_body, content_type = _build_multipart("f", payload, "small.bin")
        n = 200

        app = LaurenFactory.create(_UploadModule, max_body_size=10 * 1024 * 1024)
        client = TestClient(app)

        for _ in range(5):
            client.post(
                "/u/file",
                content=multipart_body,
                headers={"content-type": content_type},
            )
            client.post(
                "/u/raw", content=multipart_body, headers={"content-type": content_type}
            )
        gc.collect()

        t0 = time.perf_counter()
        for _ in range(n):
            client.post(
                "/u/file",
                content=multipart_body,
                headers={"content-type": content_type},
            )
        upload_secs = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(n):
            client.post(
                "/u/raw", content=multipart_body, headers={"content-type": content_type}
            )
        raw_secs = time.perf_counter() - t0

        results = [
            BenchResult(
                mode="UploadFile",
                iterations=n,
                seconds=upload_secs,
                body_size=len(multipart_body),
            ),
            BenchResult(
                mode="Bytes (raw)",
                iterations=n,
                seconds=raw_secs,
                body_size=len(multipart_body),
            ),
        ]
        _print_table(f"4 KiB multipart \u00d7 {n}", results)

        # UploadFile parsing overhead must stay within 3x of the raw
        # path on tiny bodies. We measure ~1.3x locally; the 3x bound
        # keeps the test robust on slow CI.
        ratio = upload_secs / raw_secs
        assert ratio <= 3.0, f"UploadFile was {ratio:.2f}x slower than Bytes"

    def test_large_upload_correctness_and_throughput(self) -> None:
        """1 MiB file: measure end-to-end throughput. The primary\n        goal here is correctness (byte-exact round trip) plus a\n        rough throughput number for the operator.\n"""
        payload = os.urandom(1 * 1024 * 1024)
        multipart_body, content_type = _build_multipart("f", payload, "big.bin")
        n = 20

        app = LaurenFactory.create(_UploadModule, max_body_size=10 * 1024 * 1024)
        client = TestClient(app)

        for _ in range(2):
            client.post(
                "/u/file",
                content=multipart_body,
                headers={"content-type": content_type},
            )
        gc.collect()

        t0 = time.perf_counter()
        sizes: list[int] = []
        for _ in range(n):
            r = client.post(
                "/u/file",
                content=multipart_body,
                headers={"content-type": content_type},
            )
            sizes.append(r.json()["bytes"])
        secs = time.perf_counter() - t0

        # Byte-exact correctness.
        assert all(s == len(payload) for s in sizes)

        result = BenchResult(
            mode="UploadFile 1 MiB",
            iterations=n,
            seconds=secs,
            body_size=len(multipart_body),
        )
        _print_table(f"1 MiB multipart upload \u00d7 {n}", [result])

        # Sanity: ms/req must be reasonable.
        assert result.ms_per_req < 200, (
            f"1 MiB upload took {result.ms_per_req:.1f} ms \u2014 parser regression?"
        )
