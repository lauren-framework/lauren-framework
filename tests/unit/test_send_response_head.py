"""``_send_response(head=True)``: HEAD responses carry headers only."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import pytest

from lauren._asgi import _send_response
from lauren.types import Headers, Response


class _Recorder:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    async def __call__(self, message: dict[str, Any]) -> None:
        self.messages.append(message)

    def headers(self) -> dict[bytes, bytes]:
        return dict(self.messages[0]["headers"])

    def body(self) -> bytes:
        return b"".join(m.get("body", b"") for m in self.messages[1:])


class _TrackedStream:
    """An async iterator recording how far it was consumed."""

    def __init__(self) -> None:
        self.next_calls = 0
        self.closed = False

    def __aiter__(self) -> "_TrackedStream":
        return self

    async def __anext__(self) -> bytes:
        self.next_calls += 1
        if self.next_calls > 2:
            raise StopAsyncIteration
        return b"chunk"

    async def aclose(self) -> None:
        self.closed = True


async def test_buffered_head_keeps_content_length_and_sends_no_body() -> None:
    send = _Recorder()

    await _send_response(Response(b"hello world", status=200), send, head=True)

    assert send.headers()[b"content-length"] == b"11"
    assert send.body() == b""
    assert send.messages[-1]["more_body"] is False


async def test_buffered_get_sends_the_body() -> None:
    send = _Recorder()

    await _send_response(Response(b"hello"), send)

    assert send.body() == b"hello"


async def test_streaming_head_never_iterates_and_closes_the_stream() -> None:
    stream = _TrackedStream()
    send = _Recorder()

    await _send_response(Response.stream(stream, media_type="text/plain"), send, head=True)

    assert stream.next_calls == 0
    assert stream.closed is True
    assert b"content-length" not in send.headers()
    assert send.messages[1:] == [{"type": "http.response.body", "body": b"", "more_body": False}]


async def test_streaming_head_keeps_a_content_length_the_response_set() -> None:
    send = _Recorder()
    response = Response.stream(_TrackedStream(), headers=Headers([("content-length", "10")]))

    await _send_response(response, send, head=True)

    assert send.headers()[b"content-length"] == b"10"


async def test_streaming_head_without_aclose_is_left_alone() -> None:
    class _Iterable:
        def __aiter__(self) -> AsyncIterator[bytes]:  # pragma: no cover - never called
            raise AssertionError("HEAD must not iterate the body")

    send = _Recorder()

    await _send_response(Response.stream(_Iterable()), send, head=True)

    assert send.body() == b""


async def test_streaming_head_logs_a_failing_aclose(caplog: pytest.LogCaptureFixture) -> None:
    class _Broken(_TrackedStream):
        async def aclose(self) -> None:
            raise RuntimeError("boom")

    send = _Recorder()

    with caplog.at_level(logging.WARNING, logger="lauren"):
        await _send_response(Response.stream(_Broken()), send, head=True)

    assert send.messages[-1]["more_body"] is False
    assert "HEAD" in caplog.text


async def test_head_strips_an_error_envelope_body() -> None:
    send = _Recorder()
    envelope = Response.json({"error": {"code": "route_not_found"}}, status=404)

    await _send_response(envelope, send, head=True)

    assert send.messages[0]["status"] == 404
    assert int(send.headers()[b"content-length"]) > 0
    assert send.body() == b""
