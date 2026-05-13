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


# ---------------------------------------------------------------------------
# Coverage-gap tests
# ---------------------------------------------------------------------------


class TestStateGetTypedAndRequire:
    """Lines 187, 199-200: get_typed type mismatch and require missing key."""

    def test_get_typed_type_mismatch_raises(self):
        from lauren.exceptions import StateTypeError

        s = State({"user": "alice"})
        with pytest.raises(StateTypeError, match="state\\['user'\\]"):
            s.get_typed("user", int)

    def test_get_typed_none_key_returns_none(self):
        s = State({})
        assert s.get_typed("missing", str) is None

    def test_require_missing_key_raises(self):
        from lauren.exceptions import MissingStateError

        s = State({})
        with pytest.raises(MissingStateError, match="missing"):
            s.require("missing", str)

    def test_require_type_mismatch_raises(self):
        from lauren.exceptions import StateTypeError

        s = State({"n": "hello"})
        with pytest.raises(StateTypeError):
            s.require("n", int)

    def test_require_correct_type_returns_value(self):
        s = State({"count": 42})
        assert s.require("count", int) == 42


class TestAppStateSealed:
    """Lines 227: AppState raises on mutation after seal."""

    def test_setattr_after_seal_raises(self):
        app = AppState()
        app.seal()
        with pytest.raises(RuntimeError, match="sealed"):
            app.x = "value"

    def test_set_after_seal_raises(self):
        app = AppState()
        app.seal()
        with pytest.raises(RuntimeError, match="sealed"):
            app.set("key", "value")

    def test_writes_before_seal_succeed(self):
        app = AppState()
        app.set("key", "value")
        assert app.get("key") == "value"


class TestRequestCookies:
    """Lines 393-402, 438-441: cookies parsing and caching."""

    def _make_request_with_cookie_header(self, cookie: str) -> Request:
        async def recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        return Request(
            method="GET",
            path="/",
            raw_query_string=b"",
            headers=Headers([("cookie", cookie)]),
            path_params=None,
            receive=recv,
        )

    def test_cookies_parsed_from_header(self):
        req = self._make_request_with_cookie_header("a=1; b=2")
        assert req.cookies == {"a": "1", "b": "2"}

    def test_cookies_cached_on_second_access(self):
        req = self._make_request_with_cookie_header("x=42")
        first = req.cookies
        second = req.cookies
        assert first is second

    def test_cookies_empty_when_no_header(self):
        async def recv():
            return {"type": "http.request", "body": b"", "more_body": False}

        req = Request(
            method="GET",
            path="/",
            raw_query_string=b"",
            headers=Headers([]),
            path_params=None,
            receive=recv,
        )
        assert req.cookies == {}

    def test_pair_without_equals_skipped(self):
        req = self._make_request_with_cookie_header("valid=ok; badpair; another=yes")
        cookies = req.cookies
        assert "valid" in cookies
        assert "another" in cookies
        assert "badpair" not in cookies


class TestResponseBuilderMethods:
    """Lines 841-843, 880, 883-890: created, redirect, sse."""

    def test_created_with_data(self):
        r = Response.created({"id": 1}, location="/items/1")
        assert r.status == 201
        assert r.headers.get("location") == "/items/1"

    def test_created_without_data(self):
        r = Response.created()
        assert r.status == 201

    def test_redirect(self):
        r = Response.redirect("/new", status=301)
        assert r.status == 301
        assert r.headers.get("location") == "/new"

    def test_redirect_default_status(self):
        r = Response.redirect("/home")
        assert r.status == 307

    def test_sse_string_event(self):
        import asyncio

        async def gen():
            yield "hello"

        r = Response.sse(gen())
        assert r._stream is not None

        async def collect():
            chunks: list[bytes] = []
            async for chunk in r._stream:
                chunks.append(chunk)
            return chunks

        chunks = asyncio.run(collect())
        assert any(b"data: hello" in c for c in chunks)

    def test_sse_dict_event_with_event_and_id(self):
        import asyncio

        async def gen():
            yield {"event": "update", "id": "1", "data": "payload"}

        r = Response.sse(gen())

        async def collect():
            parts: list[str] = []
            async for chunk in r._stream:
                parts.append(chunk.decode("utf-8"))
            return "".join(parts)

        body = asyncio.run(collect())
        assert "event: update" in body
        assert "id: 1" in body
        assert "data: payload" in body


class TestResponseWithCookieFull:
    """Lines 944-946, 963-965: with_cookie full options."""

    def test_all_cookie_options(self):
        r = Response(b"")
        r2 = r.with_cookie(
            "sess",
            "token123",
            max_age=3600,
            path="/app",
            domain="example.com",
            secure=True,
            http_only=True,
            same_site="Strict",
        )
        cookie = r2.headers.get("set-cookie", "")
        assert "sess=token123" in cookie
        assert "Max-Age=3600" in cookie
        assert "Path=/app" in cookie
        assert "Domain=example.com" in cookie
        assert "Secure" in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=Strict" in cookie


class TestJsonDefaultMsgspecStruct:
    """Line 1023: _json_default handles msgspec.Struct."""

    def test_msgspec_struct_serialized_as_dict(self):
        try:
            import msgspec
        except ImportError:
            pytest.skip("msgspec not installed")

        class Point(msgspec.Struct):
            x: int
            y: int

        from lauren.types import _json_default

        result = _json_default(Point(x=3, y=7))
        assert result == {"x": 3, "y": 7}


