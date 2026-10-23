# Cache

The `Cache` service, `CacheModule`, codecs and the `@cache_response` / `@idempotent` route decorators.

## Module

### `CacheModule`

Provide the app's :class:`Cache` (import the module ``for_root`` returns).

## Service

### `Cache`

Typed cache-aside service over a :class:`~lauren.stores.Store`.

Usually provided by :class:`~lauren.cache.CacheModule` and injected as
``cache: Cache``. ``default_ttl`` (seconds, ``None`` = no expiry) applies
when a call passes no ``ttl``. ``codec`` defaults to
:class:`~lauren.cache.JsonCacheCodec`. ``lock_ttl`` / ``lock_wait`` bound
the cross-process lease of ``get_or_set(lock=True)``. ``clock`` is a
zero-argument callable returning wall-clock seconds (``time.time``),
used for stale-while-revalidate and the ``Age`` header — inject a fake
one in tests.

### `TypedCache`

A :class:`Cache` view whose reads decode into one type (``cache.typed(T)``).

## Codecs

### `CacheCodec`

Encode cache values to ``bytes`` and back.

### `JsonCacheCodec`

JSON codec: encodes with a :class:`~lauren.serialization.JSONEncoder`, decodes with :mod:`json`.

``encoder=None`` uses the app's encoder when ``CacheModule`` builds the
cache, and the process-wide default encoder otherwise.

## Response caching

### `cache_response`

Cache the route's responses server-side for ``ttl`` seconds.

The key covers the method (``HEAD`` reads, never writes, the ``GET``
entry), the route template, the path parameters, the query string
(order-insensitive), the request headers named in ``vary`` and the
result of ``key(ctx)``. ``key`` must
be given for per-user data; returning ``None`` from it bypasses the
cache for that request. ``tags`` (names, or a callable of the context)
let ``Cache.invalidate_tags`` evict entries.

A response is stored only when its status is in ``statuses``, it is not
streamed, it sets no cookie, its ``Cache-Control`` has no ``private`` /
``no-store`` / ``no-cache`` and its ``Vary`` names only headers listed in
``vary``. Guards run before the lookup. ``debug_header=True`` adds
``X-Cache: HIT|MISS``; ``respect_client_no_cache=True`` lets a request's
``Cache-Control: no-cache`` force a refresh. Needs ``CacheModule`` (or a
``Cache`` provider) — checked at startup.

### `CachePolicy`

The compiled ``@cache_response(...)`` arguments of one handler.

## Idempotency keys

### `idempotent`

Make retries of an unsafe request with the same ``Idempotency-Key`` run the handler once.

The first request with a key runs the handler and stores its response
(status < 500, not streamed) for ``ttl`` seconds; a retry with the same
key and the same payload replays it with ``Idempotent-Replayed: true``.
The same key while the first request still runs answers ``409``
(``ConflictError``, ``Retry-After: 1``); with a different payload,
``422`` (``code="idempotency_key_mismatch"``). A missing header answers
``400`` when ``required``. A handler that raises stores nothing, so the
retry runs again. ``scope(ctx)`` partitions keys (per user / tenant);
``lock_ttl`` bounds how long an in-flight request holds its key. Needs
``CacheModule`` (or a ``Cache`` provider) — checked at startup.

### `IdempotencyPolicy`

The compiled ``@idempotent(...)`` arguments of one handler.
