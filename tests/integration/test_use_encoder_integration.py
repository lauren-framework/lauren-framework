"""Integration tests for @use_encoder — per-controller and per-route encoder override.

Covers:
- Controller-level: all routes inherit the controller encoder
- Route-level: only that route uses the encoder; others use app-level
- Method wins over controller when both are set
- Error responses use the route encoder
- EventStream uses the route encoder
- Fallback to app-level when no route encoder is set
- Encoder identity: we confirm the exact encoder instance is used via a sentinel
"""

from __future__ import annotations

import json as _stdlib_json
from typing import AsyncIterator

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    OrjsonEncoder,
    StdlibJSONEncoder,
    controller,
    get,
    module,
    use_encoder,
)
from lauren.exceptions import ForbiddenError
from lauren.serialization import MsgspecEncoder, PydanticEncoder
from lauren.sse import EventStream, ServerSentEvent
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Sentinel encoder — its compact output contains a recognisable marker so
# we can assert which encoder actually ran.
# ---------------------------------------------------------------------------


class _SentinelEncoder(StdlibJSONEncoder):
    MARKER = b"__sentinel__"

    def encode_compact(self, value):
        raw = super().encode_compact(value)
        return b'{"__s":1,' + raw[1:] if raw.startswith(b"{") else b"[1," + raw[1:]

    def encode(self, value):
        return self.encode_compact(value)


_SENTINEL = _SentinelEncoder()
_STDLIB = StdlibJSONEncoder()


def _client(ctrl: type, *, app_encoder=None) -> TestClient:
    @module(controllers=[ctrl])
    class M:
        pass

    kwargs = {"json_encoder": app_encoder} if app_encoder else {}
    return TestClient(LaurenFactory.create(M, **kwargs))


# ---------------------------------------------------------------------------
# Decorator validation
# ---------------------------------------------------------------------------


class TestUseEncoderValidation:
    def test_bare_usage_raises(self):
        from lauren.exceptions import DecoratorUsageError

        with pytest.raises(DecoratorUsageError, match="parentheses"):

            @use_encoder  # type: ignore[arg-type]  # no parens — must raise
            @controller("/x")
            class C:
                pass

    def test_non_encoder_arg_raises(self):
        from lauren.exceptions import DecoratorUsageError

        with pytest.raises(DecoratorUsageError, match="JSONEncoder"):
            use_encoder("not-an-encoder")  # type: ignore[arg-type]

    def test_class_instead_of_instance_raises(self):
        from lauren.exceptions import DecoratorUsageError

        with pytest.raises(DecoratorUsageError):
            use_encoder(StdlibJSONEncoder)  # type: ignore[arg-type]  # class, not instance

    def test_valid_encoder_instance_accepted(self):
        enc = _SentinelEncoder()

        @use_encoder(enc)
        @controller("/valid")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {}

        # No error during decoration
        assert getattr(C, "__lauren_use_encoder__") is enc


# ---------------------------------------------------------------------------
# Controller-level: all routes inherit the encoder
# ---------------------------------------------------------------------------


class TestControllerLevelEncoder:
    def test_all_routes_use_controller_encoder(self):
        @use_encoder(_SENTINEL)
        @controller("/ctrl")
        class C:
            @get("/a")
            async def a(self) -> dict:
                return {"route": "a"}

            @get("/b")
            async def b(self) -> dict:
                return {"route": "b"}

        client = _client(C)
        ra = client.get("/ctrl/a")
        rb = client.get("/ctrl/b")
        assert ra.status_code == 200
        assert rb.status_code == 200
        # Both encoded with sentinel (starts with {"__s":1,)
        assert ra.body.startswith(b'{"__s":1,')
        assert rb.body.startswith(b'{"__s":1,')

    def test_controller_encoder_wins_over_app_encoder(self):
        @use_encoder(_SENTINEL)
        @controller("/ctrlwin")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"x": 1}

        # App uses stdlib; controller overrides with sentinel
        client = _client(C, app_encoder=_STDLIB)
        r = client.get("/ctrlwin/")
        assert r.body.startswith(b'{"__s":1,')

    def test_routes_without_controller_encoder_use_app_encoder(self):
        @controller("/noenc")
        class C:
            @get("/")
            async def h(self) -> dict:
                return {"x": 1}

        client = _client(C, app_encoder=_STDLIB)
        r = client.get("/noenc/")
        # Normal stdlib output — no sentinel marker
        assert b"__s" not in r.body
        assert _stdlib_json.loads(r.body) == {"x": 1}


