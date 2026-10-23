"""Property test: the TestClient multipart encoder round-trips through ``lauren._multipart``."""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._multipart import iter_parts, parse_boundary  # noqa: E402
from lauren.testing._encoding import encode_multipart  # noqa: E402

# Names / filenames the WHATWG encoding leaves untouched and the (deliberately
# small) server parser reads back verbatim: no quote, CR, LF, backslash or ';'.
_NAME = (
    st.text(
        alphabet=st.characters(blacklist_characters='"\r\n\\;', blacklist_categories=("Cs", "Cc")),
        min_size=1,
        max_size=12,
    )
    .map(str.strip)
    .filter(bool)
)
_FIELD_VALUE = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30).filter(
    lambda s: "\r\n--" not in s
)
_FILE_BYTES = st.binary(max_size=200)


@settings(max_examples=200, deadline=None)
@given(
    fields=st.lists(st.tuples(_NAME, _FIELD_VALUE), max_size=4),
    files=st.lists(st.tuples(_NAME, _NAME, _FILE_BYTES), min_size=1, max_size=4),
)
def test_fields_and_files_survive_encoding(fields, files) -> None:
    encoded_files = [
        (name, (filename, content, "application/octet-stream")) for name, filename, content in files
    ]
    body, content_type = encode_multipart(fields, encoded_files)
    parts = list(iter_parts(body, parse_boundary(content_type)))
    decoded_fields = [(p.name, p.data.decode("utf-8")) for p in parts if p.filename is None]
    decoded_files = [(p.name, p.filename, p.data) for p in parts if p.filename is not None]
    assert decoded_fields == list(fields)
    assert decoded_files == list(files)
