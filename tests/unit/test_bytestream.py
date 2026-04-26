"""Unit tests for :class:`lauren.types.ByteStream`.

The zero-copy body extractor is tested in isolation (no ASGI app, no
dispatcher) by constructing a :class:`lauren.types.Request` directly
with a synthetic ``receive`` callable that replays a prepared
sequence of ASGI messages. This lets us exercise every edge case —
multi-chunk bodies, disconnects, oversize rejection, double
iteration, empty frames — without the overhead of a full app build.
"""

from __future__ import annotations

from typing import Any, Iterator

import pytest

from lauren.exceptions import RequestBodyTooLarge
from lauren.types import AppState, ByteStream, ClientInfo, Headers, Request, ServerInfo


# ---------------------------------------------------------------------------
# Synthetic ASGI receive helpers
# ---------------------------------------------------------------------------


def _make_receive(messages: list[dict[str, Any]]) -> Any:
    """Return an async callable that yields ``messages`` in order.

    Models the ASGI ``receive`` contract. If the caller drains the
    list the callable returns a terminal ``http.disconnect`` message
    — matching how real ASGI servers behave when the client closed.
    """
    iterator: Iterator[dict[str, Any]] = iter(messages)

    async def receive() -> dict[str, Any]:
        try:
            return next(iterator)
        except StopIteration:
            return {"type": "http.disconnect"}

    return receive


def _build_request(
    messages: list[dict[str, Any]],
    *,
    max_body_size: int = 10 * 1024 * 1024,
) -> Request:
    """Construct a bare :class:`Request` wired to a synthetic receive."""
    return Request(
        method="POST",
        path="/upload",
        headers=Headers(),
        client=ClientInfo(None, None),
        server=ServerInfo(None, None),
        receive=_make_receive(messages),
        app_state=AppState(),
        max_body_size=max_body_size,
    )


# ---------------------------------------------------------------------------
# Single-chunk and multi-chunk iteration
# ---------------------------------------------------------------------------


async def test_single_chunk_body_yields_one_item() -> None:
    req = _build_request(
        [
            {"type": "http.request", "body": b"hello world", "more_body": False},
        ]
    )
    stream = ByteStream(req)
    out: list[bytes] = []
    async for chunk in stream:
        out.append(chunk)
    assert out == [b"hello world"]
    assert stream.consumed is True


async def test_multi_chunk_body_preserves_chunk_boundaries() -> None:
    """Zero-copy iteration yields each ASGI chunk exactly as received.

    A concatenation-based extractor would collapse all five chunks
    into one ``bytes`` object; ``ByteStream`` must preserve the
    boundaries so streaming hashers and file writers see each chunk
    as soon as it arrives.
    """
    chunks = [b"alpha-", b"beta-", b"gamma-", b"delta-", b"omega"]
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    req = _build_request(messages)
    stream = ByteStream(req)
    received: list[bytes] = []
    async for chunk in stream:
        received.append(chunk)
    assert received == chunks


async def test_concatenated_chunks_match_full_body() -> None:
    """Semantic equivalence to ``Request.body()`` — the joined bytes
    must equal the body a buffered read would produce."""
    chunks = [b"line-" + bytes(str(i), "ascii") + b"\n" for i in range(200)]
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    req = _build_request(messages)
    stream = ByteStream(req)
    joined = b"".join([c async for c in stream])
    assert joined == b"".join(chunks)


# ---------------------------------------------------------------------------
# Edge cases: empty bodies, empty intermediate chunks, disconnects
# ---------------------------------------------------------------------------


async def test_empty_body_yields_nothing() -> None:
    req = _build_request(
        [
            {"type": "http.request", "body": b"", "more_body": False},
        ]
    )
    stream = ByteStream(req)
    chunks = [c async for c in stream]
    assert chunks == []
    assert stream.consumed is True


async def test_empty_intermediate_chunk_is_skipped() -> None:
    """ASGI spec allows zero-length intermediate chunks. Those are
    informational; the stream should keep reading rather than
    surface them to the handler (who'd have no way to distinguish
    them from the terminal ``b''``)."""
    req = _build_request(
        [
            {"type": "http.request", "body": b"head", "more_body": True},
            {"type": "http.request", "body": b"", "more_body": True},
            {"type": "http.request", "body": b"tail", "more_body": False},
        ]
    )
    stream = ByteStream(req)
    chunks = [c async for c in stream]
    assert chunks == [b"head", b"tail"]


