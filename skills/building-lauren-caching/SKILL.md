---
name: building-lauren-caching
description: Adds caching and shared state to a Lauren app on the unified key-value Store. Covers lauren.stores (Store protocol, bounded MemoryStore, StoresModule.for_root, StoreToken / StoreRegistry, Lock, sessions on a Store), lauren.cache (CacheModule.for_root, Cache.get_or_set with single-flight / lock / stale_ttl, typed decode, tag invalidation), @cache_response (keys, vary, per-user key=, store-time safety rules) and @idempotent (Idempotency-Key replay / 409 / 422), plus testing with fake clocks and the store contract suite. Use when caching expensive work or GET responses, making POSTs safe to retry, or sharing state across workers.
---

> Use `codemap find "Cache"` (or `"MemoryStore"`, `"cache_response"`) before
> reading — stores live in `lauren/_stores/`, caching in `lauren/_cache/`,
> re-exported from `lauren.stores` / `lauren.cache`.

# Lauren Stores & Caching

Every stateful feature — sessions, revocation, the cache, response caching,
idempotency keys, locks — runs on one async key-value `Store`. Pick one backend
(`MemoryStore` in core; a Redis `Store` from a companion or your own), register
it once, and point everything at it.

## Wire it up

```python run
from lauren import LaurenFactory, controller, get, injectable, module, post
from lauren.cache import Cache, CacheModule, cache_response, idempotent
from lauren.stores import MemoryStore, StoresModule
from lauren.testing import TestClient


@injectable()
class SkillPrices:
    def __init__(self, cache: Cache) -> None:
        self._cache = cache
        self.loads = 0

    async def price(self, sku: str) -> int:
        async def load() -> int:
            self.loads += 1
            return 99

        return await self._cache.get_or_set(f"price:{sku}", load, ttl=60, tags=("prices",), type=int)


@controller("/skill-shop")
class SkillShop:
    def __init__(self, prices: SkillPrices) -> None:
        self.prices = prices
        self.orders = 0

    @get("/price/{sku}")
    @cache_response(ttl=30, tags=("prices",))
    async def price(self, sku: str) -> dict:
        return {"price": await self.prices.price(sku)}

    @post("/orders")
    @idempotent(ttl=3600)
    async def order(self) -> dict:
        self.orders += 1
        return {"order": self.orders}


@module(
    imports=[StoresModule.for_root(default=MemoryStore(max_entries=10_000)), CacheModule.for_root()],
    providers=[SkillPrices],
    controllers=[SkillShop],
)
class SkillShopModule:
    pass


with TestClient(LaurenFactory.create(SkillShopModule)) as client:
    client.get("/skill-shop/price/a")
    assert "age" in client.get("/skill-shop/price/a").headers
    retry = [client.post("/skill-shop/orders", headers={"Idempotency-Key": "o-1"}) for _ in range(2)][1]
    assert retry.headers["idempotent-replayed"] == "true"
```

## Choose the right tool

| Need | Use |
|---|---|
| Cache an expensive computation | `await cache.get_or_set(key, factory, ttl=..., tags=(...), type=T)` |
| Cache a whole GET response | `@cache_response(ttl=..., vary=(...), key=...)` on the handler |
| Evict related entries | `tags=` when writing, `await cache.invalidate_tags(...)` after a write |
| Make a POST safe to retry | `@idempotent(ttl=86400, scope=lambda ctx: ...)` |
| Counter / quota / rate window | `await store.incr(key, ttl=window)` (TTL applies on creation only) |
| One worker at a time | `async with Lock(store, "job", ttl=60, wait=5): ...` |
| Sessions in the shared backend | `SessionConfig(store=store, revocation_store=store)` (adapted automatically) |

## Rules that bite

- `MemoryStore(max_entries=...)` is **required** — stores must be bounded.
- Register backends with `StoresModule.for_root(...)` (or pass them to
  `SessionConfig` / `CacheModule.for_root(store=...)`): their `startup()` /
  `aclose()` / `@post_construct` / `@pre_destruct` run with the app.
  `use_value(provide=Store, value=...)` **never runs hooks**.
- Pass a factory (`named={"x": lambda: MemoryStore(max_entries=1_000)}`) when
  several apps (tests) are built from one module — instances are shared.
- **Per-user responses need `key=`** — without it every caller shares one entry
  per URL. `key(ctx)` returning `None` skips the cache for that request. A
  request with `Authorization` bypasses the cache unless `key=` / `vary` makes
  entries per caller or `allow_authenticated=True` says the response is the
  same for everyone. `query=("page",)` stops `?_=<random>` cache busting.
- `max_entries` is shared by every namespace of a store: keep revocation,
  sessions and idempotency records out of a bounded store that also holds
  public response-cache entries.
- `@cache_response` stores only GET/HEAD, statuses 200/203/204/300/301/404/405/410
  that were *returned*, non-streamed bodies without `Set-Cookie`, without
  `Cache-Control: private|no-store|no-cache`, with `Vary` covered by `vary=`.
  Guards run before the lookup.
- Without `type=`, a cache hit is JSON data (`dict`) while a miss returns the
  factory's object — pass `type=Model` (or `cache.typed(Model)`) for models.
- A `stale_ttl` refresh runs in the background, possibly after the request:
  its factory must not close over request-scoped objects.
- `@cache_response` / `@idempotent` without `CacheModule` →
  `StartupError(reason="cache_module_missing")`. Stacking them, bare usage, a
  missing `ttl` or an unsafe `methods=` → `DecoratorUsageError`.
- No `KEYS`/`SCAN`, no pickle: invalidate with tags; values must be
  JSON-encodable (or bring a `CacheCodec`).

## Idempotency semantics

| Request | Answer |
|---|---|
| No `Idempotency-Key` (required) | `400 idempotency_key_missing` |
| First with key `K` | runs; response (status < 500) stored for `ttl` |
| Retry, same payload | replay + `Idempotent-Replayed: true` |
| Retry while first runs | `409 idempotency_key_in_use`, `Retry-After: 1` |
| Same key, other payload | `422 idempotency_key_mismatch` |
| Handler raised | nothing stored; retry runs again |

## Testing

```python fragment
clock = ManualClock()                                   # def __call__(self) -> float
store = MemoryStore(max_entries=100, clock=clock)       # TTLs without sleeping
cache = Cache(store, clock=clock)                       # Age / stale use the cache clock

app = Test.create_testing_module(AppModule).override_provider(Cache).use_value(cache).compile()

# A custom backend proves the contract:
await run_store_contract(lambda: MyStore(), advance=clock.advance)   # lauren.testing.store_contract
```

Full reference: `lauren/llms-full.txt` §Stores & Caching ·
guide: `docs/guides/caching.md`.
