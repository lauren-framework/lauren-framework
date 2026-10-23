"""Unit tests for the error wire formats: default, legacy rfc7807, RFC 9457 (F05).

The golden tests pin the ``default`` and ``rfc7807`` bodies byte for byte — they
must not change while ``rfc9457`` evolves next to them.
"""

from __future__ import annotations

import json

import pytest

from lauren._asgi import _error_response
from lauren._asgi._problem import instance_reference, json_pointer, validation_errors
from lauren.exceptions import (
    ExtractorError,
    ExtractorFieldError,
    HTTPError,
    LaurenError,
    MethodNotAllowedError,
    NotFoundError,
    RouteNotFoundError,
    TooManyRequestsError,
    UnauthorizedError,
)
from lauren.serialization import StdlibJSONEncoder

_ENC = StdlibJSONEncoder()


class _Teapot(HTTPError):
    status_code = 418
    code = "teapot"


class _Quantum(HTTPError):
    status_code = 422
    code = "quantum"
    problem_type = "https://example.com/p"
    problem_title = "Quantum"


class _UserNotFound(NotFoundError):
    code = "user_not_found"


def _golden_errors() -> list[LaurenError]:
    return [
        _Teapot("short and stout", detail={"reason": "brewing"}),
        RouteNotFoundError("No route matches GET /x"),
        MethodNotAllowedError("POST not allowed for /x", allow=["GET"]),
        ExtractorFieldError("id must be >= 1", detail={"field": "id", "constraint": "ge"}),
        _Quantum("collapsed", detail={"n": 7}),
        UnauthorizedError(""),
    ]


_DEFAULT_GOLDEN = [
    b'{"error":{"code":"teapot","message":"short and stout","detail":{"reason":"brewing"}}}',
    b'{"error":{"code":"route_not_found","message":"No route matches GET /x","detail":{}}}',
    b'{"error":{"code":"method_not_allowed","message":"POST not allowed for /x","detail":{"allow":["GET"]}}}',
    b'{"error":{"code":"extractor_field_error","message":"id must be >= 1","detail":{"field":"id","constraint":"ge"}}}',
    b'{"error":{"code":"quantum","message":"collapsed","detail":{"n":7}}}',
    b'{"error":{"code":"unauthorized","message":"","detail":{}}}',
]

_RFC7807_GOLDEN = [
    b'{"type":"urn:ietf:rfc:7231:418","title":"I\'m a Teapot","status":418,"detail":"short and stout",'
    b'"code":"teapot","errors":{"reason":"brewing"}}',
    b'{"type":"urn:ietf:rfc:7231:404","title":"Not Found","status":404,"detail":"No route matches GET /x",'
    b'"code":"route_not_found"}',
    b'{"type":"urn:ietf:rfc:7231:405","title":"Method Not Allowed","status":405,'
    b'"detail":"POST not allowed for /x","code":"method_not_allowed","errors":{"allow":["GET"]}}',
    b'{"type":"urn:ietf:rfc:7231:422","title":"Unprocessable Entity","status":422,"detail":"id must be >= 1",'
    b'"code":"extractor_field_error","errors":{"field":"id","constraint":"ge"}}',
    b'{"type":"https://example.com/p","title":"Quantum","status":422,"detail":"collapsed","code":"quantum",'
    b'"errors":{"n":7}}',
    b'{"type":"urn:ietf:rfc:7231:401","title":"Unauthorized","status":401,"detail":"Unauthorized",'
    b'"code":"unauthorized"}',
]


# ---------------------------------------------------------------------------
# Golden: default and rfc7807 are byte-identical to earlier releases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("index", range(len(_DEFAULT_GOLDEN)))
def test_default_envelope_is_byte_identical(index: int) -> None:
    resp = _error_response(_golden_errors()[index], error_format="default", encoder=_ENC)

    assert resp.body == _DEFAULT_GOLDEN[index]
    assert resp.headers.get("content-type") == "application/json"


@pytest.mark.parametrize("index", range(len(_RFC7807_GOLDEN)))
def test_rfc7807_envelope_is_byte_identical(index: int) -> None:
    resp = _error_response(_golden_errors()[index], error_format="rfc7807", encoder=_ENC)

    assert resp.body == _RFC7807_GOLDEN[index]
    assert resp.headers.get("content-type") == "application/problem+json"


