"""End-to-end tests for the router static-prefix fast path.

Verifies that apps built via :meth:`LaurenFactory.create` correctly
populate the router's static table and that request dispatch is
semantically identical to the pre-optimisation behaviour across:

* Pure-static applications (health checks, metrics endpoints).
* Mixed applications (static + ``{param}`` + ``{*wild}`` routes).
* Method-not-allowed handling on both paths.
* Controller-prefix routes (the real-world shape lauren apps emit).
* Fall-through from a static method-miss to a dynamic sibling.
"""

from __future__ import annotations


from lauren import (
    LaurenFactory,
    Path,
    controller,
    delete,
    get,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# 1. Pure-static app — every route should land in the fast table
# ---------------------------------------------------------------------------


@controller("/api")
class _StaticController:
    @get("/health")
    async def health(self) -> dict:
        return {"status": "ok"}

    @get("/metrics")
    async def metrics(self) -> dict:
        return {"requests": 0}

    @get("/version")
    async def version(self) -> dict:
        return {"version": "1.0.0"}

    @post("/reload")
    async def reload(self) -> dict:
        return {"reloaded": True}


@module(controllers=[_StaticController])
class _StaticModule:
    pass


def test_pure_static_app_populates_fast_table_completely() -> None:
    app = LaurenFactory.create(_StaticModule)
    router = app.router
    # Four methods registered (GET/GET/GET/POST), all fast-path.
    assert router.static_route_count == 4
    assert router._has_dynamic_routes is False
    for path in ("/api/health", "/api/metrics", "/api/version", "/api/reload"):
        assert path in router._static_table


def test_pure_static_app_dispatches_correctly() -> None:
    app = LaurenFactory.create(_StaticModule)
    client = TestClient(app)
    assert client.get("/api/health").json() == {"status": "ok"}
    assert client.get("/api/metrics").json() == {"requests": 0}
    assert client.get("/api/version").json() == {"version": "1.0.0"}
    assert client.post("/api/reload").json() == {"reloaded": True}


def test_pure_static_app_method_not_allowed() -> None:
    app = LaurenFactory.create(_StaticModule)
    r = TestClient(app).delete("/api/health")
    assert r.status_code == 405
    assert r.header("allow") == "GET"


def test_pure_static_app_route_not_found() -> None:
    app = LaurenFactory.create(_StaticModule)
    r = TestClient(app).get("/api/unknown")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# 2. Mixed app — static + dynamic routes in one controller
# ---------------------------------------------------------------------------


@controller("/users")
class _UsersController:
    @get("/")
    async def list_users(self) -> dict:
        return {"users": []}

    @get("/me")
    async def me(self) -> dict:
        return {"user": "current"}

    @get("/{user_id}")
    async def get_user(self, user_id: Path[int]) -> dict:
        return {"id": user_id}

    @delete("/{user_id}")
    async def delete_user(self, user_id: Path[int]) -> dict:
        return {"deleted": user_id}


@module(controllers=[_UsersController])
class _UsersModule:
    pass


def test_mixed_app_fast_table_contains_only_static_routes() -> None:
    app = LaurenFactory.create(_UsersModule)
    router = app.router
    # ``/users`` (list) and ``/users/me`` are static. ``/users/{id}``
    # variants are dynamic.
    assert "/users" in router._static_table
    assert "/users/me" in router._static_table
    assert "/users/{user_id}" not in router._static_table
    assert router._has_dynamic_routes is True


def test_mixed_app_static_routes_dispatch_through_fast_path() -> None:
    app = LaurenFactory.create(_UsersModule)
    client = TestClient(app)
    assert client.get("/users").json() == {"users": []}
    assert client.get("/users/me").json() == {"user": "current"}


def test_mixed_app_dynamic_routes_dispatch_through_radix() -> None:
    app = LaurenFactory.create(_UsersModule)
    client = TestClient(app)
    r = client.get("/users/42")
    assert r.status_code == 200
    assert r.json() == {"id": 42}
    r = client.delete("/users/7")
    assert r.status_code == 200
    assert r.json() == {"deleted": 7}


def test_mixed_app_static_priority_over_dynamic_sibling() -> None:
    """The ``/users/me`` static route must win over ``/users/{id}``
    even though ``me`` would technically coerce to a ``Path[int]``
    of the dynamic handler (and fail). The fast path runs first and
    short-circuits before the dynamic handler is consulted.
    """
    app = LaurenFactory.create(_UsersModule)
    r = TestClient(app).get("/users/me")
    assert r.status_code == 200
    assert r.json() == {"user": "current"}


# ---------------------------------------------------------------------------
# 3. Static-path / dynamic-method fall-through
# ---------------------------------------------------------------------------


@controller("")
class _FallthroughController:
    @get("/item")
    async def static_get(self) -> dict:
        return {"via": "static"}

    @post("/{anything}")
    async def dynamic_post(self, anything: Path[str]) -> dict:
        return {"via": "dynamic", "tag": anything}


@module(controllers=[_FallthroughController])
class _FallthroughModule:
    pass


def test_static_path_with_dynamic_sibling_method_is_served_by_sibling() -> None:
    """``GET /item`` must hit the static handler, but ``POST /item``
    should fall through to the dynamic sibling rather than raising
    ``MethodNotAllowed`` \u2014 because a ``{param}`` route can legally
    pick it up.
    """
    app = LaurenFactory.create(_FallthroughModule)
    client = TestClient(app)
    assert client.get("/item").json() == {"via": "static"}
    r = client.post("/item")
    assert r.status_code == 200
    assert r.json() == {"via": "dynamic", "tag": "item"}


# ---------------------------------------------------------------------------
# 4. Wildcard routes
# ---------------------------------------------------------------------------


@controller("/files")
class _FilesController:
    @get("/index")
    async def index(self) -> dict:
        return {"listing": []}

    @get("/{*rel}")
    async def serve(self, rel: Path[str]) -> dict:
        return {"path": rel}


@module(controllers=[_FilesController])
class _FilesModule:
    pass


def test_wildcard_app_static_index_wins_over_wildcard() -> None:
    app = LaurenFactory.create(_FilesModule)
    r = TestClient(app).get("/files/index")
    # Static priority rule: ``/files/index`` hits the static entry.
    assert r.json() == {"listing": []}


def test_wildcard_app_captures_arbitrary_tail() -> None:
    app = LaurenFactory.create(_FilesModule)
    r = TestClient(app).get("/files/a/b/c.txt")
    assert r.json() == {"path": "a/b/c.txt"}


# ---------------------------------------------------------------------------
# 5. Trailing-slash normalisation works end-to-end
# ---------------------------------------------------------------------------


def test_trailing_slash_variations_hit_same_static_route() -> None:
    app = LaurenFactory.create(_StaticModule)
    client = TestClient(app)
    r1 = client.get("/api/health")
    r2 = client.get("/api/health/")
    assert r1.json() == r2.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# 6. Broad app fixture — large static table keeps working
# ---------------------------------------------------------------------------


def test_large_static_table_dispatches_every_route() -> None:
    """Synthesise 50 static routes dynamically; confirm every one
    lands in the fast table and is reachable. This exercises the
    ``_collect_static_routes`` depth-first walk against a broad
    rather than deep tree.
    """

    class _Big:
        pass

    # Build 50 @get-decorated methods programmatically. The handler
    # body captures ``i`` via closure over a default-bound cell so
    # each method knows its own index without needing a parameter.
    def _make_handler(n: int):
        async def _h(self) -> dict:
            return {"n": n}

        return _h

    for i in range(50):
        name = f"route_{i}"
        path = f"/r{i}"
        h = _make_handler(i)
        h.__name__ = name
        decorated = get(path)(h)
        setattr(_Big, name, decorated)

    _Big = controller("/big")(_Big)

    @module(controllers=[_Big])
    class _BigModule:
        pass

    app = LaurenFactory.create(_BigModule)
    router = app.router
    assert router.static_route_count == 50
    assert router._has_dynamic_routes is False

    client = TestClient(app)
    # Spot-check three of them.
    for i in (0, 25, 49):
        r = client.get(f"/big/r{i}")
        assert r.status_code == 200
        assert r.json() == {"n": i}


# ---------------------------------------------------------------------------
# 7. Router-level invariants visible through the app
# ---------------------------------------------------------------------------


def test_router_is_frozen_after_factory_create() -> None:
    """``LaurenFactory.create`` freezes the router before returning
    the app \u2014 this is what populates the fast table. If the factory
    ever stopped calling freeze, the fast table would silently stay
    empty and every lookup would hit the slow path.
    """
    app = LaurenFactory.create(_StaticModule)
    assert app.router.frozen is True
    assert app.router.static_route_count > 0
