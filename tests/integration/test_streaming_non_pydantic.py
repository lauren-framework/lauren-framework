"""Regression tests: StreamingResponse[T] where T is a non-Pydantic type.

Before the fix, using MsgspecEncoder or OrjsonEncoder with a
``StreamingResponse[Greeting]`` where ``Greeting`` is a ``msgspec.Struct``
raised ``PydanticSchemaError`` because ``_build_adapter`` blindly called
``TypeAdapter(msgspec_type)`` without catching the resulting schema error.

These tests also verify that _build_adapter still works correctly for plain
Pydantic models (regression guard).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import pytest

from lauren import LaurenFactory, controller, get, module
from lauren.serialization import MsgspecEncoder, OrjsonEncoder
from lauren.streaming import StreamingResponse, _build_adapter
from lauren.testing import TestClient

# ---------------------------------------------------------------------------
# Optional-import guards
# ---------------------------------------------------------------------------

try:
    import msgspec

    HAS_MSGSPEC = True
except ImportError:
    HAS_MSGSPEC = False

try:
    import orjson  # noqa: F401

    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


# ---------------------------------------------------------------------------
# _build_adapter unit tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
class TestBuildAdapterNonPydantic:
    """_build_adapter returns None (not raises) for non-Pydantic types."""

    def test_msgspec_struct_returns_none(self) -> None:
        class Greet(msgspec.Struct):
            message: str

        # Must not raise PydanticSchemaError
        result = _build_adapter(Greet)
        assert result is None

    def test_msgspec_struct_cached_as_none(self) -> None:
        class Greet2(msgspec.Struct):
            value: int

        _build_adapter(Greet2)  # populate cache
        result = _build_adapter(Greet2)  # hit cache
        assert result is None

    def test_plain_pydantic_model_still_works(self) -> None:
        from pydantic import BaseModel

        class PydanticItem(BaseModel):
            x: int

        adapter = _build_adapter(PydanticItem)
        # Should return a real TypeAdapter, not None
        assert adapter is not None
        dumped = adapter.dump_python(PydanticItem(x=42), mode="json")
        assert dumped == {"x": 42}


# ---------------------------------------------------------------------------
# MsgspecEncoder + StreamingResponse[msgspec.Struct]
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
class TestMsgspecEncoderStreamingResponse:
    """MsgspecEncoder handles StreamingResponse[msgspec.Struct] correctly."""

    def _make_client(self) -> TestClient:
        class Greeting(msgspec.Struct):
            message: str
            count: int

        @controller("/greet")
        class GreetController:
            @get("/stream")
            async def stream(self) -> StreamingResponse[Greeting]:
                async def _gen() -> AsyncIterator[Greeting]:
                    for i in range(3):
                        yield Greeting(message=f"hello {i}", count=i)

                return _gen()

        @module(controllers=[GreetController])
        class AppModule:
            pass

        encoder = MsgspecEncoder()
        app = LaurenFactory.create(AppModule, json_encoder=encoder)
        return TestClient(app)

    def test_returns_200(self) -> None:
        r = self._make_client().get("/greet/stream")
        assert r.status_code == 200

    def test_ndjson_body_is_valid(self) -> None:
        r = self._make_client().get("/greet/stream", headers={"Accept": "application/x-ndjson"})
        assert r.status_code == 200
        lines = [line for line in r.text.strip().split("\n") if line]
        assert len(lines) == 3
        for i, line in enumerate(lines):
            obj = json.loads(line)
            assert obj == {"message": f"hello {i}", "count": i}

    def test_sse_body_is_valid(self) -> None:
        r = self._make_client().get("/greet/stream", headers={"Accept": "text/event-stream"})
        assert r.status_code == 200
        data_lines = [line[len("data: ") :] for line in r.text.split("\n") if line.startswith("data: ")]
        assert len(data_lines) == 3
        for i, data in enumerate(data_lines):
            obj = json.loads(data)
            assert obj == {"message": f"hello {i}", "count": i}


# ---------------------------------------------------------------------------
# OrjsonEncoder + StreamingResponse[msgspec.Struct]
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
@pytest.mark.skipif(not HAS_ORJSON, reason="orjson not installed")
class TestOrjsonEncoderStreamingResponse:
    """OrjsonEncoder handles StreamingResponse[msgspec.Struct] correctly."""

    def _make_client(self) -> TestClient:
        class Item(msgspec.Struct):
            name: str
            value: float

        @controller("/items")
        class ItemController:
            @get("/stream")
            async def stream(self) -> StreamingResponse[Item]:
                async def _gen() -> AsyncIterator[Item]:
                    yield Item(name="alpha", value=1.5)
                    yield Item(name="beta", value=2.5)

                return _gen()

        @module(controllers=[ItemController])
        class AppModule:
            pass

        encoder = OrjsonEncoder()
        app = LaurenFactory.create(AppModule, json_encoder=encoder)
        return TestClient(app)

    def test_returns_200(self) -> None:
        r = self._make_client().get("/items/stream")
        assert r.status_code == 200

    def test_ndjson_body_is_valid(self) -> None:
        r = self._make_client().get("/items/stream", headers={"Accept": "application/x-ndjson"})
        assert r.status_code == 200
        lines = [line for line in r.text.strip().split("\n") if line]
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"name": "alpha", "value": 1.5}
        assert json.loads(lines[1]) == {"name": "beta", "value": 2.5}


# ---------------------------------------------------------------------------
# Pydantic models still work with all three encoders (regression guard)
# ---------------------------------------------------------------------------


class TestPydanticModelStreamingRegressions:
    """Pydantic StreamingResponse still works after the _build_adapter fix."""

    def _make_pydantic_client(self, encoder=None) -> TestClient:  # type: ignore[assignment]
        from pydantic import BaseModel

        class PItem(BaseModel):
            label: str
            score: int

        @controller("/p")
        class PController:
            @get("/stream")
            async def stream(self) -> StreamingResponse[PItem]:
                async def _gen() -> AsyncIterator[PItem]:
                    yield PItem(label="first", score=10)
                    yield PItem(label="second", score=20)

                return _gen()

        @module(controllers=[PController])
        class AppModule:
            pass

        kwargs = {"json_encoder": encoder} if encoder is not None else {}
        return TestClient(LaurenFactory.create(AppModule, **kwargs))

    def test_stdlib_encoder(self) -> None:
        r = self._make_pydantic_client().get("/p/stream", headers={"Accept": "application/x-ndjson"})
        assert r.status_code == 200
        lines = [line for line in r.text.strip().split("\n") if line]
        assert json.loads(lines[0]) == {"label": "first", "score": 10}

    @pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
    def test_msgspec_encoder_with_pydantic_items(self) -> None:
        r = self._make_pydantic_client(MsgspecEncoder()).get(
            "/p/stream", headers={"Accept": "application/x-ndjson"}
        )
        assert r.status_code == 200
        lines = [line for line in r.text.strip().split("\n") if line]
        assert json.loads(lines[0]) == {"label": "first", "score": 10}

    @pytest.mark.skipif(not HAS_ORJSON, reason="orjson not installed")
    def test_orjson_encoder_with_pydantic_items(self) -> None:
        r = self._make_pydantic_client(OrjsonEncoder()).get(
            "/p/stream", headers={"Accept": "application/x-ndjson"}
        )
        assert r.status_code == 200
        lines = [line for line in r.text.strip().split("\n") if line]
        assert json.loads(lines[0]) == {"label": "first", "score": 10}
