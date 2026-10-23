"""``CacheModule``: which store the app's ``Cache`` runs on, options and startup errors."""

from __future__ import annotations

from typing import Any

import pytest

from lauren import (
    DecoratorUsageError,
    LaurenFactory,
    StartupError,
    controller,
    get,
    injectable,
    module,
)
from lauren.cache import Cache, CacheModule, JsonCacheCodec
from lauren.serialization import StdlibJSONEncoder
from lauren.stores import MemoryStore, PrefixedStore, Store, StoresModule
from lauren.testing import TestClient


class HookedStore(MemoryStore):
    def __init__(self) -> None:
        super().__init__(max_entries=100)
        self.events: list[str] = []

    async def startup(self) -> None:
        self.events.append("startup")

    async def aclose(self) -> None:
        self.events.append("aclose")


def make_app(*imports: type, providers: list[Any] | None = None, **create: Any) -> Any:
    @module(imports=list(imports), providers=providers or [])
    class AppModule:
        pass

    return LaurenFactory.create(AppModule, **create)


def resolve_cache(app: Any) -> Cache:
    with TestClient(app) as client:
        return client.resolve(Cache)


def test_without_any_store_the_cache_gets_a_private_bounded_memory_store() -> None:
    cache = resolve_cache(make_app(CacheModule.for_root()))
    assert isinstance(cache.store, PrefixedStore) and cache.store.prefix == "cache"
    assert isinstance(cache.store.base, MemoryStore) and cache.store.base.max_entries == 100_000


def test_two_apps_never_share_the_private_store() -> None:
    root_imports = (CacheModule.for_root(),)
    first, second = resolve_cache(make_app(*root_imports)), resolve_cache(make_app(*root_imports))
    assert first is not second
    assert first.store.base is not second.store.base  # type: ignore[attr-defined]


def test_the_default_stores_module_store_is_used() -> None:
    store = MemoryStore(max_entries=10)
    cache = resolve_cache(make_app(StoresModule.for_root(default=store), CacheModule.for_root()))
    assert cache.store.base is store  # type: ignore[attr-defined]


def test_a_structural_store_provider_is_used() -> None:
    @injectable()
    class AppStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__(max_entries=10)

    app = make_app(CacheModule.for_root(namespace=None), providers=[AppStore])
    cache = resolve_cache(app)
    assert isinstance(cache.store, AppStore)


def test_a_named_store_is_used() -> None:
    local = MemoryStore(max_entries=10)
    app = make_app(StoresModule.for_root(named={"local": local}), CacheModule.for_root(store="local"))
    assert resolve_cache(app).store.base is local  # type: ignore[attr-defined]


def test_an_unknown_store_name_fails_startup() -> None:
    with pytest.raises(StartupError) as info:
        make_app(CacheModule.for_root(store="nope"))
    assert info.value.detail == {"reason": "invalid_store", "name": "nope"}


def test_a_store_instance_has_its_lifecycle_run() -> None:
    store = HookedStore()
    with TestClient(make_app(CacheModule.for_root(store=store))) as client:
        assert client.resolve(Cache).store.base is store
        assert store.events == ["startup"]
    assert store.events == ["startup", "aclose"]


def test_a_store_factory_is_called_once_per_app() -> None:
    made: list[MemoryStore] = []

    def factory() -> MemoryStore:
        made.append(MemoryStore(max_entries=5))
        return made[-1]

    imports = (CacheModule.for_root(store=factory),)
    make_app(*imports)
    make_app(*imports)
    assert len(made) == 2


def test_a_factory_returning_a_non_store_fails_startup() -> None:
    with pytest.raises(StartupError) as info:
        make_app(CacheModule.for_root(store=lambda: 42))
    assert info.value.detail == {"reason": "invalid_store", "got": "int"}


def test_conflicting_cache_modules_fail_startup() -> None:
    with pytest.raises(StartupError) as info:
        make_app(CacheModule.for_root(default_ttl=1), CacheModule.for_root(default_ttl=2))
    assert info.value.detail == {"reason": "conflicting_cache_options", "count": 2}


def test_an_application_cache_binding_wins() -> None:
    from lauren import use_value

    own = Cache(MemoryStore(max_entries=1))
    app = make_app(CacheModule.for_root(), providers=[use_value(provide=Cache, value=own)])
    assert resolve_cache(app) is own


def test_options_reach_the_cache() -> None:
    codec = JsonCacheCodec(StdlibJSONEncoder())
    cache = resolve_cache(make_app(CacheModule.for_root(default_ttl=None, codec=codec)))
    assert cache.default_ttl is None and cache.codec is codec


def test_the_default_codec_uses_the_apps_json_encoder() -> None:
    encoder = StdlibJSONEncoder()
    cache = resolve_cache(make_app(CacheModule.for_root(), json_encoder=encoder))
    assert isinstance(cache.codec, JsonCacheCodec) and cache.codec.encoder is encoder


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"store": 42}, "invalid_store"),
        ({"store": ""}, "invalid_store"),
        ({"default_ttl": 0}, "invalid_ttl"),
        ({"lock_ttl": -1}, "invalid_ttl"),
        ({"lock_wait": -1}, "invalid_ttl"),
        ({"codec": object()}, "invalid_codec"),
    ],
)
def test_for_root_validates_immediately(kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        CacheModule.for_root(**kwargs)
    assert info.value.detail["reason"] == reason


def test_for_root_rejects_an_empty_namespace() -> None:
    with pytest.raises(ValueError, match="namespace"):
        CacheModule.for_root(namespace="")


def test_cache_injected_into_a_controller_shares_the_store_with_stores_module() -> None:
    store = MemoryStore(max_entries=100)

    @controller("/k")
    class KV:
        def __init__(self, cache: Cache, raw: Store) -> None:
            self.cache = cache
            self.raw = raw

        @get("/")
        async def handler(self) -> dict:
            await self.cache.set("greeting", "hi")
            return {"raw": (await self.raw.get("cache:val:greeting") or b"").decode()}

    @module(imports=[StoresModule.for_root(default=store), CacheModule.for_root()], controllers=[KV])
    class AppModule:
        pass

    with TestClient(LaurenFactory.create(AppModule)) as client:
        body = client.get("/k/").json()
    assert body["raw"].endswith('"hi"')


def test_testing_overrides_replace_the_cache() -> None:
    from lauren.testing import Test

    fake = Cache(MemoryStore(max_entries=5))

    @module(imports=[CacheModule.for_root()])
    class Root:
        pass

    app = Test.create_testing_module(Root).override_provider(Cache).use_value(fake).compile()
    assert resolve_cache(app) is fake
