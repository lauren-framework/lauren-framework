"""``StoresModule``: stores as lifecycle-managed, per-app DI providers."""

from __future__ import annotations

import functools
from typing import Annotated, Any

import pytest

from lauren import (
    Inject,
    LaurenFactory,
    StartupError,
    controller,
    get,
    injectable,
    module,
    post_construct,
    pre_destruct,
    use_value,
)
from lauren.stores import MemoryStore, Store, StoreRegistry, StoresModule, StoreToken
from lauren.testing import Test, TestClient


class HookedStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(max_entries=100)
        self.events: list[str] = []

    async def startup(self) -> None:
        self.events.append("startup")

    async def aclose(self) -> None:
        self.events.append("aclose")


@injectable()
class Counter:
    def __init__(self, store: Store, local: Annotated[Store, Inject(StoreToken("local"))]) -> None:
        self.store = store
        self.local = local

    async def hit(self) -> int:
        return await self.store.incr("hits")


@controller("/c")
class CounterController:
    def __init__(self, counter: Counter, registry: StoreRegistry) -> None:
        self._counter = counter
        self._registry = registry

    @get("/")
    async def hit(self) -> dict:
        return {"hits": await self._counter.hit(), "stores": sorted(self._registry)}


def app_module(**stores: Any) -> type:
    @module(imports=[StoresModule.for_root(**stores)], providers=[Counter], controllers=[CounterController])
    class AppModule:
        pass

    return AppModule


def test_default_and_named_stores_are_injectable() -> None:
    default, local = MemoryStore(max_entries=10), MemoryStore(max_entries=10)
    app = LaurenFactory.create(app_module(default=default, named={"local": local}))
    with TestClient(app) as client:
        assert client.get("/c/").json() == {"hits": 1, "stores": ["default", "local"]}
        counter = client.resolve(Counter)
        assert counter.store is default and counter.local is local
        registry = client.resolve(StoreRegistry)
        assert registry.get() is default and registry.get("local") is local


def test_store_lifecycle_hooks_run_with_the_app() -> None:
    default, local = HookedStore(), HookedStore()
    app = LaurenFactory.create(app_module(default=default, named={"local": local}))
    with TestClient(app):
        assert default.events == ["startup"] and local.events == ["startup"]
    assert default.events == ["startup", "aclose"] and local.events == ["startup", "aclose"]


class ChainedStore(HookedStore):
    @post_construct
    async def connect(self) -> None:
        self.events.append("connect")

    @post_construct
    async def warm(self) -> None:
        self.events.append("warm")

    @pre_destruct
    async def flush(self) -> None:
        self.events.append("flush")

    @pre_destruct
    async def disconnect(self) -> None:
        self.events.append("disconnect")


def test_every_decorated_store_hook_runs_instead_of_the_named_methods() -> None:
    store = ChainedStore()
    app = LaurenFactory.create(app_module(default=store, named={"local": MemoryStore(max_entries=10)}))
    with TestClient(app):
        started = list(store.events)
    assert started == ["connect", "warm"]
    assert store.events == ["connect", "warm", "flush", "disconnect"]


def test_factories_give_every_app_its_own_store() -> None:
    root = app_module(default=functools.partial(MemoryStore, max_entries=10), named={"local": HookedStore})
    first, second = LaurenFactory.create(root), LaurenFactory.create(root)
    with TestClient(first) as a, TestClient(second) as b:
        assert a.get("/c/").json()["hits"] == 1
        assert a.get("/c/").json()["hits"] == 2
        assert b.get("/c/").json()["hits"] == 1
        assert a.resolve(Store) is not b.resolve(Store)


def test_default_none_creates_a_bounded_memory_store_per_app() -> None:
    root = app_module(named={"local": MemoryStore(max_entries=1)})
    app = LaurenFactory.create(root)
    with TestClient(app) as client:
        store = client.resolve(Store)
        assert isinstance(store, MemoryStore) and store.max_entries == 100_000


def test_a_factory_returning_a_non_store_fails_startup() -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(app_module(named={"local": lambda: "nope"}))
    assert info.value.detail == {"reason": "invalid_store", "name": "local", "got": "str"}


def test_two_default_stores_fail_startup() -> None:
    @module(
        imports=[
            StoresModule.for_root(default=MemoryStore(max_entries=1)),
            StoresModule.for_root(
                default=MemoryStore(max_entries=1), named={"local": MemoryStore(max_entries=1)}
            ),
        ],
    )
    class Twice:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Twice)
    assert info.value.detail["reason"] == "duplicate_store_name"


def test_a_name_registered_by_two_modules_fails_startup() -> None:
    @module(
        imports=[
            StoresModule.for_root(named={"local": MemoryStore(max_entries=1)}),
            StoresModule.for_root(named={"local": MemoryStore(max_entries=1)}),
        ],
    )
    class Twice:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Twice)
    assert info.value.detail == {"reason": "duplicate_store_name", "name": "local"}


def test_an_existing_store_provider_conflicts_with_stores_module() -> None:
    @module(
        imports=[StoresModule.for_root()],
        providers=[use_value(provide=Store, value=MemoryStore(max_entries=1))],
    )
    class Conflict:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Conflict)
    assert info.value.detail == {"reason": "duplicate_store_name", "name": "default"}


def test_testing_overrides_replace_a_stores_module_store() -> None:
    fake = MemoryStore(max_entries=5)
    root = app_module(named={"local": MemoryStore(max_entries=1)})
    app = Test.create_testing_module(root).override_provider(Store).use_value(fake).compile()
    with TestClient(app) as client:
        assert client.resolve(Store) is fake
        assert client.get("/c/").json()["hits"] == 1


def test_a_namespaced_view_runs_its_backends_lifecycle() -> None:
    backend = HookedStore()
    app = LaurenFactory.create(app_module(named={"local": backend.namespace("local")}))
    with TestClient(app):
        assert backend.events == ["startup"]
    assert backend.events == ["startup", "aclose"]
