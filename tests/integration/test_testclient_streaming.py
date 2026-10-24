"""``client.stream(...)``: incremental reads, SSE parsing, close() and timeouts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from lauren import EventStream, LaurenFactory, ServerSentEvent, StreamingResponse, controller, get, module
from lauren.testing import AsyncTestClient, TestClient


@dataclass
class Tick:
    n: int


def _build(state: dict) -> object:
    @controller("/")
    class FeedController:
        @get("/events")
        async def events(self) -> EventStream:
            async def produce():
                state["finally"] = False
                try:
                    n = 0
                    while True:
                        n += 1
                        yield ServerSentEvent(data=str(n), event="tick", id=str(n))
                        await asyncio.sleep(0)
                finally:
                    state["finally"] = True

            return EventStream(produce(), keep_alive=None)

        @get("/finite")
        async def finite(self) -> EventStream:
            async def produce():
                for n in range(3):
                    yield ServerSentEvent(data=str(n))

            return EventStream(produce(), keep_alive=None)

        @get("/typed")
        async def typed(self) -> StreamingResponse[Tick]:
            async def produce():
                for n in range(3):
                    yield Tick(n=n)

            return produce()  # type: ignore[return-value]

        @get("/silent")
        async def silent(self) -> dict:
            await asyncio.Event().wait()
            return {}  # pragma: no cover

    @module(controllers=[FeedController])
    class FeedModule:
        pass

    return LaurenFactory.create(FeedModule)


def test_endless_sse_stream_is_read_chunk_by_chunk_and_closed() -> None:
    state: dict = {}
    with TestClient(_build(state)) as client:
        with client.stream("GET", "/events") as response:
            assert response.status_code == 200
            assert response.headers.get("content-type", "").startswith("text/event-stream")
            events = response.iter_sse()
            first, second = next(events), next(events)
        assert (first.event, first.data, first.id) == ("tick", "1", "1")
        assert second.data == "2"
        assert response.aborted is True
        assert state["finally"] is True


def test_explicit_close_runs_the_producer_finally() -> None:
    state: dict = {}
    with TestClient(_build(state)) as client:
        with client.stream("GET", "/events") as response:
            next(response.iter_sse())
            response.close()
            assert state["finally"] is True


def test_finite_stream_is_consumed_without_abort() -> None:
    with TestClient(_build({})) as client:
        with client.stream("GET", "/finite") as response:
            data = [e.data for e in response.iter_sse()]
        assert data == ["0", "1", "2"]
        assert response.aborted is False
        assert response.is_stream_consumed


def test_closing_after_completion_is_a_no_op() -> None:
    with TestClient(_build({})) as client:
        with client.stream("GET", "/finite") as response:
            response.read()
            response.close()
            response.close()
        assert response.aborted is False


@pytest.mark.parametrize(
    ("accept", "expected_lines"),
    [("application/x-ndjson", ['{"n": 0}', '{"n": 1}', '{"n": 2}'])],
)
def test_typed_streaming_response_lines(accept: str, expected_lines: list[str]) -> None:
    with TestClient(_build({})) as client:
        with client.stream("GET", "/typed", headers={"accept": accept}) as response:
            lines = [line.replace(" ", "") for line in response.iter_lines() if line]
        assert lines == [e.replace(" ", "") for e in expected_lines]


def test_typed_streaming_response_as_sse() -> None:
    with TestClient(_build({})) as client:
        with client.stream("GET", "/typed", headers={"accept": "text/event-stream"}) as response:
            data = [e.data for e in response.iter_sse() if e.data is not None]
        assert [d.replace(" ", "") for d in data] == ['{"n":0}', '{"n":1}', '{"n":2}']


def test_never_ending_buffered_request_hits_the_timeout() -> None:
    with TestClient(_build({}), timeout=0.2) as client:
        with pytest.raises(TimeoutError, match="GET http://testserver/events"):
            client.get("/events")


def test_stream_that_never_starts_hits_the_timeout() -> None:
    with TestClient(_build({})) as client:
        with pytest.raises(TimeoutError, match="no response"):
            with client.stream("GET", "/silent", timeout=0.1):
                pass  # pragma: no cover


def test_body_chunk_timeout() -> None:
    async def slow_body(scope, receive, send):
        if scope["type"] != "http":
            return
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"first", "more_body": True})
        await asyncio.Event().wait()

    with TestClient(slow_body) as client:
        with client.stream("GET", "/", timeout=0.1) as response:
            chunks = response.iter_bytes()
            assert next(chunks) == b"first"
            with pytest.raises(TimeoutError, match="no body chunk"):
                next(chunks)


def test_sync_stream_refuses_async_iteration() -> None:
    with TestClient(_build({})) as client:
        with client.stream("GET", "/finite") as response:

            async def consume() -> None:
                async for _ in response.aiter_bytes():
                    pass  # pragma: no cover

            with pytest.raises(RuntimeError, match="sync TestClient"):
                asyncio.run(consume())


async def test_async_client_stream_cancellation_disconnects() -> None:
    state: dict = {}
    async with AsyncTestClient(_build(state)) as client:
        async with client.stream("GET", "/events") as response:
            seen = []
            async for event in response.aiter_sse():
                seen.append(event.data)
                if len(seen) == 3:
                    break
        assert seen == ["1", "2", "3"]
        assert state["finally"] is True
        assert response.aborted is True


async def test_async_stream_exit_by_exception_still_disconnects() -> None:
    state: dict = {}
    async with AsyncTestClient(_build(state)) as client:
        with pytest.raises(ZeroDivisionError):
            async with client.stream("GET", "/events") as response:
                await anext(response.aiter_bytes())
                raise ZeroDivisionError
        assert state["finally"] is True


async def test_async_stream_refuses_sync_iteration() -> None:
    async with AsyncTestClient(_build({})) as client:
        async with client.stream("GET", "/finite") as response:
            with pytest.raises(RuntimeError, match="AsyncTestClient"):
                next(response.iter_bytes())
            with pytest.raises(RuntimeError, match="AsyncTestClient"):
                response.close()
            await response.aread()
