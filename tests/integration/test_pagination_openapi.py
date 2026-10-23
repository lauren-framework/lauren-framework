"""OpenAPI for pagination: parameters, named page components, envelopes and headers (F29)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Annotated, Any

from lauren import (
    CursorPage,
    CursorPagination,
    LaurenFactory,
    OffsetPagination,
    Page,
    PageNumberPagination,
    PaginationConfig,
    controller,
    get,
    module,
)


@dataclass
class UserOut:
    id: int


@dataclass
class OrderOut:
    number: str


@dataclass
class EventOut:
    id: int


@dataclass(frozen=True)
class EventKey:
    id: int


@controller("")
class ListController:
    @get("/users")
    async def users(self, paging: OffsetPagination) -> Page[UserOut]:
        raise NotImplementedError

    @get("/orders")
    async def orders(self, paging: PageNumberPagination) -> Page[OrderOut]:
        raise NotImplementedError

    @get("/events")
    async def events(self, paging: CursorPagination[EventKey]) -> CursorPage[EventOut]:
        raise NotImplementedError

    @get("/admin/users")
    async def admin_users(
        self, paging: Annotated[OffsetPagination, PaginationConfig(max_limit=500, limit_param="size")]
    ) -> Page[UserOut]:
        raise NotImplementedError

    @get("/numbers")
    async def numbers(self, paging: OffsetPagination) -> Page[int]:
        raise NotImplementedError

    @get("/declared", response_model=Page[OrderOut])
    async def declared(self, paging: OffsetPagination) -> Any:
        raise NotImplementedError

    @get("/static")
    async def static(self) -> Page[UserOut]:
        raise NotImplementedError

    @get("/untyped")
    async def untyped(self, paging: OffsetPagination) -> Page:  # type: ignore[type-arg]
        raise NotImplementedError


@module(controllers=[ListController])
class DocsModule:
    pass


def _doc(**config: Any) -> dict[str, Any]:
    config.setdefault("cursor_secret", "k")
    return LaurenFactory.create(DocsModule, pagination=PaginationConfig(**config)).openapi()


def _params(doc: dict[str, Any], path: str) -> dict[str, dict[str, Any]]:
    return {p["name"]: p for p in doc["paths"][path]["get"]["parameters"]}


def _ok(doc: dict[str, Any], path: str) -> dict[str, Any]:
    return doc["paths"][path]["get"]["responses"]["200"]  # type: ignore[no-any-return]


def test_offset_parameters_carry_constraints() -> None:
    params = _params(_doc(max_offset=1000), "/users")

    assert params["limit"]["schema"] == {"type": "integer", "minimum": 1, "maximum": 100, "default": 20}
    assert params["offset"]["schema"] == {"type": "integer", "minimum": 0, "default": 0, "maximum": 1000}
    assert params["limit"]["in"] == "query"
    assert params["limit"]["required"] is False


def test_per_parameter_config_is_documented() -> None:
    params = _params(_doc(), "/admin/users")

    assert params["size"]["schema"]["maximum"] == 500
    assert "limit" not in params


def test_page_number_parameters() -> None:
    params = _params(_doc(), "/orders")

    assert set(params) == {"page", "per_page"}
    assert params["page"]["schema"] == {"type": "integer", "minimum": 1, "default": 1}


def test_cursor_parameter_is_an_opaque_string() -> None:
    params = _params(_doc(), "/events")

    assert params["cursor"]["schema"] == {"type": "string"}
    assert "opaque" in params["cursor"]["description"].lower()
    assert params["limit"]["schema"]["maximum"] == 100


def test_page_components_are_named_after_the_item_type() -> None:
    doc = _doc()

    assert _ok(doc, "/users")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserOutPage"
    }
    assert _ok(doc, "/orders")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OrderOutNumberedPage"
    }
    assert _ok(doc, "/events")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/EventOutCursorPage"
    }


def test_component_names_are_valid_keys() -> None:
    names = _doc()["components"]["schemas"]

    assert all(re.fullmatch(r"[a-zA-Z0-9.\-_]+", name) for name in names)


def test_different_item_types_never_collide() -> None:
    schemas = _doc()["components"]["schemas"]

    assert schemas["UserOutPage"]["properties"]["items"]["items"] == {"$ref": "#/components/schemas/UserOut"}
    assert schemas["OrderOutPage"]["properties"]["items"]["items"] == {
        "$ref": "#/components/schemas/OrderOut"
    }
    assert schemas["IntPage"]["properties"]["items"]["items"] == {"type": "integer"}


def test_offset_page_schema_shape() -> None:
    schema = _doc()["components"]["schemas"]["UserOutPage"]

    assert schema["required"] == ["items", "meta"]
    assert schema["properties"]["meta"]["properties"] == {
        "limit": {"type": "integer"},
        "offset": {"type": "integer"},
        "total": {"type": ["integer", "null"]},
        "has_next": {"type": "boolean"},
    }


def test_cursor_page_schema_meta() -> None:
    meta = _doc(meta_keys="camel")["components"]["schemas"]["EventOutCursorPage"]["properties"]["meta"]

    assert list(meta["properties"]) == ["limit", "hasNext", "nextCursor", "prevCursor"]


def test_data_envelope_schema_has_links() -> None:
    schema = _doc(envelope="data")["components"]["schemas"]["UserOutPage"]

    assert list(schema["properties"]) == ["data", "meta", "links"]
    assert list(schema["properties"]["links"]["properties"]) == ["self", "first", "prev", "next", "last"]


def test_bare_envelope_is_an_array_without_component() -> None:
    doc = _doc(envelope="bare")

    assert _ok(doc, "/users")["content"]["application/json"]["schema"] == {
        "type": "array",
        "items": {"$ref": "#/components/schemas/UserOut"},
    }
    assert "UserOutPage" not in doc["components"]["schemas"]


def test_link_header_is_documented() -> None:
    headers = _ok(_doc(), "/users")["headers"]

    assert set(headers) == {"Link"}


def test_total_count_header_documented_when_sent() -> None:
    doc = _doc(envelope="bare")

    assert set(_ok(doc, "/users")["headers"]) == {"Link", "X-Total-Count"}
    assert set(_ok(doc, "/events")["headers"]) == {"Link"}


def test_response_model_page_is_documented() -> None:
    doc = _doc()

    assert _ok(doc, "/declared")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/OrderOutPage"
    }


def test_page_without_paging_parameter_uses_app_config() -> None:
    doc = _doc(envelope="data")

    assert "parameters" not in doc["paths"]["/static"]["get"]
    assert _ok(doc, "/static")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserOutPage"
    }


def test_unparameterised_page_gets_a_generic_component() -> None:
    doc = _doc()

    assert _ok(doc, "/untyped")["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/Page"
    }
    assert doc["components"]["schemas"]["Page"]["properties"]["items"] == {"type": "array", "items": {}}
