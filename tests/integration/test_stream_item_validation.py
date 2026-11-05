"""Integration: inbound ``Stream[T]`` items use the app's compiled validator (``item`` loc root)."""

from __future__ import annotations

import dataclasses
import json
from typing import Any, AsyncIterator

import pytest

from lauren import LaurenFactory, Stream, StreamingResponse, controller, module, post
from lauren.testing import TestClient
from lauren.validation import ValidationPolicy


@dataclasses.dataclass
class Chunk:
    seq: int
    text: str


@controller("/s")
class StreamEndpoints:
    @post("/echo")
    async def echo(self, chunks: Stream[Chunk]) -> StreamingResponse[Chunk]:
        async def produce() -> AsyncIterator[Chunk]:
            async for chunk in chunks:
                yield Chunk(seq=chunk.seq * 10, text=chunk.text)

        return produce()

    @post("/types")
    async def types(self, chunks: Stream[Chunk]) -> dict:
        return {"types": [type(c.seq).__name__ async for c in chunks]}


@module(controllers=[StreamEndpoints])
class StreamModule:
    pass


NDJSON = {"content-type": "application/x-ndjson"}


def _client(mode: str = "enforce") -> TestClient:
    return TestClient(LaurenFactory.create(StreamModule, validation=ValidationPolicy(stdlib_models=mode)))


def test_items_are_validated_and_coerced_under_enforce() -> None:
    response = _client().post(
        "/s/types", content=b'{"seq": "1", "text": "a"}\n{"seq": 2, "text": "b"}\n', headers=NDJSON
    )

    assert response.json() == {"types": ["int", "int"]}


def test_a_bad_first_item_is_a_422_rooted_at_its_index() -> None:
    response = _client().post("/s/echo", content=b'{"seq": "x"}\n', headers=NDJSON)

    error = response.json()["error"]
    assert response.status_code == 422
    assert error["message"] == "validation error in streaming body"
    assert error["detail"]["field"] == "chunks"
    assert error["detail"]["format"] == "ndjson"
    assert [(i["loc"], i["type"]) for i in error["detail"]["issues"]] == [
        (["item", 0, "seq"], "int_parsing"),
        (["item", 0, "text"], "missing"),
    ]


def test_a_bad_later_item_reports_its_own_index() -> None:
    response = _client().post(
        "/s/echo", content=b'{"seq": 1, "text": "a"}\n{"seq": "x", "text": "b"}\n', headers=NDJSON
    )

    lines = [json.loads(line) for line in response.text.splitlines() if line.strip()]
    assert lines[0] == {"seq": 10, "text": "a"}
    assert lines[-1]["error"]["detail"]["issues"][0]["loc"] == ["item", 1, "seq"]


@pytest.mark.parametrize("mode", ["legacy", "warn"])
def test_legacy_and_warn_keep_the_pre_engine_item_value(mode: str) -> None:
    pytest.importorskip("pydantic")  # the pre-engine adapter is pydantic's TypeAdapter when installed

    response = _client(mode).post("/s/types", content=b'{"seq": "1", "text": "a"}\n', headers=NDJSON)

    assert response.json() == {"types": ["int"]}


def test_streamed_responses_serialize_through_the_startup_adapter() -> None:
    app = LaurenFactory.create(StreamModule)
    handler: Any = app._handlers[("POST", "/s/echo")]

    assert handler.streaming_item_type is Chunk
    response = TestClient(app).post("/s/echo", content=b'{"seq": 1, "text": "a"}\n', headers=NDJSON)
    assert response.text.strip() == '{"seq":10,"text":"a"}'
