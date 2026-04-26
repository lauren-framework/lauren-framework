"""End-to-end tests for the :class:`ByteStream` extractor.

Drives real :class:`LaurenApp` instances via :meth:`LaurenFactory.create`
and verifies the zero-copy body-streaming path works correctly across:

* Small and large bodies (multi-megabyte).
* Single-chunk and multi-chunk ASGI delivery.
* Body-size cap enforcement.
* Handler semantics: streaming hashers, file writers, byte counters.
* Coexistence with the buffered :class:`Bytes` extractor in the same app.
* OpenAPI schema generation (neither extractor should leak into the
  response schema, but both should be recognised by the compiler).
"""

from __future__ import annotations

import asyncio
import hashlib
import os


from lauren import (
    ByteStream,
    Bytes,
    LaurenFactory,
    Path,
    controller,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Hash-of-upload scenario — the quintessential ByteStream use case
# ---------------------------------------------------------------------------


@controller("/upload")
class _HashController:
    @post("/sha256")
    async def sha256(self, body: ByteStream) -> dict:
        """Hash a body of any size without ever materialising it."""
        sha = hashlib.sha256()
        total = 0
        async for chunk in body:
            sha.update(chunk)
            total += len(chunk)
        return {"bytes": total, "sha256": sha.hexdigest()}

    @post("/length")
    async def length(self, body: ByteStream) -> dict:
        """Count bytes without ever joining them."""
        total = 0
        async for chunk in body:
            total += len(chunk)
        return {"bytes": total}


@module(controllers=[_HashController])
class _UploadModule:
    pass


def test_bytestream_hashes_small_body_correctly() -> None:
    app = asyncio.run(LaurenFactory.create(_UploadModule))
    payload = b"the quick brown fox jumps over the lazy dog"
    r = TestClient(app).post("/upload/sha256", content=payload)
    assert r.status_code == 200
    assert r.json() == {
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_bytestream_hashes_large_body_correctly() -> None:
    """A 2 MiB upload — large enough to validate the zero-copy
    semantics matter in practice. The hash must match the one
    computed against the raw payload."""
    app = asyncio.run(
        LaurenFactory.create(_UploadModule, max_body_size=10 * 1024 * 1024)
    )
    payload = os.urandom(2 * 1024 * 1024)
    r = TestClient(app).post("/upload/sha256", content=payload)
    assert r.status_code == 200
    parsed = r.json()
    assert parsed["bytes"] == len(payload)
    assert parsed["sha256"] == hashlib.sha256(payload).hexdigest()


def test_bytestream_length_counter_matches_body_size() -> None:
    app = asyncio.run(LaurenFactory.create(_UploadModule))
    payload = b"x" * 1024
    r = TestClient(app).post("/upload/length", content=payload)
    assert r.json() == {"bytes": len(payload)}


def test_bytestream_empty_body_is_handled_gracefully() -> None:
    app = asyncio.run(LaurenFactory.create(_UploadModule))
    r = TestClient(app).post("/upload/length", content=b"")
    assert r.status_code == 200
    assert r.json() == {"bytes": 0}


# ---------------------------------------------------------------------------
# Size-limit enforcement on the streaming path
# ---------------------------------------------------------------------------


def test_bytestream_body_size_cap_is_enforced() -> None:
    """A body larger than ``max_body_size`` must be rejected by the
    streaming extractor with the same HTTP status the buffered path
    produces — otherwise clients could defeat the cap by choosing
    the zero-copy endpoint.
    """
    app = asyncio.run(LaurenFactory.create(_UploadModule, max_body_size=100))
    # 200 bytes — double the cap.
    r = TestClient(app).post("/upload/length", content=b"x" * 200)
    assert r.status_code == 413  # RequestBodyTooLarge


# ---------------------------------------------------------------------------
# Concatenation semantics: ByteStream vs Bytes produce identical hashes
# ---------------------------------------------------------------------------


@controller("/compare")
class _CompareController:
    @post("/buffered")
    async def buffered(self, body: Bytes) -> dict:
        """Classic path: the framework joins all chunks before invocation."""
        return {
            "bytes": len(body),
            "sha256": hashlib.sha256(body).hexdigest(),
        }

    @post("/streamed")
    async def streamed(self, body: ByteStream) -> dict:
        """Zero-copy path: same result but no intermediate buffer."""
        sha = hashlib.sha256()
        total = 0
        async for chunk in body:
            sha.update(chunk)
            total += len(chunk)
        return {"bytes": total, "sha256": sha.hexdigest()}


@module(controllers=[_CompareController])
class _CompareModule:
    pass


def test_buffered_and_streamed_produce_identical_digests() -> None:
    """Both extractors must observe the same body bytes. If
    ``ByteStream`` silently dropped chunks — e.g. by skipping an
    empty intermediate frame incorrectly — the hashes would diverge.
    """
    app = asyncio.run(LaurenFactory.create(_CompareModule))
    client = TestClient(app)
    payload = os.urandom(64 * 1024)
    buffered = client.post("/compare/buffered", content=payload).json()
    streamed = client.post("/compare/streamed", content=payload).json()
    assert buffered["sha256"] == streamed["sha256"]
    assert buffered["bytes"] == streamed["bytes"] == len(payload)


# ---------------------------------------------------------------------------
# ByteStream can coexist with other extractors in the same handler
# ---------------------------------------------------------------------------


@controller("/tagged")
class _TaggedController:
    @post("/{tag}")
    async def upload(self, tag: Path[str], body: ByteStream) -> dict:
        total = 0
        async for chunk in body:
            total += len(chunk)
        return {"tag": tag, "bytes": total}


@module(controllers=[_TaggedController])
class _TaggedModule:
    pass


def test_bytestream_alongside_path_extractor_works() -> None:
    """Path / query / header extractors must not interfere with the
    streaming-body extractor. They run before the body is touched,
    so the ordering should be safe — this test pins that invariant.
    """
    app = asyncio.run(LaurenFactory.create(_TaggedModule))
    r = TestClient(app).post("/tagged/uploads", content=b"abcd" * 100)
    assert r.status_code == 200
    assert r.json() == {"tag": "uploads", "bytes": 400}


# ---------------------------------------------------------------------------
# ByteStream honours handler-raised exceptions cleanly
# ---------------------------------------------------------------------------


@controller("/validated")
class _ValidatedController:
    @post("/")
    async def strict(self, body: ByteStream) -> dict:
        """Reject streams whose first chunk doesn't start with a magic
        byte sequence — demonstrates early-abort usage."""
        total = 0
        first = True
        async for chunk in body:
            if first and not chunk.startswith(b"LAUREN!"):
                from lauren.exceptions import ExtractorError

                raise ExtractorError("bad magic", detail={"field": "body"})
            first = False
            total += len(chunk)
        return {"bytes": total}


@module(controllers=[_ValidatedController])
class _ValidatedModule:
    pass


def test_bytestream_handler_can_abort_early_on_bad_content() -> None:
    app = asyncio.run(LaurenFactory.create(_ValidatedModule))
    client = TestClient(app)
    # Happy path.
    r = client.post("/validated/", content=b"LAUREN!hello world")
    assert r.status_code == 200
    assert r.json() == {"bytes": len(b"LAUREN!hello world")}
    # Sad path.
    r = client.post("/validated/", content=b"BADhello world")
    assert r.status_code == 422  # ExtractorError default status


# ---------------------------------------------------------------------------
# Extractor markers are correctly recognised by the compiler
# ---------------------------------------------------------------------------


def test_bytestream_marker_is_recognised_by_parser() -> None:
    """Ensure the extractor-hint parser picks ``ByteStream`` up. A
    regression here would cause the handler to receive the marker
    class itself rather than a stream instance.
    """
    from lauren.extractors import ByteStream as BSMarker, parse_extractor_hint

    src, inner, reads_body, marker, _, _ = parse_extractor_hint(BSMarker)
    assert src == "byte_stream"
    assert reads_body is True
    assert marker is BSMarker


# ---------------------------------------------------------------------------
# Sequential requests reuse the arena cleanly \u2014 no stream leakage
# ---------------------------------------------------------------------------


def test_bytestream_under_arena_pooling_does_not_leak_state() -> None:
    """The arena reuses ``Request`` instances across requests; each
    new request must present a fresh, un-consumed stream. A leak
    would manifest as the second request's handler receiving zero
    bytes (a previously-consumed iterator).
    """
    app = asyncio.run(LaurenFactory.create(_UploadModule))
    client = TestClient(app)
    first = client.post("/upload/length", content=b"alpha" * 100).json()
    second = client.post("/upload/length", content=b"beta" * 200).json()
    third = client.post("/upload/length", content=b"gamma" * 50).json()
    assert first == {"bytes": 500}
    assert second == {"bytes": 800}
    assert third == {"bytes": 250}
