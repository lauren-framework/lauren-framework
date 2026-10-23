"""The :class:`Cache` service: cache-aside over any :class:`~lauren.stores.Store`.

Store layout (inside the cache's namespace):

* ``val:<key>`` — values written by :meth:`Cache.set` / :meth:`Cache.get_or_set`.
* ``resp:<route>:<digest>`` — responses stored by ``@cache_response``.
* ``tag:<name>`` — tag generation counters.
* ``lock:<key>`` / ``refresh:<key>`` — cross-process leases.

**Tags without scanning.** Every tag has a generation counter. An entry
records the generations of its tags when it is written; a read compares
them with the current ones and treats any difference as a miss.
``invalidate_tags`` is one ``incr`` per tag. A counter is seeded with a
random value, so a counter that was evicted and re-created never matches
an old entry again.

**Stampedes.** Concurrent misses for one key in one process share a single
factory call. ``lock=True`` extends that across processes with a store
lease; ``stale_ttl`` serves the previous value while one caller refreshes
it in the background.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import time
from typing import Any, Awaitable, Callable, Iterable

from .._stores._protocol import Store, check_key, check_ttl
from ._codec import CacheCodec, JsonCacheCodec, TypeMismatch, compile_decoder
from ._entry import Entry, decode_entry, encode_entry

_log = logging.getLogger("lauren")


class _Default:
    """Sentinel type for "use the cache's default TTL"."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "DEFAULT"


DEFAULT: Any = _Default()
_MISS: Any = object()


class _LeaderCancelled(Exception):
    """The caller computing a value was cancelled; waiters retry."""


def check_tags(tags: Iterable[str]) -> tuple[str, ...]:
    """Validate and de-duplicate tag names (order preserved)."""
    if isinstance(tags, str):
        tags = (tags,)
    out: list[str] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag:
            raise ValueError(f"cache tags must be non-empty strings; got {tag!r}")
        if tag not in out:
            out.append(tag)
    return tuple(out)


async def _call(factory: Callable[[], Any]) -> Any:
    result = factory()
    if inspect.isawaitable(result):
        result = await result
    return result


