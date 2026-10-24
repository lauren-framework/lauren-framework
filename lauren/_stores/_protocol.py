"""The :class:`Store` protocol and the argument checks every backend shares.

A store is an async key-value map with per-key TTLs. Values are ``bytes``,
keys are ``str``. Besides plain reads and writes the protocol carries the
three atomic primitives the framework's stateful features are built on:

* :meth:`Store.incr` — counters, quotas, tag generations.
* :meth:`Store.set_if_absent` — locks, idempotency keys, single-flight.
* :meth:`Store.delete_if_equal` — safe lock release (compare-and-delete).

There is deliberately no ``keys()`` / ``scan()`` / ``clear()``: bulk
invalidation uses generation counters (``incr``), which every backend can
implement without walking its keyspace.
"""

from __future__ import annotations

import math
from typing import Protocol, runtime_checkable


@runtime_checkable
class Store(Protocol):
    """Async key-value store with per-key TTLs (values are bytes, keys are str).

    ``ttl`` is seconds as a ``float``; ``None`` means "no expiry" and a
    value ``<= 0`` raises :class:`ValueError`. Every backend must pass the
    shared contract suite in :mod:`lauren.testing.store_contract`.

    A backend may also define ``async def startup(self)`` and
    ``async def aclose(self)``; :class:`~lauren.stores.StoresModule` and
    ``SessionConfig(store=...)`` run them with the app's lifecycle.
    """

    async def get(self, key: str) -> bytes | None:
        """Return the value, or ``None`` when the key is absent or expired."""
        ...

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store ``value`` under ``key``, replacing any previous value and TTL."""
        ...

    async def delete(self, *keys: str) -> int:
        """Remove the keys; return how many existed."""
        ...

    async def exists(self, key: str) -> bool:
        """Return ``True`` when the key holds an unexpired value."""
        ...

    async def expire(self, key: str, ttl: float) -> bool:
        """Set a new TTL on an existing key; ``False`` when the key is absent."""
        ...

    async def ttl(self, key: str) -> float | None:
        """Return the remaining seconds, or ``None`` when absent or without expiry."""
        ...

    async def incr(self, key: str, amount: int = 1, *, ttl: float | None = None) -> int:
        """Atomically add ``amount`` (creating the key at ``0``); ``ttl`` applies only on creation."""
        ...

    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool:
        """Atomically store ``value`` only when the key is absent (``SET NX``)."""
        ...

    async def delete_if_equal(self, key: str, expected: bytes) -> bool:
        """Atomically delete the key only when it holds ``expected`` (compare-and-delete)."""
        ...

    def namespace(self, prefix: str) -> Store:
        """Return a view whose keys are prefixed with ``"<prefix>:"``; views nest."""
        ...


def check_ttl(ttl: float | None, *, allow_none: bool = True) -> float | None:
    """Validate a TTL argument and return it as a ``float`` (or ``None``)."""
    if ttl is None:
        if allow_none:
            return None
        raise TypeError("ttl must be a positive number of seconds, not None")
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)):
        raise TypeError(f"ttl must be a positive number of seconds; got {ttl!r}")
    if not math.isfinite(ttl) or ttl <= 0:
        raise ValueError(f"ttl must be a finite number of seconds greater than 0; got {ttl!r}")
    return float(ttl)


def check_key(key: str) -> str:
    """Reject non-string keys early (a common source of silent cache misses)."""
    if not isinstance(key, str):
        raise TypeError(f"store keys must be str; got {type(key).__name__}")
    return key


def check_value(value: bytes) -> bytes:
    """Return ``value`` as ``bytes``; ``bytearray`` / ``memoryview`` are copied."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    raise TypeError(f"store values must be bytes; got {type(value).__name__}")


def check_prefix(prefix: str) -> str:
    """Validate a namespace prefix."""
    if not isinstance(prefix, str) or not prefix:
        raise ValueError(f"a store namespace must be a non-empty string; got {prefix!r}")
    return prefix


__all__ = ["Store", "check_key", "check_prefix", "check_ttl", "check_value"]
