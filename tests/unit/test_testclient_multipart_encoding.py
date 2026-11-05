"""Unit tests for the test clients' form / multipart encoders and body rules."""

from __future__ import annotations

import io
import mimetypes

import pytest

from lauren._multipart import iter_parts, parse_boundary
from lauren.testing._encoding import encode_form, encode_multipart
from lauren.testing._transport import encode_body


def _parts(body: bytes, content_type: str):
    return list(iter_parts(body, parse_boundary(content_type)))


# -- urlencoded -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        ({"a": "1"}, b"a=1"),
        ({"n": 3, "f": 1.5}, b"n=3&f=1.5"),
        ({"t": True, "f": False, "z": None}, b"t=true&f=false&z="),
        ({"tag": ["a", "b"]}, b"tag=a&tag=b"),
        ([("k", "x y"), ("k", "&")], b"k=x+y&k=%26"),
    ],
)
def test_form_encoding(data, expected: bytes) -> None:
    assert encode_form(data) == expected


# -- multipart ----------------------------------------------------------------------


def test_multipart_fields_then_files() -> None:
    body, ctype = encode_multipart({"title": "hi"}, {"doc": ("a.txt", b"hello", "text/plain")})
    parts = _parts(body, ctype)
    assert [(p.name, p.filename, p.content_type, p.data) for p in parts] == [
        ("title", None, "text/plain", b"hi"),
        ("doc", "a.txt", "text/plain", b"hello"),
    ]


def test_multipart_boundary_is_random_hex() -> None:
    _, first = encode_multipart(None, {"f": ("a", b"")})
    _, second = encode_multipart(None, {"f": ("a", b"")})
    assert first != second
    assert len(parse_boundary(first)) == 32


def test_multipart_explicit_boundary() -> None:
    _, ctype = encode_multipart(None, {"f": ("a", b"x")}, boundary="xyz")
    assert ctype == "multipart/form-data; boundary=xyz"


def test_multipart_content_type_guessed_from_filename() -> None:
    body, ctype = encode_multipart(None, {"img": ("me.png", b"\x89PNG")})
    # Compare with the platform's own table (Windows reads the registry).
    assert _parts(body, ctype)[0].content_type == mimetypes.guess_type("me.png")[0]


def test_multipart_unknown_extension_defaults_to_octet_stream() -> None:
    body, ctype = encode_multipart(None, {"blob": ("data.unknownext", b"x")})
    assert _parts(body, ctype)[0].content_type == "application/octet-stream"


def test_multipart_file_object_uses_its_basename() -> None:
    fileobj = io.BytesIO(b"content")
    fileobj.name = "/tmp/some/dir/report.csv"  # type: ignore[attr-defined]
    body, ctype = encode_multipart(None, {"report": fileobj})
    part = _parts(body, ctype)[0]
    expected_type = mimetypes.guess_type("report.csv")[0] or "application/octet-stream"
    assert (part.filename, part.content_type, part.data) == ("report.csv", expected_type, b"content")


def test_multipart_bare_bytes_get_a_default_filename() -> None:
    body, ctype = encode_multipart(None, {"raw": b"abc"})
    assert _parts(body, ctype)[0].filename == "upload"


def test_multipart_str_content_is_utf8() -> None:
    body, ctype = encode_multipart(None, {"t": ("t.txt", "héllo", "text/plain")})
    assert _parts(body, ctype)[0].data == "héllo".encode()


def test_multipart_several_files_under_one_name() -> None:
    body, ctype = encode_multipart(None, {"docs": [("a.pdf", b"1", "application/pdf"), ("b.pdf", b"2")]})
    assert [p.filename for p in _parts(body, ctype)] == ["a.pdf", "b.pdf"]


def test_multipart_list_of_pairs_form() -> None:
    body, ctype = encode_multipart([("k", "1"), ("k", "2")], [("docs", ("a", b"1")), ("docs", ("b", b"2"))])
    assert [(p.name, p.filename) for p in _parts(body, ctype)] == [
        ("k", None),
        ("k", None),
        ("docs", "a"),
        ("docs", "b"),
    ]


def test_multipart_extra_part_headers() -> None:
    body, ctype = encode_multipart(None, {"f": ("a.txt", b"x", "text/plain", {"X-Meta": "1"})})
    assert ("x-meta", "1") in _parts(body, ctype)[0].headers


def test_multipart_escapes_quotes_and_newlines_whatwg_style() -> None:
    body, _ = encode_multipart({'na"me\r\n': "v"}, {"f": ('fi"le\n.txt', b"x")})
    assert b'name="na%22me%0D%0A"' in body
    assert b'filename="fi%22le%0A.txt"' in body


def test_multipart_utf8_filename() -> None:
    body, ctype = encode_multipart(None, {"f": ("résumé.pdf", b"x")})
    assert _parts(body, ctype)[0].filename == "résumé.pdf"


def test_multipart_field_values_render_like_form_values() -> None:
    body, ctype = encode_multipart({"flag": True, "raw": b"\x00"}, {"f": ("a", b"")})
    assert [p.data for p in _parts(body, ctype)[:2]] == [b"true", b"\x00"]


def test_multipart_rejects_malformed_tuples() -> None:
    with pytest.raises(TypeError, match="tuple"):
        encode_multipart(None, {"f": ("only-a-name",)})


def test_multipart_rejects_unreadable_content() -> None:
    with pytest.raises(TypeError, match="file content"):
        encode_multipart(None, {"f": ("a", 42)})


# -- body selection rules ---------------------------------------------------------------


def test_json_body() -> None:
    body = encode_body(json={"a": 1}, data=None, files=None, content=None)
    assert (body.payload, body.content_type) == (b'{"a": 1}', "application/json")


def test_json_wins_over_content_like_1x() -> None:
    body = encode_body(json={"a": 1}, data=None, files=None, content=b"ignored")
    assert body.payload == b'{"a": 1}'


@pytest.mark.parametrize(
    "kwargs",
    [
        {"json": {}, "data": {"a": 1}, "files": None, "content": None},
        {"json": None, "data": {"a": 1}, "files": None, "content": b"x"},
        {"json": {}, "data": None, "files": {"f": ("a", b"")}, "content": None},
        {"json": None, "data": "raw", "files": {"f": ("a", b"")}, "content": None},
    ],
)
def test_conflicting_body_arguments_raise(kwargs) -> None:
    with pytest.raises(TypeError):
        encode_body(**kwargs)


def test_raw_data_is_sent_as_is() -> None:
    assert encode_body(json=None, data="a=b", files=None, content=None).payload == b"a=b"
    assert encode_body(json=None, data=b"\x01", files=None, content=None).payload == b"\x01"


@pytest.mark.parametrize(("content", "payload"), [("text", b"text"), (b"b", b"b"), (bytearray(b"ba"), b"ba")])
def test_content_bytes_like(content, payload: bytes) -> None:
    body = encode_body(json=None, data=None, files=None, content=content)
    assert (body.payload, body.streamed) == (payload, False)


def test_iterable_content_is_streamed() -> None:
    body = encode_body(json=None, data=None, files=None, content=iter([b"a", b"b"]))
    assert body.streamed and not body.replayable


def test_unsupported_content_raises() -> None:
    with pytest.raises(TypeError, match="content="):
        encode_body(json=None, data=None, files=None, content=42)