def test_rfc7807_internal_error_is_byte_identical() -> None:
    error = LaurenError("internal server error", detail={"type": "KeyError"})

    resp = _error_response(error, status=500, code="internal_error", error_format="rfc7807", encoder=_ENC)

    assert resp.body == (
        b'{"type":"urn:ietf:rfc:7231:500","title":"Internal Server Error","status":500,'
        b'"detail":"internal server error","code":"internal_error","errors":{"type":"KeyError"}}'
    )


def test_rfc7807_uses_problem_type_base_when_configured() -> None:
    resp = _error_response(
        _Teapot("x"), error_format="rfc7807", encoder=_ENC, problem_type_base="https://errors.example.com/"
    )

    assert json.loads(resp.body)["type"] == "https://errors.example.com/teapot"


# ---------------------------------------------------------------------------
# RFC 9457
# ---------------------------------------------------------------------------


def _rfc9457(err: LaurenError, **kwargs: object) -> dict[str, object]:
    resp = _error_response(err, error_format="rfc9457", encoder=_ENC, **kwargs)  # type: ignore[arg-type]
    assert resp.headers.get("content-type") == "application/problem+json"
    return json.loads(resp.body)


def test_rfc9457_defaults_type_to_about_blank_and_title_to_the_status_phrase() -> None:
    payload = _rfc9457(NotFoundError("missing"))

    assert payload == {
        "type": "about:blank",
        "title": "Not Found",
        "status": 404,
        "detail": "missing",
        "code": "not_found",
    }


def test_rfc9457_full_document_for_a_domain_error() -> None:
    payload = _rfc9457(
        _UserNotFound("user does not exist", detail={"id": 7}),
        instance="/users/7",
        problem_type_base="https://errors.example.com/",
    )

    assert payload == {
        "type": "https://errors.example.com/user_not_found",
        "title": "Not Found",
        "status": 404,
        "detail": "user does not exist",
        "instance": "/users/7",
        "code": "user_not_found",
        "context": {"id": 7},
    }


def test_rfc9457_problem_type_and_title_overrides_win_over_the_base() -> None:
    payload = _rfc9457(_Quantum("collapsed"), problem_type_base="https://errors.example.com/")

    assert (payload["type"], payload["title"]) == ("https://example.com/p", "Quantum")


def test_rfc9457_detail_falls_back_to_the_title() -> None:
    assert _rfc9457(UnauthorizedError(""))["detail"] == "Unauthorized"


def test_rfc9457_unknown_status_has_a_generic_title() -> None:
    assert _rfc9457(HTTPError("odd", status_code=499))["title"] == "HTTP Error"


def test_rfc9457_omits_context_when_detail_is_empty() -> None:
    assert "context" not in _rfc9457(NotFoundError("x"))


def test_rfc9457_internal_error_puts_the_exception_type_in_context() -> None:
    error = LaurenError("internal server error", detail={"type": "KeyError"})

    payload = _rfc9457(error, status=500, code="internal_error")

    assert payload["code"] == "internal_error"
    assert payload["context"] == {"type": "KeyError"}


def test_rfc9457_maps_issues_to_an_errors_array_with_json_pointers() -> None:
    err = ExtractorError(
        "2 validation errors",
        detail={
            "issues": [
                {"loc": ["body", "address", "city"], "type": "missing", "msg": "Field required"},
                {"loc": ("query", "page"), "type": "int_parsing", "msg": "not an int"},
            ]
        },
    )

    payload = _rfc9457(err)

    assert payload["errors"] == [
        {"detail": "Field required", "pointer": "#/address/city", "code": "missing"},
        {"detail": "not an int", "in": "query", "parameter": "page", "code": "int_parsing"},
    ]


def test_rfc9457_maps_legacy_extractor_errors_per_message() -> None:
    err = ExtractorError(
        "Validation failed for field 'body'",
        detail={"field": "body", "errors": ["name: Field required", "age: too small"]},
    )

    assert _rfc9457(err)["errors"] == [
        {"detail": "name: Field required", "parameter": "body"},
        {"detail": "age: too small", "parameter": "body"},
    ]


def test_rfc9457_no_errors_array_for_non_validation_errors() -> None:
    assert "errors" not in _rfc9457(_Teapot("x", detail={"field": "pot"}))


@pytest.mark.parametrize(
    "fmt,expected",
    [
        ("default", "application/json"),
        ("rfc7807", "application/problem+json"),
        ("rfc9457", "application/problem+json"),
    ],
)
def test_error_headers_are_merged_in_every_format(fmt: str, expected: str) -> None:
    err = TooManyRequestsError("slow down", retry_after=30, headers={"x-ratelimit-remaining": "0"})

    resp = _error_response(err, error_format=fmt, encoder=_ENC)

    assert resp.headers.get("retry-after") == "30"
    assert resp.headers.get("x-ratelimit-remaining") == "0"
    assert resp.headers.get("content-type") == expected


