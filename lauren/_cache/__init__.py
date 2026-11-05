"""Private caching runtime; the public surface is :mod:`lauren.cache`.

* ``_codec`` — :class:`CacheCodec`, :class:`JsonCacheCodec`, typed decoding.
* ``_entry`` — the byte layout of a cache entry.
* ``_service`` — :class:`Cache` (single-flight, locks, stale-while-revalidate,
  tag generations) and :class:`TypedCache`.
* ``_policy`` — ``@cache_response`` / ``@idempotent`` and their policies.
* ``_response`` — response-cache keying, store-time rules, header handling
  and :class:`ResponseCacheInterceptor`.
* ``_idempotency`` — :class:`IdempotencyInterceptor`.
* ``_module`` — :class:`CacheModule` and the factory hooks.
"""

from __future__ import annotations

from ._codec import CacheCodec, JsonCacheCodec
from ._idempotency import IdempotencyInterceptor
from ._module import CacheModule, compile_cache_routes, install_cache
from ._policy import (
    CACHE_RESPONSE_META,
    DEFAULT_CACHEABLE_STATUSES,
    IDEMPOTENT_META,
    CachePolicy,
    IdempotencyPolicy,
    cache_response,
    idempotent,
)
from ._response import ResponseCacheInterceptor
from ._service import Cache, TypedCache

__all__ = [
    "CACHE_RESPONSE_META",
    "DEFAULT_CACHEABLE_STATUSES",
    "IDEMPOTENT_META",
    "Cache",
    "CacheCodec",
    "CacheModule",
    "CachePolicy",
    "IdempotencyInterceptor",
    "IdempotencyPolicy",
    "JsonCacheCodec",
    "ResponseCacheInterceptor",
    "TypedCache",
    "cache_response",
    "compile_cache_routes",
    "idempotent",
    "install_cache",
]
