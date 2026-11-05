"""Integration: native validation of stdlib models under ``ValidationPolicy(stdlib_models=...)``."""

from __future__ import annotations

import dataclasses
import datetime
import enum
import logging
import uuid
from typing import Annotated, Any, Literal, NamedTuple, TypedDict

import pytest

from lauren import (
    Discriminated,
    Form,
    Json,
    Lauren,
    LaurenFactory,
    Path,
    Query,
    UnresolvableParameterError,
    controller,
    get,
    module,
    post,
)
from lauren.extractors import QueryField
from lauren.logging import InMemoryLogger
from lauren.testing import TestClient
from lauren.validation import Ge, MaxLen, TypeCompiler, ValidationPolicy, validation_config


@dataclasses.dataclass
class Address:
    city: str


@dataclasses.dataclass
class DUser:
    name: str
    age: int
    address: Address | None = None


class TDUser(TypedDict):
    name: str
    age: int


@validation_config(extra="forbid")
@dataclasses.dataclass
class Signup:
    name: str


@dataclasses.dataclass
class Filters:
    q: str
    page: int = 1
    tags: list[str] = dataclasses.field(default_factory=list)
    since: datetime.date | None = None


class Point(NamedTuple):
    x: int
    y: int = 0


class Kind(enum.Enum):
    A = "a"
    B = "b"


@dataclasses.dataclass
class Cat:
    kind: Literal["cat"]
    lives: int = 9


@dataclasses.dataclass
class Dog:
    kind: Literal["dog"]
    good: bool = True


@dataclasses.dataclass
class Ranged:
    age: Annotated[int, Ge(13)]


@controller("/s")
class Endpoints:
    @post("/du")
    async def du(self, body: Json[DUser]) -> dict:
        return {
            "age": body.age,
            "age_type": type(body.age).__name__,
            "address_type": type(body.address).__name__,
        }

    @post("/td")
    async def td(self, body: Json[TDUser]) -> dict:
        return {"age_type": type(body["age"]).__name__}

    @post("/signup")
    async def signup(self, body: Signup) -> dict:
        return {"name": body.name}

    @post("/ranged")
    async def ranged(self, body: Ranged) -> dict:
        return {"age": body.age}

    @get("/flag")
    async def flag(self, on: bool) -> dict:
        return {"on": on}

    @get("/maybe")
    async def maybe(self, n: Query[int | None] = None) -> dict:
        return {"n": n}

    @get("/typed/{uid}")
    async def typed(self, uid: Path[uuid.UUID], kind: Query[Kind]) -> dict:
        return {"uid_type": type(uid).__name__, "kind": str(kind)}

    @get("/filters")
    async def filters(self, f: Query[Filters]) -> dict:
        return {
            "type": type(f).__name__,
            "f": f if isinstance(f, dict) else dataclasses.asdict(f),
            "since_type": type(getattr(f, "since", None)).__name__,
        }

    @get("/point")
    async def point(self, p: Query[Point]) -> dict:
        return {"p": list(p) if isinstance(p, tuple) else p}

    @post("/form")
    async def form(self, body: Form[Filters]) -> dict:
        return {"type": type(body).__name__}

    @post("/pet")
    async def pet(self, body: Discriminated[Cat | Dog, "kind"]) -> dict:  # noqa: F821
        return {"type": type(body).__name__}

    @post("/list")
    async def as_list(self, body: Json[list[int]]) -> dict:
        return {"body": body}


@module(controllers=[Endpoints])
class AppModule:
    pass


def _client(mode: str, **kwargs: Any) -> TestClient:
    return TestClient(
        LaurenFactory.create(AppModule, validation=ValidationPolicy(stdlib_models=mode), **kwargs)
    )


PROBE = {"name": "x", "age": "not-a-number", "address": {"city": "Paris"}}


@pytest.mark.parametrize("mode", ["legacy", "warn"])
def test_legacy_and_warn_keep_the_shallow_behaviour(mode: str) -> None:
    response = _client(mode).post("/s/du", json=PROBE)

    assert response.status_code == 200
    assert response.json() == {"age": "not-a-number", "age_type": "str", "address_type": "dict"}


