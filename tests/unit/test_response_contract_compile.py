"""Unit: reading a route's declared responses and compiling its contract."""

from __future__ import annotations

import dataclasses
import inspect
from typing import Annotated, Any

import pytest

from lauren import (
    Accepted,
    Created,
    DecoratorUsageError,
    NoContent,
    Ok,
    Response,
    Status,
    StreamingResponse,
    UnresolvableParameterError,
)
from lauren._asgi import _coerce_to_response
from lauren._asgi._contracts import (
    NO_BODY,
    OPAQUE,
    UNTYPED,
    ResponseContractViolation,
    compile_response_plan,
    describe_responses,
    describe_status,
)
from lauren.decorators import RouteMeta
from lauren.serialization import StdlibJSONEncoder
from lauren.streaming import extract_streaming_item_type
from lauren.validation import TypeCompiler

EMPTY = inspect.Parameter.empty


@dataclasses.dataclass
class UserOut:
    id: int
    name: str


@dataclasses.dataclass
class UserInDB(UserOut):
    password_hash: str


@dataclasses.dataclass
class Other:
    x: int


class Opaque:
    """No validator knows this class."""


def _meta(**kwargs: Any) -> RouteMeta:
    return RouteMeta(method="GET", path="/", **kwargs)


def _handler() -> None: ...


def _table(hint: Any, **kwargs: Any) -> Any:
    return describe_responses(
        _meta(**kwargs), hint, fn=_handler, streaming_item=extract_streaming_item_type(hint)
    )


def _members(hint: Any, **kwargs: Any) -> list[tuple[int, Any, Any]]:
    return [(m.status, m.body, m.wrapper) for m in _table(hint, **kwargs).members]


@pytest.mark.parametrize(
    ("hint", "kwargs", "expected"),
    [
        (UserOut, {}, [(200, UserOut, None)]),
        (type(None), {}, [(204, NO_BODY, None)]),
        (NoContent, {}, [(204, NO_BODY, NoContent)]),
        (Created[UserOut], {}, [(201, UserOut, Created)]),
        (Ok[UserOut] | Created[UserOut], {}, [(200, UserOut, Ok), (201, UserOut, Created)]),
        (Annotated[UserOut, Status(206)], {}, [(206, UserOut, None)]),
        (Response, {}, [(200, OPAQUE, None)]),
        (EMPTY, {}, [(200, UNTYPED, None)]),
        (Any, {}, [(200, UNTYPED, None)]),
        (UserOut | None, {}, [(200, UserOut, None), (204, NO_BODY, None)]),
        (tuple[UserOut, int], {}, [(200, UserOut, None)]),
        (tuple[int, str], {}, [(200, tuple[int, str], None)]),
        (Accepted, {}, [(202, UNTYPED, Accepted)]),
        (UserOut, {"status_code": 201}, [(201, UserOut, None)]),
        (type(None), {"status_code": 202}, [(202, NO_BODY, None)]),
        (UserInDB, {"response_model": UserOut}, [(200, UserOut, None)]),
        (EMPTY, {"response_model": UserOut}, [(200, UserOut, None)]),
        (Created[UserInDB], {"response_model": UserOut}, [(201, UserOut, Created)]),
        (Response, {"response_model": UserOut}, [(200, UserOut, None)]),
        (UserOut | Other, {}, [(200, UserOut | Other, None)]),
        (UserOut | Created[Other], {}, [(200, UserOut, None), (201, Other, Created)]),
        (Created[UserOut], {"status_code": 201}, [(201, UserOut, Created)]),
    ],
)
def test_annotation_maps_to_status_table(hint: Any, kwargs: dict[str, Any], expected: list[Any]) -> None:
    assert _members(hint, **kwargs) == expected


def test_streaming_route_records_the_item_type() -> None:
    table = _table(StreamingResponse[UserOut])

    assert (table.stream_item, table.default_status) == (UserOut, 200)


def test_annotated_constraints_survive_on_the_body() -> None:
    from lauren.validation import Ge

    (member,) = _table(Annotated[int, Ge(0), Status(206)]).members

    assert member.body == Annotated[int, Ge(0)]


def test_status_description_is_kept() -> None:
    (member,) = _table(Annotated[UserOut, Status(206, description="Partial")]).members

    assert member.description == "Partial"