# ---------------------------------------------------------------------------
# Response.file() and Response.xml()
# ---------------------------------------------------------------------------


class TestResponseFile:
    """Unit tests for Response.file() async factory."""

    @pytest.mark.asyncio
    async def test_streams_file_contents(self, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_bytes(b"hello world")
        r = await Response.file(str(f))
        chunks = []
        async for chunk in r._stream:
            chunks.append(chunk)
        assert b"".join(chunks) == b"hello world"

    @pytest.mark.asyncio
    async def test_status_200(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00")
        r = await Response.file(f)
        assert r.status == 200

    @pytest.mark.asyncio
    async def test_auto_detects_pdf_mime(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"%PDF")
        r = await Response.file(f)
        assert r.media_type == "application/pdf"

    @pytest.mark.asyncio
    async def test_auto_detects_png_mime(self, tmp_path):
        f = tmp_path / "img.png"
        f.write_bytes(b"\x89PNG")
        r = await Response.file(f)
        assert "image/png" in r.media_type

    @pytest.mark.asyncio
    async def test_unknown_extension_octet_stream(self, tmp_path):
        f = tmp_path / "data.xyzzy"
        f.write_bytes(b"data")
        r = await Response.file(f)
        assert r.media_type == "application/octet-stream"

    @pytest.mark.asyncio
    async def test_custom_media_type_overrides_guess(self, tmp_path):
        f = tmp_path / "export.bin"
        f.write_bytes(b"data")
        r = await Response.file(f, media_type="application/vnd.ms-excel")
        assert r.media_type == "application/vnd.ms-excel"

    @pytest.mark.asyncio
    async def test_attachment_disposition_by_default(self, tmp_path):
        f = tmp_path / "file.pdf"
        f.write_bytes(b"%PDF")
        r = await Response.file(f)
        cd = r.headers.get("content-disposition", "")
        assert cd.startswith("attachment")

    @pytest.mark.asyncio
    async def test_inline_disposition(self, tmp_path):
        f = tmp_path / "logo.png"
        f.write_bytes(b"\x89PNG")
        r = await Response.file(f, inline=True)
        cd = r.headers.get("content-disposition", "")
        assert cd.startswith("inline")

    @pytest.mark.asyncio
    async def test_filename_defaults_to_basename(self, tmp_path):
        f = tmp_path / "quarterly.pdf"
        f.write_bytes(b"%PDF")
        r = await Response.file(f)
        cd = r.headers.get("content-disposition", "")
        assert 'filename="quarterly.pdf"' in cd

    @pytest.mark.asyncio
    async def test_custom_filename(self, tmp_path):
        f = tmp_path / "tmp123.pdf"
        f.write_bytes(b"%PDF")
        r = await Response.file(f, filename="report-q4.pdf")
        cd = r.headers.get("content-disposition", "")
        assert 'filename="report-q4.pdf"' in cd

    @pytest.mark.asyncio
    async def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            await Response.file(tmp_path / "nonexistent.txt")

    @pytest.mark.asyncio
    async def test_extra_headers_merged(self, tmp_path):
        f = tmp_path / "data.bin"
        f.write_bytes(b"x")
        r = await Response.file(f, headers=Headers([("x-custom", "value")]))
        assert r.headers.get("x-custom") == "value"

    @pytest.mark.asyncio
    async def test_stream_is_set_body_is_empty(self, tmp_path):
        f = tmp_path / "data.txt"
        f.write_bytes(b"abc")
        r = await Response.file(f)
        assert r.body == b""
        assert r._stream is not None

    @pytest.mark.asyncio
    async def test_custom_chunk_size_reads_whole_file(self, tmp_path):
        data = b"x" * 1000
        f = tmp_path / "big.bin"
        f.write_bytes(data)
        r = await Response.file(f, chunk_size=100)
        chunks = []
        async for chunk in r._stream:
            chunks.append(chunk)
        assert b"".join(chunks) == data

    @pytest.mark.asyncio
    async def test_path_object_accepted(self, tmp_path):
        import pathlib

        f = tmp_path / "file.txt"
        f.write_bytes(b"ok")
        r = await Response.file(pathlib.Path(f))
        assert r.status == 200


class TestResponseXml:
    """Unit tests for Response.xml() factory."""

    def test_string_encoded_to_utf8(self):
        r = Response.xml("<root/>")
        assert r.body == b"<root/>"

    def test_bytes_passed_through(self):
        r = Response.xml(b"<root/>")
        assert r.body == b"<root/>"

    def test_content_type_application_xml(self):
        r = Response.xml("<root/>")
        assert r.headers.get("content-type") == "application/xml"

    def test_media_type_property(self):
        r = Response.xml("<root/>")
        assert r.media_type == "application/xml"

    def test_status_200_default(self):
        r = Response.xml("<root/>")
        assert r.status == 200

    def test_custom_status(self):
        r = Response.xml("<root/>", status=201)
        assert r.status == 201

    def test_extra_headers_merged(self):
        r = Response.xml("<root/>", headers=Headers([("x-custom", "yes")]))
        assert r.headers.get("x-custom") == "yes"

    def test_unicode_content_preserved(self):
        xml = "<greeting>héllo wörld</greeting>"
        r = Response.xml(xml)
        assert "héllo wörld".encode() in r.body
