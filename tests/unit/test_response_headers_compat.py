"""``TestResponse`` / ``ResponseHeaders``: the 1.x list shape plus httpx-style accessors."""

from __future__ import annotations

from datetime import timedelta

import pytest

from lauren.testing import HTTPStatusAssertionError, ResponseHeaders, TestResponse
from lauren.testing._response import SentRequest, StreamingTestResponse, _split_lines

HEADERS = [("Content-Type", "application/json"), ("set-cookie", "a=1"), ("set-cookie", "b=2; Path=/")]


def _response(status: int | None = 200, headers=HEADERS, body: bytes = b'{"x": 1}', **kw) -> TestResponse:
    return TestResponse(status, headers, body, **kw)


# -- list behaviour (back-compat) ------------------------------------------------------


def test_headers_iterate_as_tuples() -> None:
    assert [k for k, _ in _response().headers] == ["Content-Type", "set-cookie", "set-cookie"]


def test_headers_support_int_index_slice_and_len() -> None:
    headers = _response().headers
    assert headers[0] == ("Content-Type", "application/json")
    assert headers[1:] == HEADERS[1:]
    assert len(headers) == 3


def test_headers_equal_a_list_of_tuples() -> None:
    assert _response().headers == HEADERS
    assert isinstance(_response().headers, list)


def test_tuple_membership_still_works() -> None:
    assert ("set-cookie", "a=1") in _response().headers


# -- mapping accessors -------------------------------------------------------------------


def test_get_is_case_insensitive() -> None:
    assert _response().headers.get("content-type") == "application/json"


def test_get_joins_repeated_values_like_httpx() -> None:
    assert _response().headers.get("Set-Cookie") == "a=1, b=2; Path=/"


def test_get_default() -> None:
    assert _response().headers.get("x-missing", "d") == "d"


def test_str_subscript() -> None:
    assert _response().headers["CONTENT-TYPE"] == "application/json"


def test_str_subscript_missing_raises_key_error() -> None:
    with pytest.raises(KeyError):
        _response().headers["x-missing"]


def test_getall_and_get_list() -> None:
    headers = _response().headers
    assert headers.getall("set-cookie") == ["a=1", "b=2; Path=/"]
    assert headers.get_list("SET-COOKIE") == headers.getall("set-cookie")


def test_keys_values_items() -> None:
    headers = _response().headers
    assert headers.keys() == ["content-type", "set-cookie"]
    assert headers.values() == ["application/json", "a=1, b=2; Path=/"]
    assert headers.items() == list(zip(headers.keys(), headers.values()))
    assert headers.multi_items() == HEADERS


def test_str_membership_is_case_insensitive() -> None:
    assert "content-TYPE" in _response().headers
    assert "x-missing" not in _response().headers


def test_dict_of_headers_uses_the_mapping_view() -> None:
    assert dict(ResponseHeaders([("A", "1")])) == {"a": "1"}


# -- response surface ------------------------------------------------------------------------


def test_body_and_content_are_the_same_bytes() -> None:
    response = _response()
    assert response.content == response.body == b'{"x": 1}'


def test_body_is_assignable() -> None:
    response = _response()
    response.body = b"new"
    assert response.content == b"new"


def test_text_uses_the_declared_charset() -> None:
    response = _response(
        headers=[("content-type", "text/plain; charset=latin-1")], body="é".encode("latin-1")
    )
    assert (response.encoding, response.text) == ("latin-1", "é")


def test_unknown_charset_falls_back_to_utf8() -> None:
    response = _response(headers=[("content-type", "text/plain; charset=nope")], body=b"ok")
    assert response.encoding == "utf-8"


def test_json_and_legacy_header_helpers() -> None:
    response = _response()
    assert response.json() == {"x": 1}
    assert response.header("SET-COOKIE") == "a=1"
    assert response.headers_all("set-cookie") == ["a=1", "b=2; Path=/"]
    assert response.header("x-missing") is None


def test_cookies_are_parsed_from_set_cookie() -> None:
    assert _response().cookies == {"a": "1", "b": "2"}


@pytest.mark.parametrize(
    ("status", "flags"),
    [
        (101, (True, False, False, False)),
        (204, (False, True, False, False)),
        (404, (False, False, True, False)),
        (503, (False, False, False, True)),
    ],
)
def test_status_flags(status: int, flags: tuple[bool, ...]) -> None:
    response = _response(status)
    assert (
        response.is_informational,
        response.is_success,
        response.is_client_error,
        response.is_server_error,
    ) == flags
    assert response.is_error is (flags[2] or flags[3])


def test_is_redirect_needs_a_location() -> None:
    assert _response(302, headers=[("location", "/x")]).is_redirect
    assert not _response(302, headers=[]).is_redirect


def test_reason_phrase_and_http_version() -> None:
    assert _response(404).reason_phrase == "Not Found"
    assert _response(599).reason_phrase == ""
    assert _response(None, aborted=True).reason_phrase == ""
    assert _response().http_version == "HTTP/1.1"


def test_raise_for_status_returns_self_on_success() -> None:
    response = _response()
    assert response.raise_for_status() is response


@pytest.mark.parametrize(("status", "word"), [(404, "Client error"), (500, "Server error")])
def test_raise_for_status_on_errors(status: int, word: str) -> None:
    response = _response(status, request=SentRequest("GET", "http://testserver/x", [], None))
    with pytest.raises(HTTPStatusAssertionError, match=word) as info:
        response.raise_for_status()
    assert info.value.response is response
    assert isinstance(info.value, AssertionError)
    assert "GET http://testserver/x" in str(info.value)


