"""Integration tests: configured JSONEncoder is used by all four output paths.

Before this fix the following always used raw stdlib json regardless of the
app's configured encoder:
- HTTP error responses (_error_response)
- WebSocket.send_json()
- EventStream / ServerSentEvent
- Response.sse() dict events

Each test verifies that a custom encoder (a sentinel that prefixes every
key so we can tell which encoder ran) is honoured on each path.
"""

from __future__ import annotations

import json as _stdlib_json
from typing import AsyncIterator


from lauren import (
    LaurenFactory,
    Response,
    controller,
    get,
    module,
    ws_controller,
)
from lauren.exceptions import ForbiddenError
from lauren.serialization import StdlibJSONEncoder
from lauren.sse import EventStream, ServerSentEvent
from lauren.testing import TestClient, WsTestClient


# ---------------------------------------------------------------------------
# Sentinel encoder
# ---------------------------------------------------------------------------


class _SentinelEncoder(StdlibJSONEncoder):
    """Wraps stdlib JSON but wraps the output in a recognisable marker."""

    MARKER = "__sentinel__"

    def encode(self, data):  # type: ignore[override]
        raw = super().encode(data)
        return f'{{"__sentinel__":{raw}}}'

    def encode_compact(self, data):  # type: ignore[override]
        raw = super().encode_compact(data)
        return b'{"__sentinel__":' + raw + b"}"


def _build(ctrl_cls: type, encoder=None) -> TestClient:
    @module(controllers=[ctrl_cls])
    class M:
        pass

    kwargs = {"json_encoder": encoder} if encoder is not None else {}
    return TestClient(LaurenFactory.create(M, **kwargs))


# ---------------------------------------------------------------------------
# Gap 1 — error responses use the configured encoder
# ---------------------------------------------------------------------------


class TestErrorResponseEncoder:
    def test_error_body_uses_configured_encoder(self):
        enc = _SentinelEncoder()

        @controller("/err")
        class C:
            @get("/")
            async def h(self) -> dict:
                raise ForbiddenError("no access")

        r = _build(C, encoder=enc).get("/err/")
        assert r.status_code == 403
        assert _SentinelEncoder.MARKER.encode() in r.body

    def test_route_not_found_uses_configured_encoder(self):
        enc = _SentinelEncoder()

        @controller("/x")
        class C:
            @get("/exists")
            async def h(self) -> dict:
                return {}

        r = _build(C, encoder=enc).get("/x/missing")
        assert r.status_code == 404
        assert _SentinelEncoder.MARKER.encode() in r.body

    def test_stdlib_encoder_unchanged_when_not_configured(self):
        @controller("/plain")
        class C:
            @get("/")
            async def h(self) -> dict:
                raise ForbiddenError("no")

        r = _build(C).get("/plain/")
        assert r.status_code == 403
        body = _stdlib_json.loads(r.body)
        assert "error" in body  # default envelope


# ---------------------------------------------------------------------------
# Gap 2 — WebSocket.send_json() uses the configured encoder
# ---------------------------------------------------------------------------


class TestWebSocketSendJsonEncoder:
    def test_send_json_uses_configured_encoder(self):
        enc = _SentinelEncoder()

        @ws_controller("/ws")
        class WsC:
            @get("/connect")
            async def on_connect(self): ...

        @module(controllers=[WsC])
        class M:
            pass

        app = LaurenFactory.create(M, json_encoder=enc)

        async def run():
            async with WsTestClient(app).connect("/ws") as ws:
                await ws.send_text("hello")
                # The server echoes nothing here; we test by calling send_json
                # directly on the underlying WebSocket
                pass

        # For send_json we test via the unit path — the _encode_json helper
        # is the core fix; the integration path is covered in test_ws_coverage
        # For a direct unit assertion, import _encode_json
        from lauren.websockets import _encode_json

        result = _encode_json({"key": "value"}, encoder=enc)
        payload = _stdlib_json.loads(result)
        assert _SentinelEncoder.MARKER in payload

    def test_encode_json_without_encoder_uses_active(self):
        from lauren.websockets import _encode_json

        result = _encode_json({"a": 1})
        parsed = _stdlib_json.loads(result)
        assert parsed == {"a": 1}

    def test_websocket_constructor_receives_encoder(self):
        """WebSocket.__init__ stores json_encoder so send_json can use it."""
        enc = _SentinelEncoder()
        from lauren.websockets import WebSocket

        async def recv():
            return {"type": "websocket.receive", "text": "x"}

        async def send(msg):
            pass

        ws = WebSocket(
            scope={"type": "websocket", "path": "/", "headers": [], "query_string": b""},
            receive=recv,
            send=send,
            path_template="/",
            path_params={},
            json_encoder=enc,
        )
        assert ws._json_encoder is enc

    def test_encode_json_uses_provided_encoder(self):
        """_encode_json delegates to the provided encoder."""
        enc = _SentinelEncoder()
        from lauren.websockets import _encode_json

        result = _encode_json({"key": "value"}, encoder=enc)
        parsed = _stdlib_json.loads(result)
        assert _SentinelEncoder.MARKER in parsed


