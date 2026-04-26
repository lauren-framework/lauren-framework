"""Unit tests for extractors."""

from __future__ import annotations


import pytest
from pydantic import BaseModel

from lauren import (
    Json,
    Path,
    Query,
)
from lauren.exceptions import ExtractorError, ExtractorFieldError
from lauren.extractors import (
    Bytes,
    FieldDescriptor,
    _Extraction,
    extract_parameter,
    parse_extractor_hint,
)
from lauren.types import Headers, Request


def make_request(
    method: str = "GET",
    path: str = "/",
    *,
    query: bytes = b"",
    headers: list[tuple[str, str]] | None = None,
    path_params: dict[str, str] | None = None,
    body: bytes = b"",
) -> Request:
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        method=method,
        path=path,
        raw_query_string=query,
        headers=Headers(headers or []),
        path_params=path_params,
        receive=receive,
    )


class TestParseHints:
    def test_path_int(self):
        src, inner, reads, marker, *_rest = parse_extractor_hint(Path[int])
        assert src == "path"
        assert inner is int
        assert reads is False
        assert marker is Path

    def test_query_str(self):
        src, inner, *_rest = parse_extractor_hint(Query[str])
        assert src == "query"
        assert inner is str

    def test_json_model(self):
        class User(BaseModel):
            name: str

        src, inner, reads, *_rest = parse_extractor_hint(Json[User])
        assert src == "json"
        assert inner is User
        assert reads is True

    def test_bare_type_returns_none(self):
        src, *_rest = parse_extractor_hint(int)
        assert src is None

    def test_bytes_extractor(self):
        src, inner, reads, *_rest = parse_extractor_hint(Bytes)
        assert src == "bytes"
        assert reads is True


