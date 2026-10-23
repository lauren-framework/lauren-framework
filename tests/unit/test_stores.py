"""Unit tests for the key-value store layer (``lauren.stores``) — no ASGI app."""

from __future__ import annotations

import asyncio
import threading
from typing import Any

import pytest

from lauren import DecoratorUsageError
from lauren._di.custom import Token
from lauren._stores import TTLMap, lifecycle_root
from lauren.stores import (
    Lock,
    MemoryStore,
    PrefixedStore,
    Store,
    StoreRegistry,
    StoreRevocationStore,
    StoresModule,
    StoreSessionStore,
    StoreToken,
)
from lauren.testing.store_contract import STORE_CONTRACT_CHECKS, run_store_contract


class ManualClock:
    """A clock the test moves by hand (no wall-clock dependence)."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# The shared contract — MemoryStore and namespaced views
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("check", STORE_CONTRACT_CHECKS)
async def test_memory_store_passes_the_contract(check: str) -> None:
    clock = ManualClock()
    ran = await run_store_contract(
        lambda: MemoryStore(max_entries=10_000, clock=clock), advance=clock.advance, checks=[check]
    )
    assert ran == [check]


@pytest.mark.parametrize("check", STORE_CONTRACT_CHECKS)
async def test_namespaced_view_passes_the_contract(check: str) -> None:
    clock = ManualClock()
    await run_store_contract(
        lambda: MemoryStore(max_entries=10_000, clock=clock).namespace("tenant").namespace("a"),
        advance=clock.advance,
        checks=[check],
    )


async def test_contract_accepts_an_async_advance() -> None:
    clock = ManualClock()

    async def advance(seconds: float) -> None:
        clock.advance(seconds)

    ran = await run_store_contract(
        lambda: MemoryStore(max_entries=100, clock=clock), advance=advance, checks=["expiry"]
    )
    assert ran == ["expiry"]


async def test_contract_runs_every_check_by_default_and_skips_expiry_without_advance() -> None:
    ran = await run_store_contract(lambda: MemoryStore(max_entries=10_000), concurrency=50)
    assert ran == list(STORE_CONTRACT_CHECKS)


async def test_contract_rejects_unknown_check_names() -> None:
    with pytest.raises(ValueError, match="unknown store contract checks"):
        await run_store_contract(lambda: MemoryStore(max_entries=10), checks=["nope"])


class _TtlIgnoringStore(MemoryStore):
    """A broken backend: ``incr`` re-applies the TTL to existing keys."""

    async def incr(self, key: str, amount: int = 1, *, ttl: float | None = None) -> int:
        value = await super().incr(key, amount)
        if ttl is not None:
            await self.expire(key, ttl)
        return value


async def test_contract_reports_a_violating_backend() -> None:
    with pytest.raises(AssertionError, match="does not change the TTL of an existing key"):
        await run_store_contract(lambda: _TtlIgnoringStore(max_entries=100), checks=["incr"])


class _NonRaisingStore(MemoryStore):
    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        await super().set(key, value, ttl=None if ttl is not None and ttl <= 0 else ttl)


async def test_contract_reports_a_missing_ttl_validation() -> None:
    with pytest.raises(AssertionError, match=r"set\(ttl=0\) must raise ValueError"):
        await run_store_contract(lambda: _NonRaisingStore(max_entries=100), checks=["ttl_validation"])


# ---------------------------------------------------------------------------
# MemoryStore specifics
# ---------------------------------------------------------------------------


def test_memory_store_satisfies_the_protocol() -> None:
    assert isinstance(MemoryStore(max_entries=1), Store)
    assert isinstance(MemoryStore(max_entries=1).namespace("x"), Store)


@pytest.mark.parametrize("bad", [0, -1, 1.5, True, "10"])
def test_memory_store_requires_a_positive_integer_capacity(bad: Any) -> None:
    with pytest.raises(ValueError, match="max_entries"):
        MemoryStore(max_entries=bad)


@pytest.mark.parametrize("bad", [0, -1, True, "60"])
def test_memory_store_rejects_a_non_positive_sweep_interval(bad: Any) -> None:
    with pytest.raises(ValueError, match="sweep_interval"):
        MemoryStore(max_entries=10, sweep_interval=bad)


async def test_lru_eviction_drops_the_least_recently_used_key() -> None:
    store = MemoryStore(max_entries=3)
    for key in ("a", "b", "c"):
        await store.set(key, b"1")
    await store.get("a")  # "b" is now the least recently used
    await store.set("d", b"1")
    assert await store.get("b") is None
    assert [await store.get(k) for k in ("a", "c", "d")] == [b"1", b"1", b"1"]
    assert len(store) == 3


async def test_memory_stays_bounded_under_ten_times_capacity_unique_keys() -> None:
    store = MemoryStore(max_entries=50)
    for i in range(500):
        await store.set(f"k{i}", b"v")
    assert len(store) == 50
    assert store.max_entries == 50


async def test_proactive_sweep_removes_expired_keys_that_are_never_read() -> None:
    clock = ManualClock()
    store = MemoryStore(max_entries=100, sweep_interval=10, clock=clock)
    for i in range(20):
        await store.set(f"tmp{i}", b"v", ttl=1)
    await store.set("forever", b"v")
    clock.advance(11)
    await store.set("trigger", b"v")  # any write runs the due sweep
    assert len(store) == 2


async def test_expired_entries_are_dropped_first_when_full_and_a_sweep_is_due() -> None:
    clock = ManualClock()
    store = MemoryStore(max_entries=2, sweep_interval=5, clock=clock)
    await store.set("live", b"v")
    await store.set("old", b"v", ttl=1)
    clock.advance(6)
    await store.get("live")
    await store.set("new", b"v")
    assert await store.get("live") == b"v"
    assert await store.get("new") == b"v"


async def test_manual_sweep_reports_the_number_removed() -> None:
    clock = ManualClock()
    store = MemoryStore(max_entries=10, clock=clock)
    await store.set("a", b"1", ttl=1)
    await store.set("b", b"1")
    clock.advance(2)
    assert store.sweep() == 1
    assert len(store) == 1


@pytest.mark.parametrize(
    ("call", "error"),
    [
        (lambda s: s.get(1), TypeError),
        (lambda s: s.set("k", "text"), TypeError),
        (lambda s: s.set("k", b"v", ttl="5"), TypeError),
        (lambda s: s.set("k", b"v", ttl=float("inf")), ValueError),
        (lambda s: s.incr("n", 1.5), TypeError),
        (lambda s: s.incr("n", True), TypeError),
        (lambda s: s.expire("k", None), TypeError),
    ],
)
async def test_memory_store_rejects_invalid_arguments(call: Any, error: type[Exception]) -> None:
    with pytest.raises(error):
        await call(MemoryStore(max_entries=10))


@pytest.mark.parametrize("raw", [b" 1", b"1_0", b"+1", b"1.5", b"", b"-"])
async def test_incr_only_accepts_plain_decimal_integers(raw: bytes) -> None:
    store = MemoryStore(max_entries=10)
    await store.set("n", raw)
    with pytest.raises(ValueError, match="not an integer"):
        await store.incr("n")


async def test_memory_store_copies_bytearray_and_memoryview_values() -> None:
    store = MemoryStore(max_entries=10)
    buffer = bytearray(b"abc")
    await store.set("a", buffer)
    await store.set("b", memoryview(b"xyz"))
    buffer[0] = ord("z")
    assert (await store.get("a"), await store.get("b")) == (b"abc", b"xyz")


async def test_delete_without_keys_returns_zero() -> None:
    store = MemoryStore(max_entries=10)
    assert await store.delete() == 0
    assert await store.namespace("x").delete() == 0


def test_memory_store_works_across_sequential_event_loops() -> None:
    store = MemoryStore(max_entries=10)

    async def write() -> None:
        await store.set("k", b"v")
        await store.incr("n")

    async def read() -> tuple[bytes | None, int]:
        return await store.get("k"), await store.incr("n")

    asyncio.run(write())
    assert asyncio.run(read()) == (b"v", 2)


def test_memory_store_is_safe_to_share_between_threads() -> None:
    store = MemoryStore(max_entries=10)
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        asyncio.run(_bump(store, 250))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert asyncio.run(store.get("hits")) == b"1000"


async def _bump(store: MemoryStore, times: int) -> None:
    for _ in range(times):
        await store.incr("hits")


def test_memory_store_repr_mentions_capacity() -> None:
    assert repr(MemoryStore(max_entries=7)) == "MemoryStore(max_entries=7, entries=0)"


def test_ttl_map_put_reports_live_evictions_only() -> None:
    entries: TTLMap[int] = TTLMap(max_entries=1, clock=lambda: 0.0)
    assert entries.put("a", 1, None, 0.0) == 0
    assert entries.put("b", 2, None, 0.0) == 1
    assert entries.put("c", 3, 5.0, 10.0) == 1  # "b" had no expiry: a live eviction
    assert entries.put("d", 4, None, 10.0) == 0  # "c" had already expired


# ---------------------------------------------------------------------------
# PrefixedStore
# ---------------------------------------------------------------------------


def test_namespace_view_exposes_its_prefix_and_base() -> None:
    base = MemoryStore(max_entries=10)
    view = base.namespace("a").namespace("b")
    assert isinstance(view, PrefixedStore)
    assert view.prefix == "a:b"
    assert view.base is base
    assert repr(view).endswith(", 'a:b')")


@pytest.mark.parametrize("bad", ["", None, 3])
def test_namespace_rejects_an_empty_or_non_string_prefix(bad: Any) -> None:
    with pytest.raises(ValueError, match="namespace"):
        MemoryStore(max_entries=10).namespace(bad)


def test_lifecycle_root_follows_views_and_adapters_to_the_backend() -> None:
    base = MemoryStore(max_entries=10)
    assert lifecycle_root(base) is base
    assert lifecycle_root(base.namespace("a").namespace("b")) is base
    assert lifecycle_root(StoreSessionStore(base.namespace("x"))) is base
    assert lifecycle_root(StoreRevocationStore(base)) is base


# ---------------------------------------------------------------------------
# Lock
# ---------------------------------------------------------------------------


async def test_lock_is_exclusive_until_released() -> None:
    store = MemoryStore(max_entries=10)
    first, second = Lock(store, "job", ttl=30), Lock(store, "job", ttl=30)
    assert await first.acquire()
    assert first.locked and first.key == "lock:job" and first.name == "job"
    assert not await second.acquire()
    assert await first.release()
    assert await second.acquire()


async def test_a_stale_holder_cannot_release_a_newer_lock() -> None:
    clock = ManualClock()
    store = MemoryStore(max_entries=10, clock=clock)
    stale, fresh = Lock(store, "job", ttl=5), Lock(store, "job", ttl=5)
    assert await stale.acquire()
    clock.advance(6)  # the stale holder's lease expired
    assert await fresh.acquire()
    assert not await stale.release()
    assert await store.exists("lock:job")


async def test_lock_release_when_not_held_returns_false() -> None:
    assert not await Lock(MemoryStore(max_entries=10), "x", ttl=1).release()


async def test_acquiring_twice_on_one_object_raises() -> None:
    lock = Lock(MemoryStore(max_entries=10), "x", ttl=1)
    await lock.acquire()
    with pytest.raises(RuntimeError, match="already held"):
        await lock.acquire()


async def test_lock_context_manager_releases_on_exit() -> None:
    store = MemoryStore(max_entries=10)
    async with Lock(store, "job", ttl=30) as held:
        assert held.locked
        assert await store.exists("lock:job")
    assert not await store.exists("lock:job")


async def test_lock_context_manager_raises_timeout_when_taken() -> None:
    store = MemoryStore(max_entries=10)
    await store.set("lock:job", b"someone-else")
    with pytest.raises(TimeoutError, match="could not acquire lock 'job'"):
        async with Lock(store, "job", ttl=30):
            pass  # pragma: no cover - never entered


async def test_waiting_lock_acquires_once_the_holder_releases() -> None:
    store = MemoryStore(max_entries=10)
    holder = Lock(store, "job", ttl=30)
    await holder.acquire()
    waiter = Lock(store, "job", ttl=30, wait=10, poll_interval=0.001)
    attempt = asyncio.ensure_future(waiter.acquire())
    await asyncio.sleep(0.01)
    assert not attempt.done()
    await holder.release()
    assert await asyncio.wait_for(attempt, 10)


async def test_waiting_lock_gives_up_after_wait() -> None:
    store = MemoryStore(max_entries=10)
    await store.set("lock:job", b"held")
    lock = Lock(store, "job", ttl=30, poll_interval=0.001)
    assert not await lock.acquire(wait=0.02)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"ttl": 0},
        {"wait": -1},
        {"wait": True},
        {"poll_interval": 0},
    ],
)
def test_lock_validates_its_arguments(kwargs: dict[str, Any]) -> None:
    options: dict[str, Any] = {"name": "job", "ttl": 1, **kwargs}
    name = options.pop("name")
    with pytest.raises(ValueError):
        Lock(MemoryStore(max_entries=1), name, **options)


def test_lock_repr() -> None:
    assert repr(Lock(MemoryStore(max_entries=1), "job", ttl=2)) == "Lock('job', ttl=2.0, locked=False)"


# ---------------------------------------------------------------------------
# StoreToken / StoreRegistry / StoresModule.for_root validation
# ---------------------------------------------------------------------------


def test_store_tokens_with_the_same_name_are_the_same_token() -> None:
    assert StoreToken("local") == StoreToken("local")
    assert hash(StoreToken("local")) == hash(StoreToken("local"))
    assert StoreToken("local") != StoreToken("remote")
    assert isinstance(StoreToken("local"), Token)
    assert StoreToken("local").store_name == "local"
    assert repr(StoreToken("local")) == "StoreToken('local')"


def test_store_token_requires_a_name() -> None:
    with pytest.raises(ValueError):
        StoreToken("")


def test_store_registry_looks_stores_up_by_name() -> None:
    default, local = MemoryStore(max_entries=1), MemoryStore(max_entries=1)
    registry = StoreRegistry({"default": default, "local": local})
    assert registry.get() is default
    assert registry.get("local") is local
    assert "local" in registry and "nope" not in registry
    assert sorted(registry) == ["default", "local"] and len(registry) == 2
    assert registry.names() == ("default", "local")
    assert repr(registry) == "StoreRegistry(['default', 'local'])"


def test_store_registry_unknown_name_lists_the_registered_ones() -> None:
    with pytest.raises(LookupError, match=r"no store named 'x' .*registered: default"):
        StoreRegistry({"default": MemoryStore(max_entries=1)}).get("x")


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"default": 42}, "invalid_store"),
        ({"named": {"": MemoryStore(max_entries=1)}}, "invalid_store"),
        ({"named": {"default": MemoryStore(max_entries=1)}}, "duplicate_store_name"),
        ({"named": {"x": None}}, "invalid_store"),
        ({"named": {"x": "not-a-store"}}, "invalid_store"),
    ],
)
def test_stores_module_for_root_validates_immediately(kwargs: dict[str, Any], reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        StoresModule.for_root(**kwargs)
    assert info.value.detail["reason"] == reason


async def test_store_revocation_adapter_skips_already_expired_user_cutoffs() -> None:
    store = MemoryStore(max_entries=10)
    await StoreRevocationStore(store).revoke_user("u", cutoff=1.0, ttl=-1)
    assert len(store) == 0