def test_repeated_error_headers_survive_rendering() -> None:
    err = UnauthorizedError("x", challenge=['Basic realm="x"', "Bearer"])

    resp = _error_response(err, error_format="rfc9457", encoder=_ENC)

    assert resp.headers.getall("www-authenticate") == ['Basic realm="x"', "Bearer"]


@pytest.mark.parametrize("fmt", ["default", "rfc7807", "rfc9457"])
def test_debug_member_is_added_at_the_top_level(fmt: str) -> None:
    resp = _error_response(NotFoundError("x"), error_format=fmt, encoder=_ENC, debug={"raised_at": "a.py:1"})

    assert json.loads(resp.body)["debug"] == {"raised_at": "a.py:1"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "parts,expected",
    [
        ((), "#"),
        (("address", "city"), "#/address/city"),
        (("items", 0, "sku"), "#/items/0/sku"),
        (("a/b", "m~n"), "#/a~1b/m~0n"),
        (("first name",), "#/first%20name"),
        (("café",), "#/caf%C3%A9"),
    ],
    ids=["root", "nested", "index", "rfc6901-escapes", "space", "unicode"],
)
def test_json_pointer_escapes_per_rfc6901_and_uri_fragments(parts: tuple[object, ...], expected: str) -> None:
    assert json_pointer(parts) == expected


@pytest.mark.parametrize(
    "issue,expected",
    [
        ({"loc": ["body"], "msg": "bad body"}, {"detail": "bad body", "pointer": "#"}),
        (
            {"loc": ["query", "filter", 0], "msg": "m", "type": "t"},
            {"detail": "m", "in": "query", "parameter": "filter", "pointer": "#/0", "code": "t"},
        ),
        (
            {"loc": ["frame", "data", "age"], "msg": "m"},
            {"detail": "m", "in": "frame", "pointer": "#/data/age"},
        ),
        ({"loc": "header", "msg": "m"}, {"detail": "m", "in": "header", "pointer": "#"}),
        ({"msg": "m"}, {"detail": "m"}),
    ],
    ids=["whole-body", "nested-parameter", "other-root", "scalar-loc", "no-loc"],
)
def test_issue_locations_map_to_pointer_or_parameter(
    issue: dict[str, object], expected: dict[str, object]
) -> None:
    err = ExtractorError("x", detail={"issues": [issue]})

    assert validation_errors(err, title="Unprocessable Entity") == [expected]


def test_issue_entries_that_are_not_mappings_are_skipped() -> None:
    err = ExtractorError("x", detail={"issues": ["not a mapping"]})

    assert validation_errors(err, title="t") is None


@pytest.mark.parametrize(
    "detail,expected",
    [
        ({"field": "q", "errors": "bad value"}, [{"detail": "bad value", "parameter": "q"}]),
        ({"field": "q"}, [{"detail": "q too short", "parameter": "q"}]),
        ({"field": "q", "constraint": "ge"}, [{"detail": "q too short", "parameter": "q", "code": "ge"}]),
    ],
    ids=["string-errors", "message-fallback", "constraint-code"],
)
def test_legacy_extractor_detail_shapes(detail: dict[str, object], expected: list[dict[str, object]]) -> None:
    assert validation_errors(ExtractorFieldError("q too short", detail=detail), title="t") == expected


def test_legacy_mapping_falls_back_to_the_title_without_a_message() -> None:
    err = ExtractorError("", detail={"field": "q"})

    assert validation_errors(err, title="Unprocessable Entity") == [
        {"detail": "Unprocessable Entity", "parameter": "q"}
    ]


def test_validation_errors_tolerate_a_non_mapping_detail() -> None:
    err = ExtractorError("x")
    err.detail = ["not", "a", "dict"]  # type: ignore[assignment]

    assert validation_errors(err, title="t") is None


@pytest.mark.parametrize(
    "path,expected",
    [("/users/7", "/users/7"), ("/files/a b", "/files/a%20b"), ("/café", "/caf%C3%A9")],
    ids=["plain", "space", "unicode"],
)
def test_instance_reference_percent_encodes_the_path(path: str, expected: str) -> None:
    assert instance_reference(path) == expected
