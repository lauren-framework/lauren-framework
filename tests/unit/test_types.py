"""Unit tests for Request, Response, State, Headers."""

from __future__ import annotations

import asyncio
import datetime
import decimal
import pathlib
import uuid
from dataclasses import dataclass as _dataclass

import pytest

from lauren.exceptions import MissingStateError, RequestBodyTooLarge, StateTypeError
from lauren.types import (
    AppState,
    ClientInfo,
    Headers,
    MutableHeaders,
    Request,
    Response,
    ServerInfo,
    State,
)


class TestHeaders:
    def test_case_insensitive_get(self):
        h = Headers([("Content-Type", "application/json")])
        assert h["content-type"] == "application/json"
        assert h["CONTENT-TYPE"] == "application/json"

    def test_multi_value(self):
        h = Headers([("set-cookie", "a=1"), ("set-cookie", "b=2")])
        assert h.getall("set-cookie") == ["a=1", "b=2"]

    def test_contains(self):
        h = Headers([("X-A", "1")])
        assert "x-a" in h
        assert "x-b" not in h

    def test_mutable_set_replaces(self):
        h = MutableHeaders([("x", "1"), ("x", "2")])
        h.set("x", "3")
        assert h.getall("x") == ["3"]

    def test_mutable_append(self):
        h = MutableHeaders()
        h.append("cookie", "a=1")
        h.append("cookie", "b=2")
        assert len(h.getall("cookie")) == 2

    def test_mutable_delete(self):
        h = MutableHeaders([("x", "1"), ("y", "2")])
        h.delete("x")
        assert "x" not in h


class TestState:
    def test_set_and_get(self):
        s = State()
        s.user = "alice"
        assert s.user == "alice"

    def test_get_typed_ok(self):
        s = State()
        s.count = 5
        assert s.get_typed("count", int) == 5

    def test_get_typed_wrong_type(self):
        s = State()
        s.count = "five"
        with pytest.raises(StateTypeError):
            s.get_typed("count", int)

    def test_require_missing(self):
        s = State()
        with pytest.raises(MissingStateError):
            s.require("missing", str)

    def test_has(self):
        s = State()
        s.x = 1
        assert s.has("x")
        assert not s.has("y")


class TestAppState:
    def test_read_write_before_seal(self):
        a = AppState()
        a.db = "pg"
        assert a.db == "pg"

    def test_seal_prevents_writes(self):
        a = AppState()
        a.db = "pg"
        a.seal()
        with pytest.raises(RuntimeError):
            a.db = "new"


class TestRequest:
    @pytest.mark.asyncio
    async def test_basic_properties(self):
        req = Request(
            method="GET",
            path="/users",
            raw_query_string=b"a=1&b=2",
            headers=Headers([("x", "y")]),
        )
        assert req.method == "GET"
        assert req.path == "/users"
        assert req.query_params == {"a": ["1"], "b": ["2"]}
        assert req.headers["x"] == "y"

    @pytest.mark.asyncio
    async def test_body_consumption(self):
        async def receive():
            return {"type": "http.request", "body": b"hello", "more_body": False}

        req = Request(method="POST", path="/", receive=receive)
        body = await req.body()
        assert body == b"hello"
        # Second call returns cached
        assert await req.body() == b"hello"

    @pytest.mark.asyncio
    async def test_body_too_large(self):
        async def receive():
            return {"type": "http.request", "body": b"x" * 100, "more_body": False}

        req = Request(method="POST", path="/", receive=receive, max_body_size=10)
        with pytest.raises(RequestBodyTooLarge):
            await req.body()

    @pytest.mark.asyncio
    async def test_json_body(self):
        async def receive():
            return {"type": "http.request", "body": b'{"k":1}', "more_body": False}

        req = Request(method="POST", path="/", receive=receive)
        assert await req.json() == {"k": 1}

    def test_cookies_parsed(self):
        req = Request(
            method="GET",
            path="/",
            headers=Headers([("cookie", "a=1; b=2")]),
        )
        assert req.cookies == {"a": "1", "b": "2"}