class Cache:
    """Typed cache-aside service over a :class:`~lauren.stores.Store`.

    Usually provided by :class:`~lauren.cache.CacheModule` and injected as
    ``cache: Cache``. ``default_ttl`` (seconds, ``None`` = no expiry) applies
    when a call passes no ``ttl``. ``codec`` defaults to
    :class:`~lauren.cache.JsonCacheCodec`. ``lock_ttl`` / ``lock_wait`` bound
    the cross-process lease of ``get_or_set(lock=True)``. ``clock`` is a
    zero-argument callable returning wall-clock seconds (``time.time``),
    used for stale-while-revalidate and the ``Age`` header — inject a fake
    one in tests.
    """

    def __init__(
        self,
        store: Store,
        *,
        default_ttl: float | None = 300.0,
        codec: CacheCodec | None = None,
        lock_ttl: float = 30.0,
        lock_wait: float = 5.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._store = store
        self._default_ttl = check_ttl(default_ttl)
        self._codec: CacheCodec = codec if codec is not None else JsonCacheCodec()
        self._lock_ttl = check_ttl(lock_ttl, allow_none=False)
        if isinstance(lock_wait, bool) or not isinstance(lock_wait, (int, float)) or lock_wait < 0:
            raise ValueError(f"lock_wait must be a number of seconds >= 0; got {lock_wait!r}")
        self._lock_wait = float(lock_wait)
        self._clock: Callable[[], float] = clock if clock is not None else time.time
        # (event loop, key) -> future of the in-flight factory call.
        self._inflight: dict[tuple[Any, str], asyncio.Future[Any]] = {}
        self._refreshing: set[str] = set()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._decoders: dict[Any, Callable[[Any], Any]] = {}
        self._closed = False

    # -- properties --------------------------------------------------------

    @property
    def store(self) -> Store:
        """The store entries are written to (already namespaced by ``CacheModule``)."""
        return self._store

    @property
    def default_ttl(self) -> float | None:
        return self._default_ttl

    @property
    def codec(self) -> CacheCodec:
        return self._codec

    def now(self) -> float:
        """The cache's wall clock."""
        return self._clock()

    def __repr__(self) -> str:
        return f"Cache({self._store!r}, default_ttl={self._default_ttl})"

    # -- public API ----------------------------------------------------------

    async def get(self, key: str, *, type: Any = None) -> Any:
        """Return the cached value, or ``None`` on a miss.

        With ``type=`` the data is validated into that type (a model comes
        back as a model); data that no longer validates is a miss.
        """
        found = await self._lookup(check_key(key), self._decoder(type), allow_stale=False)
        return None if found is _MISS else found[0]

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None | _Default = DEFAULT,
        tags: Iterable[str] = (),
    ) -> None:
        """Encode ``value`` and store it for ``ttl`` seconds under ``tags``."""
        check_key(key)
        seconds = self._resolve_ttl(ttl)
        gens = await self.snapshot_generations(check_tags(tags))
        await self._write(f"val:{key}", value, seconds, None, gens)

    async def delete(self, *keys: str) -> int:
        """Remove entries; return how many existed."""
        if not keys:
            return 0
        return await self._store.delete(*(f"val:{check_key(k)}" for k in keys))

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Any | Awaitable[Any]],
        *,
        ttl: float | None | _Default = DEFAULT,
        tags: Iterable[str] = (),
        type: Any = None,
        lock: bool = False,
        stale_ttl: float | None = None,
    ) -> Any:
        """Return the cached value, computing and storing it on a miss.

        ``factory`` takes no arguments and may return an awaitable.
        Concurrent misses for ``key`` in this process share one call; an
        exception propagates to every waiter and nothing is cached.
        ``lock=True`` also takes a store lease so that only one process
        computes (others poll for up to ``lock_wait`` seconds, then compute
        themselves). ``stale_ttl`` keeps an expired entry for that many extra
        seconds: readers get it immediately while one caller refreshes it in
        the background (errors are logged, never raised to readers).
        """
        check_key(key)
        seconds = self._resolve_ttl(ttl)
        stale = check_ttl(stale_ttl)
        if stale is not None and seconds is None:
            raise ValueError("stale_ttl needs a finite ttl")
        tag_names = check_tags(tags)
        decode = self._decoder(type)

        found = await self._lookup(key, decode, allow_stale=True)
        if found is not _MISS:
            value, is_stale = found
            if is_stale:
                self._schedule_refresh(key, factory, seconds, stale, tag_names)
            return value

        loop = asyncio.get_running_loop()
        flight = (loop, key)
        while True:
            pending = self._inflight.get(flight)
            if pending is None:
                break
            try:
                return await asyncio.shield(pending)
            except _LeaderCancelled:
                continue

        future: asyncio.Future[Any] = loop.create_future()
        self._inflight[flight] = future
        try:
            value = await self._compute(key, factory, seconds, stale, tag_names, lock, decode)
        except Exception as exc:
            future.set_exception(exc)
            future.exception()  # retrieved: waiters re-raise it, no "never retrieved" warning
            raise
        except BaseException:
            future.set_exception(_LeaderCancelled())
            future.exception()
            raise
        else:
            future.set_result(value)
            return value
        finally:
            if self._inflight.get(flight) is future:
                del self._inflight[flight]

    async def invalidate_tags(self, *tags: str) -> None:
        """Make every entry written under any of ``tags`` a miss (one ``incr`` per tag)."""
        for tag in check_tags(tags):
            await self._store.incr(f"tag:{tag}")

    def typed(self, tp: Any) -> TypedCache:
        """Return a view whose reads decode into ``tp``."""
        return TypedCache(self, tp)

    async def aclose(self) -> None:
        """Cancel background refreshes (run at app shutdown)."""
        self._closed = True
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # -- internals shared with response caching ------------------------------

    def _resolve_ttl(self, ttl: float | None | _Default) -> float | None:
        if isinstance(ttl, _Default):
            return self._default_ttl
        return check_ttl(ttl)

    def _decoder(self, tp: Any) -> Callable[[Any], Any]:
        if tp is None:
            return compile_decoder(None)
        try:
            decoder = self._decoders.get(tp)
        except TypeError:  # unhashable annotation
            return compile_decoder(tp)
        if decoder is None:
            decoder = self._decoders[tp] = compile_decoder(tp)
        return decoder

    async def snapshot_generations(self, tags: tuple[str, ...]) -> dict[str, int]:
        """Return the current generation of each tag, seeding missing counters."""
        gens: dict[str, int] = {}
        for tag in tags:
            key = f"tag:{tag}"
            raw = await self._store.get(key)
            if raw is None:
                seed = secrets.randbits(62) + 1
                await self._store.set_if_absent(key, str(seed).encode("ascii"))
                raw = await self._store.get(key)
                gens[tag] = seed if raw is None else int(raw)
            else:
                gens[tag] = int(raw)
        return gens

    async def _generations_current(self, gens: dict[str, int]) -> bool:
        for tag, generation in gens.items():
            raw = await self._store.get(f"tag:{tag}")
            if raw is None or int(raw) != generation:
                return False
        return True

    async def read_entry(self, store_key: str) -> Entry | None:
        """Return the entry at ``store_key`` when present and none of its tags changed."""
        raw = await self._store.get(store_key)
        if raw is None:
            return None
        entry = decode_entry(raw)
        if entry is None:
            return None
        if entry.gens and not await self._generations_current(entry.gens):
            return None
        return entry

    async def write_entry(
        self,
        store_key: str,
        payload: bytes,
        *,
        ttl: float | None,
        gens: dict[str, int],
        stale_ttl: float | None = None,
    ) -> Entry:
        """Store ``payload`` at ``store_key`` and return the entry written."""
        now = self._clock()
        soft = None if stale_ttl is None or ttl is None else now + ttl
        store_ttl = None if ttl is None else ttl + (stale_ttl or 0.0)
        await self._store.set(
            store_key,
            encode_entry(payload, written_at=now, gens=gens, soft_expiry=soft),
            ttl=store_ttl,
        )
        return Entry(payload=payload, written_at=now, gens=dict(gens), soft_expiry=soft)

    async def _write(
        self,
        store_key: str,
        value: Any,
        ttl: float | None,
        stale_ttl: float | None,
        gens: dict[str, int],
    ) -> None:
        await self.write_entry(store_key, self._codec.encode(value), ttl=ttl, gens=gens, stale_ttl=stale_ttl)

    async def _lookup(self, key: str, decode: Callable[[Any], Any], *, allow_stale: bool) -> Any:
        entry = await self.read_entry(f"val:{key}")
        if entry is None:
            return _MISS
        is_stale = entry.soft_expiry is not None and self._clock() >= entry.soft_expiry
        if is_stale and not allow_stale:
            return _MISS
        try:
            value = decode(self._codec.decode(entry.payload))
        except Exception as exc:  # noqa: BLE001 — stale schema / codec change: recompute instead of failing
            if not isinstance(exc, TypeMismatch):
                _log.debug("cached value for %r no longer validates (%s); treating it as a miss", key, exc)
            return _MISS
        return value, is_stale

    async def _compute(
        self,
        key: str,
        factory: Callable[[], Any],
        ttl: float | None,
        stale_ttl: float | None,
        tags: tuple[str, ...],
        lock: bool,
        decode: Callable[[Any], Any],
    ) -> Any:
        acquired = False
        token = secrets.token_hex(16).encode("ascii")
        lock_key = f"lock:{key}"
        if lock:
            acquired = await self._store.set_if_absent(lock_key, token, ttl=self._lock_ttl)
            if not acquired:
                value, acquired = await self._wait_for_holder(key, decode, lock_key, token)
                if value is not _MISS:
                    return value
        try:
            # Generations are read *before* computing: an invalidation that
            # lands while the factory runs leaves the new entry already stale.
            gens = await self.snapshot_generations(tags)
            value = await _call(factory)
            await self._write(f"val:{key}", value, ttl, stale_ttl, gens)
            return value
        finally:
            if acquired:
                await self._store.delete_if_equal(lock_key, token)

    async def _wait_for_holder(
        self, key: str, decode: Callable[[Any], Any], lock_key: str, token: bytes
    ) -> tuple[Any, bool]:
        """Poll while another process holds the lease: ``(value or _MISS, acquired)``."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._lock_wait
        delay = 0.01
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return _MISS, False  # never deadlock: compute ourselves
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 0.25)
            found = await self._lookup(key, decode, allow_stale=False)
            if found is not _MISS:
                return found[0], False
            if await self._store.set_if_absent(lock_key, token, ttl=self._lock_ttl):
                return _MISS, True

    def _schedule_refresh(
        self,
        key: str,
        factory: Callable[[], Any],
        ttl: float | None,
        stale_ttl: float | None,
        tags: tuple[str, ...],
    ) -> None:
        if self._closed or key in self._refreshing:
            return
        self._refreshing.add(key)
        task = asyncio.get_running_loop().create_task(self._refresh(key, factory, ttl, stale_ttl, tags))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refresh(
        self,
        key: str,
        factory: Callable[[], Any],
        ttl: float | None,
        stale_ttl: float | None,
        tags: tuple[str, ...],
    ) -> None:
        token = secrets.token_hex(16).encode("ascii")
        refresh_key = f"refresh:{key}"
        try:
            # One refresher across processes; the others keep serving stale.
            if not await self._store.set_if_absent(refresh_key, token, ttl=self._lock_ttl):
                return
            try:
                gens = await self.snapshot_generations(tags)
                value = await _call(factory)
                await self._write(f"val:{key}", value, ttl, stale_ttl, gens)
            finally:
                await self._store.delete_if_equal(refresh_key, token)
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("background refresh of cache key %r failed", key)
        finally:
            self._refreshing.discard(key)


class TypedCache:
    """A :class:`Cache` view whose reads decode into one type (``cache.typed(T)``)."""

    __slots__ = ("_cache", "_type")

    def __init__(self, cache: Cache, tp: Any) -> None:
        self._cache = cache
        self._type = tp

    @property
    def type(self) -> Any:
        return self._type

    async def get(self, key: str) -> Any:
        return await self._cache.get(key, type=self._type)

    async def set(
        self,
        key: str,
        value: Any,
        *,
        ttl: float | None | _Default = DEFAULT,
        tags: Iterable[str] = (),
    ) -> None:
        await self._cache.set(key, value, ttl=ttl, tags=tags)

    async def delete(self, *keys: str) -> int:
        return await self._cache.delete(*keys)

    async def get_or_set(
        self,
        key: str,
        factory: Callable[[], Any | Awaitable[Any]],
        *,
        ttl: float | None | _Default = DEFAULT,
        tags: Iterable[str] = (),
        lock: bool = False,
        stale_ttl: float | None = None,
    ) -> Any:
        return await self._cache.get_or_set(
            key, factory, ttl=ttl, tags=tags, type=self._type, lock=lock, stale_ttl=stale_ttl
        )

    def __repr__(self) -> str:
        return f"TypedCache({self._type!r})"


__all__ = ["DEFAULT", "Cache", "TypedCache", "check_tags"]