async def test_http_disconnect_mid_stream_terminates_cleanly() -> None:
    req = _build_request(
        [
            {"type": "http.request", "body": b"partial-", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    stream = ByteStream(req)
    chunks = [c async for c in stream]
    # We get the pre-disconnect chunk then a clean StopAsyncIteration.
    assert chunks == [b"partial-"]
    assert stream.consumed is True


# ---------------------------------------------------------------------------
# Size-limit enforcement
# ---------------------------------------------------------------------------


async def test_exceeding_max_body_size_raises_request_body_too_large() -> None:
    """The ``max_body_size`` cap must be honoured across the stream
    exactly the way :meth:`Request.body` honours it — otherwise a
    client could defeat the limit by uploading many small chunks."""
    req = _build_request(
        [
            {"type": "http.request", "body": b"a" * 60, "more_body": True},
            {"type": "http.request", "body": b"b" * 60, "more_body": False},
        ],
        max_body_size=100,
    )
    stream = ByteStream(req)
    with pytest.raises(RequestBodyTooLarge):
        async for _ in stream:
            pass
    assert stream.consumed is True


async def test_max_body_size_accumulates_across_chunks() -> None:
    """A chunk that would cross the limit causes the iterator to
    raise on *that* chunk — the prior chunks may have already been
    yielded but no further chunks must be delivered."""
    req = _build_request(
        [
            {"type": "http.request", "body": b"x" * 40, "more_body": True},
            {"type": "http.request", "body": b"y" * 40, "more_body": True},
            {"type": "http.request", "body": b"z" * 40, "more_body": False},
        ],
        max_body_size=100,
    )
    stream = ByteStream(req)
    received: list[bytes] = []
    with pytest.raises(RequestBodyTooLarge):
        async for chunk in stream:
            received.append(chunk)
    # First two chunks (80 bytes) pass; the third pushes past 100.
    assert received == [b"x" * 40, b"y" * 40]


async def test_body_at_exact_limit_succeeds() -> None:
    req = _build_request(
        [{"type": "http.request", "body": b"x" * 100, "more_body": False}],
        max_body_size=100,
    )
    stream = ByteStream(req)
    chunks = [c async for c in stream]
    assert chunks == [b"x" * 100]


# ---------------------------------------------------------------------------
# Double-iteration protection
# ---------------------------------------------------------------------------


async def test_double_iteration_yields_no_extra_chunks() -> None:
    """After a ``ByteStream`` is drained, iterating it again must not
    silently replay nor attempt to read from an already-exhausted
    ``receive`` — either behaviour would produce subtly wrong
    results. We require a clean ``StopAsyncIteration``.
    """
    req = _build_request(
        [
            {"type": "http.request", "body": b"once", "more_body": False},
        ]
    )
    stream = ByteStream(req)
    first = [c async for c in stream]
    second = [c async for c in stream]
    assert first == [b"once"]
    assert second == []


# ---------------------------------------------------------------------------
# Interop with buffered Request.body() \u2014 middleware ordering
# ---------------------------------------------------------------------------


async def test_falls_back_to_buffered_body_when_middleware_buffered_first() -> None:
    """If a middleware called ``await request.body()`` before the
    handler ran, the body is already in memory. ``ByteStream`` then
    yields that single buffered chunk rather than calling
    ``receive`` (which would return disconnect). This preserves
    correctness at the cost of the zero-copy property.
    """
    req = _build_request(
        [
            {
                "type": "http.request",
                "body": b"buffered-by-middleware",
                "more_body": False,
            },
        ]
    )
    body = await req.body()
    assert body == b"buffered-by-middleware"

    stream = ByteStream(req)
    chunks = [c async for c in stream]
    assert chunks == [b"buffered-by-middleware"]


async def test_buffered_empty_body_yields_no_chunks() -> None:
    req = _build_request(
        [
            {"type": "http.request", "body": b"", "more_body": False},
        ]
    )
    _ = await req.body()
    stream = ByteStream(req)
    chunks = [c async for c in stream]
    assert chunks == []


# ---------------------------------------------------------------------------
# read_all convenience helper
# ---------------------------------------------------------------------------


async def test_read_all_returns_concatenated_body() -> None:
    chunks = [b"one-", b"two-", b"three"]
    messages = [
        {"type": "http.request", "body": c, "more_body": i < len(chunks) - 1}
        for i, c in enumerate(chunks)
    ]
    req = _build_request(messages)
    stream = ByteStream(req)
    assert await stream.read_all() == b"one-two-three"


async def test_read_all_honours_max_body_size() -> None:
    req = _build_request(
        [{"type": "http.request", "body": b"x" * 200, "more_body": False}],
        max_body_size=100,
    )
    stream = ByteStream(req)
    with pytest.raises(RequestBodyTooLarge):
        await stream.read_all()


# ---------------------------------------------------------------------------
# __aiter__ returns self so manual driving is supported
# ---------------------------------------------------------------------------


async def test_aiter_returns_same_instance() -> None:
    """``__aiter__`` must return the same object so the user can drive
    iteration manually (e.g. ``it = stream.__aiter__(); await
    it.__anext__()``)."""
    req = _build_request(
        [
            {"type": "http.request", "body": b"data", "more_body": False},
        ]
    )
    stream = ByteStream(req)
    assert stream.__aiter__() is stream


async def test_manual_anext_drives_iteration() -> None:
    req = _build_request(
        [
            {"type": "http.request", "body": b"a", "more_body": True},
            {"type": "http.request", "body": b"b", "more_body": False},
        ]
    )
    stream = ByteStream(req)
    assert await stream.__anext__() == b"a"
    assert await stream.__anext__() == b"b"
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()
