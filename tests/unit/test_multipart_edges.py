"""MIME parameter and framing edges beyond ordinary browser uploads."""

from __future__ import annotations

import pytest

from lauren._multipart import (
    _MultipartParser,
    _build_part,
    _parse_header_parameters,
    _unquote_parameter,
    iter_parts,
)
from lauren.exceptions import ExtractorError, RequestBodyTooLarge


@pytest.mark.parametrize("value", ['"unterminated', '"trailing\\'])
def test_quoted_value_decoder_rejects_truncation(value) -> None:
    with pytest.raises(ExtractorError, match="unterminated"):
        _unquote_parameter(value)


def test_plain_filename_continuations_and_unknown_extensions_are_supported() -> None:
    parameters = _parse_header_parameters(
        'form-data; name=x; filename*unknown=ignored; filename*0="long "; filename*1="name.txt"'
    )
    assert parameters["filename"] == "long name.txt"


def test_part_builder_ignores_blank_lines_in_an_already_separated_header_block() -> None:
    part = _build_part('\r\nContent-Disposition: form-data; name="x"\r\n', b"value")
    assert (part.name, part.data) == ("x", b"value")


def test_excessive_delimiter_padding_is_rejected_before_part_headers() -> None:
    parser = _MultipartParser("B", max_header_bytes=3)
    with pytest.raises(ExtractorError, match="padding"):
        list(parser.feed(b"--B    \r\n"))


def test_truncated_final_delimiter_is_not_a_complete_part() -> None:
    body = b'--B\r\nContent-Disposition: form-data; name="x"\r\n\r\nvalue\r\n--B-'
    with pytest.raises(ExtractorError, match="missing trailing boundary"):
        list(iter_parts(body, "B"))


def test_opening_delimiter_lookalike_is_ignored_in_the_preamble() -> None:
    body = b'--B-not-a-delimiter\r\n--B\r\nContent-Disposition: form-data; name="x"\r\n\r\nvalue\r\n--B--'
    assert list(iter_parts(body, "B"))[0].data == b"value"


def test_header_budget_is_enforced_before_the_header_terminator_arrives() -> None:
    parser = _MultipartParser("B", max_header_bytes=5)
    with pytest.raises(RequestBodyTooLarge):
        list(parser.feed(b"--B\r\nContent-Disposition"))


def test_legacy_latin1_filename_headers_are_decoded_without_replacement() -> None:
    body = b'--B\r\nContent-Disposition: form-data; name="x"; filename="caf\xe9.txt"\r\n\r\nx\r\n--B--'
    assert list(iter_parts(body, "B"))[0].filename == "café.txt"


def test_chunks_after_the_closing_delimiter_are_ignored() -> None:
    parser = _MultipartParser("B")
    events = list(parser.feed(b"--B--\r\n"))
    assert events == []
    assert list(parser.feed(b"epilogue")) == []
    assert list(parser.finish()) == []
