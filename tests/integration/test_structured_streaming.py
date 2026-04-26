"""Integration tests for feature 7 — structured streaming end-to-end.

These tests drive a real :class:`LaurenApp` through
:class:`~lauren.testing.TestClient` to verify the full bidirectional
streaming contract:

* Inbound ``Stream[T]`` extractor: chunked ASGI messages are parsed one
  record at a time, validated against ``T`` (including discriminated
  unions), and delivered to the handler's ``async for`` loop.
* Outbound ``StreamingResponse[T]`` return type: yielded items are
  serialized according to the request's ``Accept`` header in one of
  three canonical formats.
* Content negotiation: ``text/event-stream`` → SSE,
  ``application/x-ndjson`` → NDJSON, ``application/json+stream`` →
  JSON Lines (default).
* OpenAPI: handlers annotated with ``StreamingResponse[T]`` surface with
  ``x-streaming: true`` and every negotiable content type is advertised.
* Input validation errors raise 422 (before headers ship) or surface as
  a trailing error frame (once the stream is flowing).
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, AsyncIterator, Literal, Union

from pydantic import BaseModel, Field

from lauren import (
    LaurenFactory,
    Stream,
    StreamingResponse,
    controller,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Fixture models — a transcript domain plus a discriminated-union event
# type to prove feature 6 and feature 7 compose cleanly.
# ---------------------------------------------------------------------------


class AudioChunk(BaseModel):
    seq: int
    text: str


class Transcript(BaseModel):
    text: str
    confidence: float


class ImageEvent(BaseModel):
    kind: Literal["image"]
    url: str


class TextEvent(BaseModel):
    kind: Literal["text"]
    content: str


Event = Annotated[Union[ImageEvent, TextEvent], Field(discriminator="kind")]


@controller("/stream", tags=["stream"])
class StreamController:
    @post("/transcribe", response_model=Transcript)
    async def transcribe(
        self, audio: Stream[AudioChunk]
    ) -> StreamingResponse[Transcript]:
        async def produce() -> AsyncIterator[Transcript]:
            async for chunk in audio:
                # The contract: ``chunk`` is a validated AudioChunk.
                yield Transcript(
                    text=chunk.text.upper(),
                    confidence=0.5 + (chunk.seq % 5) / 10.0,
                )

        return produce()

    @post("/events")
    async def events(self, inbound: Stream[Event]) -> StreamingResponse[Event]:
        # Round-trips a discriminated union through both directions to
        # prove feature 6 and feature 7 compose.
        async def produce() -> AsyncIterator[Event]:
            async for ev in inbound:
                yield ev

        return produce()


@module(controllers=[StreamController])
class StreamModule:
    pass


def _app():
    return asyncio.run(LaurenFactory.create(StreamModule, openapi_url="/openapi.json"))


def _make_ndjson(items: list[dict]) -> bytes:
    return ("\n".join(json.dumps(i) for i in items) + "\n").encode("utf-8")


def _make_sse(items: list[dict]) -> bytes:
    return "".join(f"data: {json.dumps(i)}\n\n" for i in items).encode("utf-8")


# ---------------------------------------------------------------------------
# Inbound Stream[T] — request body framing
# ---------------------------------------------------------------------------


class TestInboundNdjson:
    def test_ndjson_body_parsed_line_by_line(self):
        body = _make_ndjson(
            [
                {"seq": 1, "text": "hello"},
                {"seq": 2, "text": "world"},
                {"seq": 3, "text": "!"},
            ]
        )
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={
                "content-type": "application/x-ndjson",
                "accept": "application/x-ndjson",
            },
        )
        assert r.status_code == 200, r.text
        lines = [line for line in r.text.split("\n") if line.strip()]
        assert len(lines) == 3
        docs = [json.loads(l) for l in lines]
        assert [d["text"] for d in docs] == ["HELLO", "WORLD", "!"]
        assert all("confidence" in d for d in docs)

    def test_invalid_first_record_raises_422(self):
        # The first record fails validation — this happens during the
        # priming step before response headers have shipped, so the
        # runtime can still map the ExtractorError to a proper 422.
        body = b'{"seq": 1}\n{"seq": 2, "text": "ok"}\n'
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        assert r.status_code == 422
        payload = r.json()
        assert payload["error"]["code"] == "extractor_error"
        assert payload["error"]["detail"]["field"] == "audio"
        assert payload["error"]["detail"]["format"] == "ndjson"

    def test_invalid_mid_record_surfaces_as_trailing_error_frame(self):
        # Once headers have shipped the HTTP status is immutable; per
        # the streaming contract a mid-stream failure must surface as a
        # trailing error frame carrying the canonical error envelope.
        # Clients already parsing structured JSON can spot it by the
        # ``error`` top-level key.
        body = b'{"seq": 1, "text": "ok"}\n{"seq": 2}\n'
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        assert r.status_code == 200
        lines = [json.loads(l) for l in r.text.split("\n") if l.strip()]
        assert lines[0]["text"] == "OK"
        assert "error" in lines[-1]
        assert lines[-1]["error"]["code"] == "extractor_error"
        assert lines[-1]["error"]["detail"]["format"] == "ndjson"

    def test_malformed_first_json_raises_422(self):
        body = b'{not json}\n{"seq": 1, "text": "ok"}\n'
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        assert r.status_code == 422
        assert "invalid JSON" in r.json()["error"]["message"]


class TestInboundJsonLines:
    def test_default_format_is_jsonlines(self):
        body = _make_ndjson([{"seq": 1, "text": "x"}])
        client = TestClient(_app())
        # No content-type header — default negotiation wins.
        r = client.post("/stream/transcribe", content=body)
        assert r.status_code == 200, r.text


class TestInboundSse:
    def test_sse_body_extracted_from_data_lines(self):
        body = _make_sse(
            [
                {"seq": 1, "text": "a"},
                {"seq": 2, "text": "b"},
            ]
        )
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={
                "content-type": "text/event-stream",
                "accept": "application/x-ndjson",
            },
        )
        assert r.status_code == 200, r.text
        lines = [l for l in r.text.split("\n") if l.strip()]
        assert [json.loads(l)["text"] for l in lines] == ["A", "B"]


# ---------------------------------------------------------------------------
# Outbound content negotiation
# ---------------------------------------------------------------------------


class TestOutboundNegotiation:
    def test_accept_sse(self):
        body = _make_ndjson([{"seq": 1, "text": "x"}, {"seq": 2, "text": "y"}])
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={
                "content-type": "application/x-ndjson",
                "accept": "text/event-stream",
            },
        )
        assert r.status_code == 200
        assert "text/event-stream" in r.header("content-type")
        # Expect two SSE events, each with a ``data:`` line and a blank-
        # line terminator.
        assert r.text.count("\n\n") >= 2
        assert r.text.count("data: ") == 2

    def test_accept_ndjson(self):
        body = _make_ndjson([{"seq": 1, "text": "x"}])
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={
                "content-type": "application/x-ndjson",
                "accept": "application/x-ndjson",
            },
        )
        assert "application/x-ndjson" in r.header("content-type")

    def test_accept_jsonlines_default(self):
        body = _make_ndjson([{"seq": 1, "text": "x"}])
        client = TestClient(_app())
        r = client.post(
            "/stream/transcribe",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        # Default wire format on missing/``*/*`` Accept is jsonlines.
        assert "application/json+stream" in r.header("content-type")


# ---------------------------------------------------------------------------
# Composed with feature 6 — streams of discriminated unions
# ---------------------------------------------------------------------------


class TestStreamingDiscriminatedUnions:
    def test_round_trip_variants(self):
        body = _make_ndjson(
            [
                {"kind": "image", "url": "a.png"},
                {"kind": "text", "content": "hi"},
            ]
        )
        client = TestClient(_app())
        r = client.post(
            "/stream/events",
            content=body,
            headers={
                "content-type": "application/x-ndjson",
                "accept": "application/x-ndjson",
            },
        )
        assert r.status_code == 200, r.text
        docs = [json.loads(line) for line in r.text.split("\n") if line.strip()]
        assert docs[0]["kind"] == "image"
        assert docs[0]["url"] == "a.png"
        assert docs[1]["kind"] == "text"
        assert docs[1]["content"] == "hi"

    def test_invalid_first_variant_raises_422(self):
        # A bad first variant is caught during the priming step before
        # headers ship and surfaces as a 422 with structured detail.
        body = b'{"kind": "video", "url": "y"}\n{"kind": "image", "url": "x"}\n'
        client = TestClient(_app())
        r = client.post(
            "/stream/events",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        assert r.status_code == 422
        errs = r.json()["error"]["detail"]["errors"]
        assert any("tag" in str(e.get("type", "")).lower() for e in errs)

    def test_invalid_mid_variant_surfaces_error_frame(self):
        body = b'{"kind": "image", "url": "x"}\n{"kind": "video", "url": "y"}\n'
        client = TestClient(_app())
        r = client.post(
            "/stream/events",
            content=body,
            headers={"content-type": "application/x-ndjson"},
        )
        assert r.status_code == 200
        docs = [json.loads(l) for l in r.text.split("\n") if l.strip()]
        assert docs[0] == {"kind": "image", "url": "x"}
        assert "error" in docs[-1]

    def test_sse_event_kind_surfaces_in_output_frame(self):
        # Feature 7 sugar: when an item has a ``kind`` attribute and we're
        # emitting SSE, lauren writes the kind as the ``event:`` field so
        # browser EventSource clients can subscribe per-kind.
        body = _make_ndjson([{"kind": "image", "url": "x"}])
        client = TestClient(_app())
        r = client.post(
            "/stream/events",
            content=body,
            headers={
                "content-type": "application/x-ndjson",
                "accept": "text/event-stream",
            },
        )
        assert r.status_code == 200
        assert "event: image" in r.text


# ---------------------------------------------------------------------------
# OpenAPI surface
# ---------------------------------------------------------------------------


class TestStreamingOpenAPI:
    def test_transcribe_route_tagged_streaming(self):
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        op = doc["paths"]["/stream/transcribe"]["post"]
        assert op["x-streaming"] is True
        content = op["responses"]["200"]["content"]
        # Every negotiable content type must be listed.
        assert "text/event-stream" in content
        assert "application/x-ndjson" in content
        assert "application/json+stream" in content
        # Each maps to the Transcript schema.
        for media, spec in content.items():
            assert spec["schema"] == {"$ref": "#/components/schemas/Transcript"}

    def test_events_route_advertises_oneof_in_stream(self):
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        op = doc["paths"]["/stream/events"]["post"]
        assert op["x-streaming"] is True
        schema = op["responses"]["200"]["content"]["application/x-ndjson"]["schema"]
        assert "oneOf" in schema
        assert schema["discriminator"]["propertyName"] == "kind"

    def test_non_streaming_routes_have_no_x_streaming(self):
        # Regression guard — ``x-streaming`` must NOT leak onto ordinary
        # JSON endpoints elsewhere in the document.
        app = _app()
        client = TestClient(app)
        doc = client.get("/openapi.json").json()
        for path, item in doc["paths"].items():
            for method, op in item.items():
                if path == "/openapi.json":
                    continue
                if path.startswith("/stream/transcribe") or path.startswith(
                    "/stream/events"
                ):
                    continue
                assert (
                    "x-streaming" not in op
                ), f"x-streaming leaked into {method} {path}"
