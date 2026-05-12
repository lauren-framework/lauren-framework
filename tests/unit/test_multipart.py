"""Unit tests for :mod:`lauren._multipart`.

Exercises the private multipart parser directly, without any ASGI
plumbing. The parser is a 200-line linear scanner with a handful of
edge cases (empty fields, quoted parameters, malformed disposition
headers); each case here maps to one invariant.
"""

from __future__ import annotations

import pytest

from lauren._multipart import iter_parts, parse_boundary
from lauren.exceptions import ExtractorError


# ---------------------------------------------------------------------------
# Boundary extraction
# ---------------------------------------------------------------------------


def test_parse_boundary_extracts_plain_token() -> None:
    header = "multipart/form-data; boundary=----FormBoundary123"
    assert parse_boundary(header) == "----FormBoundary123"


def test_parse_boundary_extracts_quoted_value() -> None:
    header = 'multipart/form-data; boundary="spaced boundary"'
    assert parse_boundary(header) == "spaced boundary"


def test_parse_boundary_handles_case_insensitive_key() -> None:
    header = "multipart/form-data; BOUNDARY=abc"
    assert parse_boundary(header) == "abc"


def test_parse_boundary_raises_on_missing_header() -> None:
    with pytest.raises(ExtractorError, match="missing Content-Type"):
        parse_boundary("")


def test_parse_boundary_raises_on_non_multipart_type() -> None:
    with pytest.raises(ExtractorError, match="not multipart"):
        parse_boundary("application/json")


def test_parse_boundary_raises_on_missing_boundary_param() -> None:
    with pytest.raises(ExtractorError, match="missing boundary"):
        parse_boundary("multipart/form-data")


def test_parse_boundary_raises_on_empty_boundary_value() -> None:
    with pytest.raises(ExtractorError, match="empty multipart boundary"):
        parse_boundary("multipart/form-data; boundary=")


# ---------------------------------------------------------------------------
# Body parsing \u2014 happy path
# ---------------------------------------------------------------------------


def _build_body(parts: list[tuple[str, bytes, dict[str, str]]], boundary: str) -> bytes:
    """Assemble a well-formed multipart body from tuples of
    ``(field_name, data, extra_headers)``.

    ``extra_headers`` can include ``filename`` and ``content_type``.
    The builder only exists in the test suite so it trades safety
    for simplicity; production code must go through the real
    multipart parser.
    """
    delim = f"--{boundary}".encode()
    lines: list[bytes] = []
    for name, data, extra in parts:
        lines.append(delim)
        filename = extra.get("filename")
        ct = extra.get("content_type")
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        lines.append(f"Content-Disposition: {disposition}".encode())
        if ct is not None:
            lines.append(f"Content-Type: {ct}".encode())
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    return b"\r\n".join(lines)


def test_iter_parts_yields_single_text_field() -> None:
    boundary = "ABC"
    body = _build_body([("field1", b"hello", {})], boundary)
    parts = list(iter_parts(body, boundary))
    assert len(parts) == 1
    assert parts[0].name == "field1"
    assert parts[0].filename is None
    assert parts[0].content_type == "text/plain"
    assert parts[0].data == b"hello"


def test_iter_parts_yields_multiple_fields_in_order() -> None:
    boundary = "X"
    body = _build_body(
        [
            ("first", b"one", {}),
            ("second", b"two", {}),
            ("third", b"three", {}),
        ],
        boundary,
    )
    parts = list(iter_parts(body, boundary))
    assert [(p.name, p.data) for p in parts] == [
        ("first", b"one"),
        ("second", b"two"),
        ("third", b"three"),
    ]


def test_iter_parts_preserves_file_metadata() -> None:
    boundary = "FB"
    body = _build_body(
        [
            (
                "avatar",
                b"\x89PNG\r\n\x1a\n" + b"fake-png-body",
                {"filename": "me.png", "content_type": "image/png"},
            ),
        ],
        boundary,
    )
    parts = list(iter_parts(body, boundary))
    assert len(parts) == 1
    p = parts[0]
    assert p.name == "avatar"
    assert p.filename == "me.png"
    assert p.content_type == "image/png"
    assert p.data.startswith(b"\x89PNG\r\n\x1a\n")


def test_iter_parts_handles_empty_part_body() -> None:
    boundary = "E"
    body = _build_body([("empty", b"", {})], boundary)
    parts = list(iter_parts(body, boundary))
    assert parts[0].data == b""


