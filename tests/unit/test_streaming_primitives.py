"""Unit tests for feature 7 \u2014 Stream[T] / StreamingResponse[T] building blocks.

These tests focus on the pieces that can be exercised without standing
up a full :class:`LaurenApp`:

* ``Accept`` / ``Content-Type`` negotiation via
  :func:`negotiate_stream_format`.
* ``StreamingResponse[T]`` annotation introspection via
  :func:`extract_streaming_item_type`.
* The SSE ``data:`` line extractor helper.

End-to-end ASGI behaviour is covered in the integration suite
(``tests/integration/test_structured_streaming.py``).
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from lauren.streaming import (
    STREAM_FORMATS,
    FORMAT_TO_MEDIA_TYPE,
    MEDIA_TYPE_TO_FORMAT,
    Stream,
    StreamingResponse,
    _build_adapter,
    _sse_extract_data,
    extract_streaming_item_type,
    negotiate_stream_format,
)


class Chunk(BaseModel):
    seq: int
    text: str


class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str


class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str


Event = Annotated[Union[ImageEvent, TextEvent], Field(discriminator="kind")]


class TestNegotiation:
    def test_default_when_missing(self):
        assert negotiate_stream_format(None) == "jsonlines"
        assert negotiate_stream_format("") == "jsonlines"

    def test_sse_preferred(self):
        assert negotiate_stream_format("text/event-stream") == "sse"

    def test_ndjson_preferred(self):
        assert negotiate_stream_format("application/x-ndjson") == "ndjson"
        assert negotiate_stream_format("application/ndjson") == "ndjson"

    def test_jsonlines_preferred(self):
        assert negotiate_stream_format("application/json+stream") == "jsonlines"
        assert negotiate_stream_format("application/jsonl") == "jsonlines"

    def test_wildcard_falls_back_to_default(self):
        assert negotiate_stream_format("*/*") == "jsonlines"
        assert negotiate_stream_format("*/*", default="sse") == "sse"

    def test_first_match_wins(self):
        # Clients sometimes concatenate: "text/event-stream, application/json"
        # \u2014 the SSE flavour must win because it appears first.
        assert negotiate_stream_format("text/event-stream, application/json+stream") == "sse"

    def test_parameters_are_stripped(self):
        assert negotiate_stream_format("text/event-stream; charset=utf-8") == "sse"

    def test_unknown_falls_back_to_default(self):
        assert negotiate_stream_format("text/html") == "jsonlines"


class TestMediaTypeRegistry:
    def test_all_canonical_formats_have_media_type(self):
        for fmt in STREAM_FORMATS:
            assert fmt in FORMAT_TO_MEDIA_TYPE

    def test_reverse_lookup_consistent(self):
        for fmt, media in FORMAT_TO_MEDIA_TYPE.items():
            assert MEDIA_TYPE_TO_FORMAT[media] == fmt


class TestStreamingResponseAnnotation:
    def test_extracts_item_type_from_response_annotation(self):
        ann = StreamingResponse[Chunk]
        assert extract_streaming_item_type(ann) is Chunk

    def test_extracts_item_type_for_discriminated_union(self):
        ann = StreamingResponse[Event]
        assert extract_streaming_item_type(ann) is Event

    def test_returns_none_for_non_streaming_annotation(self):
        assert extract_streaming_item_type(int) is None
        assert extract_streaming_item_type(Chunk) is None
        import inspect as _inspect

        assert extract_streaming_item_type(_inspect.Parameter.empty) is None
        assert extract_streaming_item_type(None) is None


class TestStreamMarkerIdentity:
    def test_stream_has_correct_source(self):
        assert Stream.source == "stream"
        assert Stream.reads_body is True

    def test_stream_subscript_returns_annotated(self):
        # ``Stream[Chunk]`` must produce an annotation that downstream tools
        # (the handler-signature compiler) can detect via the standard
        # ``parse_extractor_hint`` path \u2014 which is what the integration
        # test exercises end-to-end.
        from typing import get_args

        ann = Stream[Chunk]
        args = get_args(ann)
        assert args[0] is Chunk
        assert Stream in args[1:]


class TestSseFrameExtractor:
    def test_single_data_line(self):
        block = b'event: tick\ndata: {"seq":1}'
        assert _sse_extract_data(block) == b'{"seq":1}'

    def test_multi_data_lines_concatenated(self):
        block = b"data: line1\ndata: line2"
        assert _sse_extract_data(block) == b"line1\nline2"

    def test_ignores_non_data_fields(self):
        block = b"id: 7\nevent: x\nretry: 500\n: comment"
        assert _sse_extract_data(block) == b""


class TestAdapterCache:
    def test_adapter_cached_per_type(self):
        a1 = _build_adapter(Chunk)
        a2 = _build_adapter(Chunk)
        assert a1 is a2

    def test_adapter_validates_concrete_model(self):
        adapter = _build_adapter(Chunk)
        assert adapter is not None
        out = adapter.validate_python({"seq": 1, "text": "hi"})
        assert isinstance(out, Chunk)
        assert out.seq == 1


# ---------------------------------------------------------------------------
# Additional coverage tests for streaming.py
# ---------------------------------------------------------------------------


class TestStreamReaderProperties:
    """Cover StreamReader.format and inner_type properties (lines 217, 221)."""

    def test_format_property(self):
        from lauren.streaming import StreamReader

        async def empty_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        class FakeRequest:
            pass

        fake = FakeRequest()
        fake._receive = empty_receive  # instance attribute to avoid binding

        reader = StreamReader(
            request=fake,
            inner_type=str,
            format="ndjson",
            field_name="data",
        )
        assert reader.format == "ndjson"

    def test_inner_type_property(self):
        from lauren.streaming import StreamReader

        async def empty_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        class FakeRequest:
            pass

        fake = FakeRequest()
        fake._receive = empty_receive  # instance attribute to avoid binding

        reader = StreamReader(
            request=fake,
            inner_type=int,
            format="jsonlines",
            field_name="n",
        )
        assert reader.inner_type is int


class TestStreamReaderEdgeCases:
    """Cover disconnection + trailing fragment (lines 236-241)."""

    def _make_reader(self, messages, format="jsonlines"):
        """Helper to build a StreamReader with a controlled message sequence."""
        from lauren.streaming import StreamReader
        from typing import Any

        idx = [0]

        async def receive():
            m = messages[idx[0]]
            idx[0] += 1
            return m

        class FakeRequest:
            pass

        fake = FakeRequest()
        fake._receive = receive

        # Use Any inner_type so pydantic won't reject arbitrary dicts
        return StreamReader(
            request=fake,
            inner_type=Any,
            format=format,
            field_name="data",
        )

    def test_stream_reader_http_disconnect(self):
        """When a disconnect arrives after data, StopAsyncIteration is raised."""
        import asyncio

        reader = self._make_reader(
            [
                {
                    "type": "http.request",
                    "body": b'{"x":1}\n{"x":2}\n',
                    "more_body": True,
                },
                {"type": "http.disconnect"},
            ]
        )

        async def run():
            items = []
            async for item in reader:
                items.append(item)
            return items

        result = asyncio.run(run())
        assert len(result) == 2
        assert result[0] == {"x": 1}
        assert result[1] == {"x": 2}

    def test_stream_reader_http_disconnect_continues_reading(self):
        """When more_body=True followed by disconnect, items are still yielded."""
        import asyncio

        reader = self._make_reader(
            [
                {"type": "http.request", "body": b'{"x":1}\n', "more_body": True},
                {"type": "http.disconnect"},
            ]
        )

        async def run():
            items = []
            async for item in reader:
                items.append(item)
            return items

        result = asyncio.run(run())
        assert len(result) >= 1
        assert result[0] == {"x": 1}


class TestBuildAdapterNoPydantic:
    """Cover _build_adapter when pydantic is not available (line 406)."""

    def test_build_adapter_returns_none_without_pydantic(self, monkeypatch):
        from lauren import streaming

        monkeypatch.setattr(streaming, "_PYDANTIC_AVAILABLE", False)
        # Clear cache so the monkeypatched flag takes effect (Phase 3 caches adapters).
        streaming._ADAPTER_CACHE.clear()
        result = streaming._build_adapter(str)
        assert result is None
        streaming._ADAPTER_CACHE.clear()  # restore for other tests


class TestIsDiscriminatedUnionNoPydantic:
    """Cover is_discriminated_union when pydantic is not available (line 424)."""

    def test_returns_false_without_pydantic(self, monkeypatch):
        from lauren import streaming

        monkeypatch.setattr(streaming, "_PYDANTIC_AVAILABLE", False)
        result = streaming.is_discriminated_union(str)
        assert result is False


class TestDiscriminatorVariantsEdgeCases:
    """Cover discriminator_variants empty args (line 486)."""

    def test_empty_args_returns_empty_tuple(self):
        from lauren.streaming import discriminator_variants

        result = discriminator_variants(str)  # No args
        assert result == ()
