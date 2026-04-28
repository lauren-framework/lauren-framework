"""End-to-end tests for ``Optional[Path[T]]`` / ``Path[Optional[T]]``
and their ``X | None`` PEP 604 equivalents, across every extractor
source (path, query, header, cookie).

Each test pins one concrete user shape observed in the wild:

* **Outer optional on a path parameter** — e.g. ``/item/{id}`` and
  ``/item/`` should both match, with the handler receiving ``None``
  when ``id`` is absent.
* **Outer optional on a query parameter** — a search endpoint whose
  ``q=`` query is optional.
* **Outer optional on a header / cookie** — auth endpoints that want
  to treat a missing ``Authorization`` as an anonymous request.
* **Inner optional** — ``Path[Optional[int]]`` — still coerces a
  present value through ``int`` and yields ``None`` when absent.
* **Combined with constraints** — ``Optional[Path[Annotated[int,
  PathField(ge=1)]]]`` preserves the user's constraints.
"""

from __future__ import annotations

from typing import Annotated, Optional

from lauren import (
    Cookie,
    Header,
    LaurenFactory,
    Path,
    Query,
    QueryField,
    controller,
    get,
    module,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Outer optional on path parameters — two routes share a controller
# ---------------------------------------------------------------------------


@controller("/items")
class _ItemController:
    @get("/{item_id}")
    async def by_id(self, item_id: Optional[Path[int]]) -> dict:
        # When the route matches, item_id is always present; the
        # Optional shape still has to coerce correctly through int.
        return {"id": item_id}

    @get("/")
    async def default(self, item_id: Optional[Path[int]] = None) -> dict:
        # No path parameter provided — Optional yields None.
        return {"id": item_id}


@module(controllers=[_ItemController])
class _ItemModule:
    pass


def test_outer_optional_path_param_present_coerces_to_int() -> None:
    app = LaurenFactory.create(_ItemModule)
    r = TestClient(app).get("/items/42")
    assert r.status_code == 200
    assert r.json() == {"id": 42}


def test_outer_optional_path_param_absent_yields_none() -> None:
    app = LaurenFactory.create(_ItemModule)
    r = TestClient(app).get("/items/")
    assert r.status_code == 200
    assert r.json() == {"id": None}


# ---------------------------------------------------------------------------
# Outer optional on a query parameter
# ---------------------------------------------------------------------------


@controller("/search")
class _SearchController:
    @get("/")
    async def search(self, q: Optional[Query[str]]) -> dict:
        return {"q": q}

    @get("/pep604")
    async def search604(self, q: Query[str] | None) -> dict:
        return {"q": q}


@module(controllers=[_SearchController])
class _SearchModule:
    pass


def test_optional_query_returns_value_when_present() -> None:
    app = LaurenFactory.create(_SearchModule)
    r = TestClient(app).get("/search/?q=kittens")
    assert r.json() == {"q": "kittens"}


def test_optional_query_returns_none_when_absent() -> None:
    app = LaurenFactory.create(_SearchModule)
    r = TestClient(app).get("/search/")
    assert r.status_code == 200
    assert r.json() == {"q": None}


def test_pep604_optional_query_returns_none_when_absent() -> None:
    app = LaurenFactory.create(_SearchModule)
    r = TestClient(app).get("/search/pep604")
    assert r.status_code == 200
    assert r.json() == {"q": None}


def test_pep604_optional_query_returns_value_when_present() -> None:
    app = LaurenFactory.create(_SearchModule)
    r = TestClient(app).get("/search/pep604?q=hello")
    assert r.json() == {"q": "hello"}


# ---------------------------------------------------------------------------
# Outer optional on a header — auth endpoints typically want None on
# missing Authorization rather than an error.
# ---------------------------------------------------------------------------


@controller("/auth")
class _AuthController:
    @get("/whoami")
    async def whoami(
        self,
        authorization: Optional[Header[str]] = None,
    ) -> dict:
        if authorization is None:
            return {"user": "anonymous"}
        return {"user": authorization.removeprefix("Bearer ").strip()}


@module(controllers=[_AuthController])
class _AuthModule:
    pass


def test_optional_header_returns_anonymous_when_missing() -> None:
    app = LaurenFactory.create(_AuthModule)
    r = TestClient(app).get("/auth/whoami")
    assert r.status_code == 200
    assert r.json() == {"user": "anonymous"}


def test_optional_header_extracts_value_when_present() -> None:
    app = LaurenFactory.create(_AuthModule)
    r = TestClient(app).get("/auth/whoami", headers={"authorization": "Bearer abc-123"})
    assert r.json() == {"user": "abc-123"}


# ---------------------------------------------------------------------------
# Outer optional on a cookie — session cookies that may be absent.
# ---------------------------------------------------------------------------


@controller("/session")
class _SessionController:
    @get("/id")
    async def session_id(
        self,
        session: Optional[Cookie[str]] = None,
    ) -> dict:
        return {"session": session}


@module(controllers=[_SessionController])
class _SessionModule:
    pass


def test_optional_cookie_returns_none_when_missing() -> None:
    app = LaurenFactory.create(_SessionModule)
    r = TestClient(app).get("/session/id")
    assert r.json() == {"session": None}


def test_optional_cookie_returns_value_when_present() -> None:
    app = LaurenFactory.create(_SessionModule)
    r = TestClient(app).get("/session/id", headers={"cookie": "session=abc123"})
    assert r.json() == {"session": "abc123"}


# ---------------------------------------------------------------------------
# Inner optional — ``Path[Optional[int]]``
# ---------------------------------------------------------------------------


@controller("/inner")
class _InnerController:
    @get("/{item_id}")
    async def by_id(self, item_id: Path[Optional[int]]) -> dict:
        # Even though the path param is always present when this route
        # matches, the inner-Optional shape exercises the union-aware
        # coercion path inside ``_coerce_scalar``.
        return {"id": item_id, "type": type(item_id).__name__}

    @get("/pep604/{item_id}")
    async def by_id_604(self, item_id: Path[int | None]) -> dict:
        return {"id": item_id, "type": type(item_id).__name__}


@module(controllers=[_InnerController])
class _InnerModule:
    pass


def test_inner_optional_path_coerces_through_int() -> None:
    app = LaurenFactory.create(_InnerModule)
    r = TestClient(app).get("/inner/99")
    assert r.json() == {"id": 99, "type": "int"}


def test_inner_optional_pep604_path_coerces_through_int() -> None:
    app = LaurenFactory.create(_InnerModule)
    r = TestClient(app).get("/inner/pep604/7")
    assert r.json() == {"id": 7, "type": "int"}


# ---------------------------------------------------------------------------
# Constraints survive outer-optional wrapping
# ---------------------------------------------------------------------------


@controller("/constrained")
class _ConstrainedController:
    @get("/")
    async def page(
        self,
        # ``Annotated`` metadata sits inside Optional, so the constraint
        # must still reach the validation layer.
        limit: Optional[Annotated[Query[int], QueryField(ge=1, le=100)]] = None,
    ) -> dict:
        return {"limit": limit}


@module(controllers=[_ConstrainedController])
class _ConstrainedModule:
    pass


def test_outer_optional_preserves_query_constraints_when_present() -> None:
    app = LaurenFactory.create(_ConstrainedModule)
    client = TestClient(app)
    assert client.get("/constrained/?limit=50").json() == {"limit": 50}
    # Violates ge=1 — should be a validation error, not a silent pass.
    r = client.get("/constrained/?limit=0")
    assert r.status_code >= 400


def test_outer_optional_preserves_query_constraints_when_absent() -> None:
    app = LaurenFactory.create(_ConstrainedModule)
    r = TestClient(app).get("/constrained/")
    assert r.status_code == 200
    assert r.json() == {"limit": None}


# ---------------------------------------------------------------------------
# Outer optional with explicit default — user intent wins over None
# ---------------------------------------------------------------------------


@controller("/defaulted")
class _DefaultedController:
    @get("/")
    async def page(
        self,
        # User explicitly says: "when missing, give me 10". The outer
        # Optional wrapper must not clobber this default with None.
        limit: Optional[Annotated[Query[int], QueryField(default=10)]] = 10,
    ) -> dict:
        return {"limit": limit}


@module(controllers=[_DefaultedController])
class _DefaultedModule:
    pass


def test_outer_optional_respects_user_supplied_default() -> None:
    app = LaurenFactory.create(_DefaultedModule)
    r = TestClient(app).get("/defaulted/")
    # The explicit default=10 takes precedence over the synthesised
    # None default — a user who went to the trouble of writing
    # ``default=10`` clearly didn't want None on missing.
    assert r.json() == {"limit": 10}


# ---------------------------------------------------------------------------
# Mixed Optional and required params in the same handler
# ---------------------------------------------------------------------------


@controller("/mixed")
class _MixedController:
    @get("/{tag}")
    async def page(
        self,
        tag: Path[str],
        page: Optional[Query[int]] = None,
        size: Query[int] = 20,
    ) -> dict:
        return {"tag": tag, "page": page, "size": size}


@module(controllers=[_MixedController])
class _MixedModule:
    pass


def test_mixed_required_and_optional_params_in_one_handler() -> None:
    app = LaurenFactory.create(_MixedModule)
    client = TestClient(app)
    assert client.get("/mixed/books").json() == {
        "tag": "books",
        "page": None,
        "size": 20,
    }
    assert client.get("/mixed/books?page=3&size=50").json() == {
        "tag": "books",
        "page": 3,
        "size": 50,
    }
    assert client.get("/mixed/books?page=3").json() == {
        "tag": "books",
        "page": 3,
        "size": 20,
    }
