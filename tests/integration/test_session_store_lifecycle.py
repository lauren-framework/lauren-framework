"""Session stores get their lifecycle hooks.

Regression for the 2026-10-23 probe: a store passed as
``SessionConfig(store=...)`` was registered as a bare value, so its
``@post_construct`` never ran and the first session request answered 500.
"""

from __future__ import annotations

from typing import Any, ClassVar

from lauren import (
    InMemoryRevocationStore,
    InMemorySessionStore,
    LaurenFactory,
    Session,
    SessionConfig,
    SessionStore,
    controller,
    get,
    module,
    post,
    post_construct,
    pre_destruct,
)
from lauren.testing import TestClient

SECRET = "s" * 32


class RecipeStore:
    """Shaped like the sessions guide's Redis recipe: connect / close via DI hooks."""

    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False

    def __init__(self) -> None:
        self.events: list[str] = []
        self._client: dict[str, dict[str, Any]] | None = None

    @post_construct
    async def connect(self) -> None:
        self.events.append("connect")
        self._client = {}

    @pre_destruct
    async def close(self) -> None:
        self.events.append("close")

    def new_id(self) -> str:
        return f"sid-{len(self.events)}-{id(self)}"

    async def load(self, session_id: str) -> dict[str, Any] | None:
        assert self._client is not None, "connect() never ran"
        return self._client.get(session_id)

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        assert self._client is not None, "connect() never ran"
        self._client[session_id] = dict(data)

    async def delete(self, session_id: str) -> None:
        assert self._client is not None
        self._client.pop(session_id, None)


class ConventionStore(InMemorySessionStore):
    """A store using the ``startup()`` / ``aclose()`` convention."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    async def startup(self) -> None:
        self.events.append("startup")

    async def aclose(self) -> None:
        self.events.append("aclose")


@controller("/s")
class SessionController:
    @get("/")
    async def read(self, session: Session) -> dict:
        return {"visits": session.get("visits", 0)}

    @post("/visit")
    async def visit(self, session: Session) -> dict:
        session["visits"] = session.get("visits", 0) + 1
        return {"visits": session["visits"]}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()
        return {"ok": True}


@module(controllers=[SessionController])
class SessionModule:
    pass


def build(**config: Any) -> Any:
    return LaurenFactory.create(SessionModule, sessions=SessionConfig(secret=SECRET, secure=False, **config))


def test_recipe_store_hooks_run_at_startup_and_shutdown() -> None:
    store = RecipeStore()
    app = build(store=store)
    assert store.events == []
    with TestClient(app) as client:
        assert store.events == ["connect"]
        assert client.post("/s/visit").json() == {"visits": 1}
        assert client.post("/s/visit").json() == {"visits": 2}
    assert store.events == ["connect", "close"]


def test_recipe_store_works_without_a_lifespan_context() -> None:
    store = RecipeStore()
    client = TestClient(build(store=store))
    assert client.post("/s/visit").status_code == 200
    assert store.events == ["connect"]
    client.close()
    assert store.events == ["connect", "close"]


def test_revocation_store_hooks_run_too() -> None:
    class HookedRevocation(InMemoryRevocationStore):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[str] = []

        @post_construct
        def opened(self) -> None:
            self.events.append("open")

        @pre_destruct
        def closed(self) -> None:
            self.events.append("close")

    revocation = HookedRevocation()
    with TestClient(build(revocation_store=revocation, max_age=3600)):
        assert revocation.events == ["open"]
    assert revocation.events == ["open", "close"]


def test_overriding_the_session_store_token_still_starts_the_configured_store() -> None:
    store = RecipeStore()
    fake = RecipeStore()

    @module(controllers=[SessionController])
    class Root:
        pass

    # Build through the override plan the testing builder activates.
    from lauren._di._overrides import OverridePlan, ProviderOverride, building_with

    plan = OverridePlan({SessionStore: ProviderOverride(token=SessionStore, kind="value", value=fake)})
    with building_with(plan):
        app = LaurenFactory.create(Root, sessions=SessionConfig(secret=SECRET, secure=False, store=store))
    with TestClient(app) as client:
        assert client.resolve(SessionStore) is fake
        assert store.events == ["connect"]
        assert client.post("/s/visit").status_code == 200
    assert fake.events == []


def test_startup_and_aclose_methods_are_honoured() -> None:
    store = ConventionStore()
    with TestClient(build(store=store)) as client:
        assert store.events == ["startup"]
        assert client.post("/s/visit").status_code == 200
    assert store.events == ["startup", "aclose"]


def test_a_store_without_hooks_still_works() -> None:
    with TestClient(build(store=InMemorySessionStore())) as client:
        assert client.post("/s/visit").json() == {"visits": 1}