def test_iter_parts_preserves_binary_integrity() -> None:
    """Random binary payloads must round-trip through the parser\n    byte-for-byte. A bug in the CRLF-aware scanner would surface\n    as data corruption on any payload that happens to contain\n    ``\\r\\n`` sequences.\n"""
    import os

    boundary = "BIN"
    payload = os.urandom(16 * 1024)
    body = _build_body([("blob", payload, {"filename": "blob.bin"})], boundary)
    parts = list(iter_parts(body, boundary))
    assert parts[0].data == payload


def test_iter_parts_multiple_files_with_same_name() -> None:
    """A form with ``<input name=\"files\" multiple>`` produces
    several parts sharing the same name. The parser must yield
    each one distinctly so the extractor can collect them into a
    list.
    """
    boundary = "M"
    body = _build_body(
        [
            ("files", b"A", {"filename": "a.txt"}),
            ("files", b"B", {"filename": "b.txt"}),
            ("files", b"C", {"filename": "c.txt"}),
        ],
        boundary,
    )
    parts = list(iter_parts(body, boundary))
    assert [p.filename for p in parts] == ["a.txt", "b.txt", "c.txt"]
    assert all(p.name == "files" for p in parts)


# ---------------------------------------------------------------------------
# Body parsing \u2014 error paths
# ---------------------------------------------------------------------------


def test_iter_parts_raises_on_missing_opening_boundary() -> None:
    body = b"no boundary here at all"
    with pytest.raises(ExtractorError, match="missing opening boundary"):
        list(iter_parts(body, "ZZZ"))


def test_iter_parts_raises_on_truncated_body() -> None:
    # Missing final ``--`` terminator and trailing CRLF.
    body = b"--B\r\n"
    with pytest.raises(ExtractorError):
        list(iter_parts(body, "B"))


def test_iter_parts_raises_on_missing_content_disposition() -> None:
    # Hand-crafted body with a part that omits the disposition header.
    boundary = "B"
    body = b"--B\r\nContent-Type: text/plain\r\n\r\nbody-no-disposition\r\n--B--\r\n"
    with pytest.raises(ExtractorError, match="missing Content-Disposition"):
        list(iter_parts(body, boundary))


def test_iter_parts_raises_on_disposition_missing_name() -> None:
    boundary = "B"
    body = (
        b"--B\r\n"
        b"Content-Disposition: form-data\r\n"  # no name=
        b"\r\n"
        b"body\r\n"
        b"--B--\r\n"
    )
    with pytest.raises(ExtractorError, match="missing name"):
        list(iter_parts(body, boundary))


def test_iter_parts_raises_on_unterminated_part() -> None:
    boundary = "B"
    body = b'--B\r\nContent-Disposition: form-data; name="x"\r\n\r\norphan-body-no-closing-boundary'
    with pytest.raises(ExtractorError, match="missing trailing boundary"):
        list(iter_parts(body, boundary))


# ---------------------------------------------------------------------------
# Parameter parsing \u2014 quoted filenames with backslash escaping
# ---------------------------------------------------------------------------


def test_iter_parts_handles_backslash_escaped_filename() -> None:
    """RFC 7578 filenames may contain escaped quotes inside quoted
    strings. The parser's parameter unescaper must honour the
    single-backslash convention.
    """
    boundary = "B"
    body = (
        b"--B\r\n"
        b'Content-Disposition: form-data; name="f"; filename="weird\\"name.txt"\r\n'
        b"\r\n"
        b"data\r\n"
        b"--B--\r\n"
    )
    parts = list(iter_parts(body, boundary))
    assert parts[0].filename == 'weird"name.txt'


# ---------------------------------------------------------------------------
# Preamble handling \u2014 some clients add a preamble before the first boundary
# ---------------------------------------------------------------------------


def test_iter_parts_ignores_preamble_bytes() -> None:
    boundary = "B"
    body = (
        b"This is a preamble that clients sometimes include\r\n"
        b"and we must skip past it to the first boundary.\r\n"
        b"--B\r\n"
        b'Content-Disposition: form-data; name="x"\r\n'
        b"\r\n"
        b"hello\r\n"
        b"--B--\r\n"
    )
    parts = list(iter_parts(body, boundary))
    assert len(parts) == 1
    assert parts[0].data == b"hello"