class TestResponse:
    def test_json_factory(self):
        r = Response.json({"k": "v"})
        assert r.status == 200
        assert r.body == b'{"k":"v"}'
        assert r.headers["content-type"] == "application/json"

    def test_text_factory(self):
        r = Response.text("hello")
        assert r.body == b"hello"
        assert "text/plain" in r.headers["content-type"]

    def test_html_factory(self):
        r = Response.html("<h1>hi</h1>")
        assert "text/html" in r.headers["content-type"]

    def test_no_content(self):
        r = Response.no_content()
        assert r.status == 204

    def test_created(self):
        r = Response.created({"id": 1}, location="/x/1")
        assert r.status == 201
        assert r.headers["location"] == "/x/1"

    def test_redirect(self):
        r = Response.redirect("/login", status=302)
        assert r.status == 302
        assert r.headers["location"] == "/login"

    def test_with_status_is_immutable(self):
        r = Response.json({})
        r2 = r.with_status(201)
        assert r.status == 200
        assert r2.status == 201

    def test_with_header(self):
        r = Response.json({}).with_header("x-trace", "abc")
        assert r.headers["x-trace"] == "abc"

    def test_with_cookie(self):
        r = Response.json({}).with_cookie("session", "abc", max_age=3600, http_only=True, secure=True)
        cookie = r.headers["set-cookie"]
        assert "session=abc" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie

    def test_delete_cookie(self):
        r = Response.json({}).delete_cookie("session")
        assert "Max-Age=0" in r.headers["set-cookie"]

    def test_pydantic_serialization(self):
        from pydantic import BaseModel

        class M(BaseModel):
            a: int = 1

        r = Response.json(M())
        import json

        assert json.loads(r.body) == {"a": 1}


# ---------------------------------------------------------------------------
# Additional coverage for types.py
# ---------------------------------------------------------------------------


class TestHeadersEdgeCases:
    def test_len_counts_unique_keys(self):
        h = Headers([("a", "1"), ("b", "2"), ("a", "3")])
        # unique keys: a, b → 2
        assert len(h) == 2

    def test_mutable_copy_returns_new_instance(self):
        h = Headers([("x", "1")])
        mc = h.mutable_copy()
        assert isinstance(mc, MutableHeaders)
        mc.set("x", "2")
        # Original unchanged
        assert h["x"] == "1"

    def test_non_string_contains_returns_false(self):
        h = Headers([("x", "1")])
        assert 42 not in h


class TestStateEdgeCases:
    def test_getattr_missing_raises(self):
        s = State()
        with pytest.raises(AttributeError):
            _ = s.missing_key

    def test_require_wrong_type_raises(self):
        s = State({"n": "hello"})
        with pytest.raises(StateTypeError):
            s.require("n", int)

    def test_asdict_returns_copy(self):
        s = State({"a": 1, "b": 2})
        d = s.asdict()
        assert d == {"a": 1, "b": 2}
        d["a"] = 99
        assert s.get("a") == 1  # original unchanged


class TestAppStateEdgeCases:
    def test_setattr_after_seal_raises(self):
        app_state = AppState()
        app_state.seal()
        with pytest.raises(RuntimeError, match="sealed"):
            app_state.x = "value"

    def test_set_after_seal_raises(self):
        app_state = AppState()
        app_state.seal()
        with pytest.raises(RuntimeError, match="sealed"):
            app_state.set("x", "value")


class TestRequestEdgeCases:
    def _make_request(self, body: bytes = b"", query: bytes = b"") -> Request:
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}

        return Request(
            method="GET",
            path="/test",
            raw_query_string=query,
            client=ClientInfo("127.0.0.1", 1234),
            server=ServerInfo("localhost", 8000),
            receive=receive,
        )

    def test_url_with_query_string(self):
        req = self._make_request(query=b"a=1&b=2")
        assert req.url == "/test?a=1&b=2"

    def test_url_without_query_string(self):
        req = self._make_request()
        assert req.url == "/test"

    def test_client_info(self):
        req = self._make_request()
        assert req.client.host == "127.0.0.1"
        assert req.client.port == 1234

    def test_server_info(self):
        req = self._make_request()
        assert req.server.host == "localhost"
        assert req.server.port == 8000

    def test_text_method(self):
        req = self._make_request(body=b"hello world")

        async def run():
            return await req.text()

        result = asyncio.run(run())
        assert result == "hello world"

    def test_json_method(self):
        req = self._make_request(body=b'{"x": 42}')

        async def run():
            return await req.json()

        result = asyncio.run(run())
        assert result == {"x": 42}

    def test_json_method_empty_returns_none(self):
        req = self._make_request(body=b"")

        async def run():
            return await req.json()

        result = asyncio.run(run())
        assert result is None

    def test_stream_method(self):
        req = self._make_request(body=b"chunk data")

        async def run():
            chunks = []
            async for chunk in req.stream():
                chunks.append(chunk)
            return b"".join(chunks)

        result = asyncio.run(run())
        assert result == b"chunk data"

    def test_stream_with_cached_body(self):
        req = self._make_request(body=b"cached")

        async def run():
            # First consume via body()
            _ = await req.body()
            # Then stream should yield the cached body
            chunks = []
            async for chunk in req.stream():
                chunks.append(chunk)
            return b"".join(chunks)

        result = asyncio.run(run())
        assert result == b"cached"

    def test_handler_metadata_accessors(self):
        req = self._make_request()
        assert req.get_handler_class() is None
        assert req.get_route_handler_func() is None
        assert req.get_route_template() is None
        assert req.get_matched_route() is None

    def test_request_reset(self):
        req = self._make_request()

        async def new_receive():
            return {"type": "http.request", "body": b"new", "more_body": False}

        req.reset(
            method="POST",
            path="/new",
            raw_query_string=b"x=1",
            headers=Headers([("x", "y")]),
            client=ClientInfo("10.0.0.1", 80),
            server=ServerInfo("example.com", 443),
            receive=new_receive,
            app_state=AppState(),
            max_body_size=1024,
        )
        assert req.method == "POST"
        assert req.path == "/new"