def test_raise_for_status_on_an_aborted_response() -> None:
    with pytest.raises(HTTPStatusAssertionError, match="aborted by client"):
        _response(None, aborted=True).raise_for_status()


def test_defaults_for_new_attributes() -> None:
    response = _response()
    assert (response.aborted, response.url, response.request, response.history) == (False, "", None, [])
    assert response.elapsed == timedelta(0)


def test_equality_and_repr() -> None:
    assert _response() == _response()
    assert _response() != _response(201)
    assert _response().__eq__(object()) is NotImplemented
    assert repr(_response(None, aborted=True)) == "<TestResponse [None aborted]>"


def test_sent_request_repr_and_headers() -> None:
    sent = SentRequest("POST", "http://testserver/a", [("Host", "testserver")], b"x")
    assert repr(sent) == "<SentRequest [POST http://testserver/a]>"
    assert sent.headers.get("host") == "testserver"


# -- streaming responses -------------------------------------------------------------------


class _ListSource:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)
        self.aborted = False
        self.closed = False

    def next_chunk(self) -> bytes | None:
        return self._chunks.pop(0) if self._chunks else None

    async def anext_chunk(self) -> bytes | None:
        return self.next_chunk()

    def close(self) -> None:
        self.closed = True

    async def aclose(self) -> None:
        self.closed = True


def _stream(chunks: list[bytes], headers=(("content-type", "text/event-stream"),)) -> StreamingTestResponse:
    return StreamingTestResponse(200, list(headers), source=_ListSource(chunks))


def test_iter_bytes_accumulates_the_body() -> None:
    response = _stream([b"ab", b"cd"])
    assert list(response.iter_bytes()) == [b"ab", b"cd"]
    assert response.body == b"abcd" and response.is_stream_consumed


def test_iter_text_handles_split_multibyte_characters() -> None:
    data = "é".encode()
    assert "".join(_stream([data[:1], data[1:]]).iter_text()) == "é"


def test_iter_lines_handles_all_terminators_and_split_crlf() -> None:
    assert list(_stream([b"a\r", b"\nb\rc\n", b"tail"]).iter_lines()) == ["a", "b", "c", "tail"]


def test_split_lines_keeps_unicode_separators_inside_lines() -> None:
    assert _split_lines("a b\n", final=True) == (["a b"], "")


def test_iter_sse_parses_events() -> None:
    raw = b": hello\n\nid: 1\nevent: tick\ndata: a\ndata: b\nretry: 50\n\ndata:no-space\nignored: x\n\n"
    events = list(_stream([raw[:7], raw[7:]]).iter_sse())
    assert [(e.event, e.id, e.data, e.retry, e.comment) for e in events] == [
        (None, None, None, None, "hello"),
        ("tick", "1", "a\nb", 50, None),
        (None, None, "no-space", None, None),
    ]


def test_iter_sse_drops_an_unterminated_trailing_event() -> None:
    assert [e.data for e in _stream([b"data: 1\n\ndata: cut"]).iter_sse()] == ["1"]


def test_read_returns_everything_and_close_is_idempotent() -> None:
    response = _stream([b"a", b"b"])
    assert response.read() == b"ab"
    response.close()
    response.close()
    assert response.is_closed and response._source.closed


def test_iter_raw_aliases_iter_bytes() -> None:
    assert list(_stream([b"a"]).iter_raw()) == [b"a"]


async def test_async_iteration_variants() -> None:
    response = _stream([b"data: 1\n\n", b"data: 2\n\n"])
    assert [e.data async for e in response.aiter_sse()] == ["1", "2"]
    assert [c async for c in _stream([b"x"]).aiter_raw()] == [b"x"]
    assert [t async for t in _stream([b"a\nb"]).aiter_lines()] == ["a", "b"]
    assert await _stream([b"q"]).aread() == b"q"
    closing = _stream([b"z"])
    await closing.aclose()
    await closing.aclose()
    assert closing.is_closed


def test_non_charset_content_type_parameters_are_skipped() -> None:
    assert (
        _response(headers=[("content-type", "text/plain; format=flowed; charset=ascii")]).encoding == "ascii"
    )


def test_malformed_set_cookie_values_are_skipped() -> None:
    response = _response(headers=[("set-cookie", "novalue"), ("set-cookie", "=x"), ("set-cookie", "ok=1")])
    assert response.cookies == {"ok": "1"}


def test_split_lines_flushes_a_final_lone_cr() -> None:
    assert _split_lines("a\r", final=True) == (["a"], "")


def test_iter_sse_ignores_leading_blank_lines_and_bare_fields() -> None:
    events = list(_stream([b"\n\ndata\n\n"]).iter_sse())
    assert [e.data for e in events] == [""]


def test_streaming_body_setter_replaces_the_parts() -> None:
    response = _stream([b"x"])
    response.body = b"replaced"
    assert response.content == b"replaced"


def test_iterating_a_consumed_stream_yields_nothing() -> None:
    response = _stream([b"x"])
    response.read()
    assert list(response.iter_bytes()) == []


def test_truncated_utf8_at_the_end_raises() -> None:
    with pytest.raises(UnicodeDecodeError):
        list(_stream(["é".encode()[:1]], headers=[("content-type", "text/plain")]).iter_text())


async def test_async_iteration_edge_cases() -> None:
    consumed = _stream([b"x"])
    await consumed.aread()
    assert [c async for c in consumed.aiter_bytes()] == []
    data = "é".encode()
    assert [t async for t in _stream([data[:1], data[1:]]).aiter_text()] == ["é"]
    assert [line async for line in _stream([b"a\nb"]).aiter_lines()] == ["a", "b"]
