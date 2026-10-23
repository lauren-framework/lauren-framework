"""Integration: the startup-compiled validation plans of each parameter source and mode."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import sys
from typing import Annotated, Any, Literal, TypedDict

import pytest

from lauren import (
    Cookie,
    Form,
    Header,
    Json,
    LaurenFactory,
    Path,
    Query,
    UnresolvableParameterError,
    controller,
    get,
    module,
    post,
)
from lauren._schema._compiler import TypeCompiler
from lauren._schema._issues import ValidationIssue
from lauren._schema._params import ValidationRuntime, merge_failures
from lauren.exceptions import ExtractorError
from lauren.extractors import Extraction
from lauren.logging import InMemoryLogger
from lauren.testing import TestClient, WsTestClient
from lauren.validation import ValidationPolicy
from lauren.websockets import WebSocket, on_message, ws_controller


@dataclasses.dataclass
class DUser:
    name: str
    age: int


class Filters(TypedDict):
    q: str
    page: int


@controller("/p")
class Endpoints:
    @get("/ids/{ids}")
    async def ids(self, ids: Path[list[int]]) -> dict:
        return {"ids": ids}

    @get("/headers")
    async def headers(self, x_tags: Header[list[str]], x_limit: Header[int | None] = None) -> dict:
        return {"tags": x_tags, "limit": x_limit}

    @get("/cookie")
    async def cookie(self, sid: Cookie[int]) -> dict:
        return {"sid": sid}

    @post("/user")
    async def user(self, body: Json[DUser]) -> dict:
        return {"ok": True}

    @get("/td")
    async def td(self, f: Query[Filters]) -> dict:
        return {"f": f}

    @get("/optional-model")
    async def optional_model(self, f: Query[DUser | None] = None) -> dict:
        return {"f": None if f is None else dataclasses.asdict(f)}

    @get("/choice")
    async def choice(self, mode: Query[Literal["a", "b"]]) -> dict:
        return {"mode": mode}


@module(controllers=[Endpoints])
class AppModule:
    pass


def _client(mode: str = "warn") -> TestClient:
    return TestClient(LaurenFactory.create(AppModule, validation=ValidationPolicy(stdlib_models=mode)))


@pytest.mark.parametrize(("mode", "ids"), [("warn", [1, 2]), ("enforce", [1, 2])])
def test_path_lists_split_on_commas(mode: str, ids: list[int]) -> None:
    assert _client(mode).get("/p/ids/1,2").json() == {"ids": ids}


def test_path_list_items_are_located_by_index_under_enforce() -> None:
    detail = _client("enforce").get("/p/ids/1,x").json()["error"]["detail"]

    assert detail["issues"][0]["loc"] == ["path", "ids", 1]


def test_header_lists_and_optional_headers() -> None:
    client = _client("enforce")

    assert client.get("/p/headers", headers={"x-tags": "a,b"}).json() == {"tags": ["a", "b"], "limit": None}
    assert client.get("/p/headers", headers={"x-tags": "a", "x-limit": "3"}).json() == {
        "tags": ["a"],
        "limit": 3,
    }


def test_missing_header_and_cookie_report_missing_issues() -> None:
    client = _client()

    header = client.get("/p/headers").json()["error"]
    cookie = client.get("/p/cookie").json()["error"]

    assert header["message"] == "missing header 'x-tags'"
    assert header["detail"]["issues"] == [
        {"loc": ["header", "x-tags"], "type": "missing", "msg": "Field required"}
    ]
    assert cookie["detail"]["issues"][0]["loc"] == ["cookie", "sid"]


def test_cookie_values_are_coerced() -> None:
    response = _client().get("/p/cookie", headers={"cookie": "sid=7"})

    assert response.json() == {"sid": 7}


def test_legacy_crashes_are_wrapped_and_get_native_issues() -> None:
    response = _client("legacy").post("/p/user", json=[1])

    error = response.json()["error"]
    assert response.status_code == 422
    assert error["message"].startswith("failed to extract 'body'")
    assert error["detail"]["issues"][0]["type"] == "model_type"


def test_non_utf8_json_is_invalid_json() -> None:
    response = _client().post(
        "/p/user", content=b'{"name": "\xff"}', headers={"content-type": "application/json"}
    )

    error = response.json()["error"]
    assert error["message"].startswith("failed to extract 'body'")
    assert error["detail"]["issues"][0]["type"] == "json_invalid"


def test_typeddict_query_models_keep_the_legacy_lookup_in_warn_mode() -> None:
    response = _client("warn").get("/p/td?q=x&page=2")

    assert response.status_code == 422
    assert response.json()["error"]["message"] == "missing query parameter 'f'"


def test_typeddict_query_models_read_fields_under_enforce() -> None:
    assert _client("enforce").get("/p/td?q=x&page=2").json() == {"f": {"q": "x", "page": 2}}


def test_optional_query_model_without_fields_is_none() -> None:
    assert _client("enforce").get("/p/optional-model").json() == {"f": None}
    assert _client("enforce").get("/p/optional-model?name=a&age=3").json() == {"f": {"name": "a", "age": 3}}


def test_literal_query_values_are_checked_under_enforce() -> None:
    assert _client("warn").get("/p/choice?mode=c").json() == {"mode": "c"}
    assert (
        _client("enforce").get("/p/choice?mode=c").json()["error"]["detail"]["issues"][0]["type"]
        == "literal_error"
    )


# ---------------------------------------------------------------------------
# pydantic query / form models
# ---------------------------------------------------------------------------


def test_pydantic_query_and_form_models() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Search(pydantic.BaseModel):
        q: str
        tags: list[str] = []
        page_size: int = pydantic.Field(default=10, alias="pageSize")

    @controller("/py")
    class PydEndpoints:
        @get("/search")
        async def search(self, s: Query[Search]) -> dict:
            return s.model_dump()

        @get("/maybe")
        async def maybe(self, s: Query[Search | None] = None) -> dict:
            return {"s": None if s is None else s.q}

        @post("/form")
        async def form(self, s: Form[Search]) -> dict:
            return s.model_dump()

    @module(controllers=[PydEndpoints])
    class PydModule:
        pass

    client = TestClient(LaurenFactory.create(PydModule))
    form_headers = {"content-type": "application/x-www-form-urlencoded"}

    assert client.get("/py/search?q=x&tags=a&pageSize=5").json() == {"q": "x", "tags": ["a"], "page_size": 5}
    assert client.get("/py/maybe").json() == {"s": None}
    error = client.get("/py/search?pageSize=x").json()["error"]
    assert error["message"] == "Validation failed for field 's'"
    assert [(i["loc"], i["type"]) for i in error["detail"]["issues"]] == [
        (["query", "q"], "missing"),
        (["query", "pageSize"], "int_parsing"),
    ]
    assert client.post("/py/form", content=b"q=x&tags=a&tags=b", headers=form_headers).json()["tags"] == [
        "a",
        "b",
    ]
    form_error = client.post("/py/form", content=b"pageSize=x", headers=form_headers).json()["error"]
    assert form_error["detail"]["issues"][0]["loc"] == ["form", "q"]


# ---------------------------------------------------------------------------
# Startup: types the engine cannot compile
# ---------------------------------------------------------------------------


class _Opaque:
    pass


def test_backend_compile_failures_follow_the_mode(caplog: pytest.LogCaptureFixture) -> None:
    pydantic = pytest.importorskip("pydantic")

    @controller("/bad")
    class BadEndpoints:
        @post("/")
        async def create(self, body: Json[Annotated[_Opaque, pydantic.Field(discriminator="kind")]]) -> dict:
            return {}

    @module(controllers=[BadEndpoints])
    class BadModule:
        pass

    with caplog.at_level(logging.WARNING, logger="lauren"):
        LaurenFactory.create(BadModule)
    assert any("not validated natively" in r.getMessage() for r in caplog.records)
    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(BadModule, validation=ValidationPolicy(stdlib_models="enforce"))
    assert info.value.detail["reason"] == "unsupported_type"


# ---------------------------------------------------------------------------
# WebSocket payloads in the pre-engine modes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Msg:
    text: str
    count: int


@ws_controller("/ws")
class Gateway:
    @on_message("msg")
    async def msg(self, ws: WebSocket, body: Json[Msg]) -> None:
        await ws.send_json({"count": body.count})


@module(controllers=[Gateway])
class WsModule:
    pass


async def _send(app: Any, payload: Any) -> dict[str, Any]:
    async with WsTestClient(app).connect("/ws") as ws:
        await ws.send_json({"event": "msg", "data": payload})
        return await ws.receive_json()


def test_websocket_warn_mode_logs_what_enforce_would_reject() -> None:
    pytest.importorskip("pydantic")
    log = InMemoryLogger()
    app = LaurenFactory.create(WsModule, logger=log)

    reply = asyncio.run(_send(app, {"text": "x", "count": True}))

    assert reply == {"count": 1}  # pydantic's TypeAdapter (the 1.x validator) accepts True
    assert any("WEBSOCKET /ws #msg" in r.message for r in log.records if r.context == "Validation")


def test_websocket_warn_mode_rejections_carry_native_issues() -> None:
    app = LaurenFactory.create(WsModule)

    frame = asyncio.run(_send(app, {"text": "x"}))

    assert frame["error"]["detail"]["issues"] == [
        {"loc": ["frame", "data", "count"], "type": "missing", "msg": "Field required"}
    ]


@pytest.fixture
def block_pydantic(monkeypatch: pytest.MonkeyPatch, _preload_lauren: None) -> None:
    import lauren.streaming

    monkeypatch.setitem(sys.modules, "pydantic", None)  # type: ignore[arg-type]
    monkeypatch.setattr(lauren.streaming, "_PYDANTIC_AVAILABLE", False)


def test_websocket_legacy_mode_without_pydantic_is_shallow(block_pydantic: None) -> None:
    app = LaurenFactory.create(WsModule, validation=ValidationPolicy(stdlib_models="legacy"))

    assert asyncio.run(_send(app, {"text": "x", "count": "many"})) == {"count": "many"}
    frame = asyncio.run(_send(app, {"text": "x"}))
    assert frame["error"]["detail"]["issues"][0]["type"] == "missing"


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------


def test_a_single_failure_is_capped_at_max_issues() -> None:
    runtime = ValidationRuntime(TypeCompiler(ValidationPolicy(max_issues=1)), include_input=False)
    issues = [ValidationIssue(("query", str(i)), "t", "m") for i in range(3)]
    err = ExtractorError.from_issues(issues)
    ext = Extraction(
        name="q", source="query", inner_type=int, field_descriptor=None, default=None, has_default=False
    )

    merged = merge_failures([(ext, err)], runtime)

    assert merged is err
    assert len(merged.detail["issues"]) == 1


def test_warn_memory_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import lauren._schema._params as params

    monkeypatch.setattr(params, "_WARN_LIMIT", 2)
    log = InMemoryLogger()
    runtime = ValidationRuntime(TypeCompiler(), include_input=False, logger=log)

    for route in ("a", "b", "c"):
        runtime.warn(route, [ValidationIssue(("body",), "t", "m")])

    assert [r.extra["route"] for r in log.records] == ["a", "b"]


# ---------------------------------------------------------------------------
# Less common shapes
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Exploding:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, int) and self.value < 0:
            raise RuntimeError("boom")


@dataclasses.dataclass
class OpaqueModel:
    thing: _Opaque
    name: str = ""


@controller("/more")
class MoreEndpoints:
    @get("/list")
    async def as_list(self, n: Query[list[int]]) -> dict:
        return {"n": n}

    @get("/explicit-path")
    async def explicit_path(self, item: Path[int] = 5) -> dict:
        return {"item": item}

    @get("/required-path")
    async def required_path(self, item: Path[int]) -> dict:
        return {"item": item}

    @post("/maybe-body")
    async def maybe_body(self, body: Json[DUser] | None = None) -> dict:
        return {"body": body is not None}

    @post("/explode")
    async def explode(self, body: Json[Exploding]) -> dict:
        return {"value": body.value}

    @get("/alias")
    async def alias(
        self, page: Query[int, __import__("lauren.validation").validation.Alias("pageNum")]
    ) -> dict:
        return {"page": page}

    @get("/bare-list")
    async def bare_list(self, n: Query[list]) -> dict:
        return {"n": n}

    @post("/raw-form")
    async def raw_form(self, data: Form[dict]) -> dict:
        return {"data": data}

    @get("/opaque")
    async def opaque(self, m: Query[OpaqueModel]) -> dict:
        return {"type": type(m).__name__}

    @get("/td-raw")
    async def td_raw(self, f: Query[Filters]) -> dict:
        return {"f": f}


@module(controllers=[MoreEndpoints])
class MoreModule:
    pass


@pytest.fixture(scope="module")
def more() -> TestClient:
    return TestClient(LaurenFactory.create(MoreModule))


def test_query_list_failures_keep_the_legacy_message(more: TestClient) -> None:
    error = more.get("/more/list?n=1&n=x").json()["error"]

    assert error["message"] == "invalid literal for int() with base 10: 'x'"
    assert error["detail"]["issues"][0]["loc"] == ["query", "n", 1]


def test_explicit_path_markers_outside_the_template(more: TestClient) -> None:
    assert more.get("/more/explicit-path").json() == {"item": 5}
    error = more.get("/more/required-path").json()["error"]
    assert error["message"] == "missing path parameter 'item'"


def test_optional_json_bodies_default_when_absent(more: TestClient) -> None:
    assert more.post("/more/maybe-body").json() == {"body": False}


def test_a_crashing_shadow_validation_never_breaks_the_request(more: TestClient) -> None:
    # Native validation builds ``Exploding(-1)`` whose __post_init__ raises a
    # RuntimeError; the legacy value (built from the same payload) is "-1".
    assert more.post("/more/explode", json={"value": "-1"}).json() == {"value": "-1"}


def test_alias_markers_rename_parameters(more: TestClient) -> None:
    assert more.get("/more/alias?pageNum=2").json() == {"page": 2}
    assert more.get("/more/alias?page=2").json()["error"]["detail"]["issues"][0]["loc"] == [
        "query",
        "pageNum",
    ]


def test_untyped_lists_keep_the_first_value_until_enforced(more: TestClient) -> None:
    @controller("/bare")
    class Bare:
        @get("/")
        async def bare(self, n: Query[list], m: Query[list[int] | None] = None) -> dict:
            return {"n": n, "m": m}

    @module(controllers=[Bare])
    class BareModule:
        pass

    enforced = TestClient(
        LaurenFactory.create(BareModule, validation=ValidationPolicy(stdlib_models="enforce"))
    )

    assert more.get("/more/bare-list?n=a&n=b").json() == {"n": "a"}
    assert enforced.get("/bare/?n=a&n=b&m=1&m=2").json() == {"n": ["a", "b"], "m": [1, 2]}


def test_raw_forms_pass_through(more: TestClient) -> None:
    response = more.post(
        "/more/raw-form", content=b"a=1", headers={"content-type": "application/x-www-form-urlencoded"}
    )
    assert response.json() == {"data": {"a": ["1"]}}


def test_query_models_with_unsupported_fields_keep_the_legacy_path(more: TestClient) -> None:
    assert more.get("/more/opaque?name=x").status_code == 422


def test_typeddict_query_models_legacy_scalar_lookup(more: TestClient) -> None:
    assert more.get("/more/td-raw?f=raw").json() == {"f": "raw"}


def test_warn_without_a_logger_still_logs_to_stdlib(caplog: pytest.LogCaptureFixture) -> None:
    runtime = ValidationRuntime(TypeCompiler(), include_input=False)

    with caplog.at_level(logging.WARNING, logger="lauren"):
        runtime.warn("GET /x", [ValidationIssue(("query", "x"), "int_parsing", "m")])

    assert any("GET /x" in r.getMessage() for r in caplog.records)


@dataclasses.dataclass
class OpaqueMsg:
    thing: _Opaque
    text: str


@ws_controller("/opaque")
class OpaqueGateway:
    @on_message("msg")
    async def msg(self, ws: WebSocket, body: Json[OpaqueMsg]) -> None:
        await ws.send_json({"ok": True})


@module(controllers=[OpaqueGateway])
class OpaqueWsModule:
    pass


def test_websocket_payloads_the_engine_cannot_compile_keep_the_legacy_validator(block_pydantic: None) -> None:
    app = LaurenFactory.create(OpaqueWsModule)

    async def run() -> dict[str, Any]:
        async with WsTestClient(app).connect("/opaque") as ws:
            await ws.send_json({"event": "msg", "data": {"text": "x"}})
            return await ws.receive_json()

    issue = asyncio.run(run())["error"]["detail"]["issues"][0]
    assert issue["type"] == "value_error"
    assert issue["loc"] == ["frame", "data"]


@dataclasses.dataclass
class ExplodingMsg:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, int):
            raise RuntimeError("boom")


@ws_controller("/explode")
class ExplodingGateway:
    @on_message("msg")
    async def msg(self, ws: WebSocket, body: Json[ExplodingMsg]) -> None:
        await ws.send_json({"value": body.value})


@module(controllers=[ExplodingGateway])
class ExplodingWsModule:
    pass


def test_a_crashing_websocket_shadow_validation_is_ignored(block_pydantic: None) -> None:
    app = LaurenFactory.create(ExplodingWsModule)

    async def run() -> dict[str, Any]:
        async with WsTestClient(app).connect("/explode") as ws:
            await ws.send_json({"event": "msg", "data": {"value": "1"}})
            return await ws.receive_json()

    assert asyncio.run(run()) == {"value": "1"}


def test_pydantic_query_models_read_optional_and_annotated_lists() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Search(pydantic.BaseModel):
        tags: list[str] | None = None
        ids: Annotated[list[int], pydantic.Field(max_length=3)] = []

    @controller("/pyl")
    class Endpoints2:
        @get("/")
        async def search(self, s: Query[Search]) -> dict:
            return s.model_dump()

    @module(controllers=[Endpoints2])
    class Mod2:
        pass

    client = TestClient(LaurenFactory.create(Mod2))

    assert client.get("/pyl/?tags=a&ids=1").json() == {"tags": ["a"], "ids": [1]}
