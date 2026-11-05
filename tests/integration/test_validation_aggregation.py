"""Integration: every failing parameter of a request is reported in one 422 with ``issues``."""

from __future__ import annotations

import dataclasses
from typing import Annotated, Any

import pytest

from lauren import (
    ExtractorError,
    Header,
    Json,
    LaurenFactory,
    Query,
    UnauthorizedError,
    controller,
    get,
    module,
    pipe,
    post,
)
from lauren.extractors import Extraction, ExtractionMarker, QueryField
from lauren.testing import TestClient
from lauren.types import ExecutionContext
from lauren.validation import Le, ValidationPolicy


@dataclasses.dataclass
class Item:
    name: str
    qty: int


class CurrentUser(ExtractionMarker):
    source = "current_user"

    async def extract(self, execution_context: ExecutionContext, extraction: Extraction) -> object:
        if execution_context.request.headers.get("authorization") is None:
            raise UnauthorizedError("not authenticated")
        return "ada"


class Picky(ExtractionMarker):
    source = "picky"

    async def extract(self, execution_context: ExecutionContext, extraction: Extraction) -> object:
        raise ExtractorError("picky says no")


@pipe()
def positive(value: int) -> int:
    if value <= 0:
        raise ValueError("must be positive")
    return value


@controller("/v")
class Endpoints:
    @get("/page")
    async def page(self, page: int, size: int = QueryField(default=10, le=100)) -> dict:
        return {"page": page, "size": size}

    @post("/items/{item_id}")
    async def item(self, item_id: int, verbose: Query[bool], x_count: Header[int], body: Json[Item]) -> dict:
        return {}

    @get("/me")
    async def me(self, page: int, user: CurrentUser) -> dict:
        return {"user": user}

    @get("/picky")
    async def picky(self, page: int, value: Picky) -> dict:
        return {}

    @get("/piped")
    async def piped(self, n: Query[int, positive], m: int) -> dict:
        return {}

    @get("/marked")
    async def marked(self, size: Annotated[int, Le(5)] = 1) -> dict:
        return {"size": size}

    @get("/many")
    async def many(self, a: int, b: int, c: int, d: int) -> dict:
        return {}

    @post("/body")
    async def body(self, body: Json[Item]) -> dict:
        return {"name": body.name}


@module(controllers=[Endpoints])
class AppModule:
    pass


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(LaurenFactory.create(AppModule))


def _detail(response: Any) -> dict[str, Any]:
    assert response.status_code == 422
    return response.json()["error"]["detail"]


def test_two_bad_query_parameters_are_reported_together(client: TestClient) -> None:
    response = client.get("/v/page?page=abc&size=1000")

    payload = response.json()["error"]
    assert response.status_code == 422
    assert payload["code"] == "extractor_error"
    assert payload["message"] == "2 validation errors"
    assert payload["detail"]["field"] == "page"
    assert payload["detail"]["errors"] == [
        "query.page: Input should be a valid integer, unable to parse string as an integer",
        "query.size: Input should be less than or equal to 100",
    ]
    assert payload["detail"]["issues"] == [
        {
            "loc": ["query", "page"],
            "type": "int_parsing",
            "msg": "Input should be a valid integer, unable to parse string as an integer",
        },
        {
            "loc": ["query", "size"],
            "type": "less_than_equal",
            "msg": "Input should be less than or equal to 100",
            "ctx": {"le": 100},
        },
    ]


def test_one_bad_scalar_keeps_its_code_message_and_detail(client: TestClient) -> None:
    response = client.get("/v/page?page=1&size=1000")

    payload = response.json()["error"]
    assert payload["code"] == "extractor_field_error"
    assert payload["message"] == "size must be <= 100"
    assert payload["detail"]["field"] == "size"
    assert payload["detail"]["constraint"] == "le"
    assert payload["detail"]["issues"][0]["loc"] == ["query", "size"]


def test_missing_parameter_has_a_missing_issue(client: TestClient) -> None:
    payload = client.get("/v/page").json()["error"]

    assert payload["message"] == "missing query parameter 'page'"
    assert payload["detail"]["issues"] == [
        {"loc": ["query", "page"], "type": "missing", "msg": "Field required"}
    ]


def test_path_query_header_and_body_failures_share_one_response(client: TestClient) -> None:
    response = client.post(
        "/v/items/abc?verbose=yes", headers={"x-count": "many"}, json={"name": "a", "qty": 1}
    )

    detail = _detail(response)
    assert [issue["loc"] for issue in detail["issues"]] == [["path", "item_id"], ["header", "x-count"]]

    response = client.post("/v/items/abc?verbose=1", headers={"x-count": "many"}, json=[])
    assert [issue["loc"] for issue in _detail(response)["issues"]] == [
        ["path", "item_id"],
        ["header", "x-count"],
        ["body"],
    ]


