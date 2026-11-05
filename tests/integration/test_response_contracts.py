"""Integration: response contracts — ``response_validation="off" | "filter" | "strict"``."""

from __future__ import annotations

import dataclasses
import inspect
import json
import typing
from typing import Any

import pytest

import lauren._typing.forwardref as forwardref
from lauren import (
    Created,
    Lauren,
    LaurenFactory,
    StartupError,
    StreamingResponse,
    UnresolvableParameterError,
    controller,
    exception_handler,
    get,
    interceptor,
    module,
    post,
    use_interceptors,
)
from lauren.logging import InMemoryLogger, LogLevel
from lauren.testing import TestClient
from lauren.types import Response


@dataclasses.dataclass
class UserOut:
    id: int
    name: str


@dataclasses.dataclass
class UserInDB(UserOut):
    password_hash: str


class Row:
    """An ORM-like entity: attributes, more of them than the DTO declares."""

    def __init__(self, **attrs: Any) -> None:
        self.__dict__.update(attrs)


@controller("/users")
class UserController:
    @get("/me")
    async def me(self) -> UserOut:
        return UserInDB(1, "ada", "bcrypt$secret")

    @get("/dict")
    async def as_dict(self) -> UserOut:
        return {"id": 2, "name": "bo", "token": "t0k3n"}  # type: ignore[return-value]

    @get("/row")
    async def row(self) -> UserOut:
        return Row(id=3, name="cy", api_key="sk-live")  # type: ignore[return-value]

    @get("/missing")
    async def missing(self) -> UserOut:
        return {"id": 4, "password_hash": "bcrypt$leak"}  # type: ignore[return-value]

    @get("/wrong-leaf")
    async def wrong_leaf(self) -> UserOut:
        return {"id": "four", "name": "di"}  # type: ignore[return-value]

    @get("/list")
    async def many(self) -> list[UserOut]:
        return [UserOut(1, "a"), UserInDB(2, "b", "h"), Row(id=3, name="c", x=1)]  # type: ignore[list-item]

    @get("/off", response_validation="off")
    async def off(self) -> UserOut:
        return UserInDB(5, "ed", "bcrypt$off")

    @get("/strict", response_validation="strict")
    async def strict(self) -> UserOut:
        return {"id": "six", "name": "fi"}  # type: ignore[return-value]

    @post("/", status_code=201)
    async def create(self) -> Created[UserOut]:
        return Created(UserInDB(7, "gu", "bcrypt$c"), location="/users/7")

    @get("/model", response_model=UserOut)
    async def with_model(self) -> dict:
        return {"id": 8, "name": "ha", "internal": True}

    @get("/untyped")
    async def untyped(self):  # type: ignore[no-untyped-def]
        return {"anything": "goes"}

    @get("/raw")
    async def raw(self) -> Response:
        return Response.json({"raw": True})


def _client(mode: str = "filter", **kwargs: Any) -> TestClient:
    @module(controllers=[UserController])
    class UsersModule:
        pass

    return TestClient(LaurenFactory.create(UsersModule, response_validation=mode, **kwargs))


# -- the leak (PRD §1.2) ----------------------------------------------------


def test_off_mode_sends_the_runtime_object_as_today() -> None:
    assert _client("off").get("/users/me").json() == {
        "id": 1,
        "name": "ada",
        "password_hash": "bcrypt$secret",
    }


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_enforced_modes_drop_subclass_fields(mode: str) -> None:
    assert _client(mode).get("/users/me").json() == {"id": 1, "name": "ada"}


def test_pydantic_subclass_does_not_leak_under_filter() -> None:
    pydantic = pytest.importorskip("pydantic")

    class PublicUser(pydantic.BaseModel):
        id: int
        name: str

    class StoredUser(PublicUser):
        password_hash: str

    @controller("/p")
    class PydanticController:
        @get("/me")
        async def me(self) -> PublicUser:
            return StoredUser(id=1, name="a", password_hash="bcrypt$...")

    @module(controllers=[PydanticController])
    class PydanticModule:
        pass

    off = TestClient(LaurenFactory.create(PydanticModule)).get("/p/me").json()
    filtered = (
        TestClient(LaurenFactory.create(PydanticModule, response_validation="filter")).get("/p/me").json()
    )

    assert "password_hash" in off
    assert filtered == {"id": 1, "name": "a"}


# -- projection -------------------------------------------------------------


