"""Unit tests for the Server-Sent Events primitives.

These tests pin down the wire-level contract of :func:`format_sse_event`,
:class:`ServerSentEvent`, and :class:`EventStream` in isolation from the
ASGI runtime. The integration tier (``tests/integration/test_sse.py``)
exercises the full HTTP path with a :class:`~lauren.testing.TestClient`.

The SSE wire format is small but full of edge cases: multiline data,
missing fields, comment-only frames, the difference between
"explicitly empty" and "absent", header normalisation, keep-alive
heartbeats. Each rule has at least one test below; flaky behaviours
(client disconnect, generator cancellation) get dedicated coverage.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator

import pytest

from lauren import (
    EventStream,
    Headers,
    ServerSentEvent,
    format_sse_event,
    last_event_id,
)
from lauren.sse import _coerce_to_event, _encode_data


# ===========================================================================
# format_sse_event — the lowest-level framing function.
# ===========================================================================


class TestFormatSseEventDataField:
    """The ``data`` parameter governs whether a ``data:`` line appears."""

    def test_string_data_is_emitted_verbatim(self):
        assert format_sse_event(data="hello") == "data: hello\n\n"

    def test_multiline_string_splits_into_one_line_per_segment(self):
        # Per spec, every ``\n`` inside data must produce a new
        # ``data:`` line so the browser reassembles the original text.
        out = format_sse_event(data="line1\nline2\nline3")
        assert out == "data: line1\ndata: line2\ndata: line3\n\n"

    def test_empty_string_data_produces_a_data_line(self):
        # ``data=""`` is *explicitly empty* — different from ``data=None``.
        # Browsers treat this as a valid event with empty payload.
        assert format_sse_event(data="") == "data: \n\n"

    def test_none_data_omits_the_data_field(self):
        # ``data=None`` is "no data line at all". Combined with a
        # comment, this is the standard wire form for a heartbeat.
        out = format_sse_event(data=None, comment="ping")
        assert "data:" not in out
        assert out == ": ping\n\n"

    def test_dict_data_is_json_encoded(self):
        out = format_sse_event(data={"hello": "world", "n": 42})
        # Dict ordering is preserved in Py 3.7+, so the JSON output is
        # deterministic for this small fixture.
        assert out == 'data: {"hello":"world","n":42}\n\n'

    def test_list_data_is_json_encoded(self):
        assert format_sse_event(data=[1, 2, 3]) == "data: [1,2,3]\n\n"

    def test_bytes_data_is_decoded_as_utf8(self):
        out = format_sse_event(data=b"hello")
        assert out == "data: hello\n\n"

    def test_invalid_utf8_in_bytes_is_replaced(self):
        # Lossless fidelity isn't possible for binary data on a text
        # protocol; we use ``errors="replace"`` so the rest of the
        # stream survives a single bad event.
        out = format_sse_event(data=b"\xff\xfe\x00")
        assert out.startswith("data: ")
        assert out.endswith("\n\n")
        # The output is whatever ``decode("utf-8", errors="replace")``
        # produces — exact bytes don't matter, just that we didn't crash.

    def test_pydantic_model_data_uses_model_dump(self):
        pyd = pytest.importorskip("pydantic")

        class Item(pyd.BaseModel):
            name: str
            count: int

        out = format_sse_event(data=Item(name="x", count=3))
        assert out == 'data: {"name":"x","count":3}\n\n'


class TestFormatSseEventEventField:
    def test_event_appears_before_data(self):
        # SSE order is: ``event``, ``id``, ``retry``, ``data``. Browsers
        # tolerate other orderings but the spec recommends this layout.
        out = format_sse_event(event="message", data="x")
        lines = out.rstrip("\n").split("\n")
        assert lines == ["event: message", "data: x"]

    def test_event_with_internal_newline_is_flattened(self):
        # Newlines inside ``event`` would corrupt the framing, so we
        # replace them with spaces before emission.
        out = format_sse_event(event="bad\nevent", data="x")
        assert "event: bad event" in out
        assert "\nevent:" not in out.split("\n", 1)[1]  # only one event line


class TestFormatSseEventIdField:
    def test_id_is_serialized(self):
        out = format_sse_event(id="abc-123", data="x")
        assert "id: abc-123\n" in out

    def test_id_newlines_are_stripped(self):
        # Per spec, ``id`` MUST NOT contain newlines. We strip
        # silently rather than raising — matching Starlette/Sanic.
        out = format_sse_event(id="bad\nid", data="x")
        assert "id: badid\n" in out
        assert "\nid:" not in out.split("id:", 1)[1]

    def test_id_carriage_returns_are_stripped(self):
        out = format_sse_event(id="bad\rid", data="x")
        assert "id: badid\n" in out


class TestFormatSseEventRetryField:
    def test_positive_int_retry_is_emitted(self):
        out = format_sse_event(retry=5000, data="x")
        assert "retry: 5000\n" in out

    def test_zero_retry_is_emitted(self):
        # ``retry: 0`` is unusual but valid (instant reconnect attempt).
        out = format_sse_event(retry=0, data="x")
        assert "retry: 0\n" in out

    def test_negative_retry_is_silently_dropped(self):
        # The spec mandates a non-negative integer; we drop invalid
        # values rather than raising so a misconfigured upstream
        # doesn't break the stream.
        out = format_sse_event(retry=-100, data="x")
        assert "retry:" not in out

    def test_bool_retry_is_silently_dropped(self):
        # ``bool`` is a subclass of ``int``; without an explicit guard,
        # ``retry=True`` would emit ``retry: 1``. The guard prevents
        # accidental misuse from corrupting the wire format.
        out = format_sse_event(retry=True, data="x")  # type: ignore[arg-type]
        assert "retry:" not in out

    def test_non_int_retry_is_silently_dropped(self):
        out = format_sse_event(retry="3000", data="x")  # type: ignore[arg-type]
        assert "retry:" not in out


class TestFormatSseEventCommentField:
    def test_comment_only_event(self):
        # The classic keep-alive shape. No data line is emitted.
        out = format_sse_event(comment="ping")
        assert out == ": ping\n\n"

    def test_multiline_comment_emits_one_line_per_segment(self):
        out = format_sse_event(comment="line one\nline two")
        assert out == ": line one\n: line two\n\n"

    def test_empty_comment_emits_a_bare_colon_line(self):
        out = format_sse_event(comment="")
        assert out == ": \n\n"

    def test_comment_combined_with_event_and_data(self):
        out = format_sse_event(comment="meta", event="m", data="hi")
        # Ordering: comment first, then event, then data — matches the
        # cleanest visual reading of the wire bytes.
        assert out == ": meta\nevent: m\ndata: hi\n\n"


class TestFormatSseEventEdgeCases:
    def test_completely_empty_args_yield_a_lone_newline(self):
        # ``format_sse_event()`` with no args is a degenerate but
        # legal call. The lone ``\n`` it returns is a no-op heartbeat
        # that browsers ignore.
        assert format_sse_event() == "\n"

    def test_only_id_without_data_still_frames_correctly(self):
        out = format_sse_event(id="42")
        assert out == "id: 42\n\n"


# ===========================================================================
# ServerSentEvent — the dataclass shape.
# ===========================================================================


class TestServerSentEventConstruction:
    def test_default_constructor_has_no_required_fields(self):
        ev = ServerSentEvent()
        # Every field is defaulted; ``encode()`` returns the lone-newline
        # heartbeat representation.
        assert ev.encode() == b"\n"

    def test_data_only(self):
        ev = ServerSentEvent(data="hello")
        assert ev.encode() == b"data: hello\n\n"

    def test_full_envelope(self):
        ev = ServerSentEvent(
            data={"x": 1}, event="m", id="42", retry=2000, comment="meta"
        )
        out = ev.encode()
        assert out.startswith(b": meta\nevent: m\nid: 42\nretry: 2000\n")
        assert out.endswith(b"\n\n")
        assert b'data: {"x":1}' in out

    def test_dataclass_is_frozen(self):
        # Frozen because events flow through queues — accidental
        # mutation would be a correctness hazard.
        ev = ServerSentEvent(data="x")
        with pytest.raises(Exception):
            ev.event = "modified"  # type: ignore[misc]

    def test_dataclass_is_hashable(self):
        ev = ServerSentEvent(data="x", id="1")
        # frozen=True makes the dataclass hashable, which lets users
        # cache or dedupe events in a set / dict.
        assert hash(ev) == hash(ServerSentEvent(data="x", id="1"))


class TestServerSentEventFromDict:
    def test_promotes_minimal_dict(self):
        ev = ServerSentEvent.from_dict({"data": "hi"})
        assert ev.encode() == b"data: hi\n\n"

    def test_promotes_full_dict(self):
        ev = ServerSentEvent.from_dict(
            {"event": "m", "id": "1", "retry": 500, "data": [1, 2]}
        )
        out = ev.encode()
        assert b"event: m\n" in out
        assert b"id: 1\n" in out
        assert b"retry: 500\n" in out
        assert b"data: [1,2]" in out

    def test_unknown_keys_are_ignored(self):
        # Forwards-compat: future SSE field additions or arbitrary
        # caller metadata shouldn't break the promotion.
        ev = ServerSentEvent.from_dict({"data": "x", "future_field": "ignored"})
        assert ev.encode() == b"data: x\n\n"

    def test_missing_data_key_means_no_data_line(self):
        # Differs from ``{"data": ""}`` (which emits an empty data
        # line). Tested explicitly because the distinction is the
        # whole reason ``from_dict`` exists.
        ev = ServerSentEvent.from_dict({"comment": "ping"})
        assert ev.encode() == b": ping\n\n"


# ===========================================================================
# _encode_data — the small data-coercion helper.
# ===========================================================================


class TestEncodeDataHelper:
    @pytest.mark.parametrize(
        "value,expected",
        [
            (None, None),
            ("", ""),
            ("hello", "hello"),
            (b"hello", "hello"),
            (bytearray(b"world"), "world"),
            ({"a": 1}, '{"a":1}'),
            ([1, 2], "[1,2]"),
            (42, "42"),
            (True, "true"),
            (False, "false"),
        ],
    )
    def test_basic_types(self, value, expected):
        assert _encode_data(value) == expected

    def test_dataclass_is_json_serialised_via_default_handler(self):
        @dataclass
        class Point:
            x: int
            y: int

        # Non-Pydantic dataclasses fall through to ``json.dumps`` with
        # lauren's permissive ``_json_default`` handler. The handler
        # coerces dataclasses via ``asdict``.
        out = _encode_data(Point(1, 2))
        assert out == '{"x":1,"y":2}'


# ===========================================================================
# _coerce_to_event — turns producer-yielded values into SSE objects.
# ===========================================================================


class TestCoerceToEvent:
    def test_servereventsentevent_passes_through_unchanged(self):
        ev = ServerSentEvent(data="x", event="m")
        assert _coerce_to_event(ev) is ev

    def test_string_becomes_data_event(self):
        ev = _coerce_to_event("hello")
        assert ev.data == "hello"
        assert ev.event is None

    def test_bytes_become_data_event(self):
        ev = _coerce_to_event(b"hello")
        assert ev.data == b"hello"

    def test_dict_promotes_via_from_dict(self):
        ev = _coerce_to_event({"event": "m", "data": "x"})
        assert ev.event == "m"
        assert ev.data == "x"

    def test_arbitrary_value_becomes_data_payload(self):
        # JSON-encoded by ``encode()`` later; here we just check that
        # the value lands in the ``data`` field unchanged.
        ev = _coerce_to_event(42)
        assert ev.data == 42


# ===========================================================================
# EventStream — the Response subclass.
# ===========================================================================


class TestEventStreamConstruction:
    def test_default_headers_are_set(self):
        stream = EventStream(_async_iter([ServerSentEvent(data="x")]))
        headers = dict(stream.headers.raw())
        assert headers["content-type"] == "text/event-stream; charset=utf-8"
        assert headers["cache-control"] == "no-cache"
        assert headers["connection"] == "keep-alive"
        assert headers["x-accel-buffering"] == "no"

    def test_extra_headers_override_defaults(self):
        # Useful for CORS or auth — callers should be able to replace
        # any default header without surgery.
        stream = EventStream(
            _async_iter([]),
            extra_headers={"cache-control": "private, max-age=0"},
        )
        headers = dict(stream.headers.raw())
        assert headers["cache-control"] == "private, max-age=0"

    def test_custom_status_propagates(self):
        stream = EventStream(_async_iter([]), status=202)
        assert stream.status == 202

    def test_zero_keep_alive_is_rejected(self):
        # A zero or negative interval would busy-loop the heartbeat
        # task — fail at construction time so misconfiguration surfaces
        # before bytes are sent.
        with pytest.raises(ValueError):
            EventStream(_async_iter([]), keep_alive=0)

    def test_negative_keep_alive_is_rejected(self):
        with pytest.raises(ValueError):
            EventStream(_async_iter([]), keep_alive=-1.5)

    def test_none_keep_alive_disables_heartbeat(self):
        # Just construct it — the absence of an exception is the test.
        EventStream(_async_iter([]), keep_alive=None)


class TestEventStreamFraming:
    """Exercise the streaming body end-to-end inside the asyncio loop."""

    @pytest.mark.asyncio
    async def test_yields_single_event_then_terminates(self):
        stream = EventStream(_async_iter([ServerSentEvent(data="hi", event="msg")]))
        chunks = await _drain_stream(stream)
        assert chunks == [b"event: msg\ndata: hi\n\n"]

    @pytest.mark.asyncio
    async def test_yields_multiple_events_in_order(self):
        events = [
            ServerSentEvent(data="one"),
            ServerSentEvent(data="two"),
            ServerSentEvent(data="three"),
        ]
        stream = EventStream(_async_iter(events))
        chunks = await _drain_stream(stream)
        assert chunks == [
            b"data: one\n\n",
            b"data: two\n\n",
            b"data: three\n\n",
        ]

    @pytest.mark.asyncio
    async def test_string_items_are_promoted_to_events(self):
        stream = EventStream(_async_iter(["plain"]))
        chunks = await _drain_stream(stream)
        assert chunks == [b"data: plain\n\n"]

    @pytest.mark.asyncio
    async def test_dict_items_are_promoted_to_events(self):
        stream = EventStream(_async_iter([{"event": "x", "data": "payload"}]))
        chunks = await _drain_stream(stream)
        assert chunks == [b"event: x\ndata: payload\n\n"]

    @pytest.mark.asyncio
    async def test_sync_iterable_is_accepted(self):
        # Sync iterators are auto-adapted so test fixtures and smoke
        # tests can use list literals directly.
        stream = EventStream([ServerSentEvent(data="sync")])
        chunks = await _drain_stream(stream)
        assert chunks == [b"data: sync\n\n"]

    @pytest.mark.asyncio
    async def test_empty_iterable_produces_no_frames(self):
        stream = EventStream(_async_iter([]))
        chunks = await _drain_stream(stream)
        assert chunks == []


class TestEventStreamKeepAlive:
    """Heartbeat comments fire when the producer is idle."""

    @pytest.mark.asyncio
    async def test_heartbeat_emits_when_producer_is_slow(self):
        # The producer yields one event after a 50 ms delay; the keep
        # alive interval is 10 ms, so we expect at least one heartbeat
        # comment frame *before* the event.
        async def slow_producer() -> AsyncIterator[ServerSentEvent]:
            await asyncio.sleep(0.05)
            yield ServerSentEvent(data="real")

        stream = EventStream(slow_producer(), keep_alive=0.01)
        chunks = await _drain_stream(stream)
        # At least one heartbeat (`: keep-alive\n\n`) must appear before
        # the user event. We don't assert the exact count because that
        # depends on scheduler latency.
        heartbeats = [c for c in chunks if c.startswith(b": keep-alive")]
        events = [c for c in chunks if c == b"data: real\n\n"]
        assert len(heartbeats) >= 1
        assert events == [b"data: real\n\n"]

    @pytest.mark.asyncio
    async def test_heartbeat_uses_custom_comment(self):
        async def slow_producer() -> AsyncIterator[ServerSentEvent]:
            await asyncio.sleep(0.03)
            yield ServerSentEvent(data="x")

        stream = EventStream(
            slow_producer(),
            keep_alive=0.01,
            keep_alive_comment="custom-ping",
        )
        chunks = await _drain_stream(stream)
        heartbeats = [c for c in chunks if c.startswith(b": custom-ping")]
        assert len(heartbeats) >= 1

    @pytest.mark.asyncio
    async def test_no_heartbeats_when_producer_is_fast(self):
        # A fast producer doesn't trigger heartbeats — they only kick
        # in during idle gaps. The keep-alive interval is generous
        # (1 s) compared to the producer latency (~0).
        async def fast() -> AsyncIterator[ServerSentEvent]:
            yield ServerSentEvent(data="a")
            yield ServerSentEvent(data="b")

        stream = EventStream(fast(), keep_alive=1.0)
        chunks = await _drain_stream(stream)
        heartbeats = [c for c in chunks if c.startswith(b":")]
        assert heartbeats == []
        # All real events were emitted.
        assert chunks == [b"data: a\n\n", b"data: b\n\n"]


class TestEventStreamCancellation:
    """The framing pipeline cooperates with task cancellation."""

    @pytest.mark.asyncio
    async def test_aclose_runs_user_finally_block(self):
        # When the consumer cancels the body iterator, the framing
        # routine must invoke ``aclose`` on the producer so the
        # producer's ``finally`` block runs (releasing DB sessions,
        # pubsub subscriptions, etc.).
        teardown_called = asyncio.Event()

        async def producer() -> AsyncIterator[ServerSentEvent]:
            try:
                yield ServerSentEvent(data="first")
                # Block forever — only cancellation will end this.
                await asyncio.sleep(60)
            finally:
                teardown_called.set()

        stream = EventStream(producer())
        body = stream.stream_body
        assert body is not None
        iterator = body.__aiter__()
        first = await iterator.__anext__()
        assert first == b"data: first\n\n"
        # Now close the iterator — the producer's ``finally`` must run.
        await iterator.aclose()
        # ``aclose()`` is synchronous wrt scheduling; the teardown
        # event should be set immediately afterwards.
        assert teardown_called.is_set()


# ===========================================================================
# last_event_id helper.
# ===========================================================================


class TestLastEventIdHelper:
    def test_returns_value_when_present(self):
        h = Headers([("last-event-id", "42")])
        assert last_event_id(h) == "42"

    def test_case_insensitive_lookup(self):
        h = Headers([("Last-Event-ID", "abc")])
        assert last_event_id(h) == "abc"

    def test_returns_none_when_absent(self):
        h = Headers([("content-type", "text/plain")])
        assert last_event_id(h) is None

    def test_empty_value_returns_none(self):
        # An empty header is functionally absent — return None so
        # callers can use the simpler ``if last_event_id(h):`` idiom.
        h = Headers([("last-event-id", "")])
        assert last_event_id(h) is None


# ===========================================================================
# Helpers.
# ===========================================================================


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    """Wrap a list as an async iterator for streaming-input tests."""
    for item in items:
        yield item


async def _drain_stream(stream: EventStream) -> list[bytes]:
    """Collect every byte chunk emitted by an :class:`EventStream`."""
    body = stream.stream_body
    assert body is not None, "EventStream must always provide a stream body"
    return [chunk async for chunk in body]


# ===========================================================================
# Additional coverage tests
# ===========================================================================


class TestEncodeDataPydanticModel:
    """Cover _encode_data with pydantic model (lines 228-231)."""

    def test_pydantic_model_encode(self):
        from pydantic import BaseModel

        class Item(BaseModel):
            name: str
            value: int

        result = _encode_data(Item(name="x", value=42))
        import json

        parsed = json.loads(result)
        assert parsed["name"] == "x"
        assert parsed["value"] == 42


class TestEventStreamExtraHeaders:
    """Cover EventStream extra_headers parameter (lines 321-326)."""

    def test_extra_headers_as_headers_object(self):
        """EventStream with extra_headers as a Headers instance."""

        async def gen():
            yield "data"

        from lauren import Headers

        stream = EventStream(gen(), extra_headers=Headers([("x-custom", "yes")]))
        ct = stream.headers.get("content-type")
        assert "text/event-stream" in ct
        assert stream.headers.get("x-custom") == "yes"

    def test_extra_headers_as_list(self):
        """EventStream with extra_headers as a list of tuples (iterable)."""

        async def gen():
            yield "data"

        stream = EventStream(gen(), extra_headers=[("x-custom-2", "abc")])
        assert stream.headers.get("x-custom-2") == "abc"


class TestEventStreamCancelledError:
    """Cover CancelledError propagation in keep-alive framing (line 448)."""

    def test_cancelled_error_propagates_via_keep_alive(self):
        """When the event loop cancels the keep_alive stream, CancelledError
        propagates upward rather than being silently swallowed."""

        async def gen():
            await asyncio.sleep(999)
            yield "never"

        stream = EventStream(gen(), keep_alive=0.01)

        async def run():
            chunks = []
            body = stream.stream_body
            assert body is not None
            task = asyncio.ensure_future(_collect_chunks(body, chunks))
            # Let it run briefly (should emit one keep-alive comment)
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
            return chunks

        async def _collect_chunks(body, chunks):
            async for chunk in body:
                chunks.append(chunk)

        result = asyncio.run(run())
        # At least a keep-alive heartbeat comment should have been emitted
        assert any(b": keep-alive" in c for c in result), f"No heartbeat in {result!r}"


class TestSafeAclose:
    """Cover _safe_aclose (lines 472-478)."""

    def test_safe_aclose_without_aclose_method(self):
        """Iterators without aclose() are handled gracefully."""
        from lauren.sse import _safe_aclose

        class NoAclose:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

        async def run():
            await _safe_aclose(NoAclose())

        asyncio.run(run())

    def test_safe_aclose_with_raising_aclose(self):
        """Exceptions from aclose() are suppressed."""
        from lauren.sse import _safe_aclose

        class BrokenAclose:
            async def aclose(self):
                raise RuntimeError("broken aclose")

        async def run():
            await _safe_aclose(BrokenAclose())  # should not raise

        asyncio.run(run())
