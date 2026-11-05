"""Session stores get their lifecycle hooks, and sessions run on any ``Store``.

Regression for the 2026-10-23 probe: a store passed as
``SessionConfig(store=...)`` was registered as a bare value, so its
``@post_construct`` never ran and the first session request answered 500.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from lauren import (
    InMemoryRevocationStore,
    InMemorySessionStore,
    LaurenFactory,
    RevocationStore,
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
from lauren.stores import MemoryStore, Store, StoreRevocationStore, StoresModule, StoreSessionStore
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


class ConventionStore(MemoryStore):
    """A ``Store`` using the ``startup()`` / ``aclose()`` convention."""

    def __init__(self) -> None:
        super().__init__(max_entries=1_000)
        self.events: list[str] = []

    async def startup(self) -> None:
        self.events.append("startup")

    async def aclose(self) -> None:
        self.events.append("aclose")


class ConventionSessionStore(InMemorySessionStore):
    """A session store using the ``startup()`` / ``aclose()`` convention."""

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


def test_startup_and_aclose_methods_are_honoured() -> None:
    store = ConventionSessionStore()
    with TestClient(build(store=store)) as client:
        assert store.events == ["startup"]
        assert client.post("/s/visit").status_code == 200
    assert store.events == ["startup", "aclose"]


def test_a_store_without_hooks_still_works() -> None:
    with TestClient(build(store=InMemorySessionStore())) as client:
        assert client.post("/s/visit").json() == {"visits": 1}


def test_store_passed_to_sessions_is_adapted_and_round_trips() -> None:
    store = MemoryStore(max_entries=1_000)
    app = build(store=store, max_age=600)
    with TestClient(app) as client:
        assert client.post("/s/visit").json() == {"visits": 1}
        assert client.get("/s/").json() == {"visits": 1}
        resolved = client.resolve(SessionStore)
    assert isinstance(resolved, StoreSessionStore)
    assert len(store) == 1


async def test_adapted_session_entries_live_under_the_session_namespace_with_max_age() -> None:
    store = MemoryStore(max_entries=100)
    adapter = StoreSessionStore(store)
    await adapter.save("abc", {"a": 1}, max_age=60)
    assert await store.exists("session:abc")
    remaining = await store.ttl("session:abc")
    assert remaining is not None and 59 < remaining <= 60
    assert await adapter.load("abc") == {"a": 1}
    await adapter.delete("abc")
    assert await adapter.load("abc") is None
    assert repr(adapter).startswith("StoreSessionStore(PrefixedStore(")


async def test_session_adapter_without_namespace_and_with_non_positive_max_age() -> None:
    store = MemoryStore(max_entries=100)
    adapter = StoreSessionStore(store, namespace=None)
    assert adapter.store is store
    await adapter.save("forever", {}, max_age=None)
    assert await store.ttl("forever") is None
    await adapter.save("gone", {}, max_age=-1)
    assert not await store.exists("gone")
    assert len(adapter.new_id()) >= 32


def test_convention_store_startup_and_aclose_run_once_when_shared_with_stores_module() -> None:
    store = ConventionStore()

    @module(imports=[StoresModule.for_root(default=store)], controllers=[SessionController])
    class SharedModule:
        pass

    app = LaurenFactory.create(
        SharedModule,
        sessions=SessionConfig(secret=SECRET, secure=False, store=store, revocation_store=store, max_age=600),
    )
    with TestClient(app) as client:
        assert store.events == ["startup"]
        assert client.post("/s/visit").status_code == 200
        assert client.resolve(Store) is store
    assert store.events == ["startup", "aclose"]


def test_revocation_over_a_store_denies_a_replayed_cookie() -> None:
    store = MemoryStore(max_entries=1_000)
    app = build(store=store, revocation_store=store.namespace("revoked"), max_age=600)
    with TestClient(app) as client:
        client.post("/s/visit")
        cookie = client.cookies.get("lauren_session")
        assert cookie
        assert client.post("/s/logout").status_code == 200
        assert isinstance(client.resolve(RevocationStore), StoreRevocationStore)
    replay = TestClient(app, cookies={"lauren_session": cookie})
    assert replay.get("/s/").json() == {"visits": 0}
    replay.close()


async def test_revocation_adapter_semantics() -> None:
    store = MemoryStore(max_entries=100)
    revocation = StoreRevocationStore(store)
    await revocation.revoke_token("t1", ttl=60)
    await revocation.revoke_token("t2", ttl=-5)  # already expired: nothing written
    await revocation.revoke_user("u1", cutoff=123.5, ttl=60)
    await revocation.revoke_user("u2")
    assert await revocation.is_token_revoked("t1")
    assert not await revocation.is_token_revoked("t2")
    assert await revocation.user_cutoff("u1") == 123.5
    assert (await revocation.user_cutoff("u2") or 0) > 0
    assert await revocation.user_cutoff("nobody") is None
    assert await store.exists("revocation:token:t1")
    assert revocation.store.prefix == "revocation"  # type: ignore[attr-defined]
    assert repr(revocation).startswith("StoreRevocationStore(")


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


@pytest.mark.parametrize("value", [MemoryStore, lambda: None])
def test_non_store_values_are_not_adapted(value: Any) -> None:
    from lauren._sessions._adapters import adapt_revocation_store, adapt_session_store

    assert adapt_session_store(value, None) is value
    assert adapt_revocation_store(value) is value
    assert adapt_revocation_store(None) is None