# ---------------------------------------------------------------------------
# Route-level: only the decorated route is affected
# ---------------------------------------------------------------------------


class TestRouteLevelEncoder:
    def test_route_encoder_applies_only_to_decorated_route(self):
        @controller("/mixed")
        class C:
            @get("/fast")
            @use_encoder(_SENTINEL)
            async def fast(self) -> dict:
                return {"route": "fast"}

            @get("/normal")
            async def normal(self) -> dict:
                return {"route": "normal"}

        client = _client(C, app_encoder=_STDLIB)
        r_fast = client.get("/mixed/fast")
        r_normal = client.get("/mixed/normal")

        assert r_fast.body.startswith(b'{"__s":1,')  # sentinel used
        assert b"__s" not in r_normal.body  # stdlib used
        assert _stdlib_json.loads(r_normal.body) == {"route": "normal"}

    def test_method_encoder_wins_over_controller_encoder(self):
        method_enc = _SentinelEncoder()
        ctrl_enc = StdlibJSONEncoder()

        @use_encoder(ctrl_enc)
        @controller("/priority")
        class C:
            @get("/override")
            @use_encoder(method_enc)
            async def override(self) -> dict:
                return {"x": 1}

            @get("/default")
            async def default(self) -> dict:
                return {"x": 2}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)

        r_override = client.get("/priority/override")
        r_default = client.get("/priority/default")

        # override used method_enc (sentinel), default used ctrl_enc (stdlib)
        assert r_override.body.startswith(b'{"__s":1,')
        assert b"__s" not in r_default.body

    def test_three_encoders_three_routes(self):
        enc_a = _SentinelEncoder()

        @controller("/three")
        class C:
            @get("/a")
            @use_encoder(enc_a)
            async def a(self) -> dict:
                return {"r": "a"}

            @get("/b")
            async def b(self) -> dict:
                return {"r": "b"}

        client = _client(C, app_encoder=_STDLIB)
        assert client.get("/three/a").body.startswith(b'{"__s":1,')
        assert b"__s" not in client.get("/three/b").body


# ---------------------------------------------------------------------------
# Real encoders end-to-end
# ---------------------------------------------------------------------------


class TestRealEncoders:
    def test_orjson_route_encoder(self):
        pytest.importorskip("orjson")

        @controller("/orjson")
        class C:
            @get("/")
            @use_encoder(OrjsonEncoder())
            async def h(self) -> dict:
                return {"encoder": "orjson"}

        r = _client(C, app_encoder=_STDLIB).get("/orjson/")
        assert r.status_code == 200
        assert _stdlib_json.loads(r.body) == {"encoder": "orjson"}

    def test_msgspec_route_encoder(self):
        pytest.importorskip("msgspec")

        @controller("/msgspec")
        class C:
            @get("/")
            @use_encoder(MsgspecEncoder())
            async def h(self) -> dict:
                return {"encoder": "msgspec"}

        r = _client(C, app_encoder=_STDLIB).get("/msgspec/")
        assert r.status_code == 200
        assert _stdlib_json.loads(r.body) == {"encoder": "msgspec"}

    def test_pydantic_route_encoder_honours_field_serializer(self):
        from pydantic import field_serializer

        class Score(BaseModel):
            value: float

            @field_serializer("value")
            def fmt(self, v: float) -> str:
                return f"{v:.2f}"

        @controller("/pydantic")
        class C:
            @get("/")
            @use_encoder(PydanticEncoder())
            async def h(self) -> Score:
                return Score(value=3.14159)

        r = _client(C, app_encoder=_STDLIB).get("/pydantic/")
        assert r.status_code == 200
        assert _stdlib_json.loads(r.body) == {"value": "3.14"}


