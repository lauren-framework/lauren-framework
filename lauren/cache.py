"""Caching: the ``Cache`` service, response caching and idempotency keys.

Import a configured cache module, inject ``cache: Cache`` and use
cache-aside with stampede protection and tag invalidation::

    from lauren import injectable, module
    from lauren.cache import Cache, CacheModule

    @injectable()
    class ProductService:
        def __init__(self, cache: Cache, repo: ProductRepository) -> None:
            self._cache = cache
            self._repo = repo

        async def get(self, product_id: int) -> ProductOut:
            return await self._cache.get_or_set(
                f"product:{product_id}",
                lambda: self._repo.find(product_id),
                ttl=60,
                tags=(f"product:{product_id}", "catalog"),
                type=ProductOut,
            )

    @module(imports=[CacheModule.for_root(default_ttl=300)], providers=[ProductService])
    class AppModule: ...

Route decorators (metadata only, compiled at startup):

* ``@cache_response(ttl=30, vary=(...), key=..., tags=(...))`` — server-side
  response caching with correct-by-default store rules (no ``Set-Cookie``
  replay, ``Vary``-aware keys, no streaming bodies, GET/HEAD only).
* ``@idempotent(ttl=86400)`` — ``Idempotency-Key`` handling for unsafe
  methods: replay, ``409`` while in flight, ``422`` on a payload mismatch.

Both need ``CacheModule`` (``StartupError``, ``reason="cache_module_missing"``
otherwise). The cache runs on any :class:`~lauren.stores.Store`.
"""

from __future__ import annotations

from ._cache import (
    DEFAULT_CACHEABLE_STATUSES,
    Cache,
    CacheCodec,
    CacheModule,
    CachePolicy,
    IdempotencyPolicy,
    JsonCacheCodec,
    TypedCache,
    cache_response,
    idempotent,
)

__all__ = [
    "DEFAULT_CACHEABLE_STATUSES",
    "Cache",
    "CacheCodec",
    "CacheModule",
    "CachePolicy",
    "IdempotencyPolicy",
    "JsonCacheCodec",
    "TypedCache",
    "cache_response",
    "idempotent",
]
