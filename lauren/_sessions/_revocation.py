"""Session revocation — making stateless cookie sessions revocable.

A `SignedCookieSessionStore` keeps no per-session server state, so by
construction a logged-out (or stolen) cookie can be replayed until it
expires. The fix is the standard token-blocklist pattern: consult a
*small* server-side store that holds only the things that were revoked,
each entry self-pruning at the cookie's natural expiry.

Two complementary mechanisms live here:

* **Per-session deny-list** — `invalidate()` records the cookie's unique
  token id; a replayed cookie carrying that id is rejected on load. This
  is the cookie store's equivalent of deleting a server-side row.
* **Per-user cutoff (epoch)** — `revoke_user(user_id)` stamps a cutoff
  time; any session minted before it is rejected. This is "log out all my
  devices" / force-logout-on-password-change, and works for both the
  cookie store and server-side stores.

Both are bounded: the store only ever holds *revoked* tokens / users, and
TTLs prune them once the underlying cookie could no longer be presented.
Enable revocation by passing a `RevocationStore` to
`SessionConfig(revocation_store=...)`; it stays off (and the cookie store
stays truly stateless) unless you ask for it.
"""

from __future__ import annotations

import asyncio
import time
from typing import ClassVar, Protocol, runtime_checkable


@runtime_checkable
class RevocationStore(Protocol):
    """Server-side index of revoked session tokens and per-user cutoffs.

    Implementations must be safe under concurrent access and should prune
    expired entries so the index stays bounded. The default
    :class:`InMemoryRevocationStore` does both; production multi-worker
    deployments implement the same surface over Redis or a database.
    """

    async def revoke_token(self, token_id: str, *, ttl: int | None = None) -> None: ...

    async def is_token_revoked(self, token_id: str) -> bool: ...

    async def revoke_user(
        self, user_id: str, *, cutoff: float | None = None, ttl: int | None = None
    ) -> None: ...

    async def user_cutoff(self, user_id: str) -> float | None: ...


class InMemoryRevocationStore:
    """Process-local revocation index guarded by an ``asyncio.Lock``.

    Holds a deny-list of revoked token ids and a per-user cutoff map, both
    with lazy TTL pruning. Fine for development and single-worker
    deployments; multi-worker production wants a Redis-backed store with
    the same surface (a shared blocklist is the whole point).
    """

    requires_secret: ClassVar[bool] = False

    def __init__(self) -> None:
        # token_id -> expiry epoch (or None = no expiry)
        self._tokens: dict[str, float | None] = {}
        # user_id -> (cutoff epoch, entry expiry epoch or None)
        self._users: dict[str, tuple[float, float | None]] = {}
        self._lock = asyncio.Lock()

    async def revoke_token(self, token_id: str, *, ttl: int | None = None) -> None:
        async with self._lock:
            self._tokens[token_id] = (time.time() + ttl) if ttl else None

    async def is_token_revoked(self, token_id: str) -> bool:
        async with self._lock:
            if token_id not in self._tokens:
                return False
            expiry = self._tokens[token_id]
            if expiry is not None and time.time() >= expiry:
                del self._tokens[token_id]  # lazy prune
                return False
            return True

    async def revoke_user(self, user_id: str, *, cutoff: float | None = None, ttl: int | None = None) -> None:
        at = cutoff if cutoff is not None else time.time()
        entry_expiry = (time.time() + ttl) if ttl else None
        async with self._lock:
            self._users[user_id] = (at, entry_expiry)

    async def user_cutoff(self, user_id: str) -> float | None:
        async with self._lock:
            entry = self._users.get(user_id)
            if entry is None:
                return None
            cutoff, expiry = entry
            if expiry is not None and time.time() >= expiry:
                del self._users[user_id]  # lazy prune
                return None
            return cutoff

    # -- test/introspection helpers (not part of the Protocol) ---------

    def _token_count(self) -> int:  # pragma: no cover - convenience for tests
        return len(self._tokens)