def test_four_sources_fail_together(client: TestClient) -> None:
    response = client.post(
        "/v/items/abc", headers={"x-count": "many", "content-type": "application/json"}, content=b'{"qty": 1}'
    )

    locs = [issue["loc"] for issue in _detail(response)["issues"]]
    assert locs == [["path", "item_id"], ["query", "verbose"], ["header", "x-count"], ["body", "name"]]


def test_an_authentication_error_short_circuits_validation(client: TestClient) -> None:
    response = client.get("/v/me?page=abc")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


def test_a_custom_extractor_error_joins_the_issue_list(client: TestClient) -> None:
    detail = _detail(client.get("/v/picky?page=abc"))

    assert detail["issues"][1] == {
        "loc": ["picky", "value"],
        "type": "value_error",
        "msg": "picky says no",
        "ctx": {"error": "picky says no"},
    }


def test_a_pipe_failure_is_a_value_error_at_the_parameter(client: TestClient) -> None:
    detail = _detail(client.get("/v/piped?n=-1&m=x"))

    assert [(i["loc"], i["type"]) for i in detail["issues"]] == [
        (["query", "n"], "value_error"),
        (["query", "m"], "int_parsing"),
    ]
    assert "must be positive" in detail["issues"][0]["msg"]


def test_marker_constraints_on_bare_parameters(client: TestClient) -> None:
    assert client.get("/v/marked?size=3").json() == {"size": 3}
    detail = _detail(client.get("/v/marked?size=9"))
    assert detail["issues"][0]["ctx"] == {"le": 5}


def test_max_issues_caps_the_list() -> None:
    client = TestClient(LaurenFactory.create(AppModule, validation=ValidationPolicy(max_issues=2)))

    detail = _detail(client.get("/v/many?a=x&b=x&c=x&d=x"))

    assert len(detail["issues"]) == 2


def test_body_too_large_is_413_not_422() -> None:
    client = TestClient(LaurenFactory.create(AppModule, max_body_size=8))

    response = client.post("/v/body", json={"name": "a very long name", "qty": 1})

    assert response.status_code == 413


def test_invalid_json_has_a_json_invalid_issue(client: TestClient) -> None:
    response = client.post("/v/body", content=b"{nope", headers={"content-type": "application/json"})

    detail = _detail(response)
    assert response.json()["error"]["message"].startswith("invalid JSON")
    assert detail["issues"][0]["loc"] == ["body"]
    assert detail["issues"][0]["type"] == "json_invalid"


def test_missing_body_has_a_missing_issue(client: TestClient) -> None:
    detail = _detail(client.post("/v/body"))

    assert detail["issues"] == [{"loc": ["body"], "type": "missing", "msg": "Field required"}]


# ---------------------------------------------------------------------------
# Wire formats and input echo
# ---------------------------------------------------------------------------


def test_rfc7807_carries_the_issues_in_the_errors_member() -> None:
    client = TestClient(LaurenFactory.create(AppModule, error_format="rfc7807"))

    body = client.get("/v/page?page=abc&size=1000").json()

    assert body["status"] == 422
    assert [issue["type"] for issue in body["errors"]["issues"]] == ["int_parsing", "less_than_equal"]


def test_rfc9457_builds_the_errors_array_from_the_issues() -> None:
    client = TestClient(LaurenFactory.create(AppModule, error_format="rfc9457"))

    body = client.get("/v/page?page=abc&size=1000").json()

    assert body["errors"] == [
        {
            "detail": "Input should be a valid integer, unable to parse string as an integer",
            "in": "query",
            "parameter": "page",
            "code": "int_parsing",
        },
        {
            "detail": "Input should be less than or equal to 100",
            "in": "query",
            "parameter": "size",
            "code": "less_than_equal",
        },
    ]


def test_input_is_not_echoed_by_default(client: TestClient) -> None:
    detail = _detail(client.get("/v/page?page=abc"))

    assert "input" not in detail["issues"][0]


def test_input_is_echoed_when_debug_is_on() -> None:
    client = TestClient(LaurenFactory.create(AppModule, debug=True))

    detail = _detail(client.get("/v/page?page=abc"))

    assert detail["issues"][0]["input"] == "abc"


@pytest.mark.parametrize(("include_input", "expected"), [(True, True), (False, False)])
def test_include_input_overrides_debug(include_input: bool, expected: bool) -> None:
    client = TestClient(
        LaurenFactory.create(AppModule, debug=True, validation=ValidationPolicy(include_input=include_input))
    )

    detail = _detail(client.get("/v/page?page=abc"))

    assert ("input" in detail["issues"][0]) is expected
