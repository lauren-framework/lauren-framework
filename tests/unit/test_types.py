"""Unit tests for Request, Response, State, Headers."""

from __future__ import annotations

import pytest

from lauren.exceptions import MissingStateError, RequestBodyTooLarge, StateTypeError
from lauren.types import (
    AppState,
    Headers,
    MutableHeaders,
    Request,
    Response,
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
        r = Response.json({}).with_cookie(
            "session", "abc", max_age=3600, http_only=True, secure=True
        )
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
