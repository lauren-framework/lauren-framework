"""Arbitrary byte chunks preserve multipart binary payloads."""

from __future__ import annotations

import pytest

pytest.importorskip("hypothesis")
from hypothesis import assume, given, strategies as st

from lauren._multipart import _MultipartParser


@given(st.binary(max_size=4096), st.lists(st.integers(1, 127), min_size=1, max_size=20))
def test_binary_round_trip_is_independent_of_chunk_boundaries(payload, sizes) -> None:
    boundary = b"property-boundary"
    assume(b"\r\n--" + boundary not in payload)
    body = (
        b"--"
        + boundary
        + b'\r\nContent-Disposition: form-data; name="x"; filename="x.bin"\r\n\r\n'
        + payload
        + b"\r\n--"
        + boundary
        + b"--\r\n"
    )
    parser = _MultipartParser(boundary.decode())
    pieces = []
    position = 0
    index = 0
    while position < len(body):
        size = sizes[index % len(sizes)]
        for kind, value in parser.feed(body[position : position + size]):
            if kind == "data":
                pieces.append(value)
        position += size
        index += 1
    for kind, value in parser.finish():
        if kind == "data":
            pieces.append(value)
    assert b"".join(pieces) == payload
