"""Body limits on ``Request``: early ``Content-Length`` rejection and the route limit setter (F03 §5.4)."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import controller, get, patch, post
from lauren.exceptions import DecoratorUsageError, RequestBodyTooLarge
from lauren.types import ByteStream, Headers, Request


class CountingReceive:
    """An ASGI ``receive`` that records how often it was awaited."""

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = list(chunks)
        self.calls = 0

    async def __call__(self) -> dict[str, Any]:
        self.calls += 1
        chunk = self.chunks.pop(0) if self.chunks else b""
        return {"type": "http.request", "body": chunk, "more_body": bool(self.chunks)}


def _request(receive: CountingReceive, *, content_length: str | None, limit: int = 10) -> Request:
    headers = Headers([("content-length", content_length)] if content_length is not None else [])
    return Request(method="POST", path="/", headers=headers, receive=receive, max_body_size=limit)


async def _drain_stream(request: Request) -> bytes:
    return b"".join([chunk async for chunk in request.stream()])


async def _drain_byte_stream(request: Request) -> bytes:
    return b"".join([chunk async for chunk in ByteStream(request)])


async def _read_body(request: Request) -> bytes:
    return await request.body()


READERS = [_read_body, _drain_stream, _drain_byte_stream]


@pytest.mark.parametrize("read", READERS)
async def test_oversize_content_length_is_rejected_before_any_receive(read: Any) -> None:
    receive = CountingReceive([b"x" * 50])
    request = _request(receive, content_length="50")

    with pytest.raises(RequestBodyTooLarge) as excinfo:
        await read(request)

    assert receive.calls == 0
    assert excinfo.value.detail == {"max_body_size": 10}


@pytest.mark.parametrize("read", READERS)
@pytest.mark.parametrize("header", ["9" * 5000, "0" * 5000 + "11"])
async def test_very_long_content_length_is_still_413_before_receive(read: Any, header: str) -> None:
    receive = CountingReceive([b"x" * 50])
    request = _request(receive, content_length=header)

    with pytest.raises(RequestBodyTooLarge):
        await read(request)

    assert receive.calls == 0


@pytest.mark.parametrize("read", READERS)
async def test_zero_padded_content_length_stays_within_limit(read: Any) -> None:
    request = _request(CountingReceive([b"a" * 10]), content_length="0" * 5000 + "10")

    assert await read(request) == b"a" * 10


@pytest.mark.parametrize("read", READERS)
@pytest.mark.parametrize("header", [None, "abc", "-5", "1, 1", "５"])
async def test_absent_or_invalid_content_length_falls_back_to_counting(read: Any, header: str | None) -> None:
    receive = CountingReceive([b"x" * 6, b"x" * 6])
    request = _request(receive, content_length=header)

    with pytest.raises(RequestBodyTooLarge):
        await read(request)

    assert receive.calls >= 1


@pytest.mark.parametrize("read", READERS)
async def test_a_body_within_the_limit_is_read(read: Any) -> None:
    receive = CountingReceive([b"abc", b"def"])
    request = _request(receive, content_length="6")

    assert await read(request) == b"abcdef"


async def test_byte_stream_marks_itself_consumed_after_an_early_rejection() -> None:
    stream = ByteStream(_request(CountingReceive([b"x" * 50]), content_length="50"))

    with pytest.raises(RequestBodyTooLarge):
        await stream.__anext__()

    assert stream.consumed
    with pytest.raises(StopAsyncIteration):
        await stream.__anext__()


def test_set_body_limit_replaces_the_limit() -> None:
    request = _request(CountingReceive([]), content_length=None, limit=100)

    request._set_body_limit(5)

    assert request._max_body_size == 5


async def test_set_body_limit_rejects_an_already_buffered_oversize_body() -> None:
    request = _request(CountingReceive([b"x" * 8]), content_length=None, limit=100)
    await request.body()

    with pytest.raises(RequestBodyTooLarge) as excinfo:
        request._set_body_limit(4)

    assert excinfo.value.detail == {"max_body_size": 4}


async def test_a_raised_limit_lets_a_larger_body_through() -> None:
    request = _request(CountingReceive([b"x" * 50]), content_length="50", limit=10)

    request._set_body_limit(100)

    assert len(await request.body()) == 50


# --- decorator validation ---------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, "1MB", 1.5, True])
def test_invalid_max_body_size_is_a_decoration_time_error(bad: Any) -> None:
    with pytest.raises(DecoratorUsageError) as excinfo:
        post("/upload", max_body_size=bad)(lambda: None)

    assert excinfo.value.detail["option"] == "max_body_size"
    assert excinfo.value.detail["reason"] == "invalid_max_body_size"


@pytest.mark.parametrize(
    ("option", "value", "reason"),
    [
        ("consumes", [], "invalid_consumes"),
        ("consumes", "json", "invalid_consumes"),
        ("consumes", ["application/json; charset=utf-8"], "invalid_consumes"),
        ("consumes", {"application/json"}, "invalid_consumes"),
        ("produces", ["application/*"], "invalid_produces"),
        ("produces", ["*/*"], "invalid_produces"),
    ],
)
def test_invalid_media_type_options_are_decoration_time_errors(option: str, value: Any, reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as excinfo:
        patch("/x", **{option: value})(lambda: None)

    assert excinfo.value.detail["reason"] == reason


def test_media_type_options_are_normalised() -> None:
    def handler() -> None: ...

    get("/x", produces="Application/JSON")(handler)
    post("/x", consumes=["application/*+JSON", "application/*+json"], max_body_size=1024)(handler)

    get_meta, post_meta = handler.__lauren_route__  # type: ignore[attr-defined]
    assert get_meta.produces == ("application/json",)
    assert post_meta.consumes == ("application/*+json",)
    assert post_meta.max_body_size == 1024


def test_controller_options_are_validated_too() -> None:
    with pytest.raises(DecoratorUsageError) as excinfo:

        @controller("/c", max_body_size=0)
        class Bad:
            pass

    assert excinfo.value.detail["decorator"] == "controller"
    assert excinfo.value.detail["option"] == "max_body_size"
