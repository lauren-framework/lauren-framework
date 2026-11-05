"""Session backends.

A :class:`SessionStore` is the pluggable persistence behind the session
engine. Two backends ship in core:

* :class:`InMemorySessionStore` — process-local; dev / single-worker.
* :class:`SignedCookieSessionStore` — stateless; the whole payload rides
  in the (signed) cookie, so there is no server-side row at all.

Production multi-worker deployments pass any :class:`~lauren.stores.Store`
(adapted by :class:`~lauren.stores.StoreSessionStore`) or implement the
same Protocol over Redis / a database. Stores must be safe under
concurrent access.
"""

from __future__ import annotations

import secrets
import threading
from typing import Any, Callable, ClassVar, Protocol, runtime_checkable

from .._stores._memory import DEFAULT_MAX_ENTRIES, TTLMap


@runtime_checkable
class SessionStore(Protocol):
    """Pluggable, async, concurrency-safe session backend.

    ``load`` returns the stored data for a session id (or ``None`` when
    absent/expired); ``save`` persists it with an optional TTL; ``delete``
    removes it; ``new_id`` mints an unguessable id. ``client_side`` marks
    stateless cookie stores so the engine keeps the payload in the cookie
    instead of calling ``load``/``save`` for data.
    """

    #: When ``True`` the factory rejects an empty signing secret.
    requires_secret: ClassVar[bool]
    #: When ``True`` the engine treats the store as stateless — the data
    #: lives in the cookie and ``load``/``save`` are not used for it.
    client_side: ClassVar[bool]

    async def load(self, session_id: str) -> dict[str, Any] | None: ...

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None: ...

    async def delete(self, session_id: str) -> None: ...

    def new_id(self) -> str: ...


def _new_id() -> str:
    """Return an unguessable, URL-safe, cookie-safe session id."""
    return secrets.token_urlsafe(32)


class InMemorySessionStore:
    """Bounded, process-local session store.

    Fine for development and single-worker deployments. Sessions expire
    after ``max_age`` (checked on load and swept proactively, so a session
    that is written once and never revisited does not linger), and at most
    ``max_entries`` sessions are kept — the least recently used are dropped
    first. ``clock`` (seconds, ``time.monotonic`` by default) exists for
    tests. Values are kept as Python objects (copied on save and load), so
    they need not be JSON-serialisable.

    Multi-worker production swaps in a shared backend: pass any
    :class:`~lauren.stores.Store` (e.g. a Redis store) as
    ``SessionConfig(store=...)`` and it is adapted automatically.
    """

    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        sweep_interval: float = 60.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._entries: TTLMap[dict[str, Any]] = TTLMap(
            max_entries=max_entries, sweep_interval=sweep_interval, clock=clock
        )
        self._lock = threading.Lock()

    def new_id(self) -> str:
        return _new_id()

    async def load(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._entries.get(session_id, self._entries.clock())
            if entry is None:
                return None
            self._entries.touch(session_id)
            return dict(entry[0])

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        with self._lock:
            now = self._entries.clock()
            self._entries.maybe_sweep(now)
            self._entries.put(session_id, dict(data), (now + max_age) if max_age else None, now)

    async def delete(self, session_id: str) -> None:
        with self._lock:
            if self._entries.get(session_id, self._entries.clock()) is not None:
                self._entries.remove(session_id)

    # -- test/introspection helpers (not part of the Protocol) ---------

    def _count(self) -> int:
        """Sessions currently held, including expired ones not yet swept."""
        return len(self._entries)


class SignedCookieSessionStore:
    """Stateless store — the session payload lives in the signed cookie.

    There is **no server-side row**: ``save``/``load``/``delete`` are
    no-ops because the engine serialises the data straight into the
    cookie envelope. The cookie is signed (tamper-proof) but **not
    encrypted** — the client can read it — so never put confidential data
    here. ``max_bytes`` caps the encoded cookie size; an over-size
    payload raises ``ValueError`` at request time.
    """

    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = True

    def __init__(self, *, max_bytes: int = 4096) -> None:
        self.max_bytes = max_bytes

    def new_id(self) -> str:
        # The id is not used for storage in the cookie store; the engine
        # keeps the data in the cookie. A token is still handy for code
        # that reads ``session.id`` (e.g. CSRF binding).
        return _new_id()

    async def load(self, session_id: str) -> dict[str, Any] | None:
        return None

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        return None

    async def delete(self, session_id: str) -> None:
        return None