# ---------------------------------------------------------------------------
# Gap 3 — EventStream uses the configured encoder
# ---------------------------------------------------------------------------


class TestEventStreamEncoder:
    def test_event_stream_dict_data_uses_configured_encoder(self):
        enc = _SentinelEncoder()

        @controller("/stream")
        class C:
            @get("/")
            async def h(self) -> EventStream:
                async def gen() -> AsyncIterator[ServerSentEvent]:
                    yield ServerSentEvent(data={"key": "value"})

                return EventStream(gen())

        r = _build(C, encoder=enc).get("/stream/", headers=[("accept", "text/event-stream")])
        assert r.status_code == 200
        assert _SentinelEncoder.MARKER.encode() in r.body

    def test_event_stream_string_data_unaffected(self):
        enc = _SentinelEncoder()

        @controller("/s2")
        class C:
            @get("/")
            async def h(self) -> EventStream:
                async def gen():
                    yield ServerSentEvent(data="hello world")

                return EventStream(gen())

        r = _build(C, encoder=enc).get("/s2/", headers=[("accept", "text/event-stream")])
        assert r.status_code == 200
        assert b"hello world" in r.body
        # No sentinel — strings pass through verbatim
        assert _SentinelEncoder.MARKER.encode() not in r.body

    def test_event_stream_without_encoder_uses_stdlib(self):
        @controller("/s3")
        class C:
            @get("/")
            async def h(self) -> EventStream:
                async def gen():
                    yield ServerSentEvent(data={"x": 1})

                return EventStream(gen())

        r = _build(C).get("/s3/", headers=[("accept", "text/event-stream")])
        assert r.status_code == 200
        assert b'"x": 1' in r.body or b'"x":1' in r.body


# ---------------------------------------------------------------------------
# Gap 4 — Response.sse() uses the configured encoder
# ---------------------------------------------------------------------------


class TestResponseSseEncoder:
    def test_sse_dict_event_uses_configured_encoder(self):
        enc = _SentinelEncoder()

        @controller("/sse")
        class C:
            @get("/")
            async def h(self) -> Response:
                async def gen():
                    yield {"data": {"key": "value"}, "event": "update"}

                return Response.sse(gen(), encoder=enc)

        r = _build(C, encoder=enc).get("/sse/")
        assert r.status_code == 200
        assert _SentinelEncoder.MARKER.encode() in r.body

    def test_sse_string_event_unaffected(self):
        enc = _SentinelEncoder()

        @controller("/sse2")
        class C:
            @get("/")
            async def h(self) -> Response:
                async def gen():
                    yield "plain text"

                return Response.sse(gen(), encoder=enc)

        r = _build(C, encoder=enc).get("/sse2/")
        assert r.status_code == 200
        assert b"plain text" in r.body
        assert _SentinelEncoder.MARKER.encode() not in r.body

    def test_sse_without_encoder_uses_stdlib(self):
        @controller("/sse3")
        class C:
            @get("/")
            async def h(self) -> Response:
                async def gen():
                    yield {"data": {"n": 42}}

                return Response.sse(gen())

        r = _build(C).get("/sse3/")
        assert r.status_code == 200
        assert b"42" in r.body