def test_mapping_extra_keys_are_dropped() -> None:
    assert _client().get("/users/dict").json() == {"id": 2, "name": "bo"}


def test_entity_is_read_through_the_declared_fields() -> None:
    assert _client().get("/users/row").json() == {"id": 3, "name": "cy"}


def test_every_list_element_is_projected() -> None:
    assert _client().get("/users/list").json() == [
        {"id": 1, "name": "a"},
        {"id": 2, "name": "b"},
        {"id": 3, "name": "c"},
    ]


def test_typed_result_value_is_projected() -> None:
    response = _client().post("/users/")

    assert (response.status_code, response.json(), response.header("location")) == (
        201,
        {"id": 7, "name": "gu"},
        "/users/7",
    )


def test_response_model_is_enforced_under_filter() -> None:
    assert _client().get("/users/model").json() == {"id": 8, "name": "ha"}


def test_untyped_route_is_not_enforced() -> None:
    assert _client("strict").get("/users/untyped").json() == {"anything": "goes"}


def test_response_instances_pass_through() -> None:
    assert _client("strict").get("/users/raw").json() == {"raw": True}


# -- violations -------------------------------------------------------------


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_missing_required_field_is_a_500(mode: str) -> None:
    response = _client(mode).get("/users/missing")

    assert response.status_code == 500
    assert response.json() == {
        "error": {"code": "response_validation_error", "message": "response validation failed", "detail": {}}
    }
    assert b"leak" not in response.body


def test_violation_is_logged_with_structured_issues() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    _client(logger=log).get("/users/missing")

    (record,) = [r for r in log.records if r.context == "ResponseContract" and r.level == LogLevel.ERROR]
    assert record.extra["issues"] == [
        {"loc": ["response", "name"], "type": "missing", "msg": "Field required"}
    ]
    assert record.extra["handler"] == "UserController.missing"
    assert record.extra["route"] == "GET /users/missing"


def test_strict_mode_rejects_wrong_leaf_types() -> None:
    assert _client("strict").get("/users/wrong-leaf").status_code == 500


def test_filter_mode_passes_wrong_leaf_types() -> None:
    assert _client("filter").get("/users/wrong-leaf").json() == {"id": "four", "name": "di"}


def test_debug_pages_include_the_issues() -> None:
    response = _client(debug=True).get("/users/missing")

    assert response.json()["error"]["detail"]["issues"] == [
        {"loc": ["response", "name"], "type": "missing", "msg": "Field required"}
    ]
    assert response.header("cache-control") == "no-store"


def test_problem_details_format_carries_the_code() -> None:
    body = _client(error_format="rfc9457").get("/users/missing").json()

    assert (body["status"], body["code"]) == (500, "response_validation_error")


def test_exception_handlers_do_not_see_violations() -> None:
    @exception_handler(Exception)
    async def catch_all(exc: Exception, request: Any) -> Response:
        return Response.json({"caught": True}, status=599)

    response = _client(global_exception_handlers=[catch_all]).get("/users/missing")

    assert response.status_code == 500


def test_interceptors_see_the_violation_raised() -> None:
    seen: list[str] = []

    @interceptor()
    class Spy:
        async def intercept(self, ctx: Any, call_handler: Any) -> Any:
            try:
                return await call_handler.handle()
            except Exception as exc:
                seen.append(type(exc).__name__)
                raise

    @use_interceptors(Spy)
    @controller("/spy")
    class SpyController:
        @get("/")
        async def bad(self) -> UserOut:
            return {"id": 1}  # type: ignore[return-value]

    @module(controllers=[SpyController])
    class SpyModule:
        pass

    response = TestClient(LaurenFactory.create(SpyModule, response_validation="filter")).get("/spy/")

    assert (response.status_code, seen) == (500, ["ResponseContractViolation"])


# -- modes and overrides ----------------------------------------------------


def test_route_off_beats_an_enforcing_app() -> None:
    assert "password_hash" in _client("filter").get("/users/off").json()


def test_route_strict_beats_an_off_app() -> None:
    assert _client("off").get("/users/strict").status_code == 500


def test_imperative_app_accepts_response_validation() -> None:
    app = Lauren(openapi_url=None, docs_url=None, redoc_url=None, response_validation="filter")

    @app.get("/me")
    async def me() -> UserOut:
        return UserInDB(1, "a", "h")

    assert TestClient(app).get("/me").json() == {"id": 1, "name": "a"}


