"""Private key-value store runtime; the public surface is :mod:`lauren.stores`.

* ``_protocol`` — the :class:`Store` protocol and shared argument checks.
* ``_memory`` — the bounded ``TTLMap`` engine and :class:`MemoryStore`.
* ``_prefixed`` — :class:`PrefixedStore`, the namespaced view.
* ``_lock`` — :class:`Lock` over ``set_if_absent`` / ``delete_if_equal``.
* ``_module`` — :class:`StoresModule`, :class:`StoreRegistry`,
  :class:`StoreToken` and the factory's Phase 2 hook.
* ``_lifecycle`` — lifecycle adoption for store wrappers.

Nothing here imports the session package, so sessions can build on it.
"""

from __future__ import annotations

from ._lifecycle import adopt_store_lifecycle, lifecycle_root
from ._lock import Lock
from ._memory import DEFAULT_MAX_ENTRIES, MemoryStore, TTLMap
from ._module import StoreRegistry, StoresModule, StoreToken, install_stores
from ._prefixed import PrefixedStore
from ._protocol import Store, check_key, check_prefix, check_ttl, check_value

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "Lock",
    "MemoryStore",
    "PrefixedStore",
    "Store",
    "StoreRegistry",
    "StoreToken",
    "StoresModule",
    "TTLMap",
    "adopt_store_lifecycle",
    "check_key",
    "check_prefix",
    "check_ttl",
    "check_value",
    "install_stores",
    "lifecycle_root",
]
