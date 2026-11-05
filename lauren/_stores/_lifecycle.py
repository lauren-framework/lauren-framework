"""Find the object whose lifecycle a store wrapper depends on.

Wrappers (namespaced views, the session / revocation adapters, …) expose the
object they delegate to as ``_lauren_backend``. Following that chain gives
the object whose ``startup()`` / ``aclose()`` (or ``@post_construct`` /
``@pre_destruct``) hooks must run with the app.
"""

from __future__ import annotations

from typing import Any


def lifecycle_root(obj: Any) -> Any:
    """Return the innermost backend ``obj`` delegates to (``obj`` itself for plain stores)."""
    seen: set[int] = set()
    while id(obj) not in seen:
        seen.add(id(obj))
        backend = getattr(obj, "_lauren_backend", None)
        if backend is None:
            break
        obj = backend
    return obj


def adopt_store_lifecycle(container: Any, obj: Any) -> None:
    """Run the lifecycle hooks of ``obj`` and of the backend it wraps with the app.

    Hooks run once per object however many tokens expose it (the container
    de-duplicates by identity).
    """
    container.adopt_lifecycle(obj)
    root = lifecycle_root(obj)
    if root is not obj:
        container.adopt_lifecycle(root)


__all__ = ["adopt_store_lifecycle", "lifecycle_root"]
