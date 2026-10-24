"""The shared :class:`~lauren.stores.Store` contract suite.

Every backend — core's ``MemoryStore`` and namespaced views, and any
companion store (Redis, a database, …) — proves compliance by running the
same checks::

    import pytest
    from lauren.stores import MemoryStore
    from lauren.testing.store_contract import STORE_CONTRACT_CHECKS, run_store_contract


    class ManualClock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += seconds


    @pytest.mark.parametrize("check", STORE_CONTRACT_CHECKS)
    async def test_memory_store_contract(check: str) -> None:
        clock = ManualClock()
        await run_store_contract(
            lambda: MemoryStore(max_entries=10_000, clock=clock),
            advance=clock.advance,
            checks=[check],
        )

``advance(seconds)`` moves the store's clock forward (it may be async — a
networked backend can pass ``asyncio.sleep`` together with short TTLs).
Without it the expiry checks are skipped. ``make_store`` must return a
fresh, empty store (or a view of one) on every call.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from ..stores import Store

MakeStore = Callable[[], Store]
Advance = Callable[[float], Any]
_Check = Callable[[MakeStore, "Advance | None", int], Awaitable[None]]


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"Store contract violated: {message}")


async def _raises(exc_type: type[BaseException], call: Callable[[], Awaitable[Any]], what: str) -> None:
    try:
        await call()
    except exc_type:
        return
    raise AssertionError(f"Store contract violated: {what} must raise {exc_type.__name__}")


async def _advance(advance: Advance, seconds: float) -> None:
    result = advance(seconds)
    if inspect.isawaitable(result):
        await result


async def _round_trip(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    _expect(await store.get("missing") is None, "get() of an absent key returns None")
    await store.set("k", b"v1")
    _expect(await store.get("k") == b"v1", "get() returns what set() stored")
    await store.set("k", b"v2")
    _expect(await store.get("k") == b"v2", "set() replaces the previous value")
    await store.set("empty", b"")
    _expect(await store.get("empty") == b"", "an empty value is stored, not treated as absent")


async def _delete_and_exists(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    await store.set("a", b"1")
    await store.set("b", b"2")
    _expect(await store.exists("a"), "exists() is True for a stored key")
    _expect(not await store.exists("zzz"), "exists() is False for an absent key")
    _expect(await store.delete("a", "b", "zzz") == 2, "delete() returns how many keys existed")
    _expect(await store.get("a") is None and not await store.exists("b"), "delete() removes the keys")
    _expect(await store.delete("a") == 0, "delete() of an absent key returns 0")


async def _ttl_validation(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    for bad in (0, -1):
        await _raises(ValueError, lambda: store.set("k", b"v", ttl=bad), f"set(ttl={bad})")
        await _raises(
            ValueError, lambda: store.set_if_absent("k", b"v", ttl=bad), f"set_if_absent(ttl={bad})"
        )
        await _raises(ValueError, lambda: store.incr("n", ttl=bad), f"incr(ttl={bad})")
    await store.set("k", b"v")
    await _raises(ValueError, lambda: store.expire("k", 0), "expire(ttl=0)")


async def _ttl_semantics(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    _expect(await store.ttl("missing") is None, "ttl() of an absent key is None")
    await store.set("forever", b"v")
    _expect(await store.ttl("forever") is None, "ttl() of a key without expiry is None")
    await store.set("short", b"v", ttl=30)
    remaining = await store.ttl("short")
    _expect(
        remaining is not None and 0 < remaining <= 30,
        f"ttl() returns the remaining seconds (got {remaining!r})",
    )
    await store.set("short", b"v2")
    _expect(await store.ttl("short") is None, "set() without ttl clears a previous expiry")
    _expect(not await store.expire("missing", 10), "expire() of an absent key returns False")
    _expect(await store.expire("forever", 10), "expire() of a present key returns True")
    remaining = await store.ttl("forever")
    _expect(remaining is not None and 0 < remaining <= 10, "expire() sets the TTL")


async def _incr(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    _expect(await store.incr("n") == 1, "incr() creates the key at 0 before adding")
    _expect(await store.incr("n", 5) == 6, "incr() adds the amount")
    _expect(await store.incr("n", -2) == 4, "incr() accepts negative amounts")
    _expect(await store.get("n") == b"4", "incr() stores the decimal ASCII value")
    await store.incr("fresh", ttl=30)
    remaining = await store.ttl("fresh")
    _expect(
        remaining is not None and 0 < remaining <= 30, "incr(ttl=...) applies the TTL when it creates the key"
    )
    await store.incr("plain")
    await store.incr("plain", ttl=30)
    _expect(await store.ttl("plain") is None, "incr(ttl=...) does not change the TTL of an existing key")
    await store.set("text", b"not-a-number")
    await _raises(ValueError, lambda: store.incr("text"), "incr() of a non-integer value")


async def _incr_atomic(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    results = await asyncio.gather(*(store.incr("hits") for _ in range(n)))
    _expect(
        sorted(results) == list(range(1, n + 1)), f"{n} concurrent incr() calls each see a distinct value"
    )
    _expect(await store.get("hits") == str(n).encode("ascii"), f"{n} concurrent incr() calls add up")


async def _set_if_absent(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    _expect(await store.set_if_absent("k", b"first"), "set_if_absent() stores into an absent key")
    _expect(not await store.set_if_absent("k", b"second"), "set_if_absent() refuses a present key")
    _expect(await store.get("k") == b"first", "a refused set_if_absent() leaves the value unchanged")
    results = await asyncio.gather(*(store.set_if_absent("race", str(i).encode()) for i in range(n)))
    _expect(results.count(True) == 1, f"exactly one of {n} concurrent set_if_absent() calls wins")


async def _delete_if_equal(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    await store.set("lock", b"token-a")
    _expect(not await store.delete_if_equal("lock", b"token-b"), "delete_if_equal() refuses another value")
    _expect(await store.get("lock") == b"token-a", "a refused delete_if_equal() keeps the key")
    _expect(await store.delete_if_equal("lock", b"token-a"), "delete_if_equal() deletes a matching value")
    _expect(await store.get("lock") is None, "delete_if_equal() removes the key")
    _expect(
        not await store.delete_if_equal("lock", b"token-a"), "delete_if_equal() of an absent key is False"
    )


async def _namespaces(make: MakeStore, advance: Advance | None, n: int) -> None:
    store = make()
    users = store.namespace("users")
    await users.set("1", b"alice")
    _expect(await store.get("users:1") == b"alice", 'namespace("users") prefixes keys with "users:"')
    _expect(await store.get("1") is None, "a namespaced write does not touch the bare key")
    nested = users.namespace("admins")
    await nested.set("1", b"root")
    _expect(await store.get("users:admins:1") == b"root", "namespaces nest")
    other = store.namespace("orders")
    _expect(await other.get("1") is None, "sibling namespaces are isolated")
    _expect(await users.incr("count") == 1 and await store.get("users:count") == b"1", "incr() is namespaced")
    _expect(await users.set_if_absent("x", b"1"), "set_if_absent() works through a namespace")
    _expect(await users.delete_if_equal("x", b"1"), "delete_if_equal() works through a namespace")
    _expect(await users.delete("1", "missing") == 1, "delete() through a namespace counts its own keys")
    _expect(await store.get("users:admins:1") == b"root", "deleting in a namespace leaves nested keys alone")


async def _expiry(make: MakeStore, advance: Advance | None, n: int) -> None:
    if advance is None:
        return
    store = make()
    await store.set("k", b"v", ttl=1)
    await store.set("keep", b"v", ttl=1)
    await store.expire("keep", 100)
    await store.incr("n", ttl=1)
    await store.set_if_absent("lock", b"a", ttl=1)
    await _advance(advance, 1.5)
    _expect(await store.get("k") is None, "a key is gone once its TTL elapsed")
    _expect(not await store.exists("k") and await store.ttl("k") is None, "an expired key is absent")
    _expect(await store.get("keep") == b"v", "expire() extended the TTL")
    _expect(await store.incr("n", ttl=1) == 1, "incr() of an expired key starts again at 0")
    _expect(await store.set_if_absent("lock", b"b"), "set_if_absent() succeeds once the holder's TTL elapsed")
    _expect(not await store.delete_if_equal("k", b"v"), "delete_if_equal() of an expired key is False")


_CHECKS: dict[str, _Check] = {
    "round_trip": _round_trip,
    "delete_and_exists": _delete_and_exists,
    "ttl_validation": _ttl_validation,
    "ttl_semantics": _ttl_semantics,
    "incr": _incr,
    "incr_atomic": _incr_atomic,
    "set_if_absent": _set_if_absent,
    "delete_if_equal": _delete_if_equal,
    "namespaces": _namespaces,
    "expiry": _expiry,
}

#: Names of every contract check, for ``pytest.mark.parametrize``.
STORE_CONTRACT_CHECKS: tuple[str, ...] = tuple(_CHECKS)


async def run_store_contract(
    make_store: MakeStore,
    *,
    advance: Advance | None = None,
    checks: Iterable[str] | None = None,
    concurrency: int = 1000,
) -> list[str]:
    """Run the contract checks against stores from ``make_store``; return the names run.

    Raises :class:`AssertionError` describing the first violation.
    ``concurrency`` is the number of concurrent tasks the atomicity checks
    start; ``checks`` selects a subset of :data:`STORE_CONTRACT_CHECKS`.
    """
    names = list(STORE_CONTRACT_CHECKS if checks is None else checks)
    unknown = [name for name in names if name not in _CHECKS]
    if unknown:
        raise ValueError(f"unknown store contract checks: {unknown}; known: {list(STORE_CONTRACT_CHECKS)}")
    for name in names:
        await _CHECKS[name](make_store, advance, concurrency)
    return names


__all__ = ["STORE_CONTRACT_CHECKS", "run_store_contract"]
