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
        assert (
            negotiate_stream_format("text/event-stream, application/json+stream")
            == "sse"
        )

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
