"""Hypothesis properties of ``MemoryStore`` and response-cache keys.

Skipped automatically when hypothesis is not installed.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren import Headers  # noqa: E402
from lauren._cache._response import response_cache_key  # noqa: E402
from lauren.stores import MemoryStore  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=150)

_keys = st.sampled_from(["a", "b", "c", "d", "e"])
_values = st.binary(max_size=4)
_ttls = st.none() | st.integers(min_value=1, max_value=5)
_ops = st.one_of(
    st.tuples(st.just("set"), _keys, _values, _ttls),
    st.tuples(st.just("get"), _keys),
    st.tuples(st.just("delete"), _keys),
    st.tuples(st.just("incr"), _keys, st.integers(min_value=-3, max_value=3), _ttls),
    st.tuples(st.just("set_if_absent"), _keys, _values, _ttls),
    st.tuples(st.just("delete_if_equal"), _keys, _values),
    st.tuples(st.just("expire"), _keys, st.integers(min_value=1, max_value=5)),
    st.tuples(st.just("advance"), st.integers(min_value=0, max_value=4)),
)


class Model:
    """Reference semantics: a dict of ``key -> (value, expires_at)`` and a virtual clock."""

    def __init__(self) -> None:
        self.now = 0.0
        self.data: dict[str, tuple[bytes, float | None]] = {}

    def live(self, key: str) -> tuple[bytes, float | None] | None:
        entry = self.data.get(key)
        if entry is None or (entry[1] is not None and self.now >= entry[1]):
            self.data.pop(key, None)
            return None
        return entry

    def apply(self, op: tuple[Any, ...]) -> Any:
        name = op[0]
        if name == "advance":
            self.now += op[1]
            return None
        key = op[1]
        entry = self.live(key)
        if name == "get":
            return None if entry is None else entry[0]
        if name == "set":
            self.data[key] = (op[2], None if op[3] is None else self.now + op[3])
            return None
        if name == "delete":
            self.data.pop(key, None)
            return int(entry is not None)
        if name == "incr":
            if entry is not None and not re.fullmatch(rb"-?[0-9]+", entry[0]):
                return "ValueError"
            current = 0 if entry is None else int(entry[0])
            expires = (None if op[3] is None else self.now + op[3]) if entry is None else entry[1]
            self.data[key] = (str(current + op[2]).encode(), expires)
            return current + op[2]
        if name == "set_if_absent":
            if entry is not None:
                return False
            self.data[key] = (op[2], None if op[3] is None else self.now + op[3])
            return True
        if name == "delete_if_equal":
            if entry is None or entry[0] != op[2]:
                return False
            del self.data[key]
            return True
        if name == "expire":
            if entry is None:
                return False
            self.data[key] = (entry[0], self.now + op[2])
            return True
        raise AssertionError(name)  # pragma: no cover


async def _apply(store: MemoryStore, op: tuple[Any, ...]) -> Any:
    name = op[0]
    if name == "get":
        return await store.get(op[1])
    if name == "set":
        return await store.set(op[1], op[2], ttl=op[3])
    if name == "delete":
        return await store.delete(op[1])
    if name == "incr":
        try:
            return await store.incr(op[1], op[2], ttl=op[3])
        except ValueError:
            return "ValueError"
    if name == "set_if_absent":
        return await store.set_if_absent(op[1], op[2], ttl=op[3])
    if name == "delete_if_equal":
        return await store.delete_if_equal(op[1], op[2])
    if name == "expire":
        return await store.expire(op[1], op[2])
    raise AssertionError(name)  # pragma: no cover


@_TOLERANT
@given(st.lists(_ops, max_size=40))
def test_memory_store_agrees_with_the_reference_model(ops: list[tuple[Any, ...]]) -> None:
    model = Model()
    store = MemoryStore(max_entries=1_000, sweep_interval=3, clock=lambda: model.now)

    async def run() -> None:
        for op in ops:
            if op[0] == "advance":
                model.apply(op)
                continue
            expected = model.apply(op)
            assert await _apply(store, op) == expected, op
        for key in ("a", "b", "c", "d", "e"):
            entry = model.live(key)
            assert await store.get(key) == (None if entry is None else entry[0])

    asyncio.run(run())


@_TOLERANT
@given(st.integers(min_value=1, max_value=30), st.lists(st.text(min_size=1, max_size=6), max_size=300))
def test_memory_store_never_exceeds_max_entries(capacity: int, keys: list[str]) -> None:
    store = MemoryStore(max_entries=capacity)

    async def run() -> None:
        for key in keys:
            await store.set(key, b"v")
            assert len(store) <= capacity

    asyncio.run(run())


_params = st.dictionaries(
    st.text(alphabet="abcdefgh", min_size=1, max_size=3),
    st.lists(st.text(alphabet="xyz012", max_size=3), min_size=1, max_size=3),
    max_size=5,
)


def _key(query: dict[str, list[str]], language: str = "en") -> str:
    return response_cache_key(
        method="GET",
        route_template="/items",
        path_params={},
        query_params=query,
        headers=Headers([("accept-language", language)]),
        vary=("accept-language",),
    )


@_TOLERANT
@given(_params, st.randoms(use_true_random=False))
def test_response_keys_ignore_query_parameter_order(query: dict[str, list[str]], rnd: Any) -> None:
    names = list(query)
    rnd.shuffle(names)
    assert _key({name: query[name] for name in names}) == _key(query)


@_TOLERANT
@given(
    _params, st.text(alphabet="abcdefghij-", min_size=1, max_size=8), st.text(alphabet="klmnop", min_size=1)
)
def test_response_keys_differ_when_a_vary_header_differs(query: dict[str, list[str]], a: str, b: str) -> None:
    assert _key(query, a) != _key(query, b)