class TestResponseEdgeCases:
    def test_body_none_becomes_empty_bytes(self):
        r = Response(body=None)
        assert r.body == b""

    def test_status_code_alias(self):
        r = Response(status=201)
        assert r.status_code == 201

    def test_stream_body_property(self):
        async def gen():
            yield b"data"

        r = Response.stream(gen())
        assert r.stream_body is not None

    def test_response_empty_factory(self):
        r = Response.empty(204)
        assert r.status == 204
        assert r.body == b""

    def test_response_no_content_factory(self):
        r = Response.no_content()
        assert r.status == 204

    def test_response_created_with_location(self):
        r = Response.created({"id": 1}, location="/items/1")
        assert r.status == 201
        assert r.headers.get("location") == "/items/1"

    def test_response_accepted_no_data(self):
        r = Response.accepted()
        assert r.status == 202
        assert r.body == b""

    def test_response_redirect(self):
        r = Response.redirect("/home")
        assert r.status == 307
        assert r.headers.get("location") == "/home"

    def test_response_without_header(self):
        r = Response.json({}, headers=Headers([("x-custom", "value")]))
        r2 = r.without_header("x-custom")
        assert r2.headers.get("x-custom") is None

    def test_response_with_media_type(self):
        r = Response(b"data")
        r2 = r.with_media_type("text/plain")
        assert r2.media_type == "text/plain"

    def test_response_with_body_str(self):
        r = Response(b"")
        r2 = r.with_body("hello world")
        assert r2.body == b"hello world"

    def test_response_with_cookie_domain_samesite(self):
        r = Response(b"")
        r2 = r.with_cookie(
            "session",
            "abc",
            domain="example.com",
            same_site="Strict",
        )
        cookie = r2.headers.get("set-cookie")
        assert "Domain=example.com" in cookie
        assert "SameSite=Strict" in cookie


class TestJsonDefault:
    def test_timedelta(self):
        from lauren.types import _json_default

        td = datetime.timedelta(seconds=90)
        assert _json_default(td) == 90.0

    def test_uuid(self):
        from lauren.types import _json_default

        u = uuid.UUID("12345678-1234-5678-1234-567812345678")
        assert _json_default(u) == "12345678-1234-5678-1234-567812345678"

    def test_purepath(self):
        from lauren.types import _json_default

        p = pathlib.PurePosixPath("/a/b/c")
        assert _json_default(p) == "/a/b/c"

    def test_decimal(self):
        from lauren.types import _json_default

        d = decimal.Decimal("3.14")
        assert _json_default(d) == "3.14"

    def test_set(self):
        from lauren.types import _json_default

        s = frozenset([1, 2, 3])
        result = _json_default(s)
        assert sorted(result) == [1, 2, 3]

    def test_dataclass_instance(self):
        from lauren.types import _json_default

        @_dataclass
        class Point:
            x: int
            y: int

        result = _json_default(Point(1, 2))
        assert result == {"x": 1, "y": 2}

    def test_object_with_dict(self):
        from lauren.types import _json_default

        class Obj:
            def __init__(self):
                self.public = "value"
                self._private = "hidden"

        result = _json_default(Obj())
        assert result == {"public": "value"}

    def test_unknown_type_raises(self):
        from lauren.types import _json_default

        # Use a __slots__ class that has no __dict__, no model_dump, no enum, etc.
        class _Slotted:
            __slots__ = ("_x",)

            def __init__(self):
                self._x = 1

        with pytest.raises(TypeError, match="not JSON serializable"):
            _json_default(_Slotted())