def test_app_exposes_its_default_mode() -> None:
    @module(controllers=[UserController])
    class UsersModule:
        pass

    assert LaurenFactory.create(UsersModule, response_validation="strict").response_validation == "strict"


def test_unknown_app_mode_fails_at_startup() -> None:
    @module()
    class Empty:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Empty, response_validation="on")

    assert info.value.detail["reason"] == "invalid_response_validation"


def test_unsupported_declared_type_fails_at_startup() -> None:
    class Opaque:
        pass

    @controller("/o")
    class OpaqueController:
        @get("/")
        async def get_it(self) -> Opaque:
            return Opaque()

    @module(controllers=[OpaqueController])
    class OpaqueModule:
        pass

    LaurenFactory.create(OpaqueModule)  # "off" never compiles the type

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(OpaqueModule, response_validation="strict")

    assert info.value.detail["reason"] == "unsupported_response_type"
    assert info.value.detail["param"] == "<return>"


def test_documentation_only_response_model_is_reported_once() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    _client("off", logger=log)

    warnings = [r for r in log.records if r.context == "ResponseContract" and r.level == LogLevel.WARN]
    assert len(warnings) == 1
    assert warnings[0].extra["routes"] == ["GET /users/model"]


def test_enforced_response_model_is_not_reported() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    _client("filter", logger=log)

    assert not [r for r in log.records if r.context == "ResponseContract" and r.level == LogLevel.WARN]


def test_untyped_routes_are_noted_at_verbose_level() -> None:
    log = InMemoryLogger(level=LogLevel.VERBOSE)

    _client("filter", logger=log)

    notes = [
        r.extra["route"]
        for r in log.records
        if r.context == "ResponseContract" and r.level == LogLevel.VERBOSE
    ]
    assert notes == ["GET /users/untyped", "GET /users/raw"]


# -- no reflection on the request path --------------------------------------


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_contracts_never_reflect_on_the_request_path(monkeypatch: pytest.MonkeyPatch, mode: str) -> None:
    client = _client(mode)
    client.get("/users/me")

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("reflection on the request path")

    monkeypatch.setattr(typing, "get_type_hints", forbidden)
    monkeypatch.setattr(inspect, "signature", forbidden)
    monkeypatch.setattr(forwardref, "resolve_type_hints", forbidden)

    assert client.get("/users/row").status_code == 200
    assert client.get("/users/list").status_code == 200
    assert client.post("/users/").status_code == 201
    assert client.get("/users/missing").status_code == 500


# -- typed streams ----------------------------------------------------------


@controller("/stream")
class StreamController:
    @get("/ok")
    async def ok(self) -> StreamingResponse[UserOut]:
        async def produce() -> Any:
            yield UserInDB(1, "a", "h1")
            yield Row(id=2, name="b", key="k")

        return produce()

    @get("/first-bad")
    async def first_bad(self) -> StreamingResponse[UserOut]:
        async def produce() -> Any:
            yield {"id": 1}

        return produce()

    @get("/later-bad")
    async def later_bad(self) -> StreamingResponse[UserOut]:
        async def produce() -> Any:
            yield UserOut(1, "a")
            yield {"id": 2}

        return produce()


def _stream_client(mode: str, **kwargs: Any) -> TestClient:
    @module(controllers=[StreamController])
    class StreamModule:
        pass

    return TestClient(LaurenFactory.create(StreamModule, response_validation=mode, **kwargs))


def _lines(response: Any) -> list[Any]:
    return [json.loads(line) for line in response.text.splitlines() if line]


def test_stream_items_are_projected_under_filter() -> None:
    response = _stream_client("filter").get("/stream/ok")

    assert _lines(response) == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]


def test_stream_first_item_violation_is_a_500() -> None:
    assert _stream_client("filter").get("/stream/first-bad").status_code == 500


def test_stream_later_violation_ends_with_an_error_frame() -> None:
    log = InMemoryLogger(level=LogLevel.DEBUG)

    response = _stream_client("strict", logger=log).get("/stream/later-bad")

    assert response.status_code == 200
    assert _lines(response) == [
        {"id": 1, "name": "a"},
        {
            "error": {
                "code": "response_validation_error",
                "message": "response validation failed",
                "detail": {},
            }
        },
    ]
    issues = [r.extra["issues"] for r in log.records if r.context == "ResponseContract"]
    assert issues == [[{"loc": ["response", "item", 1, "name"], "type": "missing", "msg": "Field required"}]]
