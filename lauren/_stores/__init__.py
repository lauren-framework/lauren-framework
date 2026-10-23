"""Private key-value store runtime.

* ``_protocol`` — shared argument checks.
* ``_memory`` — the bounded ``TTLMap`` engine and ``MemoryStore``.
* ``_prefixed`` — ``PrefixedStore``, the namespaced view.
* ``_lifecycle`` — lifecycle adoption for store wrappers.

Nothing here imports the session package, so sessions can build on it.
"""

from __future__ import annotations

from ._lifecycle import adopt_store_lifecycle, lifecycle_root
from ._memory import DEFAULT_MAX_ENTRIES, MemoryStore, TTLMap
from ._prefixed import PrefixedStore
from ._protocol import Store, check_key, check_prefix, check_ttl, check_value

__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "MemoryStore",
    "PrefixedStore",
    "Store",
    "TTLMap",
    "adopt_store_lifecycle",
    "check_key",
    "check_prefix",
    "check_ttl",
    "check_value",
    "lifecycle_root",
]
