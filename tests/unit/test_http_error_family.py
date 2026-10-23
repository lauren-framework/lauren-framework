"""Unit tests for the built-in HTTP error family, error headers and instance overrides (F05)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import lauren
from lauren.exceptions import (
    BadRequestError,
    ConflictError,
    ExtractorError,
    ForbiddenError,
    GoneError,
    HTTPError,
    MethodNotAllowedError,
    NotAcceptableError,
    NotFoundError,
    PreconditionFailedError,
    RequestTimeoutError,
    RouteNotFoundError,
    ServiceUnavailableError,
    StartupError,
    TooManyRequestsError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
)

_FAMILY: list[tuple[type[HTTPError], int, str]] = [
    (BadRequestError, 400, "bad_request"),
    (NotFoundError, 404, "not_found"),
    (NotAcceptableError, 406, "not_acceptable"),
    (ConflictError, 409, "conflict"),
    (GoneError, 410, "gone"),
    (PreconditionFailedError, 412, "precondition_failed"),
    (UnsupportedMediaTypeError, 415, "unsupported_media_type"),
    (TooManyRequestsError, 429, "too_many_requests"),
    (ServiceUnavailableError, 503, "service_unavailable"),
    (RequestTimeoutError, 504, "request_timeout"),
]

_RETRY_AFTER_CLASSES = [TooManyRequestsError, ServiceUnavailableError, RequestTimeoutError]


# ---------------------------------------------------------------------------
# The family
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cls,status,code", _FAMILY, ids=[c.__name__ for c, _, _ in _FAMILY])
def test_family_class_has_status_and_code(cls: type[HTTPError], status: int, code: str) -> None:
    err = cls("boom", detail={"k": 1})

    assert isinstance(err, HTTPError)
    assert (err.status_code, err.code) == (status, code)


@pytest.mark.parametrize("cls,status,code", _FAMILY, ids=[c.__name__ for c, _, _ in _FAMILY])
def test_family_payload_shape_is_the_classic_envelope(cls: type[HTTPError], status: int, code: str) -> None:
    err = cls("boom", detail={"k": 1})

    assert err.to_payload() == {"error": {"code": code, "message": "boom", "detail": {"k": 1}}}


@pytest.mark.parametrize("cls", [c for c, _, _ in _FAMILY], ids=[c.__name__ for c, _, _ in _FAMILY])
def test_family_class_is_importable_from_the_package(cls: type[HTTPError]) -> None:
    assert getattr(lauren, cls.__name__) is cls
    assert cls.__name__ in lauren.__all__


def test_route_not_found_is_a_not_found_error_with_its_own_code() -> None:
    err = RouteNotFoundError("no route")

    assert isinstance(err, NotFoundError)
    assert (err.status_code, err.code) == (404, "route_not_found")


def test_request_timeout_envelope_matches_the_timeout_middleware_code() -> None:
    err = RequestTimeoutError("request timed out")

    assert err.status_code == 504
    assert err.to_payload()["error"]["code"] == "request_timeout"


# ---------------------------------------------------------------------------
# headers=
# ---------------------------------------------------------------------------


def test_errors_carry_no_headers_by_default() -> None:
    assert NotFoundError("x").headers == ()


@pytest.mark.parametrize(
    "headers",
    [{"Cache-Control": "no-store"}, [("Cache-Control", "no-store")]],
    ids=["mapping", "iterable"],
)
def test_headers_accept_mapping_or_pairs_and_lower_case_names(headers: object) -> None:
    err = NotFoundError("x", headers=headers)  # type: ignore[arg-type]

    assert err.headers == (("cache-control", "no-store"),)


def test_headers_keep_repeated_names_from_the_iterable_form() -> None:
    err = UnauthorizedError("x", headers=[("WWW-Authenticate", "Basic"), ("www-authenticate", "Bearer")])

    assert err.headers == (("www-authenticate", "Basic"), ("www-authenticate", "Bearer"))


def test_headers_are_available_on_startup_errors_too() -> None:
    assert StartupError("x", headers={"x-a": "1"}).headers == (("x-a", "1"),)


@pytest.mark.parametrize(
    "headers",
    [
        {"x-evil": "a\r\nSet-Cookie: s=1"},
        {"x-evil": "a\nb"},
        {"x-evil": "a\x00b"},
        {"x-ev\nil": "a"},
        {"": "a"},
        {"x evil": "a"},
        {"x:evil": "a"},
        {"x-snow": "☃"},
    ],
    ids=[
        "crlf-value",
        "lf-value",
        "nul-value",
        "lf-name",
        "empty-name",
        "space-name",
        "colon-name",
        "non-latin1",
    ],
)
def test_headers_reject_invalid_names_and_values(headers: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        NotFoundError("x", headers=headers)


@pytest.mark.parametrize("name", ["Content-Type", "content-length"])
def test_headers_reject_framework_owned_names(name: str) -> None:
    with pytest.raises(ValueError, match="set by the framework"):
        NotFoundError("x", headers={name: "text/plain"})


@pytest.mark.parametrize(
    "headers",
    [{"x-a": 1}, [("x-a",)], ["x-a: 1"], "x-a: 1"],
    ids=["non-str-value", "short-pair", "string-item", "plain-string"],
)
def test_headers_reject_malformed_shapes(headers: object) -> None:
    with pytest.raises(TypeError):
        NotFoundError("x", headers=headers)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Typed sugar: challenge=, retry_after=, allow=
# ---------------------------------------------------------------------------


def test_unauthorized_challenge_sets_www_authenticate() -> None:
    err = UnauthorizedError("bad token", challenge='Bearer realm="api", error="invalid_token"')

    assert err.headers == (("www-authenticate", 'Bearer realm="api", error="invalid_token"'),)


def test_unauthorized_accepts_several_challenges() -> None:
    err = UnauthorizedError("x", challenge=['Basic realm="x"', "Bearer"])

    assert [v for k, v in err.headers if k == "www-authenticate"] == ['Basic realm="x"', "Bearer"]


def test_unauthorized_challenge_comes_before_explicit_headers() -> None:
    err = UnauthorizedError("x", challenge="Bearer", headers={"set-cookie": "sid=; Max-Age=0"})

    assert err.headers == (("www-authenticate", "Bearer"), ("set-cookie", "sid=; Max-Age=0"))


def test_unauthorized_rejects_an_empty_challenge_list() -> None:
    with pytest.raises(ValueError, match="challenge"):
        UnauthorizedError("x", challenge=[])


def test_unauthorized_rejects_a_challenge_with_crlf() -> None:
    with pytest.raises(ValueError):
        UnauthorizedError("x", challenge="Bearer\r\nX-Evil: 1")


@pytest.mark.parametrize("cls", _RETRY_AFTER_CLASSES, ids=[c.__name__ for c in _RETRY_AFTER_CLASSES])
@pytest.mark.parametrize(
    "value,expected",
    [
        (30, "30"),
        (0, "0"),
        (1.2, "2"),
        (timedelta(minutes=2), "120"),
        (datetime(2026, 10, 23, 18, 0, tzinfo=timezone.utc), "Fri, 23 Oct 2026 18:00:00 GMT"),
        (datetime(2026, 10, 23, 20, 0, tzinfo=timezone(timedelta(hours=2))), "Fri, 23 Oct 2026 18:00:00 GMT"),
    ],
    ids=["int", "zero", "float-rounds-up", "timedelta", "utc-datetime", "offset-datetime"],
)
def test_retry_after_renders_seconds_or_http_date(cls: type[HTTPError], value: object, expected: str) -> None:
    err = cls("later", retry_after=value)  # type: ignore[call-arg]

    assert ("retry-after", expected) in err.headers
    assert err.retry_after == value  # type: ignore[attr-defined]


@pytest.mark.parametrize("cls", _RETRY_AFTER_CLASSES, ids=[c.__name__ for c in _RETRY_AFTER_CLASSES])
def test_retry_after_is_optional(cls: type[HTTPError]) -> None:
    err = cls("later")

    assert err.headers == ()
    assert err.retry_after is None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "value,error",
    [
        (datetime(2026, 10, 23, 18, 0), ValueError),
        (-1, ValueError),
        (float("inf"), ValueError),
        (float("nan"), ValueError),
        (timedelta(seconds=-5), ValueError),
        (True, TypeError),
        ("30", TypeError),
    ],
    ids=["naive-datetime", "negative", "infinite", "nan", "negative-timedelta", "bool", "string"],
)
def test_retry_after_rejects_invalid_values(value: object, error: type[Exception]) -> None:
    with pytest.raises(error):
        TooManyRequestsError("x", retry_after=value)  # type: ignore[arg-type]


def test_retry_after_is_only_accepted_by_the_retryable_classes() -> None:
    with pytest.raises(TypeError):
        NotFoundError("x", retry_after=3)  # type: ignore[call-arg]


def test_challenge_is_only_accepted_by_unauthorized() -> None:
    with pytest.raises(TypeError):
        ForbiddenError("x", challenge="Bearer")  # type: ignore[call-arg]


def test_method_not_allowed_allow_sets_header_and_detail() -> None:
    err = MethodNotAllowedError("use PUT", allow=["PUT", "PATCH"])

    assert err.headers == (("allow", "PUT, PATCH"),)
    assert err.detail == {"allow": ["PUT", "PATCH"]}
    assert err.allow == ["PUT", "PATCH"]


def test_method_not_allowed_without_allow_sends_no_header() -> None:
    err = MethodNotAllowedError("nope")

    assert err.headers == ()
    assert err.detail == {"allow": []}


def test_method_not_allowed_merges_an_explicit_detail() -> None:
    err = MethodNotAllowedError("use PUT", allow=["PUT"], detail={"hint": "idempotent"})

    assert err.detail == {"allow": ["PUT"], "hint": "idempotent"}


# ---------------------------------------------------------------------------
# Instance-level overrides
# ---------------------------------------------------------------------------


def test_instance_overrides_shadow_class_attributes() -> None:
    err = HTTPError("I'm a teapot", status_code=418, code="teapot")

    assert (err.status_code, err.code) == (418, "teapot")
    assert (HTTPError.status_code, HTTPError.code) == (500, "http_error")


def test_instance_code_override_on_a_family_class_keeps_its_status() -> None:
    err = ConflictError("version mismatch", code="stale_version", detail={"expected": 4, "got": 3})

    assert (err.status_code, err.code) == (409, "stale_version")
    assert ConflictError.code == "conflict"


@pytest.mark.parametrize(
    "status", [399, 600, True, "404", 404.0], ids=["low", "high", "bool", "str", "float"]
)
def test_instance_status_override_must_be_an_error_status(status: object) -> None:
    with pytest.raises(ValueError, match="status_code"):
        HTTPError("x", status_code=status)  # type: ignore[arg-type]


@pytest.mark.parametrize("code", ["", 7], ids=["empty", "non-str"])
def test_instance_code_override_must_be_a_non_empty_string(code: object) -> None:
    with pytest.raises(ValueError, match="code"):
        HTTPError("x", code=code)  # type: ignore[arg-type]


def test_problem_type_and_title_default_to_none() -> None:
    assert (HTTPError.problem_type, HTTPError.problem_title) == (None, None)


def test_subclass_with_its_own_init_still_has_empty_headers() -> None:
    class Legacy(ExtractorError):
        def __init__(self) -> None:  # never calls super().__init__
            Exception.__init__(self, "legacy")

    assert Legacy().headers == ()
