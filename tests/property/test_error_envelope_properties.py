"""Property tests for the error envelope in every ``error_format`` (F05).

For any error status, JSON-safe code / message / detail and valid headers:

* the rendered status equals the error's ``status_code``;
* the body parses as JSON and, under ``rfc9457``, ``status`` matches;
* header values never contain CR or LF, and invalid ones are rejected when the
  error is constructed (never at render time).
"""

from __future__ import annotations

import json

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._asgi import _error_response  # noqa: E402
from lauren._asgi._problem import json_pointer  # noqa: E402
from lauren.exceptions import HTTPError  # noqa: E402
from lauren.serialization import StdlibJSONEncoder  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=100)
_ENC = StdlibJSONEncoder()

_json_scalars = st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(max_size=20)
_json_values = st.recursive(
    _json_scalars,
    lambda children: (
        st.lists(children, max_size=4) | st.dictionaries(st.text(max_size=8), children, max_size=4)
    ),
    max_leaves=12,
)
_codes = st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=24)
_header_names = st.text(alphabet="abcdefghijklmnopqrstuvwxyz-", min_size=1, max_size=16).filter(
    lambda n: n not in ("content-type", "content-length")
)
_header_values = st.text(alphabet=st.characters(min_codepoint=0x20, max_codepoint=0xFF), max_size=40)


@_TOLERANT
@given(
    status=st.integers(400, 599),
    code=_codes,
    message=st.text(max_size=60),
    detail=st.dictionaries(st.text(max_size=8), _json_values, max_size=4),
    headers=st.lists(st.tuples(_header_names, _header_values), max_size=4),
    fmt=st.sampled_from(["default", "rfc7807", "rfc9457"]),
)
def test_rendered_envelope_round_trips(
    status: int,
    code: str,
    message: str,
    detail: dict[str, object],
    headers: list[tuple[str, str]],
    fmt: str,
) -> None:
    err = HTTPError(message, status_code=status, code=code, detail=detail, headers=headers)

    resp = _error_response(err, error_format=fmt, encoder=_ENC, instance="/p")

    assert resp.status == status
    body = json.loads(resp.body)
    if fmt == "default":
        assert body["error"]["code"] == code
    else:
        assert (body["status"], body["code"]) == (status, code)
    for name, value in resp.headers.raw():
        assert "\r" not in value and "\n" not in value
    for name, value in headers:
        assert value in resp.headers.getall(name)


@_TOLERANT
@given(
    prefix=st.text(max_size=10),
    breaker=st.sampled_from(["\r", "\n", "\r\n", "\x00"]),
    suffix=st.text(max_size=10),
)
def test_header_values_with_line_breaks_are_rejected_at_construction(
    prefix: str, breaker: str, suffix: str
) -> None:
    with pytest.raises(ValueError):
        HTTPError("x", headers={"x-test": prefix + breaker + suffix})


@_TOLERANT
@given(parts=st.lists(st.text(max_size=10) | st.integers(0, 99), max_size=5))
def test_json_pointer_tokens_round_trip(parts: list[object]) -> None:
    from urllib.parse import unquote

    pointer = json_pointer(parts)

    assert pointer.startswith("#")
    tokens = pointer[1:].split("/")[1:] if parts else []
    decoded = [unquote(t).replace("~1", "/").replace("~0", "~") for t in tokens]
    assert decoded == [str(p) for p in parts]
