"""Session backends.

A :class:`SessionStore` is the pluggable persistence behind the session
engine. Two backends ship in core:

* :class:`InMemorySessionStore` — process-local; dev / single-worker.
* :class:`SignedCookieSessionStore` — stateless; the whole payload rides
  in the (signed) cookie, so there is no server-side row at all.

Production multi-worker deployments implement the same Protocol over
Redis / a database. Stores must be safe under concurrent access.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any, ClassVar, Protocol, runtime_checkable


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
    """Process-local session store guarded by an ``asyncio.Lock``.

    Fine for development and single-worker deployments. TTL expiry is
    lazy (evaluated on ``load``). Multi-worker production should swap in a
    Redis-backed store with the same surface — the engine does not care
    which backend it talks to.
    """

    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False

    def __init__(self) -> None:
        self._data: dict[str, dict[str, Any]] = {}
        self._expiry: dict[str, float | None] = {}
        self._lock = asyncio.Lock()

    def new_id(self) -> str:
        return _new_id()

    async def load(self, session_id: str) -> dict[str, Any] | None:
        async with self._lock:
            exp = self._expiry.get(session_id)
            if exp is not None and time.time() >= exp:
                # Lazy expiry — drop and report absent.
                self._data.pop(session_id, None)
                self._expiry.pop(session_id, None)
                return None
            data = self._data.get(session_id)
            return dict(data) if data is not None else None

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        async with self._lock:
            self._data[session_id] = dict(data)
            self._expiry[session_id] = (time.time() + max_age) if max_age else None

    async def delete(self, session_id: str) -> None:
        async with self._lock:
            self._data.pop(session_id, None)
            self._expiry.pop(session_id, None)

    # -- test/introspection helpers (not part of the Protocol) ---------

    def _count(self) -> int:  # pragma: no cover - convenience for tests
        return len(self._data)


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
