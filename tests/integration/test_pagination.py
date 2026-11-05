"""Pagination end to end: parameters, envelopes, ``Link`` headers, cursors and startup checks (F29)."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Annotated, Any, TypedDict

import pytest

from lauren import (
    CallHandler,
    CursorPage,
    CursorPagination,
    ExecutionContext,
    Lauren,
    LaurenFactory,
    OffsetPagination,
    Page,
    PageNumberPagination,
    PaginationConfig,
    Response,
    controller,
    get,
    injectable,
    interceptor,
    module,
    paginate_sequence,
    use_interceptors,
)
from lauren.exceptions import StartupError, UnresolvableParameterError
from lauren.testing import TestClient

SECRET = "test-cursor-secret"


@dataclass
class UserOut:
    id: int
    name: str


@dataclass(frozen=True)
class UserKey:
    id: int


USERS = [UserOut(id=i, name=f"user{i}") for i in range(1, 36)]


@injectable()
class UserRepo:
    def list(self, limit: int, offset: int) -> tuple[list[UserOut], int]:
        return USERS[offset : offset + limit], len(USERS)

    def after(self, key: UserKey | None, direction: str, limit: int) -> list[UserOut]:
        if direction == "prev":
            assert key is not None
            return [u for u in reversed(USERS) if u.id < key.id][:limit]
        start = key.id if key is not None else 0
        return [u for u in USERS if u.id > start][:limit]


@interceptor()
class StampLink:
    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Response:
        response = await call_handler.handle()
        return response.with_header("x-saw-link", "yes" if response.headers.get("link") else "no")


@controller("/users")
class UserController:
    def __init__(self, repo: UserRepo) -> None:
        self.repo = repo

    @get("/")
    async def list_users(self, paging: OffsetPagination) -> Page[UserOut]:
        rows, total = self.repo.list(limit=paging.limit, offset=paging.offset)
        return Page.of(rows, paging, total=total)

    @get("/sync")
    def list_sync(self, paging: OffsetPagination) -> Page[UserOut]:
        rows, total = self.repo.list(limit=paging.limit, offset=paging.offset)
        return Page.of(rows, paging, total=total)

    @get("/probe")
    async def probe(self, paging: OffsetPagination) -> Page[UserOut]:
        rows, _ = self.repo.list(limit=paging.limit + 1, offset=paging.offset)
        return Page.from_rows(rows, paging)

    @get("/pages")
    async def pages(self, paging: PageNumberPagination) -> Page[UserOut]:
        return paginate_sequence(USERS, paging)

    @get("/small")
    async def small(
        self, paging: Annotated[OffsetPagination, PaginationConfig(max_limit=5)]
    ) -> Page[UserOut]:
        return paginate_sequence(USERS, paging)

    @get("/cursor")
    async def cursor(self, paging: CursorPagination[UserKey]) -> CursorPage[UserOut]:
        rows = self.repo.after(paging.cursor, paging.direction, paging.limit + 1)
        return CursorPage.from_rows(rows, paging, key=lambda u: UserKey(id=u.id))

    @get("/created")
    async def created(self, paging: OffsetPagination) -> Any:
        return paginate_sequence(USERS, paging), 206

    @get("/manual")
    async def manual(self) -> Page[UserOut]:
        return Page(items=USERS[:2], limit=2, offset=0, total=35, has_next=True)

    @get("/intercepted")
    @use_interceptors(StampLink)
    async def intercepted(self, paging: OffsetPagination) -> Page[UserOut]:
        return paginate_sequence(USERS, paging)

    @get("/filtered")
    async def filtered(self, paging: OffsetPagination, min_id: int = 0) -> Page[UserOut]:
        return paginate_sequence([u for u in USERS if u.id >= min_id], paging)


@module(controllers=[UserController], providers=[UserRepo])
class AppModule:
    pass


def _client(**config: Any) -> TestClient:
    config.setdefault("cursor_secret", SECRET)
    return TestClient(LaurenFactory.create(AppModule, pagination=PaginationConfig(**config)))


def _links(response: Any) -> dict[str, str]:
    """Parse a ``Link`` header into ``{rel: url}``."""
    out: dict[str, str] = {}
    header = response.headers.get("link")
    for part in header.split(", ") if header else ():
        url, rel = part.split("; ")
        out[rel.removeprefix('rel="').removesuffix('"')] = url.strip("<>")
    return out


# ---------------------------------------------------------------------------
# Offset pages
# ---------------------------------------------------------------------------


def test_offset_page_items_envelope() -> None:
    response = _client().get("/users/?limit=10&offset=10")

    assert response.status_code == 200
    body = response.json()
    assert [u["id"] for u in body["items"]] == list(range(11, 21))
    assert body["meta"] == {"limit": 10, "offset": 10, "total": 35, "has_next": True}


def test_offset_page_link_header() -> None:
    response = _client().get("/users/?limit=10&offset=10")

    assert _links(response) == {
        "first": "/users/?limit=10&offset=0",
        "prev": "/users/?limit=10&offset=0",
        "next": "/users/?limit=10&offset=20",
        "last": "/users/?limit=10&offset=30",
    }


def test_offset_page_defaults_without_query() -> None:
    body = _client().get("/users/").json()

    assert body["meta"] == {"limit": 20, "offset": 0, "total": 35, "has_next": True}


def test_total_header_is_opt_in_for_items_envelope() -> None:
    assert _client().get("/users/").headers.get("x-total-count") is None
    assert _client(total_header=True).get("/users/").headers.get("x-total-count") == "35"


def test_sync_handler_returns_page() -> None:
    response = _client().get("/users/sync?limit=5")

    assert response.json()["meta"]["limit"] == 5
    assert "next" in _links(response)


def test_probe_row_helper_sets_has_next_without_total() -> None:
    response = _client().get("/users/probe?limit=10&offset=30")

    assert response.json()["meta"] == {"limit": 10, "offset": 30, "total": None, "has_next": False}
    assert set(_links(response)) == {"first", "prev"}


def test_page_with_status_tuple() -> None:
    response = _client().get("/users/created?limit=5")

    assert response.status_code == 206
    assert "next" in _links(response)


def test_interceptor_sees_rendered_page_response() -> None:
    response = _client().get("/users/intercepted")

    assert response.headers.get("x-saw-link") == "yes"


def test_unrelated_query_parameters_survive_in_links() -> None:
    response = _client().get("/users/filtered?min_id=30&limit=2&q=a+b")

    assert _links(response)["next"] == "/users/filtered?min_id=30&limit=2&q=a%20b&offset=2"


def test_limit_above_maximum_is_422_with_query_loc() -> None:
    response = _client().get("/users/?limit=500")

    assert response.status_code == 422
    issue = response.json()["error"]["detail"]["issues"][0]
    assert (issue["loc"], issue["type"]) == (["query", "limit"], "less_than_equal")


def test_pagination_issues_merge_with_other_parameter_failures() -> None:
    response = _client().get("/users/filtered?min_id=x&offset=-1")

    assert response.status_code == 422
    locs = [issue["loc"] for issue in response.json()["error"]["detail"]["issues"]]
    assert locs == [["query", "offset"], ["query", "min_id"]]


def test_per_parameter_config_caps_limit() -> None:
    client = _client()

    assert client.get("/users/small").json()["meta"]["limit"] == 5
    assert client.get("/users/small?limit=6").status_code == 422


def test_app_config_renames_parameters_in_links() -> None:
    response = _client(limit_param="size", offset_param="skip").get("/users/?size=5&skip=5")

    assert response.json()["meta"]["offset"] == 5
    assert _links(response)["next"] == "/users/?size=5&skip=10"


# ---------------------------------------------------------------------------
# Envelopes
# ---------------------------------------------------------------------------


def test_data_envelope_with_links_object() -> None:
    body = _client(envelope="data").get("/users/?limit=10&offset=30").json()

    assert [u["id"] for u in body["data"]] == list(range(31, 36))
    assert body["links"] == {
        "self": "/users/?limit=10&offset=30",
        "first": "/users/?limit=10&offset=0",
        "prev": "/users/?limit=10&offset=20",
        "next": None,
        "last": "/users/?limit=10&offset=30",
    }


def test_bare_envelope_is_array_with_headers() -> None:
    response = _client(envelope="bare").get("/users/?limit=3")

    assert [u["id"] for u in response.json()] == [1, 2, 3]
    assert response.headers.get("x-total-count") == "35"
    assert _links(response)["next"] == "/users/?limit=3&offset=3"


def test_bare_envelope_total_header_can_be_disabled() -> None:
    response = _client(envelope="bare", total_header=False).get("/users/?limit=3")

    assert response.headers.get("x-total-count") is None


def test_camel_meta_keys() -> None:
    meta = _client(meta_keys="camel").get("/users/pages?page=2&per_page=10").json()["meta"]

    assert meta == {"page": 2, "perPage": 10, "total": 35, "totalPages": 4, "hasNext": True}


def test_manually_built_page_follows_app_envelope_without_links() -> None:
    response = _client(envelope="data").get("/users/manual")

    assert response.json()["meta"] == {"limit": 2, "offset": 0, "total": 35, "has_next": True}
    assert response.json()["links"]["next"] is None
    assert response.headers.get("link") is None


def test_lauren_imperative_app_accepts_pagination() -> None:
    app = Lauren(
        openapi_url=None, docs_url=None, redoc_url=None, pagination=PaginationConfig(envelope="bare")
    )

    @app.get("/n")
    async def numbers(paging: OffsetPagination) -> Page[int]:
        return paginate_sequence(range(10), paging)

    with TestClient(app) as client:
        assert client.get("/n?limit=3").json() == [0, 1, 2]


# ---------------------------------------------------------------------------
# Page-number pages
# ---------------------------------------------------------------------------


def test_page_number_meta_and_links() -> None:
    response = _client().get("/users/pages?page=2&per_page=10")

    assert response.json()["meta"] == {
        "page": 2,
        "per_page": 10,
        "total": 35,
        "total_pages": 4,
        "has_next": True,
    }
    assert _links(response) == {
        "first": "/users/pages?page=1&per_page=10",
        "prev": "/users/pages?page=1&per_page=10",
        "next": "/users/pages?page=3&per_page=10",
        "last": "/users/pages?page=4&per_page=10",
    }


# ---------------------------------------------------------------------------
# Cursor pages
# ---------------------------------------------------------------------------


def test_cursor_first_page() -> None:
    response = _client().get("/users/cursor?limit=5")

    body = response.json()
    assert [u["id"] for u in body["items"]] == [1, 2, 3, 4, 5]
    assert body["meta"]["has_next"] is True
    assert body["meta"]["prev_cursor"] is None
    assert set(_links(response)) == {"first", "next"}


def test_cursor_next_and_prev_navigation() -> None:
    client = _client()
    first = client.get("/users/cursor?limit=5").json()

    second = client.get("/users/cursor", params={"limit": 5, "cursor": first["meta"]["next_cursor"]}).json()
    back = client.get("/users/cursor", params={"limit": 5, "cursor": second["meta"]["prev_cursor"]}).json()

    assert [u["id"] for u in second["items"]] == [6, 7, 8, 9, 10]
    assert [u["id"] for u in back["items"]] == [1, 2, 3, 4, 5]
    assert back["meta"]["prev_cursor"] is None


@pytest.mark.parametrize("envelope", ["items", "data", "bare"])
def test_cursor_page_in_every_envelope(envelope: str) -> None:
    response = _client(envelope=envelope).get("/users/cursor?limit=2")

    body = response.json()
    items = body if envelope == "bare" else body["items" if envelope == "items" else "data"]
    assert [u["id"] for u in items] == [1, 2]
    assert "next" in _links(response)
    assert response.headers.get("x-total-count") is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda token: token[:-1] + ("0" if token[-1] != "0" else "1"), "signature"),
        (lambda token: "garbage", "signature"),
        (lambda token: "a" * 5000, "malformed"),
    ],
)
def test_tampered_cursor_is_400_invalid_cursor(mutate: Any, reason: str) -> None:
    client = _client()
    token = client.get("/users/cursor?limit=5").json()["meta"]["next_cursor"]

    response = client.get("/users/cursor", params={"cursor": mutate(token)})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_cursor"
    assert error["detail"] == {"param": "cursor", "reason": reason}


def test_cursor_from_another_secret_is_rejected() -> None:
    token = _client(cursor_secret="other").get("/users/cursor?limit=5").json()["meta"]["next_cursor"]

    response = _client().get("/users/cursor", params={"cursor": token})

    assert response.json()["error"]["code"] == "invalid_cursor"


def test_cursor_secret_rotation_keeps_old_cursors_valid() -> None:
    token = _client(cursor_secret="old").get("/users/cursor?limit=5").json()["meta"]["next_cursor"]

    response = _client(cursor_secret=["new", "old"]).get("/users/cursor", params={"cursor": token})

    assert [u["id"] for u in response.json()["items"]][:1] == [6]


def test_invalid_cursor_in_problem_details_format() -> None:
    app = LaurenFactory.create(
        AppModule, pagination=PaginationConfig(cursor_secret=SECRET), error_format="rfc9457"
    )

    response = TestClient(app).get("/users/cursor?cursor=bad")

    assert response.status_code == 400
    assert response.json()["code"] == "invalid_cursor"


# ---------------------------------------------------------------------------
# URLs behind a root path / with a base URL
# ---------------------------------------------------------------------------


def test_links_include_server_root_path() -> None:
    app = LaurenFactory.create(AppModule, pagination=PaginationConfig(cursor_secret=SECRET))

    response = TestClient(app, root_path="/api").get("/users/?limit=10")

    assert _links(response)["next"] == "/api/users/?limit=10&offset=10"


def test_links_include_app_root_path() -> None:
    app = LaurenFactory.create(AppModule, pagination=PaginationConfig(cursor_secret=SECRET), root_path="/v2")

    response = TestClient(app).get("/v2/users/?limit=10")

    assert _links(response)["next"] == "/v2/users/?limit=10&offset=10"


def test_links_include_mount_prefix() -> None:
    child = LaurenFactory.create(AppModule, pagination=PaginationConfig(cursor_secret=SECRET))
    parent = LaurenFactory.create(_EmptyModule, mounts={"/v1": child})

    response = TestClient(parent).get("/v1/users/?limit=10")

    assert _links(response)["next"] == "/v1/users/?limit=10&offset=10"


@controller("/api/things")
class _PrefixedController:
    @get("/")
    async def things(self, paging: OffsetPagination) -> Page[int]:
        return paginate_sequence(range(30), paging)


@module(controllers=[_PrefixedController])
class _PrefixedModule:
    pass


def test_root_path_already_in_the_path_is_not_repeated() -> None:
    response = TestClient(LaurenFactory.create(_PrefixedModule), root_path="/api").get(
        "/api/things/?limit=10"
    )

    assert _links(response)["next"] == "/api/things/?limit=10&offset=10"


def test_base_url_gives_absolute_links() -> None:
    response = _client(base_url="https://api.example.com").get("/users/?limit=10")

    assert _links(response)["next"] == "https://api.example.com/users/?limit=10&offset=10"


# ---------------------------------------------------------------------------
# Startup validation
# ---------------------------------------------------------------------------


def test_cursor_route_without_secret_fails_at_startup() -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["reason"] == "pagination_secret_missing"
    assert info.value.detail["route"] == "GET /users/cursor"
    assert info.value.detail["param"] == "paging"


def test_unsupported_cursor_type_fails_at_startup() -> None:
    class Opaque:
        pass

    @controller("/bad")
    class BadController:
        @get("/")
        async def index(self, paging: CursorPagination[Opaque]) -> dict:
            return {}

    @module(controllers=[BadController])
    class BadModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(BadModule, pagination=PaginationConfig(cursor_secret=SECRET))

    assert info.value.detail["reason"] == "unsupported_cursor_type"


def test_invalid_app_config_fails_at_startup() -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule, pagination=PaginationConfig(default_limit=500, max_limit=100))

    assert info.value.detail == {
        "reason": "invalid_pagination_config",
        "field": "default_limit",
        "layer": "app",
    }


# ---------------------------------------------------------------------------
# Item backends serialise to the same JSON
# ---------------------------------------------------------------------------


class ItemDict(TypedDict):
    id: int
    name: str


def _items_app(make: Any) -> TestClient:
    @controller("/items")
    class ItemController:
        @get("/")
        async def index(self, paging: OffsetPagination) -> Any:
            return paginate_sequence([make(i, f"n{i}") for i in range(1, 6)], paging)

    @module(controllers=[ItemController])
    class ItemModule:
        pass

    return TestClient(LaurenFactory.create(ItemModule))


_EXPECTED_ITEMS = {
    "items": [{"id": 1, "name": "n1"}, {"id": 2, "name": "n2"}],
    "meta": {"limit": 2, "offset": 0, "total": 5, "has_next": True},
}


def _backend(name: str) -> Any:
    if name == "dataclass":
        return lambda i, n: UserOut(id=i, name=n)
    if name == "typeddict":
        return lambda i, n: ItemDict(id=i, name=n)
    if name == "pydantic":
        pydantic = pytest.importorskip("pydantic")
        model = pydantic.create_model("PydItem", id=(int, ...), name=(str, ...))
        return lambda i, n: model(id=i, name=n)
    msgspec = pytest.importorskip("msgspec")
    struct = msgspec.defstruct("MsgItem", [("id", int), ("name", str)])
    return lambda i, n: struct(id=i, name=n)


@pytest.mark.parametrize("backend", ["dataclass", "typeddict", "pydantic", "msgspec"])
def test_item_backends_serialise_identically(backend: str) -> None:
    response = _items_app(_backend(backend)).get("/items/?limit=2")

    assert response.json() == _EXPECTED_ITEMS


@pytest.mark.parametrize("encoder_name", ["orjson", "msgspec"])
def test_page_serialises_through_alternative_encoders(encoder_name: str) -> None:
    from lauren import MsgspecEncoder, OrjsonEncoder

    pytest.importorskip(encoder_name)
    encoder = OrjsonEncoder() if encoder_name == "orjson" else MsgspecEncoder()
    app = LaurenFactory.create(
        AppModule, pagination=PaginationConfig(cursor_secret=SECRET), json_encoder=encoder
    )

    body = TestClient(app).get("/users/?limit=1").json()

    assert body == {
        "items": [{"id": 1, "name": "user1"}],
        "meta": {"limit": 1, "offset": 0, "total": 35, "has_next": True},
    }


def test_page_nested_in_a_dict_serialises_as_fields() -> None:
    page = Page.of([UserOut(id=1, name="a")], OffsetPagination(limit=1), total=1)

    assert json.loads(Response.json({"page": page}).body) == {"page": dataclasses.asdict(page)}


@module()
class _EmptyModule:
    pass


# ---------------------------------------------------------------------------
# Response contracts: a page is a wrapper, its items are projected
# ---------------------------------------------------------------------------


@dataclass
class UserRecord(UserOut):
    password: str


RECORDS = [UserRecord(id=i, name=f"user{i}", password=f"secret{i}") for i in range(1, 6)]


@controller("/records")
class RecordController:
    @get("/")
    async def offset(self, paging: OffsetPagination) -> Page[UserOut]:
        return paginate_sequence(RECORDS, paging)

    @get("/cursor")
    async def cursor(self, paging: CursorPagination[UserKey]) -> CursorPage[UserOut]:
        start = paging.cursor.id if paging.cursor is not None else 0
        rows = [r for r in RECORDS if r.id > start][: paging.limit + 1]
        return CursorPage.from_rows(rows, paging, key=lambda r: UserKey(id=r.id))

    @get("/broken")
    async def broken(self, paging: OffsetPagination) -> Page[UserOut]:
        return paginate_sequence([{"id": "not-a-number", "name": "x"}], paging)

    @get("/partial", status_code=206)
    async def partial(self, paging: OffsetPagination) -> Page[UserOut]:
        return paginate_sequence(RECORDS, paging)

    @get("/not-a-page")
    async def not_a_page(self) -> Page[UserOut]:
        return [{"id": 1, "name": "x"}]  # type: ignore[return-value]


@module(controllers=[RecordController])
class RecordModule:
    pass


def _records(mode: str, **config: Any) -> TestClient:
    config.setdefault("cursor_secret", SECRET)
    app = LaurenFactory.create(RecordModule, response_validation=mode, pagination=PaginationConfig(**config))
    return TestClient(app)


@pytest.mark.parametrize("mode", ["filter", "strict"])
def test_contract_projects_page_items_and_keeps_the_page_response(mode: str) -> None:
    response = _records(mode).get("/records/?limit=2")

    assert response.status_code == 200
    assert response.json() == {
        "items": [{"id": 1, "name": "user1"}, {"id": 2, "name": "user2"}],
        "meta": {"limit": 2, "offset": 0, "total": 5, "has_next": True},
    }
    assert set(_links(response)) == {"first", "next", "last"}


def test_contract_projects_cursor_page_items_and_keeps_cursors() -> None:
    client = _records("filter")

    first = client.get("/records/cursor?limit=2").json()
    second = client.get(f"/records/cursor?limit=2&cursor={first['meta']['next_cursor']}").json()

    assert first["items"] == [{"id": 1, "name": "user1"}, {"id": 2, "name": "user2"}]
    assert second["items"] == [{"id": 3, "name": "user3"}, {"id": 4, "name": "user4"}]


def test_strict_contract_rejects_an_invalid_page_item_with_an_items_loc(
    caplog: pytest.LogCaptureFixture,
) -> None:
    response = _records("strict").get("/records/broken")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "response_validation_error"
    assert "response.items.0.id" in caplog.text


def test_contract_rejects_a_plain_list_for_a_page_route() -> None:
    response = _records("filter").get("/records/not-a-page")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "response_validation_error"


def test_declared_status_page_uses_the_app_envelope() -> None:
    response = _records("off", envelope="data").get("/records/partial?limit=2")

    assert response.status_code == 206
    assert [item["id"] for item in response.json()["data"]] == [1, 2]
    assert response.json()["links"]["next"] is not None


def test_openapi_documents_the_page_under_the_declared_status() -> None:
    spec = LaurenFactory.create(RecordModule, pagination=PaginationConfig(cursor_secret=SECRET)).openapi()

    responses = spec["paths"]["/records/partial"]["get"]["responses"]
    assert "200" not in responses
    assert responses["206"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/UserOutPage"
    }
    assert "Link" in responses["206"]["headers"]
