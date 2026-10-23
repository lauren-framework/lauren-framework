"""``Lock`` — a mutual-exclusion lease over any :class:`Store`.

Acquisition is ``set_if_absent(key, token, ttl=...)`` with a random token;
release is ``delete_if_equal(key, token)``, so a holder whose lease already
expired can never delete a lock another worker acquired since. The TTL is
the lease: a crashed holder blocks others for at most ``ttl`` seconds.
"""

from __future__ import annotations

import asyncio
import secrets
from types import TracebackType

from ._protocol import Store, check_ttl

_UNSET: object = object()


class Lock:
    """Async context manager holding the store key ``"lock:<name>"`` for at most ``ttl`` seconds.

    ``wait`` (seconds) is how long :meth:`acquire` keeps retrying, with
    capped exponential backoff starting at ``poll_interval``; ``None`` or
    ``0`` tries once. ``async with`` raises :class:`TimeoutError` when the
    lock could not be acquired. Pick a ``ttl`` longer than the critical
    section: the lease is not extended automatically.
    """

    __slots__ = ("_store", "_name", "_key", "_ttl", "_wait", "_poll", "_token")

    def __init__(
        self,
        store: Store,
        name: str,
        *,
        ttl: float,
        wait: float | None = None,
        poll_interval: float = 0.05,
    ) -> None:
        if not isinstance(name, str) or not name:
            raise ValueError(f"Lock name must be a non-empty string; got {name!r}")
        self._ttl = check_ttl(ttl, allow_none=False)
        self._wait = self._check_wait(wait)
        if (
            isinstance(poll_interval, bool)
            or not isinstance(poll_interval, (int, float))
            or poll_interval <= 0
        ):
            raise ValueError(f"poll_interval must be a positive number of seconds; got {poll_interval!r}")
        self._store = store
        self._name = name
        self._key = f"lock:{name}"
        self._poll = float(poll_interval)
        self._token: bytes | None = None

    @staticmethod
    def _check_wait(wait: float | None) -> float | None:
        if wait is None:
            return None
        if isinstance(wait, bool) or not isinstance(wait, (int, float)) or wait < 0:
            raise ValueError(f"wait must be None or a number of seconds >= 0; got {wait!r}")
        return float(wait)

    @property
    def name(self) -> str:
        return self._name

    @property
    def key(self) -> str:
        """The store key holding the lease."""
        return self._key

    @property
    def locked(self) -> bool:
        """``True`` while this object holds the lock (it may still have expired in the store)."""
        return self._token is not None

    async def acquire(self, *, wait: float | None | object = _UNSET) -> bool:
        """Try to take the lock; return ``True`` on success.

        ``wait`` overrides the constructor's value for this call.
        """
        if self._token is not None:
            raise RuntimeError(f"Lock {self._name!r} is already held by this object")
        seconds = self._wait if wait is _UNSET else self._check_wait(wait)  # type: ignore[arg-type]
        token = secrets.token_hex(16).encode("ascii")
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds if seconds else None
        delay = self._poll
        while True:
            if await self._store.set_if_absent(self._key, token, ttl=self._ttl):
                self._token = token
                return True
            if deadline is None:
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, 1.0)

    async def release(self) -> bool:
        """Release the lock; ``False`` when it was not held or its lease had expired."""
        token, self._token = self._token, None
        if token is None:
            return False
        return await self._store.delete_if_equal(self._key, token)

    async def __aenter__(self) -> Lock:
        if not await self.acquire():
            raise TimeoutError(f"could not acquire lock {self._name!r} (wait={self._wait!r}s)")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.release()

    def __repr__(self) -> str:
        return f"Lock({self._name!r}, ttl={self._ttl}, locked={self.locked})"


__all__ = ["Lock"]
