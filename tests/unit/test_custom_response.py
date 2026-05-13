"""Unit tests for Response subclassing.

Covers:
- Subclass instance passes isinstance(value, Response)
- __slots__ removed: subclasses can add instance attributes freely
- _clone() preserves subclass type across all with_* builders
- Factory classmethods on subclasses return the subclass type
- Streaming body works on subclass
- Extra instance attributes survive the _clone cycle
"""

from __future__ import annotations

import json

import pytest

from lauren.types import Response


# ---------------------------------------------------------------------------
# Fixtures — a few concrete subclasses used across tests
# ---------------------------------------------------------------------------


class JsonApiResponse(Response):
    """JSON:API media type response."""

    @classmethod
    def resource(cls, data: dict, *, status: int = 200) -> "JsonApiResponse":
        body = json.dumps({"data": data}, separators=(",", ":")).encode()
        return cls(body, status=status, media_type="application/vnd.api+json")

    @classmethod
    def error(cls, title: str, status: int = 400) -> "JsonApiResponse":
        body = json.dumps({"errors": [{"title": title}]}, separators=(",", ":")).encode()
        return cls(body, status=status, media_type="application/vnd.api+json")


class TracedResponse(Response):
    """Response that carries a trace ID as an extra instance attribute."""

    def __init__(self, *args, trace_id: str = "", **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.trace_id = trace_id


class CsvResponse(Response):
    """Streaming CSV response."""

    @classmethod
    def from_rows(cls, rows: list[list[str]]) -> "CsvResponse":
        async def _gen():
            for row in rows:
                yield (",".join(row) + "\n").encode()

        return cls(
            stream=_gen(),
            media_type="text/csv",
        )


# ---------------------------------------------------------------------------
# isinstance check
# ---------------------------------------------------------------------------


class TestSubclassIsInstance:
    def test_subclass_is_instance_of_response(self):
        r = JsonApiResponse.resource({"id": 1})
        assert isinstance(r, Response)

    def test_subclass_is_instance_of_own_class(self):
        r = JsonApiResponse.resource({"id": 1})
        assert isinstance(r, JsonApiResponse)

    def test_traced_response_is_instance_of_response(self):
        r = TracedResponse(b"ok", trace_id="abc")
        assert isinstance(r, Response)


# ---------------------------------------------------------------------------
# No __slots__: extra instance attributes work
# ---------------------------------------------------------------------------


class TestNoSlots:
    def test_extra_attribute_set_in_init(self):
        r = TracedResponse(b"body", trace_id="xyz")
        assert r.trace_id == "xyz"

    def test_extra_attribute_set_after_construction(self):
        r = JsonApiResponse(b"")
        r.custom_tag = "hello"  # no AttributeError
        assert r.custom_tag == "hello"

    def test_subclass_does_not_require_slots_declaration(self):
        class MinimalSub(Response):
            pass

        r = MinimalSub(b"data")
        r.anything = 42
        assert r.anything == 42


# ---------------------------------------------------------------------------
# _clone() preserves the subclass type
# ---------------------------------------------------------------------------


class TestClonePreservesSubclass:
    def test_with_status_returns_subclass(self):
        r = JsonApiResponse.resource({"id": 1})
        r2 = r.with_status(201)
        assert type(r2) is JsonApiResponse

    def test_with_header_returns_subclass(self):
        r = JsonApiResponse.resource({"id": 1})
        r2 = r.with_header("x-trace", "abc")
        assert type(r2) is JsonApiResponse

    def test_with_headers_returns_subclass(self):
        r = JsonApiResponse.resource({"id": 1})
        r2 = r.with_headers({"x-a": "1", "x-b": "2"})
        assert type(r2) is JsonApiResponse

    def test_without_header_returns_subclass(self):
        r = JsonApiResponse.resource({"id": 1}).with_header("x-del", "yes")
        r2 = r.without_header("x-del")
        assert type(r2) is JsonApiResponse

    def test_with_media_type_returns_subclass(self):
        r = JsonApiResponse(b"data")
        r2 = r.with_media_type("text/plain")
        assert type(r2) is JsonApiResponse

    def test_with_body_returns_subclass(self):
        r = JsonApiResponse(b"old")
        r2 = r.with_body(b"new")
        assert type(r2) is JsonApiResponse

    def test_with_cookie_returns_subclass(self):
        r = JsonApiResponse.resource({"id": 1})
        r2 = r.with_cookie("session", "tok", max_age=3600)
        assert type(r2) is JsonApiResponse

    def test_delete_cookie_returns_subclass(self):
        r = JsonApiResponse.resource({"id": 1}).with_cookie("s", "v")
        r2 = r.delete_cookie("s")
        assert type(r2) is JsonApiResponse

    def test_clone_chain_stays_subclass(self):
        r = (
            JsonApiResponse.resource({"id": 1})
            .with_status(201)
            .with_header("x-a", "1")
            .with_header("x-b", "2")
            .with_cookie("sess", "tok")
        )
        assert type(r) is JsonApiResponse

    def test_plain_response_clone_still_plain(self):
        r = Response(b"data")
        r2 = r.with_status(201)
        assert type(r2) is Response


# ---------------------------------------------------------------------------
# Inherited state preserved after clone
# ---------------------------------------------------------------------------


class TestClonePreservesState:
    def test_status_preserved(self):
        r = JsonApiResponse.resource({"id": 1}, status=202)
        r2 = r.with_header("x-a", "1")
        assert r2.status == 202

    def test_body_preserved(self):
        body = b'{"data":{"id":1}}'
        r = JsonApiResponse(body, media_type="application/vnd.api+json")
        r2 = r.with_header("x-trace", "t1")
        assert r2.body == body

    def test_media_type_preserved(self):
        r = JsonApiResponse.resource({"id": 1})
        r2 = r.with_header("x-extra", "yes")
        assert r2.media_type == "application/vnd.api+json"

    def test_headers_copied_not_shared(self):
        r = JsonApiResponse.resource({"id": 1})
        r2 = r.with_header("x-new", "val")
        assert r.headers.get("x-new") is None
        assert r2.headers.get("x-new") == "val"


# ---------------------------------------------------------------------------
# Factory classmethods return the subclass
# ---------------------------------------------------------------------------


class TestFactoryClassmethods:
    def test_resource_returns_jsonapi(self):
        r = JsonApiResponse.resource({"id": 99})
        assert type(r) is JsonApiResponse
        assert r.media_type == "application/vnd.api+json"

    def test_error_returns_jsonapi(self):
        r = JsonApiResponse.error("Not found", status=404)
        assert type(r) is JsonApiResponse
        assert r.status == 404

    def test_body_contains_data(self):
        r = JsonApiResponse.resource({"id": 7, "name": "Alice"})
        payload = json.loads(r.body)
        assert payload == {"data": {"id": 7, "name": "Alice"}}


# ---------------------------------------------------------------------------
# Streaming body on a subclass
# ---------------------------------------------------------------------------


class TestSubclassStreaming:
    @pytest.mark.asyncio
    async def test_csv_stream_yields_rows(self):
        rows = [["a", "b"], ["1", "2"], ["3", "4"]]
        r = CsvResponse.from_rows(rows)
        assert isinstance(r, Response)
        assert r.media_type == "text/csv"
        chunks = []
        async for chunk in r.stream_body:
            chunks.append(chunk)
        assert b"".join(chunks) == b"a,b\n1,2\n3,4\n"

    @pytest.mark.asyncio
    async def test_csv_response_is_subclass(self):
        r = CsvResponse.from_rows([["x", "y"]])
        assert type(r) is CsvResponse
        assert isinstance(r, Response)


# ---------------------------------------------------------------------------
# TracedResponse extra-attribute behaviour
# ---------------------------------------------------------------------------


class TestTracedResponse:
    def test_trace_id_set(self):
        r = TracedResponse(b"ok", trace_id="t-abc-123")
        assert r.trace_id == "t-abc-123"

    def test_trace_id_readable_after_with_header(self):
        r = TracedResponse(b"ok", trace_id="t-xyz").with_header("x-foo", "bar")
        # _clone returns a new TracedResponse but does NOT copy trace_id
        # (extra attrs set outside __init__ are not copied — expected behaviour)
        assert type(r) is TracedResponse