@pytest.mark.parametrize(
    ("hint", "kwargs", "reason"),
    [
        (UserOut, {"status_code": 204}, "body_not_allowed"),
        (dict, {"status_code": 304}, "body_not_allowed"),
        (Annotated[UserOut, Status(204)], {}, "body_not_allowed"),
        (Created[UserOut], {"status_code": 200}, "status_conflict"),
        (NoContent, {"status_code": 200}, "status_conflict"),
        (Ok[UserOut] | Ok[Other], {}, "duplicate_status"),
        (UserOut | Ok[Other], {}, "duplicate_status"),
        (Annotated[UserOut, Status(206)], {"status_code": 200}, "status_conflict"),
        (Annotated[UserOut, Status(206)] | Other, {}, "ambiguous_status"),
        (Annotated[UserOut, Status(99)], {}, "invalid_status_code"),
        (Annotated[UserOut, Status(206), Status(207)], {}, "multiple_status"),
        (Annotated[Created[UserOut], Status(200)], {}, "status_conflict"),
    ],
)
def test_contradictory_declarations_are_rejected(hint: Any, kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        _table(hint, **kwargs)

    assert info.value.detail == {"decorator": "route", "target": "_handler", "reason": reason}


@pytest.mark.parametrize(
    ("hint", "kwargs"),
    [
        (type(None), {"status_code": 204}),
        (NoContent, {"status_code": 204}),
        (EMPTY, {"status_code": 204}),
        (Response, {"status_code": 304}),
        (Created[UserOut] | None, {"status_code": 202}),
    ],
)
def test_bodyless_statuses_accept_bodyless_declarations(hint: Any, kwargs: dict[str, Any]) -> None:
    _table(hint, **kwargs)


@pytest.mark.parametrize(
    ("hint", "kwargs", "declared"),
    [
        (UserOut, {}, False),
        (type(None), {}, False),
        (UserOut, {"status_code": 201}, True),
        (Created[UserOut], {}, True),
        (Annotated[UserOut, Status(206)], {}, True),
        (UserOut, {"response_model": UserOut}, False),
    ],
)
def test_status_declared_flags_runtime_work(hint: Any, kwargs: dict[str, Any], declared: bool) -> None:
    assert _table(hint, **kwargs).status_declared is declared


def _plan(hint: Any, mode: str, **kwargs: Any) -> Any:
    table = _table(hint, **kwargs)
    return compile_response_plan(
        table,
        mode=mode,
        compiler=TypeCompiler(),
        coerce=_coerce_to_response,
        controller_cls=_Ctrl,
        fn=_handler,
        route="GET /",
    )


class _Ctrl:
    pass


def test_plain_route_with_contract_off_needs_no_plan() -> None:
    assert _plan(UserOut, "off") is None


def test_untyped_route_never_enforces() -> None:
    assert _plan(EMPTY, "strict") is None


def test_declared_status_compiles_a_plan_without_projectors() -> None:
    plan = _plan(UserOut, "off", status_code=201)

    assert (plan.mode, plan._bare, plan.default_status) == ("off", None, 201)


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_enforced_route_projects_plain_values(mode: str) -> None:
    plan = _plan(UserOut, mode)

    response = plan.coerce(UserInDB(1, "a", "secret"), StdlibJSONEncoder())

    assert response.body == b'{"id":1,"name":"a"}'


def test_unsupported_declared_type_fails_with_unsupported_response_type() -> None:
    with pytest.raises(UnresolvableParameterError) as info:
        _plan(Opaque, "filter")

    assert info.value.detail["reason"] == "unsupported_response_type"
    assert info.value.detail["param"] == "<return>"
    assert info.value.detail["class"] == "_Ctrl"
    assert info.value.detail["handler"] == "_handler"


def test_violation_carries_prefixed_issues() -> None:
    plan = _plan(UserOut, "filter")

    with pytest.raises(ResponseContractViolation) as info:
        plan.coerce({"id": 1}, StdlibJSONEncoder())

    assert [issue.loc for issue in info.value.issues] == [("response", "name")]
    assert info.value.route == "GET /"
    assert "violates its contract" in str(info.value)


@pytest.mark.parametrize(
    ("raw", "status", "body"),
    [
        (None, 204, b""),
        ((UserOut(1, "a"), 201), 201, b'{"id":1,"name":"a"}'),
        ((UserOut(1, "a"), 202, {"X-A": "1"}), 202, b'{"id":1,"name":"a"}'),
        (Response.text("raw", status=418), 418, b"raw"),
    ],
)
def test_plan_coerce_shapes(raw: Any, status: int, body: bytes) -> None:
    plan = _plan(UserOut, "filter")

    response = plan.coerce(raw, StdlibJSONEncoder())

    assert (response.status, response.body) == (status, body)


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_text_route_keeps_its_text_body(mode: str) -> None:
    plan = _plan(str, mode, status_code=201)

    response = plan.coerce("plain", StdlibJSONEncoder())

    assert (response.status, response.body) == (201, b"plain")
    assert response.headers.get("content-type", "").startswith("text/plain")


def test_plan_rejects_an_undeclared_wrapper_under_a_contract() -> None:
    plan = _plan(Ok[UserOut], "filter")

    with pytest.raises(ResponseContractViolation):
        plan.coerce(Created(UserOut(1, "a")), StdlibJSONEncoder())


def test_plan_unwraps_an_undeclared_wrapper_when_off() -> None:
    plan = _plan(Ok[UserOut], "off")

    response = plan.coerce(Created(UserOut(1, "a"), location="/u/1"), StdlibJSONEncoder())

    assert (response.status, response.headers.get("location")) == (201, "/u/1")


def test_plan_rejects_plain_values_on_typed_results_only_routes() -> None:
    plan = _plan(Created[UserOut], "filter")

    with pytest.raises(ResponseContractViolation):
        plan.coerce(UserOut(1, "a"), StdlibJSONEncoder())


def test_plan_sends_no_content_wrapper_headers() -> None:
    plan = _plan(NoContent, "off")

    response = plan.coerce(NoContent(headers={"X-Done": "1"}), StdlibJSONEncoder())

    assert (response.status, response.body, response.headers.get("x-done")) == (204, b"", "1")


def test_markers_have_readable_reprs() -> None:
    assert [repr(m) for m in (NO_BODY, OPAQUE, UNTYPED)] == ["NO_BODY", "OPAQUE", "UNTYPED"]


@pytest.mark.parametrize(("status", "text"), [(200, "Success"), (201, "Created"), (299, "Response")])
def test_describe_status(status: int, text: str) -> None:
    assert describe_status(status) == text
