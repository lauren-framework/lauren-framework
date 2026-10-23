"""One async key-value contract for every stateful feature.

:class:`Store` is a small protocol — bytes values, per-key TTLs, atomic
``incr`` / ``set_if_absent`` / ``delete_if_equal`` and cheap namespaced
views. Sessions, session revocation, the cache, response caching,
idempotency keys and locks all run on it, so one backend instance serves
them all::

    from lauren import LaurenFactory, SessionConfig, module
    from lauren.stores import MemoryStore, StoresModule

    store = MemoryStore(max_entries=200_000)

    @module(imports=[StoresModule.for_root(default=store)])
    class AppModule: ...

    app = LaurenFactory.create(
        AppModule,
        sessions=SessionConfig(secret="…", store=store, revocation_store=store, max_age=3600),
    )

Core ships the protocol, the bounded :class:`MemoryStore`, :class:`Lock`,
the session / revocation adapters and :class:`StoresModule`. Networked
backends (Redis, …) live in companion packages and prove compliance with
:func:`lauren.testing.store_contract.run_store_contract`.

A store registered through :class:`StoresModule` or passed to
``SessionConfig(store=...)`` has its ``startup()`` / ``aclose()`` (or
``@post_construct`` / ``@pre_destruct``) hooks run with the app;
``use_value`` never runs hooks.
"""

from __future__ import annotations

from ._sessions._adapters import StoreRevocationStore, StoreSessionStore
from ._stores import Lock, MemoryStore, PrefixedStore, Store, StoreRegistry, StoresModule, StoreToken

__all__ = [
    "Lock",
    "MemoryStore",
    "PrefixedStore",
    "Store",
    "StoreRegistry",
    "StoreRevocationStore",
    "StoreSessionStore",
    "StoreToken",
    "StoresModule",
]