def test_enforce_rejects_the_dataclass_probe() -> None:
    response = _client("enforce").post("/s/du", json=PROBE)

    assert response.status_code == 422
    assert response.json()["error"]["detail"]["issues"] == [
        {
            "loc": ["body", "age"],
            "type": "int_parsing",
            "msg": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]


def test_enforce_builds_nested_instances() -> None:
    response = _client("enforce").post("/s/du", json={"name": "x", "age": "3", "address": {"city": "Paris"}})

    assert response.json() == {"age": 3, "age_type": "int", "address_type": "Address"}


def test_warn_logs_once_per_route_and_issue_type(caplog: pytest.LogCaptureFixture) -> None:
    log = InMemoryLogger()
    client = TestClient(LaurenFactory.create(AppModule, logger=log))

    with caplog.at_level(logging.WARNING, logger="lauren"):
        for _ in range(3):
            assert client.post("/s/du", json=PROBE).status_code == 200

    warnings = [r for r in log.records if r.context == "Validation"]
    assert len(warnings) == 1
    assert "POST /s/du" in warnings[0].message
    assert "body.age" in warnings[0].message
    assert warnings[0].extra["issues"] == [{"loc": ["body", "age"], "type": "int_parsing"}]
    assert "not-a-number" not in warnings[0].message
    assert sum("would reject" in r.getMessage() for r in caplog.records) == 1


def test_warn_mode_does_not_log_valid_requests() -> None:
    log = InMemoryLogger()
    client = TestClient(LaurenFactory.create(AppModule, logger=log))

    client.post("/s/du", json={"name": "x", "age": 3})

    assert not [r for r in log.records if r.context == "Validation"]


@pytest.mark.parametrize(("mode", "status"), [("legacy", 200), ("warn", 200), ("enforce", 422)])
def test_typeddict_probe(mode: str, status: int) -> None:
    response = _client(mode).post("/s/td", json={"name": "x", "age": "nope"})

    assert response.status_code == status


def test_typeddict_values_are_coerced_under_enforce() -> None:
    assert _client("enforce").post("/s/td", json={"name": "x", "age": "4"}).json() == {"age_type": "int"}


@pytest.mark.parametrize("mode", ["legacy", "warn", "enforce"])
def test_extra_forbid_is_enforced_in_every_mode(mode: str) -> None:
    response = _client(mode).post("/s/signup", json={"name": "x", "admin": True})

    assert response.status_code == 422
    assert response.json()["error"]["detail"]["issues"] == [
        {"loc": ["body", "admin"], "type": "extra_forbidden", "msg": "Extra inputs are not permitted"}
    ]


def test_marker_constraints_are_enforced_in_warn_mode() -> None:
    response = _client("warn").post("/s/ranged", json={"age": 5})

    assert response.status_code == 422
    assert response.json()["error"]["detail"]["issues"][0]["ctx"] == {"ge": 13}


@pytest.mark.parametrize(("mode", "status", "body"), [("warn", 200, {"on": False}), ("enforce", 422, None)])
def test_unknown_boolean_tokens(mode: str, status: int, body: Any) -> None:
    response = _client(mode).get("/s/flag?on=maybe")

    assert response.status_code == status
    if body is not None:
        assert response.json() == body
    else:
        assert response.json()["error"]["detail"]["issues"][0]["type"] == "bool_parsing"


@pytest.mark.parametrize("mode", ["legacy", "warn", "enforce"])
def test_off_is_false_in_every_mode(mode: str) -> None:
    assert _client(mode).get("/s/flag?on=off").json() == {"on": False}


@pytest.mark.parametrize(("mode", "status"), [("warn", 422), ("enforce", 200)])
def test_an_empty_optional_value_is_none_under_enforce(mode: str, status: int) -> None:
    response = _client(mode).get("/s/maybe?n=")

    assert response.status_code == status
    if status == 200:
        assert response.json() == {"n": None}


@pytest.mark.parametrize(("mode", "uid_type"), [("warn", "str"), ("enforce", "UUID")])
def test_typed_scalars_are_parsed_under_enforce(mode: str, uid_type: str) -> None:
    response = _client(mode).get(f"/s/typed/{uuid.uuid4()}?kind=a")

    assert response.json()["uid_type"] == uid_type


def test_bad_typed_scalars_are_rejected_under_enforce() -> None:
    detail = _client("enforce").get("/s/typed/nope?kind=z").json()["error"]["detail"]

    assert [(i["loc"], i["type"]) for i in detail["issues"]] == [
        (["path", "uid"], "uuid_parsing"),
        (["query", "kind"], "enum"),
    ]


def test_query_model_under_enforce_reads_repeated_keys() -> None:
    response = _client("enforce").get("/s/filters?q=x&page=2&tags=a&tags=b&since=2024-01-02")

    assert response.json() == {
        "type": "Filters",
        "f": {"q": "x", "page": 2, "tags": ["a", "b"], "since": "2024-01-02"},
        "since_type": "date",
    }


def test_query_model_issues_are_located_in_the_query() -> None:
    detail = _client("enforce").get("/s/filters?page=x").json()["error"]["detail"]

    assert [(i["loc"], i["type"]) for i in detail["issues"]] == [
        (["query", "q"], "missing"),
        (["query", "page"], "int_parsing"),
    ]


def test_query_model_keeps_the_legacy_value_in_warn_mode() -> None:
    response = _client("warn").get("/s/filters?q=x&since=2024-01-02")

    assert response.json()["since_type"] == "str"


def test_namedtuple_from_the_query_string() -> None:
    assert _client("enforce").get("/s/point?x=3").json() == {"p": [3, 0]}


@pytest.mark.parametrize(("mode", "type_name"), [("warn", "dict"), ("enforce", "Filters")])
def test_form_models(mode: str, type_name: str) -> None:
    response = _client(mode).post(
        "/s/form", content=b"q=x&tags=a&tags=b", headers={"content-type": "application/x-www-form-urlencoded"}
    )

    assert response.json() == {"type": type_name}


def test_form_model_issues_are_located_in_the_form() -> None:
    response = _client("enforce").post(
        "/s/form", content=b"page=x", headers={"content-type": "application/x-www-form-urlencoded"}
    )

    assert [i["loc"] for i in response.json()["error"]["detail"]["issues"]] == [
        ["form", "q"],
        ["form", "page"],
    ]


def test_discriminated_variant_issues_nest_under_the_tag() -> None:
    detail = (
        _client("enforce").post("/s/pet", json={"kind": "cat", "lives": "many"}).json()["error"]["detail"]
    )

    assert detail["issues"][0]["loc"] == ["body", "cat", "lives"]


@pytest.mark.parametrize(
    ("payload", "issue_type"),
    [({"lives": 1}, "union_tag_not_found"), ({"kind": "fish"}, "union_tag_invalid")],
)
def test_discriminated_tag_errors_keep_their_messages(payload: dict[str, Any], issue_type: str) -> None:
    for mode in ("warn", "enforce"):
        error = _client(mode).post("/s/pet", json=payload).json()["error"]
        assert error["detail"]["issues"][0]["type"] == issue_type
    assert error["detail"]["issues"][0]["loc"] == ["body"]


def test_json_containers_are_validated_under_enforce() -> None:
    assert _client("enforce").post("/s/list", json=["1", 2]).json() == {"body": [1, 2]}
    assert _client("warn").post("/s/list", json=["1", 2]).json() == {"body": ["1", 2]}


def test_strict_json_mode_rejects_numeric_strings() -> None:
    client = TestClient(
        LaurenFactory.create(
            AppModule, validation=ValidationPolicy(stdlib_models="enforce", json_mode="strict")
        )
    )

    detail = client.post("/s/du", json={"name": "x", "age": "3"}).json()["error"]["detail"]

    assert detail["issues"][0]["type"] == "int_type"


def test_query_strings_stay_lax_in_strict_json_mode() -> None:
    client = TestClient(LaurenFactory.create(AppModule, validation=ValidationPolicy(json_mode="strict")))

    assert client.get("/s/flag?on=1").json() == {"on": True}


# ---------------------------------------------------------------------------
# App surface and startup behaviour
# ---------------------------------------------------------------------------


def test_the_app_exposes_its_type_compiler_and_policy() -> None:
    policy = ValidationPolicy(stdlib_models="enforce")
    app = LaurenFactory.create(AppModule, validation=policy)

    assert isinstance(app.type_compiler, TypeCompiler)
    assert app.validation_policy is policy
    assert app.type_compiler.compile(DUser).validate({"name": "a", "age": "2"}).age == 2


def test_validation_must_be_a_policy() -> None:
    with pytest.raises(Exception) as info:
        LaurenFactory.create(AppModule, validation="enforce")  # type: ignore[arg-type]

    assert info.value.detail["reason"] == "invalid_validation_policy"  # type: ignore[attr-defined]


def test_lauren_forwards_the_validation_policy() -> None:
    app = Lauren(
        validation=ValidationPolicy(stdlib_models="enforce"), openapi_url=None, docs_url=None, redoc_url=None
    )

    @app.post("/u")
    async def create(body: Json[DUser]) -> dict:
        return {"age": body.age}

    assert TestClient(app).post("/u", json={"name": "a", "age": "x"}).status_code == 422


def _single_route_module(handler: Any) -> type:
    @controller("/x")
    class Ctl:
        pass

    setattr(Ctl, handler.__name__, handler)

    @module(controllers=[Ctl])
    class Mod:
        pass

    return Mod


def test_bad_marker_on_a_body_model_fails_startup() -> None:
    @dataclasses.dataclass
    class Bad:
        name: Annotated[str, Ge(1)]

    @controller("/x")
    class Ctl:
        @post("/")
        async def create(self, body: Json[Bad]) -> dict:
            return {}

    @module(controllers=[Ctl])
    class Mod:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(Mod)

    assert info.value.detail == {
        "class": "Ctl",
        "handler": "create",
        "param": "body",
        "type": "Bad",
        "reason": "bad_constraint",
        "field": "Bad.name",
    }


def test_bad_pattern_on_a_parameter_fails_startup() -> None:
    from lauren.validation import Pattern

    @controller("/x")
    class Ctl:
        @get("/")
        async def show(self, q: Query[str, Pattern("(")]) -> dict:
            return {}

    @module(controllers=[Ctl])
    class Mod:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(Mod)

    assert info.value.detail["reason"] == "bad_pattern"
    assert info.value.detail["param"] == "q"


class _Opaque:
    pass


@dataclasses.dataclass
class HasOpaque:
    thing: _Opaque


@controller("/o")
class OpaqueController:
    @post("/")
    async def create(self, body: Json[HasOpaque]) -> dict:
        return {"type": type(body).__name__}

    @get("/")
    async def search(self, tags: Query[list[str]] = QueryField(max_length=2)) -> dict:
        return {"tags": tags}


@module(controllers=[OpaqueController])
class OpaqueModule:
    pass


def test_unsupported_field_types_keep_the_legacy_path_with_a_startup_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="lauren"):
        app = LaurenFactory.create(OpaqueModule)

    notes = [r.getMessage() for r in caplog.records]
    assert any("not validated natively" in n and "_Opaque" in n for n in notes)
    assert any("FieldDescriptor constraint ignored" in n for n in notes)
    assert TestClient(app).get("/o/?tags=a&tags=b&tags=c").json() == {"tags": ["a", "b", "c"]}


def test_unsupported_field_types_fail_startup_under_enforce() -> None:
    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(OpaqueModule, validation=ValidationPolicy(stdlib_models="enforce"))

    assert info.value.detail["reason"] in ("unsupported_type", "bad_constraint")


def test_parameters_have_compiled_plans() -> None:
    app = LaurenFactory.create(AppModule)

    handler = app._handlers[("POST", "/s/du")]

    assert handler.extractions[0].compiled is not None
    assert MaxLen(1) == MaxLen(1)
