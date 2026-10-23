"""Unit: route-decorator options (``status_code=``, ``response_validation=``,
``response_headers=``) and ``@raises`` — decoration-time validation."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import (
    ConflictError,
    DecoratorUsageError,
    HTTPError,
    Lauren,
    NotFoundError,
    UnauthorizedError,
    controller,
    get,
    post,
    raises,
)
from lauren.decorators import (
    RAISES_META,
    ROUTE_META,
    RaisesSpec,
    RouteMeta,
    _ROUTE_OPTION_VALIDATORS,
    _validate_route_options,
)


def _handler() -> None:
    return None


@pytest.mark.parametrize("status", [100, 201, 204, 304, 599])
def test_status_code_in_range_is_stored_on_route_meta(status: int) -> None:
    fn = post("/", status_code=status)(_handler_copy())

    meta: RouteMeta = getattr(fn, ROUTE_META)[0]

    assert meta.status_code == status


@pytest.mark.parametrize("status", [99, 600, True, "201", 201.0])
def test_invalid_status_code_is_rejected_at_decoration(status: Any) -> None:
    decorate = post("/", status_code=status)

    with pytest.raises(DecoratorUsageError) as info:
        decorate(_handler_copy())

    assert info.value.detail["decorator"] == "post"
    assert info.value.detail["option"] == "status_code"
    assert info.value.detail["reason"] == "invalid_status_code"
    assert info.value.detail["target"].endswith("handler")


@pytest.mark.parametrize("mode", ["off", "filter", "strict"])
def test_response_validation_modes_are_accepted(mode: str) -> None:
    fn = get("/", response_validation=mode)(_handler_copy())

    assert getattr(fn, ROUTE_META)[0].response_validation == mode


@pytest.mark.parametrize("mode", ["on", "FILTER", 1, ""])
def test_unknown_response_validation_is_rejected(mode: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        get("/", response_validation=mode)(_handler_copy())

    assert info.value.detail["reason"] == "invalid_response_validation"


def test_response_headers_accept_descriptions_and_header_objects() -> None:
    spec = {"Location": "URL of the user", "X-Rate": {"description": "quota", "schema": {"type": "integer"}}}

    fn = post("/", response_headers=spec)(_handler_copy())

    assert getattr(fn, ROUTE_META)[0].response_headers == spec


@pytest.mark.parametrize(
    "headers",
    [["Location"], {"": "empty name"}, {3: "not a name"}, {"X-A": 3}],
)
def test_invalid_response_headers_are_rejected(headers: Any) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        post("/", response_headers=headers)(_handler_copy())

    assert info.value.detail["reason"] == "invalid_response_headers"


def test_unset_options_default_to_none_and_empty() -> None:
    meta: RouteMeta = getattr(get("/")(_handler_copy()), ROUTE_META)[0]

    assert (meta.status_code, meta.response_validation, meta.response_headers) == (None, None, {})


def test_option_table_lists_every_f01_option() -> None:
    assert set(_ROUTE_OPTION_VALIDATORS) >= {"status_code", "response_validation", "response_headers"}


def test_validate_route_options_passes_unknown_keys_through() -> None:
    assert _validate_route_options("get", _handler, {"summary": "x", "status_code": None}) == {
        "summary": "x",
        "status_code": None,
    }


def test_imperative_app_validates_options_at_decoration() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None)

    with pytest.raises(DecoratorUsageError) as info:

        @app.post("/", status_code=700)
        async def create() -> None: ...

    assert info.value.detail["decorator"] == "app.post"


# ---------------------------------------------------------------------------
# @raises
# ---------------------------------------------------------------------------


class _TeapotError(HTTPError):
    status_code = 418
    code = "teapot"


class _NoStatusError(HTTPError):
    status_code = None


def test_raises_records_specs_in_source_order() -> None:
    @raises(NotFoundError, description="no such user")
    @raises(ConflictError)
    def handler() -> None: ...

    specs = getattr(handler, RAISES_META)

    assert [(s.exc_type, s.description) for s in specs] == [
        (NotFoundError, "no such user"),
        (ConflictError, None),
    ]


def test_raises_dedupes_repeated_error_classes_keeping_the_first() -> None:
    @raises(NotFoundError, description="top")
    @raises(NotFoundError, description="bottom")
    def handler() -> None: ...

    assert [s.description for s in getattr(handler, RAISES_META)] == ["top"]


def test_raises_spec_exposes_status_and_code() -> None:
    spec = RaisesSpec(_TeapotError, "short and stout")

    assert (spec.status_code, spec.code) == (418, "teapot")


def test_class_level_raises_reads_only_own_dict() -> None:
    @raises(UnauthorizedError)
    @controller("/base")
    class Base: ...

    @controller("/derived")
    class Derived(Base): ...

    raises(ConflictError)(Derived)

    assert [s.exc_type for s in Derived.__dict__[RAISES_META]] == [ConflictError]
    assert [s.exc_type for s in Base.__dict__[RAISES_META]] == [UnauthorizedError]


def test_raises_returns_the_same_object() -> None:
    def handler() -> None: ...

    assert raises(NotFoundError)(handler) is handler


@pytest.mark.parametrize(
    ("args", "reason"),
    [
        ((ValueError,), "not_http_error"),
        ((_NoStatusError,), "no_status_code"),
        ((), "no_exceptions"),
    ],
)
def test_invalid_raises_arguments_are_rejected(args: tuple[Any, ...], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        raises(*args)

    assert info.value.detail["decorator"] == "raises"
    assert info.value.detail["reason"] == reason


def test_bare_raises_on_a_function_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:

        @raises  # type: ignore[arg-type]
        def handler() -> None: ...

    assert info.value.detail["decorator"] == "raises"


def test_bare_raises_on_a_class_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError):

        @raises  # type: ignore[arg-type]
        class Controller: ...


def test_raises_description_must_be_a_string() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        raises(NotFoundError, description=3)  # type: ignore[arg-type]

    assert info.value.detail["reason"] == "invalid_description"


def _handler_copy() -> Any:
    def handler() -> None:
        return None

    return handler
