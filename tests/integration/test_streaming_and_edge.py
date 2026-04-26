"""Integration tests for streaming responses, SSE, and edge cases."""

from __future__ import annotations

import asyncio
from typing import AsyncIterator


from lauren import (
    Bytes,
    LaurenFactory,
    Request,
    Response,
    controller,
    get,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Streaming
# ---------------------------------------------------------------------------


async def _chunker() -> AsyncIterator[bytes]:
    for i in range(3):
        yield f"chunk{i}\n".encode()
        await asyncio.sleep(0)  # yield control


async def _events() -> AsyncIterator[dict]:
    for i in range(3):
        yield {"data": {"i": i}, "event": "tick", "id": str(i)}


@controller("/stream")
class StreamController:
    @get("/plain")
    async def plain(self) -> Response:
        return Response.stream(_chunker(), media_type="text/plain")

    @get("/sse")
    async def sse(self) -> Response:
        return Response.sse(_events())


@module(controllers=[StreamController])
class StreamModule:
    pass


class TestStreaming:
    def test_stream_chunks(self):
        app = asyncio.run(LaurenFactory.create(StreamModule))
        client = TestClient(app)
        r = client.get("/stream/plain")
        assert r.status_code == 200
        assert r.text == "chunk0\nchunk1\nchunk2\n"

    def test_sse_format(self):
        app = asyncio.run(LaurenFactory.create(StreamModule))
        client = TestClient(app)
        r = client.get("/stream/sse")
        assert "text/event-stream" in r.header("content-type")
        text = r.text
        assert "event: tick" in text
        assert "id: 0" in text
        assert 'data: {"i":0}' in text


# ---------------------------------------------------------------------------
# Raw bytes body
# ---------------------------------------------------------------------------


@controller("/raw")
class RawController:
    @post("/")
    async def echo(self, body: Bytes) -> Response:
        return Response.bytes(body)


@module(controllers=[RawController])
class RawModule:
    pass


class TestRawBytes:
    def test_raw_echo(self):
        app = asyncio.run(LaurenFactory.create(RawModule))
        client = TestClient(app)
        r = client.post("/raw/", content=b"\x00\x01\x02rawdata")
        assert r.body == b"\x00\x01\x02rawdata"


# ---------------------------------------------------------------------------
# Body size limit
# ---------------------------------------------------------------------------


@controller("/limit")
class LimitController:
    @post("/")
    async def eat(self, request: Request) -> Response:
        data = await request.body()
        return Response.json({"size": len(data)})


@module(controllers=[LimitController])
class LimitModule:
    pass


class TestBodyLimit:
    def test_limit_rejects_large_body(self):
        app = asyncio.run(LaurenFactory.create(LimitModule, max_body_size=16))
        client = TestClient(app)
        r = client.post("/limit/", content=b"x" * 100)
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "request_body_too_large"

    def test_limit_accepts_small_body(self):
        app = asyncio.run(LaurenFactory.create(LimitModule, max_body_size=128))
        client = TestClient(app)
        r = client.post("/limit/", content=b"hi")
        assert r.json() == {"size": 2}


# ---------------------------------------------------------------------------
# Wildcard routes (file paths)
# ---------------------------------------------------------------------------


@controller("/files")
class FileController:
    @get("/{*rest}")
    async def get_file(self, rest):
        # rest is captured via path params; we inject via Request for simplicity
        return {"path": rest}

    @get("/{*rest}", operation_id="doesnt_conflict")  # same route (already used)
    async def dup(self, rest):
        return {"dup": rest}


# The second route conflicts — replace with a second module
@controller("/files2")
class FileController2:
    @get("/static/{*rest}")
    async def get_static(self, rest: str) -> dict:
        return {"path": rest}


@module(controllers=[FileController2])
class FilesModule:
    pass


class TestWildcards:
    def test_wildcard_captures_full_path(self):
        app = asyncio.run(LaurenFactory.create(FilesModule))
        client = TestClient(app)
        r = client.get("/files2/static/a/b/c.txt")
        assert r.json() == {"path": "a/b/c.txt"}