# ---------------------------------------------------------------------------
# Error responses use the route encoder
# ---------------------------------------------------------------------------


class TestErrorResponseUsesRouteEncoder:
    def test_http_error_uses_route_encoder(self):
        @controller("/erroute")
        class C:
            @get("/")
            @use_encoder(_SENTINEL)
            async def h(self) -> dict:
                raise ForbiddenError("denied")

        r = _client(C).get("/erroute/")
        assert r.status_code == 403
        assert r.body.startswith(b'{"__s":1,')

    def test_error_on_controller_level_uses_controller_encoder(self):
        @use_encoder(_SENTINEL)
        @controller("/erctrl")
        class C:
            @get("/")
            async def h(self) -> dict:
                raise ForbiddenError("denied")

        r = _client(C).get("/erctrl/")
        assert r.status_code == 403
        assert r.body.startswith(b'{"__s":1,')

    def test_route_not_found_uses_app_encoder_not_route(self):
        """404 for unknown path uses app encoder (compiled not available yet)."""

        @controller("/notfound")
        class C:
            @get("/exists")
            async def h(self) -> dict:
                return {}

        r = _client(C, app_encoder=_STDLIB).get("/notfound/missing")
        assert r.status_code == 404
        assert b"__s" not in r.body  # app encoder used, not sentinel


# ---------------------------------------------------------------------------
# EventStream uses the route encoder
# ---------------------------------------------------------------------------


class TestEventStreamUsesRouteEncoder:
    def test_event_stream_uses_route_encoder(self):
        @controller("/sse")
        class C:
            @get("/")
            @use_encoder(_SENTINEL)
            async def h(self) -> EventStream:
                async def gen() -> AsyncIterator[ServerSentEvent]:
                    yield ServerSentEvent(data={"key": "value"})

                return EventStream(gen())

        r = _client(C).get("/sse/", headers=[("accept", "text/event-stream")])
        assert r.status_code == 200
        # The sentinel encoder wraps dicts with {"__s":1, ...}
        assert b"__s" in r.body


# ---------------------------------------------------------------------------
# Compiled handler introspection
# ---------------------------------------------------------------------------


class TestCompiledHandlerEncoder:
    def test_compiled_encoder_field_set_for_route(self):
        enc = _SentinelEncoder()

        @controller("/inspect")
        class C:
            @get("/enc")
            @use_encoder(enc)
            async def with_enc(self) -> dict:
                return {}

            @get("/no-enc")
            async def without_enc(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        handlers = app._handlers

        enc_key = ("GET", "/inspect/enc")
        no_enc_key = ("GET", "/inspect/no-enc")

        assert handlers[enc_key].encoder is enc
        assert handlers[no_enc_key].encoder is None

    def test_ctrl_encoder_propagated_to_all_routes(self):
        enc = _SentinelEncoder()

        @use_encoder(enc)
        @controller("/ctrl-inspect")
        class C:
            @get("/a")
            async def a(self) -> dict:
                return {}

            @get("/b")
            async def b(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        handlers = app._handlers
        assert handlers[("GET", "/ctrl-inspect/a")].encoder is enc
        assert handlers[("GET", "/ctrl-inspect/b")].encoder is enc

    def test_method_encoder_overrides_ctrl_encoder_in_compiled(self):
        ctrl_enc = StdlibJSONEncoder()
        method_enc = _SentinelEncoder()

        @use_encoder(ctrl_enc)
        @controller("/both")
        class C:
            @get("/override")
            @use_encoder(method_enc)
            async def override(self) -> dict:
                return {}

            @get("/inherit")
            async def inherit(self) -> dict:
                return {}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        handlers = app._handlers
        assert handlers[("GET", "/both/override")].encoder is method_enc
        assert handlers[("GET", "/both/inherit")].encoder is ctrl_enc
