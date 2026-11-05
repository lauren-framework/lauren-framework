"""Bounded in-process storage: the ``TTLMap`` engine and :class:`MemoryStore`.

Every operation is a short synchronous critical section guarded by a
``threading.Lock`` and never awaits while holding it. That makes the store
loop-agnostic (an ``asyncio.Lock`` binds to the first event loop that waits
on it) and safe to share between the event loop and worker threads.

Memory is bounded twice over: ``max_entries`` caps the map (least recently
used entries are evicted first), and expired entries are swept proactively —
on read, and in one pass at most every ``sweep_interval`` seconds of the
store's clock — so entries that are written once and never read again do
not accumulate.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict
from typing import Callable, Generic, TypeVar

from ._prefixed import PrefixedStore
from ._protocol import check_key, check_ttl, check_value

V = TypeVar("V")

#: What ``incr`` accepts as a stored integer (the Redis ``INCR`` rule: no
#: whitespace, sign other than ``-``, or digit separators).
_INTEGER = re.compile(rb"-?[0-9]+")

#: Capacity used when the framework creates a store implicitly
#: (``StoresModule.for_root()``, ``CacheModule`` without a store, the
#: in-memory session and revocation stores).
DEFAULT_MAX_ENTRIES = 100_000


def _check_capacity(max_entries: int, sweep_interval: float) -> None:
    if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries <= 0:
        raise ValueError(f"max_entries must be a positive integer; got {max_entries!r}")
    if (
        isinstance(sweep_interval, bool)
        or not isinstance(sweep_interval, (int, float))
        or not sweep_interval > 0
    ):
        raise ValueError(f"sweep_interval must be a positive number of seconds; got {sweep_interval!r}")


class TTLMap(Generic[V]):
    """An LRU-bounded ordered map with per-entry expiry.

    Not thread-safe on its own: callers hold their lock around every call.
    ``now`` is passed in so one clock reading serves a whole operation.
    """

    __slots__ = ("_data", "_max_entries", "_sweep_interval", "_next_sweep", "clock")

    def __init__(
        self,
        *,
        max_entries: int,
        sweep_interval: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        _check_capacity(max_entries, sweep_interval)
        self.clock: Callable[[], float] = clock if clock is not None else time.monotonic
        self._data: OrderedDict[str, tuple[V, float | None]] = OrderedDict()
        self._max_entries = max_entries
        self._sweep_interval = float(sweep_interval)
        self._next_sweep = self.clock() + self._sweep_interval

    @property
    def max_entries(self) -> int:
        return self._max_entries

    def __len__(self) -> int:
        return len(self._data)

    def get(self, key: str, now: float) -> tuple[V, float | None] | None:
        """Return ``(value, expires_at)`` or ``None``; an expired entry is dropped."""
        entry = self._data.get(key)
        if entry is None:
            return None
        expires_at = entry[1]
        if expires_at is not None and now >= expires_at:
            del self._data[key]
            return None
        return entry

    def touch(self, key: str) -> None:
        """Mark ``key`` as most recently used."""
        self._data.move_to_end(key)

    def put(self, key: str, value: V, expires_at: float | None, now: float) -> int:
        """Insert or replace ``key``, then enforce ``max_entries``.

        Returns how many *live* entries were evicted to make room.
        """
        data = self._data
        if key in data:
            data.move_to_end(key)
        data[key] = (value, expires_at)
        evicted = 0
        if len(data) > self._max_entries:
            # Prefer dropping expired entries over live ones when a sweep is due.
            self.maybe_sweep(now)
            while len(data) > self._max_entries:
                _, (_, exp) = data.popitem(last=False)
                if exp is None or now < exp:
                    evicted += 1
        return evicted

    def set_expiry(self, key: str, expires_at: float | None) -> None:
        """Replace the expiry of an existing (unexpired) entry."""
        value = self._data[key][0]
        self._data[key] = (value, expires_at)

    def remove(self, key: str) -> None:
        del self._data[key]

    def maybe_sweep(self, now: float) -> int:
        """Sweep expired entries when ``sweep_interval`` has elapsed."""
        if now < self._next_sweep:
            return 0
        return self.sweep(now)

    def sweep(self, now: float) -> int:
        """Remove every expired entry; return how many were removed."""
        expired = [key for key, (_, exp) in self._data.items() if exp is not None and now >= exp]
        for key in expired:
            del self._data[key]
        self._next_sweep = now + self._sweep_interval
        return len(expired)


class MemoryStore:
    """Bounded, process-local :class:`~lauren.stores.Store`.

    ``max_entries`` is required (``ValueError`` when not a positive int): the
    least recently used entries are evicted beyond it, so memory stays
    bounded whatever the traffic. ``sweep_interval`` (seconds) bounds how
    long expired-but-unread entries linger. ``clock`` is a zero-argument
    callable returning seconds (``time.monotonic`` by default) — inject a
    fake one to test TTLs without sleeping.

    Fine for development, tests and single-worker deployments; share state
    across workers with a networked backend (e.g. the ``lauren-redis``
    companion) that implements the same protocol.
    """

    __slots__ = ("_map", "_lock", "__weakref__")

    def __init__(
        self,
        *,
        max_entries: int,
        sweep_interval: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._map: TTLMap[bytes] = TTLMap(max_entries=max_entries, sweep_interval=sweep_interval, clock=clock)
        self._lock = threading.Lock()

    @property
    def max_entries(self) -> int:
        """The LRU capacity."""
        return self._map.max_entries

    def __len__(self) -> int:
        """Entries currently held, including expired ones not yet swept."""
        return len(self._map)

    def __repr__(self) -> str:
        return f"MemoryStore(max_entries={self._map.max_entries}, entries={len(self._map)})"

    def _expiry(self, now: float, ttl: float | None) -> float | None:
        return None if ttl is None else now + ttl

    def sweep(self) -> int:
        """Remove every expired entry now; return how many were removed."""
        with self._lock:
            return self._map.sweep(self._map.clock())

    async def get(self, key: str) -> bytes | None:
        check_key(key)
        with self._lock:
            entry = self._map.get(key, self._map.clock())
            if entry is None:
                return None
            self._map.touch(key)
            return entry[0]

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        check_key(key)
        data = check_value(value)
        seconds = check_ttl(ttl)
        with self._lock:
            now = self._map.clock()
            self._map.maybe_sweep(now)
            self._map.put(key, data, self._expiry(now, seconds), now)

    async def delete(self, *keys: str) -> int:
        removed = 0
        with self._lock:
            now = self._map.clock()
            for key in keys:
                if self._map.get(check_key(key), now) is not None:
                    self._map.remove(key)
                    removed += 1
            self._map.maybe_sweep(now)
        return removed

    async def exists(self, key: str) -> bool:
        check_key(key)
        with self._lock:
            return self._map.get(key, self._map.clock()) is not None

    async def expire(self, key: str, ttl: float) -> bool:
        check_key(key)
        seconds = check_ttl(ttl, allow_none=False)
        with self._lock:
            now = self._map.clock()
            if self._map.get(key, now) is None:
                return False
            self._map.set_expiry(key, self._expiry(now, seconds))
            return True

    async def ttl(self, key: str) -> float | None:
        check_key(key)
        with self._lock:
            now = self._map.clock()
            entry = self._map.get(key, now)
            if entry is None or entry[1] is None:
                return None
            return entry[1] - now

    async def incr(self, key: str, amount: int = 1, *, ttl: float | None = None) -> int:
        check_key(key)
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise TypeError(f"incr amount must be an int; got {amount!r}")
        seconds = check_ttl(ttl)
        with self._lock:
            now = self._map.clock()
            self._map.maybe_sweep(now)
            entry = self._map.get(key, now)
            if entry is None:
                value, expires_at = amount, self._expiry(now, seconds)
            else:
                if not _INTEGER.fullmatch(entry[0]):
                    raise ValueError(f"the value at {key!r} is not an integer")
                current = int(entry[0])
                value, expires_at = current + amount, entry[1]
            self._map.put(key, str(value).encode("ascii"), expires_at, now)
            return value

    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool:
        check_key(key)
        data = check_value(value)
        seconds = check_ttl(ttl)
        with self._lock:
            now = self._map.clock()
            self._map.maybe_sweep(now)
            if self._map.get(key, now) is not None:
                return False
            self._map.put(key, data, self._expiry(now, seconds), now)
            return True

    async def delete_if_equal(self, key: str, expected: bytes) -> bool:
        check_key(key)
        wanted = check_value(expected)
        with self._lock:
            entry = self._map.get(key, self._map.clock())
            if entry is None or entry[0] != wanted:
                return False
            self._map.remove(key)
            return True

    def namespace(self, prefix: str) -> PrefixedStore:
        """Return a view whose keys are prefixed with ``"<prefix>:"``."""
        return PrefixedStore(self, prefix)


__all__ = ["DEFAULT_MAX_ENTRIES", "MemoryStore", "TTLMap"]
