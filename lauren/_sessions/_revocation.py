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

import logging
import threading
import time
from typing import Callable, ClassVar, Protocol, runtime_checkable

from .._stores._memory import DEFAULT_MAX_ENTRIES, TTLMap


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
    """Bounded, process-local revocation index.

    Holds a deny-list of revoked token ids and a per-user cutoff map. Entries
    expire with their TTL (checked on lookup and swept proactively) and each
    map keeps at most ``max_entries`` entries. An entry evicted to make room
    is no longer enforced, so size ``max_entries`` above the revocations you
    expect within one session lifetime (a warning is logged the first time a
    live entry is evicted). Fine for development and single-worker
    deployments; multi-worker production wants a shared backend with the
    same surface.
    """

    requires_secret: ClassVar[bool] = False

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        sweep_interval: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        # token_id -> True (presence is the deny-list entry)
        self._tokens: TTLMap[bool] = TTLMap(
            max_entries=max_entries, sweep_interval=sweep_interval, clock=clock
        )
        # user_id -> cutoff epoch (wall-clock seconds)
        self._users: TTLMap[float] = TTLMap(
            max_entries=max_entries, sweep_interval=sweep_interval, clock=clock
        )
        self._lock = threading.Lock()
        self._warned = False

    def _put(self, entries: TTLMap[bool] | TTLMap[float], key: str, value: object, ttl: int | None) -> None:
        now = entries.clock()
        entries.maybe_sweep(now)
        evicted = entries.put(key, value, (now + ttl) if ttl else None, now)  # type: ignore[arg-type]
        if evicted and not self._warned:
            self._warned = True
            logging.getLogger("lauren").warning(
                "InMemoryRevocationStore is full (max_entries=%d): the least recently used "
                "revocations were dropped and are no longer enforced; raise max_entries or use "
                "a shared store",
                entries.max_entries,
            )

    async def revoke_token(self, token_id: str, *, ttl: int | None = None) -> None:
        with self._lock:
            self._put(self._tokens, token_id, True, ttl)

    async def is_token_revoked(self, token_id: str) -> bool:
        with self._lock:
            return self._tokens.get(token_id, self._tokens.clock()) is not None

    async def revoke_user(self, user_id: str, *, cutoff: float | None = None, ttl: int | None = None) -> None:
        at = cutoff if cutoff is not None else time.time()
        with self._lock:
            self._put(self._users, user_id, at, ttl)

    async def user_cutoff(self, user_id: str) -> float | None:
        with self._lock:
            entry = self._users.get(user_id, self._users.clock())
            return None if entry is None else entry[0]

    # -- test/introspection helpers (not part of the Protocol) ---------

    def _token_count(self) -> int:
        """Deny-list entries currently held, including expired ones not yet swept."""
        return len(self._tokens)
