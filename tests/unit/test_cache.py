"""Unit tests for the ``Cache`` service (``lauren.cache``) — no ASGI app."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, TypedDict

import pytest

from lauren.cache import Cache, JsonCacheCodec, TypedCache
from lauren.serialization import StdlibJSONEncoder
from lauren.stores import MemoryStore


class ManualClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@dataclasses.dataclass
class Product:
    id: int
    name: str


class ProductDict(TypedDict):
    id: int
    name: str


def make_cache(**kwargs: Any) -> tuple[Cache, MemoryStore, ManualClock]:
    clock = ManualClock()
    store = MemoryStore(max_entries=10_000, clock=clock)
    return Cache(store, clock=clock, **kwargs), store, clock


# ---------------------------------------------------------------------------
# get / set / delete
# ---------------------------------------------------------------------------


async def test_set_then_get_round_trips_json_data() -> None:
    cache, _, _ = make_cache()
    await cache.set("k", {"a": [1, 2]})
    assert await cache.get("k") == {"a": [1, 2]}


async def test_get_of_a_missing_key_is_none() -> None:
    cache, _, _ = make_cache()
    assert await cache.get("nope") is None


async def test_entries_expire_after_their_ttl() -> None:
    cache, _, clock = make_cache()
    await cache.set("k", 1, ttl=10)
    clock.advance(11)
    assert await cache.get("k") is None


async def test_default_ttl_applies_when_no_ttl_is_given() -> None:
    cache, store, _ = make_cache(default_ttl=42)
    await cache.set("k", 1)
    remaining = await store.ttl("val:k")
    assert remaining is not None and 41 < remaining <= 42


async def test_explicit_none_ttl_means_no_expiry() -> None:
    cache, store, _ = make_cache(default_ttl=42)
    await cache.set("k", 1, ttl=None)
    assert await store.ttl("val:k") is None


async def test_delete_removes_entries_and_counts_them() -> None:
    cache, _, _ = make_cache()
    await cache.set("a", 1)
    await cache.set("b", 2)
    assert await cache.delete("a", "b", "c") == 2
    assert await cache.get("a") is None
    assert await cache.delete() == 0


async def test_values_are_written_under_the_val_prefix() -> None:
    cache, store, _ = make_cache()
    await cache.set("k", "v")
    assert await store.exists("val:k")


@pytest.mark.parametrize("bad", [0, -5, "10", True])
async def test_set_rejects_an_invalid_ttl(bad: Any) -> None:
    cache, _, _ = make_cache()
    with pytest.raises((TypeError, ValueError)):
        await cache.set("k", 1, ttl=bad)


@pytest.mark.parametrize(
    "kwargs",
    [{"default_ttl": 0}, {"lock_ttl": 0}, {"lock_wait": -1}, {"lock_wait": True}],
)
def test_constructor_validates_its_options(kwargs: dict[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        Cache(MemoryStore(max_entries=10), **kwargs)


async def test_corrupt_entries_are_misses() -> None:
    cache, store, _ = make_cache()
    await store.set("val:k", b"\x09garbage")
    await store.set("val:j", b"\x01\x00\x00\x00\x02{]")
    assert await cache.get("k") is None
    assert await cache.get("j") is None


async def test_codec_uses_the_given_json_encoder() -> None:
    codec = JsonCacheCodec(StdlibJSONEncoder())
    assert codec.encoder is not None
    assert codec.decode(codec.encode({"a": 1})) == {"a": 1}


async def test_default_codec_encodes_dataclasses_like_responses() -> None:
    cache, _, _ = make_cache()
    await cache.set("p", Product(1, "pen"))
    assert await cache.get("p") == {"id": 1, "name": "pen"}


def test_cache_properties_and_repr() -> None:
    store = MemoryStore(max_entries=1)
    cache = Cache(store, default_ttl=None)
    assert cache.store is store
    assert cache.default_ttl is None
    assert isinstance(cache.codec, JsonCacheCodec)
    assert repr(cache).startswith("Cache(MemoryStore(")


# ---------------------------------------------------------------------------
# Typed decoding
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tp", "value", "expected"),
    [
        (Product, Product(1, "pen"), Product(1, "pen")),
        (ProductDict, {"id": 1, "name": "pen"}, {"id": 1, "name": "pen"}),
        (list[Product], [Product(1, "a"), Product(2, "b")], [Product(1, "a"), Product(2, "b")]),
        (tuple[Product, ...], [Product(1, "a")], (Product(1, "a"),)),
        (set[int], [1, 2], {1, 2}),
        (dict[str, Product], {"x": Product(1, "a")}, {"x": Product(1, "a")}),
        (Product | None, None, None),
        (Product | None, Product(3, "c"), Product(3, "c")),
        (int | None, None, None),
        (int | None, 5, 5),
        (int | str, "x", "x"),
        (int, 5, 5),
        (float, 2, 2.0),
        (str, "s", "s"),
        (bool, True, True),
        (Any, {"k": 1}, {"k": 1}),
        (bytes, "opaque", "opaque"),
    ],
)
async def test_typed_get_decodes_into_the_declared_type(tp: Any, value: Any, expected: Any) -> None:
    cache, _, _ = make_cache()
    await cache.set("k", value)
    assert await cache.get("k", type=tp) == expected


@pytest.mark.parametrize(
    ("tp", "value"),
    [
        (Product, {"id": 1}),
        (Product, [1, 2]),
        (ProductDict, {"name": "x"}),
        (list[int], {"a": 1}),
        (tuple[int, ...], 5),
        (dict[str, int], [1]),
        (int, "five"),
        (int, True),
    ],
)
async def test_data_that_no_longer_validates_is_a_miss(tp: Any, value: Any) -> None:
    cache, _, _ = make_cache()
    await cache.set("k", value)
    assert await cache.get("k", type=tp) is None


async def test_typed_decode_supports_pydantic_models() -> None:
    pydantic = pytest.importorskip("pydantic")

    class Item(pydantic.BaseModel):
        id: int

    cache, _, _ = make_cache()
    await cache.set("k", Item(id=3))
    assert await cache.get("k", type=Item) == Item(id=3)


async def test_typed_decode_supports_msgspec_structs() -> None:
    msgspec = pytest.importorskip("msgspec")

    class Item(msgspec.Struct):
        id: int

    cache, _, _ = make_cache()
    await cache.set("k", {"id": 4})
    assert await cache.get("k", type=Item) == Item(id=4)


async def test_typed_view_binds_the_type() -> None:
    cache, _, _ = make_cache()
    products = cache.typed(Product)
    assert isinstance(products, TypedCache) and products.type is Product
    await products.set("p", Product(1, "pen"), ttl=10, tags=("t",))
    assert await products.get("p") == Product(1, "pen")
    assert await products.get_or_set("q", lambda: Product(2, "ink")) == Product(2, "ink")
    assert await products.get("q") == Product(2, "ink")
    assert await products.delete("p", "q") == 2
    assert repr(products) == f"TypedCache({Product!r})"


async def test_unhashable_type_arguments_still_decode() -> None:
    cache, _, _ = make_cache()
    await cache.set("k", [1])
    unhashable: Any = type("Weird", (), {"__hash__": None, "__eq__": lambda a, b: a is b})()
    assert await cache.get("k", type=unhashable) == [1]


# ---------------------------------------------------------------------------
# get_or_set: single-flight, errors, locks
# ---------------------------------------------------------------------------


async def test_get_or_set_computes_once_then_hits() -> None:
    cache, _, _ = make_cache()
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return 7

    assert await cache.get_or_set("k", factory) == 7
    assert await cache.get_or_set("k", factory) == 7
    assert calls == 1


async def test_get_or_set_accepts_a_sync_factory() -> None:
    cache, _, _ = make_cache()
    assert await cache.get_or_set("k", lambda: "sync") == "sync"


async def test_concurrent_misses_share_one_factory_call() -> None:
    cache, _, _ = make_cache()
    release = asyncio.Event()
    calls = 0

    async def factory() -> str:
        nonlocal calls
        calls += 1
        await release.wait()
        return "value"

    tasks = [asyncio.ensure_future(cache.get_or_set("k", factory)) for _ in range(100)]
    await asyncio.sleep(0)
    release.set()
    assert await asyncio.gather(*tasks) == ["value"] * 100
    assert calls == 1


async def test_factory_errors_reach_every_waiter_and_cache_nothing() -> None:
    cache, _, _ = make_cache()
    release = asyncio.Event()

    async def factory() -> str:
        await release.wait()
        raise RuntimeError("database down")

    tasks = [asyncio.ensure_future(cache.get_or_set("k", factory)) for _ in range(5)]
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert all(isinstance(r, RuntimeError) for r in results)
    assert await cache.get("k") is None
    assert await cache.get_or_set("k", lambda: "recovered") == "recovered"


async def test_a_cancelled_leader_lets_waiters_compute() -> None:
    cache, _, _ = make_cache()
    started, never = asyncio.Event(), asyncio.Event()

    async def slow() -> str:
        started.set()
        await never.wait()
        return "unreachable"  # pragma: no cover

    leader = asyncio.ensure_future(cache.get_or_set("k", slow))
    await started.wait()
    waiter = asyncio.ensure_future(cache.get_or_set("k", lambda: "from-waiter"))
    await asyncio.sleep(0)
    leader.cancel()
    assert await waiter == "from-waiter"
    with pytest.raises(asyncio.CancelledError):
        await leader


async def test_none_is_cached_like_any_other_value() -> None:
    cache, _, _ = make_cache()
    calls = 0

    def factory() -> None:
        nonlocal calls
        calls += 1

    assert await cache.get_or_set("k", factory) is None
    assert await cache.get_or_set("k", factory) is None
    assert calls == 1


async def test_get_or_set_with_type_decodes_hits() -> None:
    cache, _, _ = make_cache()
    first = await cache.get_or_set("p", lambda: Product(1, "pen"), type=Product)
    second = await cache.get_or_set("p", lambda: Product(9, "other"), type=Product)
    assert first == second == Product(1, "pen")


async def test_lock_true_takes_and_releases_a_store_lease() -> None:
    cache, store, _ = make_cache()
    seen: list[bool] = []

    async def factory() -> int:
        seen.append(await store.exists("lock:k"))
        return 1

    assert await cache.get_or_set("k", factory, lock=True) == 1
    assert seen == [True]
    assert not await store.exists("lock:k")


async def test_lock_true_waits_for_another_process_and_reads_its_value() -> None:
    cache, store, _ = make_cache()
    await store.set("lock:k", b"other-process", ttl=30)
    other = Cache(store, clock=cache.now)

    async def other_process_finishes() -> None:
        await asyncio.sleep(0.02)
        await other.set("k", "computed-elsewhere")

    writer = asyncio.ensure_future(other_process_finishes())
    value = await cache.get_or_set("k", lambda: "computed-here", lock=True)
    await writer
    assert value == "computed-elsewhere"


async def test_lock_true_takes_over_when_the_holder_releases_without_a_value() -> None:
    cache, store, _ = make_cache()
    await store.set("lock:k", b"other", ttl=30)

    async def release_soon() -> None:
        await asyncio.sleep(0.02)
        await store.delete("lock:k")

    releaser = asyncio.ensure_future(release_soon())
    assert await cache.get_or_set("k", lambda: "mine", lock=True) == "mine"
    await releaser
    assert not await store.exists("lock:k")


async def test_lock_true_computes_itself_after_lock_wait() -> None:
    cache, store, _ = make_cache(lock_wait=0.03)
    await store.set("lock:k", b"stuck-holder", ttl=30)
    assert await cache.get_or_set("k", lambda: "fallback", lock=True) == "fallback"
    assert await store.get("lock:k") == b"stuck-holder"


async def test_lock_true_with_zero_wait_computes_immediately() -> None:
    cache, store, _ = make_cache(lock_wait=0)
    await store.set("lock:k", b"held", ttl=30)
    assert await cache.get_or_set("k", lambda: "now", lock=True) == "now"


# ---------------------------------------------------------------------------
# Stale-while-revalidate
# ---------------------------------------------------------------------------


async def test_stale_entry_is_served_while_one_refresh_runs() -> None:
    cache, _, clock = make_cache()
    calls = 0

    async def factory() -> int:
        nonlocal calls
        calls += 1
        return calls

    assert await cache.get_or_set("k", factory, ttl=10, stale_ttl=60) == 1
    clock.advance(15)
    stale_reads = [await cache.get_or_set("k", factory, ttl=10, stale_ttl=60) for _ in range(3)]
    assert stale_reads == [1, 1, 1]
    await asyncio.gather(*cache._tasks)
    assert calls == 2
    assert await cache.get_or_set("k", factory, ttl=10, stale_ttl=60) == 2


async def test_plain_get_treats_a_stale_entry_as_a_miss() -> None:
    cache, _, clock = make_cache()
    await cache.get_or_set("k", lambda: 1, ttl=10, stale_ttl=60)
    clock.advance(11)
    assert await cache.get("k") is None


async def test_stale_entries_disappear_after_ttl_plus_stale_ttl() -> None:
    cache, _, clock = make_cache()
    await cache.get_or_set("k", lambda: 1, ttl=10, stale_ttl=5)
    clock.advance(16)
    assert await cache.get_or_set("k", lambda: 2, ttl=10, stale_ttl=5) == 2


async def test_refresh_errors_are_logged_not_raised(caplog: pytest.LogCaptureFixture) -> None:
    cache, _, clock = make_cache()
    await cache.get_or_set("k", lambda: 1, ttl=10, stale_ttl=60)
    clock.advance(11)

    def broken() -> int:
        raise RuntimeError("refresh failed")

    with caplog.at_level(logging.ERROR, logger="lauren"):
        assert await cache.get_or_set("k", broken, ttl=10, stale_ttl=60) == 1
        await asyncio.gather(*cache._tasks)
    assert any("background refresh of cache key 'k' failed" in r.getMessage() for r in caplog.records)


async def test_another_process_refreshing_skips_the_local_refresh() -> None:
    cache, store, clock = make_cache()
    await cache.get_or_set("k", lambda: 1, ttl=10, stale_ttl=60)
    clock.advance(11)
    await store.set("refresh:k", b"other", ttl=30)
    calls = 0

    def factory() -> int:
        nonlocal calls
        calls += 1
        return 2

    assert await cache.get_or_set("k", factory, ttl=10, stale_ttl=60) == 1
    await asyncio.gather(*cache._tasks)
    assert calls == 0


async def test_aclose_cancels_running_refreshes() -> None:
    cache, _, clock = make_cache()
    await cache.get_or_set("k", lambda: 1, ttl=10, stale_ttl=60)
    clock.advance(11)
    never = asyncio.Event()

    async def hangs() -> int:
        await never.wait()
        return 2  # pragma: no cover

    await cache.get_or_set("k", hangs, ttl=10, stale_ttl=60)
    await asyncio.sleep(0)
    tasks = list(cache._tasks)
    await cache.aclose()
    assert all(t.cancelled() for t in tasks)
    # After close no new refresh is scheduled.
    await cache.get_or_set("k", hangs, ttl=10, stale_ttl=60)
    assert not cache._tasks


async def test_stale_ttl_requires_a_finite_ttl() -> None:
    cache, _, _ = make_cache()
    with pytest.raises(ValueError, match="stale_ttl needs a finite ttl"):
        await cache.get_or_set("k", lambda: 1, ttl=None, stale_ttl=5)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


async def test_invalidate_tags_turns_tagged_entries_into_misses() -> None:
    cache, _, _ = make_cache()
    await cache.set("a", 1, tags=("catalog", "product:1"))
    await cache.set("b", 2, tags=("catalog",))
    await cache.set("c", 3)
    await cache.invalidate_tags("catalog")
    assert (await cache.get("a"), await cache.get("b"), await cache.get("c")) == (None, None, 3)


async def test_invalidating_one_tag_leaves_other_tags_alone() -> None:
    cache, _, _ = make_cache()
    await cache.set("p1", 1, tags=("product:1",))
    await cache.set("p2", 2, tags=("product:2",))
    await cache.invalidate_tags("product:1")
    assert await cache.get("p1") is None
    assert await cache.get("p2") == 2


async def test_entries_written_after_an_invalidation_hit_again() -> None:
    cache, _, _ = make_cache()
    await cache.set("a", 1, tags="catalog")
    await cache.invalidate_tags("catalog")
    await cache.set("a", 2, tags="catalog")
    assert await cache.get("a") == 2


async def test_an_invalidation_during_computation_leaves_the_new_entry_stale() -> None:
    cache, _, _ = make_cache()

    async def factory() -> str:
        await cache.invalidate_tags("catalog")  # a write lands while computing
        return "computed-from-old-data"

    assert await cache.get_or_set("k", factory, tags=("catalog",)) == "computed-from-old-data"
    assert await cache.get("k") is None


async def test_an_evicted_tag_counter_never_resurrects_old_entries() -> None:
    cache, store, _ = make_cache()
    await cache.set("a", 1, tags=("catalog",))
    await store.delete("tag:catalog")  # evicted by LRU, expired, flushed …
    assert await cache.get("a") is None


async def test_tags_must_be_non_empty_strings() -> None:
    cache, _, _ = make_cache()
    with pytest.raises(ValueError, match="cache tags"):
        await cache.set("k", 1, tags=("",))
    with pytest.raises(ValueError, match="cache tags"):
        await cache.invalidate_tags(3)  # type: ignore[arg-type]


async def test_a_cached_none_is_a_hit_for_an_optional_model() -> None:
    cache, _, _ = make_cache()
    calls = 0

    def factory() -> None:
        nonlocal calls
        calls += 1

    assert await cache.get_or_set("k", factory, type=Product | None) is None
    assert await cache.get_or_set("k", factory, type=Product | None) is None
    assert calls == 1


async def test_duplicate_tags_are_recorded_once() -> None:
    cache, store, _ = make_cache()
    await cache.set("k", 1, tags=("a", "a", "b"))
    await cache.invalidate_tags("b", "b")
    assert await cache.get("k") is None
    assert await store.get("tag:b") is not None


def test_default_ttl_sentinel_repr() -> None:
    from lauren._cache._service import DEFAULT

    assert repr(DEFAULT) == "DEFAULT"