class TestPathExtraction:
    @pytest.mark.asyncio
    async def test_extract_path_int(self):
        req = make_request(path_params={"id": "42"})
        ext = _Extraction(
            name="id",
            source="path",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        val = await extract_parameter(req, ext)
        assert val == 42

    @pytest.mark.asyncio
    async def test_path_missing_raises(self):
        req = make_request()
        ext = _Extraction(
            name="id",
            source="path",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        with pytest.raises(ExtractorFieldError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_path_invalid_int(self):
        req = make_request(path_params={"id": "notanint"})
        ext = _Extraction(
            name="id",
            source="path",
            inner_type=int,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)


class TestQueryExtraction:
    @pytest.mark.asyncio
    async def test_basic_query(self):
        req = make_request(query=b"q=hello")
        ext = _Extraction(
            name="q",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "hello"

    @pytest.mark.asyncio
    async def test_query_list(self):
        req = make_request(query=b"tag=a&tag=b&tag=c")
        ext = _Extraction(
            name="tag",
            source="query",
            inner_type=list[str],
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_query_int_with_ge(self):
        req = make_request(query=b"page=0")
        fd = FieldDescriptor(ge=1)
        ext = _Extraction(
            name="page",
            source="query",
            inner_type=int,
            field_descriptor=fd,
            default=None,
            has_default=False,
        )
        with pytest.raises(ExtractorFieldError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_query_default(self):
        req = make_request()
        ext = _Extraction(
            name="q",
            source="query",
            inner_type=str,
            field_descriptor=None,
            default="default",
            has_default=True,
        )
        assert await extract_parameter(req, ext) == "default"

    @pytest.mark.asyncio
    async def test_query_alias(self):
        req = make_request(query=b"user-id=7")
        fd = FieldDescriptor(alias="user-id")
        ext = _Extraction(
            name="user_id",
            source="query",
            inner_type=int,
            field_descriptor=fd,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == 7


class TestHeaderExtraction:
    @pytest.mark.asyncio
    async def test_header_basic(self):
        req = make_request(headers=[("X-Request-Id", "abc")])
        ext = _Extraction(
            name="x-request-id",
            source="header",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "abc"

    @pytest.mark.asyncio
    async def test_header_with_default(self):
        req = make_request()
        ext = _Extraction(
            name="x-missing",
            source="header",
            inner_type=str,
            field_descriptor=None,
            default="fallback",
            has_default=True,
        )
        assert await extract_parameter(req, ext) == "fallback"

    @pytest.mark.asyncio
    async def test_header_underscore_to_hyphen(self):
        req = make_request(headers=[("x-api-key", "secret")])
        ext = _Extraction(
            name="x_api_key",
            source="header",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "secret"


class TestCookieExtraction:
    @pytest.mark.asyncio
    async def test_cookie_basic(self):
        req = make_request(headers=[("cookie", "session=abc; theme=dark")])
        ext = _Extraction(
            name="session",
            source="cookie",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
        )
        assert await extract_parameter(req, ext) == "abc"


class TestJsonExtraction:
    @pytest.mark.asyncio
    async def test_json_dict(self):
        req = make_request(body=b'{"k":"v"}')
        ext = _Extraction(
            name="body",
            source="json",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        assert await extract_parameter(req, ext) == {"k": "v"}

    @pytest.mark.asyncio
    async def test_json_pydantic_validation(self):
        class User(BaseModel):
            name: str
            age: int

        req = make_request(body=b'{"name":"Alice","age":30}')
        ext = _Extraction(
            name="user",
            source="json",
            inner_type=User,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        u = await extract_parameter(req, ext)
        assert u.name == "Alice"
        assert u.age == 30

    @pytest.mark.asyncio
    async def test_json_validation_error(self):
        class User(BaseModel):
            name: str
            age: int

        req = make_request(body=b'{"name":"Alice","age":"not-int"}')
        ext = _Extraction(
            name="user",
            source="json",
            inner_type=User,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_json_malformed(self):
        req = make_request(body=b"not json")
        ext = _Extraction(
            name="body",
            source="json",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        with pytest.raises(ExtractorError):
            await extract_parameter(req, ext)


class TestBytesExtraction:
    @pytest.mark.asyncio
    async def test_raw_bytes(self):
        req = make_request(body=b"\x00\x01\x02")
        ext = _Extraction(
            name="body",
            source="bytes",
            inner_type=bytes,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        assert await extract_parameter(req, ext) == b"\x00\x01\x02"


class TestCustomExtractor:
    """Verify the custom-extractor hook via :meth:`_ExtractorMarker.extract`."""

    @pytest.mark.asyncio
    async def test_custom_extract_classmethod_invoked(self):
        from lauren.extractors import _ExtractorMarker

        class Echo(_ExtractorMarker):
            source = "echo"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
                return f"echo:{extraction.name}:{request.method}"

        req = make_request(method="POST")
        ext = _Extraction(
            name="x",
            source="echo",
            inner_type=str,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=Echo,
        )
        value = await extract_parameter(req, ext)
        assert value == "echo:x:POST"

    @pytest.mark.asyncio
    async def test_custom_extract_httperror_propagates(self):
        from lauren.exceptions import UnauthorizedError
        from lauren.extractors import _ExtractorMarker

        class AuthMe(_ExtractorMarker):
            source = "auth_me"

            @classmethod
            async def extract(cls, request, extraction, *, container, request_cache):
                raise UnauthorizedError("nope")

        req = make_request()
        ext = _Extraction(
            name="u",
            source="auth_me",
            inner_type=object,
            field_descriptor=None,
            default=None,
            has_default=False,
            marker_cls=AuthMe,
        )
        with pytest.raises(UnauthorizedError):
            await extract_parameter(req, ext)

    @pytest.mark.asyncio
    async def test_bare_marker_class_parsed(self):
        from lauren.extractors import _ExtractorMarker

        class Marker(_ExtractorMarker):
            source = "marker"

        src, inner, reads, cls, *_rest = parse_extractor_hint(Marker)
        assert src == "marker"
        assert cls is Marker


class TestFormExtraction:
    @pytest.mark.asyncio
    async def test_form_basic(self):
        req = make_request(body=b"name=alice&age=30")
        ext = _Extraction(
            name="form",
            source="form",
            inner_type=dict,
            field_descriptor=None,
            default=None,
            has_default=False,
            reads_body=True,
        )
        val = await extract_parameter(req, ext)
        assert val["name"] == ["alice"]
        assert val["age"] == ["30"]
