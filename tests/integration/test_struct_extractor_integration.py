"""Integration tests: Query[T] / Json[T] with msgspec.Struct and dataclass.

Covers:
- Query[MsgspecStruct] — fields collected from query string, coerced
- Query[DataclassStruct] — same
- Json[MsgspecStruct] — validated from request body
- Json[DataclassStruct] — same
- Bare params: MsgspecStruct — auto-promoted to JSON body
- Bare params: DataclassStruct — auto-promoted to JSON body
- OrjsonEncoder + Query[MsgspecStruct] — coercion still works
- Missing required field → 422
"""

from __future__ import annotations

import dataclasses

import pytest

from lauren import LaurenFactory, Query, controller, get, module, post
from lauren.testing import TestClient

try:
    import msgspec

    HAS_MSGSPEC = True
except ImportError:
    HAS_MSGSPEC = False

try:
    import orjson  # noqa: F401

    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


# ---------------------------------------------------------------------------
# Shared test helpers
# ---------------------------------------------------------------------------


def _build(ctrl_cls: type, encoder=None) -> TestClient:
    @module(controllers=[ctrl_cls])
    class M:
        pass

    kwargs = {"json_encoder": encoder} if encoder is not None else {}
    return TestClient(LaurenFactory.create(M, **kwargs))


# ---------------------------------------------------------------------------
# msgspec.Struct tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
class TestQueryMsgspecStruct:
    """Query[MsgspecStruct] collects and coerces query-string fields."""

    def _make_client(self):
        class PageParams(msgspec.Struct):
            page: int
            size: int = 20

        @controller("/items")
        class C:
            @get("/")
            async def list_items(self, params: Query[PageParams]) -> dict:
                return {"page": params.page, "size": params.size}

        return _build(C)

    def test_returns_struct_instance(self) -> None:
        r = self._make_client().get("/items/?page=3")
        assert r.status_code == 200
        assert r.json() == {"page": 3, "size": 20}

    def test_overrides_default(self) -> None:
        r = self._make_client().get("/items/?page=2&size=50")
        assert r.status_code == 200
        assert r.json() == {"page": 2, "size": 50}

    def test_missing_required_field_returns_422(self) -> None:
        r = self._make_client().get("/items/")
        assert r.status_code == 422

    def test_alongside_scalar_query_param(self) -> None:
        class PageParams(msgspec.Struct):
            page: int

        @controller("/v2")
        class C2:
            @get("/")
            async def h(self, params: Query[PageParams], q: str = "") -> dict:
                return {"page": params.page, "q": q}

        r = _build(C2).get("/v2/?page=1&q=hello")
        assert r.status_code == 200
        assert r.json() == {"page": 1, "q": "hello"}

    @pytest.mark.skipif(not HAS_ORJSON, reason="orjson not installed")
    def test_with_orjson_encoder(self) -> None:
        from lauren.serialization import OrjsonEncoder

        class PageParams(msgspec.Struct):
            page: int
            size: int = 20

        @controller("/o")
        class CO:
            @get("/")
            async def h(self, params: Query[PageParams]) -> dict:
                return {"page": params.page, "size": params.size}

        r = _build(CO, encoder=OrjsonEncoder()).get("/o/?page=7&size=100")
        assert r.status_code == 200
        assert r.json() == {"page": 7, "size": 100}


@pytest.mark.skipif(not HAS_MSGSPEC, reason="msgspec not installed")
class TestJsonMsgspecStruct:
    """Json[MsgspecStruct] / bare struct params use the request body."""

    def test_json_explicit_marker(self) -> None:
        from lauren import Json

        class Body(msgspec.Struct):
            name: str
            value: int

        @controller("/b")
        class C:
            @post("/")
            async def create(self, body: Json[Body]) -> dict:
                return {"name": body.name, "value": body.value}

        r = _build(C).post("/b/", json={"name": "alpha", "value": 42})
        assert r.status_code == 200
        assert r.json() == {"name": "alpha", "value": 42}

    def test_bare_struct_auto_promotes_to_json_body(self) -> None:
        class Body(msgspec.Struct):
            name: str
            count: int = 0

        @controller("/auto")
        class C:
            @post("/")
            async def create(self, body: Body) -> dict:
                return {"name": body.name, "count": body.count}

        r = _build(C).post("/auto/", json={"name": "test", "count": 5})
        assert r.status_code == 200
        assert r.json() == {"name": "test", "count": 5}

    def test_bare_struct_missing_required_returns_422(self) -> None:
        class Body(msgspec.Struct):
            name: str

        @controller("/miss")
        class C:
            @post("/")
            async def create(self, body: Body) -> dict:
                return {"name": body.name}

        r = _build(C).post("/miss/", json={})
        assert r.status_code == 422


# ---------------------------------------------------------------------------
# Python dataclass tests
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class DCPageParams:
    page: int
    size: int = 20


class TestQueryDataclass:
    """Query[DataclassType] collects and coerces query-string fields."""

    def _make_client(self):
        @controller("/dc")
        class C:
            @get("/")
            async def h(self, params: Query[DCPageParams]) -> dict:
                return {"page": params.page, "size": params.size}

        return _build(C)

    def test_returns_dataclass_instance(self) -> None:
        r = self._make_client().get("/dc/?page=4")
        assert r.status_code == 200
        assert r.json() == {"page": 4, "size": 20}

    def test_overrides_default(self) -> None:
        r = self._make_client().get("/dc/?page=1&size=10")
        assert r.status_code == 200
        assert r.json() == {"page": 1, "size": 10}

    def test_missing_required_field_returns_422(self) -> None:
        r = self._make_client().get("/dc/")
        assert r.status_code == 422


@dataclasses.dataclass
class DCBody:
    name: str
    count: int = 0


class TestJsonDataclass:
    """Json[DataclassType] / bare dataclass params use the request body."""

    def test_bare_dataclass_auto_promotes_to_json_body(self) -> None:
        @controller("/dcauto")
        class C:
            @post("/")
            async def create(self, body: DCBody) -> dict:
                return {"name": body.name, "count": body.count}

        r = _build(C).post("/dcauto/", json={"name": "hello", "count": 3})
        assert r.status_code == 200
        assert r.json() == {"name": "hello", "count": 3}

    def test_explicit_json_marker(self) -> None:
        from lauren import Json

        @controller("/dcjson")
        class C:
            @post("/")
            async def create(self, body: Json[DCBody]) -> dict:
                return {"name": body.name, "count": body.count}

        r = _build(C).post("/dcjson/", json={"name": "world", "count": 7})
        assert r.status_code == 200
        assert r.json() == {"name": "world", "count": 7}
