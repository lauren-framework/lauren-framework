"""Sessions and revocation on any :class:`~lauren.stores.Store`.

``SessionConfig(store=..., revocation_store=...)`` adapts a value that
implements the ``Store`` protocol (but not ``SessionStore`` /
``RevocationStore``) automatically, so one backend instance — a
``MemoryStore`` or a Redis store — can serve sessions, revocation, the cache
and idempotency keys side by side under different namespaces.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, ClassVar

from .._stores._protocol import Store
from ._serializer import JSONSessionSerializer, SessionSerializer


def _namespaced(store: Store, namespace: str | None) -> Store:
    return store.namespace(namespace) if namespace else store


def _entry_ttl(ttl: int | float | None) -> tuple[bool, float | None]:
    """Map a session-protocol TTL onto a store TTL: ``(write?, ttl)``.

    ``None`` / ``0`` mean "no expiry" (the in-memory stores' semantics); a
    negative TTL describes an entry that is already expired, so nothing is
    written.
    """
    if not ttl:
        return True, None
    if ttl < 0:
        return False, None
    return True, float(ttl)


class StoreSessionStore:
    """:class:`~lauren.sessions.SessionStore` over any :class:`~lauren.stores.Store`.

    Session payloads are serialised with ``serializer`` (compact JSON by
    default) and saved with ``ttl=max_age``. Keys live under
    ``"<namespace>:"`` (``"session"`` by default; ``None`` disables the
    prefix).
    """

    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False

    def __init__(
        self,
        store: Store,
        *,
        namespace: str | None = "session",
        serializer: SessionSerializer | None = None,
    ) -> None:
        self._store = _namespaced(store, namespace)
        self._serializer: SessionSerializer = (
            serializer if serializer is not None else JSONSessionSerializer()
        )
        # The object whose lifecycle (startup / aclose) this adapter depends on.
        self._lauren_backend = store

    @property
    def store(self) -> Store:
        """The (namespaced) store session payloads are written to."""
        return self._store

    def new_id(self) -> str:
        return secrets.token_urlsafe(32)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._store.get(session_id)
        return None if raw is None else self._serializer.loads(raw)

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        write, ttl = _entry_ttl(max_age)
        if write:
            await self._store.set(session_id, self._serializer.dumps(data), ttl=ttl)

    async def delete(self, session_id: str) -> None:
        await self._store.delete(session_id)

    def __repr__(self) -> str:
        return f"StoreSessionStore({self._store!r})"


class StoreRevocationStore:
    """:class:`~lauren.sessions.RevocationStore` over any :class:`~lauren.stores.Store`.

    A revoked token is the key ``token:<id>``; a user cutoff is the key
    ``user:<id>`` holding the cutoff epoch. Both expire with their TTL, so
    the index stays bounded by the backend. Keys live under
    ``"<namespace>:"`` (``"revocation"`` by default).
    """

    requires_secret: ClassVar[bool] = False

    def __init__(self, store: Store, *, namespace: str | None = "revocation") -> None:
        self._store = _namespaced(store, namespace)
        self._lauren_backend = store

    @property
    def store(self) -> Store:
        """The (namespaced) store the index is written to."""
        return self._store

    async def revoke_token(self, token_id: str, *, ttl: int | None = None) -> None:
        write, seconds = _entry_ttl(ttl)
        if write:
            await self._store.set(f"token:{token_id}", b"1", ttl=seconds)

    async def is_token_revoked(self, token_id: str) -> bool:
        return await self._store.exists(f"token:{token_id}")

    async def revoke_user(self, user_id: str, *, cutoff: float | None = None, ttl: int | None = None) -> None:
        at = cutoff if cutoff is not None else time.time()
        write, seconds = _entry_ttl(ttl)
        if write:
            await self._store.set(f"user:{user_id}", repr(float(at)).encode("ascii"), ttl=seconds)

    async def user_cutoff(self, user_id: str) -> float | None:
        raw = await self._store.get(f"user:{user_id}")
        return None if raw is None else float(raw)

    def __repr__(self) -> str:
        return f"StoreRevocationStore({self._store!r})"


def _is_store(obj: Any) -> bool:
    return obj is not None and not isinstance(obj, type) and isinstance(obj, Store)


def adapt_session_store(store: Any, serializer: SessionSerializer | None) -> Any:
    """Wrap a bare :class:`Store` in :class:`StoreSessionStore`; pass anything else through."""
    if _is_store(store) and not (hasattr(store, "load") and hasattr(store, "save")):
        return StoreSessionStore(store, serializer=serializer)
    return store


def adapt_revocation_store(store: Any) -> Any:
    """Wrap a bare :class:`Store` in :class:`StoreRevocationStore`; pass anything else through."""
    if _is_store(store) and not hasattr(store, "revoke_token"):
        return StoreRevocationStore(store)
    return store


__all__ = [
    "StoreRevocationStore",
    "StoreSessionStore",
    "adapt_revocation_store",
    "adapt_session_store",
]
