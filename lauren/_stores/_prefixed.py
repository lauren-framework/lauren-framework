"""``PrefixedStore`` — the namespaced view every backend can reuse."""

from __future__ import annotations

from ._protocol import Store, check_key, check_prefix


class PrefixedStore:
    """A view of ``store`` whose keys are prefixed with ``"<prefix>:"``.

    Views are cheap, share the underlying store's capacity, clock and
    connection, and nest: ``PrefixedStore(s, "a").namespace("b")`` writes
    ``"a:b:<key>"``. A backend implements :meth:`Store.namespace` by
    returning ``PrefixedStore(self, prefix)``.
    """

    __slots__ = ("_base", "_prefix", "_name", "_lauren_backend")

    def __init__(self, store: Store, prefix: str) -> None:
        self._name = check_prefix(prefix)
        self._base = store
        self._prefix = prefix + ":"
        # The object whose lifecycle (startup / aclose) this view depends on.
        self._lauren_backend = store

    @property
    def prefix(self) -> str:
        """The full key prefix without the trailing colon (``"a:b"``)."""
        return self._name

    @property
    def base(self) -> Store:
        """The store the view writes to."""
        return self._base

    def _key(self, key: str) -> str:
        return self._prefix + check_key(key)

    async def get(self, key: str) -> bytes | None:
        return await self._base.get(self._key(key))

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        await self._base.set(self._key(key), value, ttl=ttl)

    async def delete(self, *keys: str) -> int:
        if not keys:
            return 0
        return await self._base.delete(*(self._key(k) for k in keys))

    async def exists(self, key: str) -> bool:
        return await self._base.exists(self._key(key))

    async def expire(self, key: str, ttl: float) -> bool:
        return await self._base.expire(self._key(key), ttl)

    async def ttl(self, key: str) -> float | None:
        return await self._base.ttl(self._key(key))

    async def incr(self, key: str, amount: int = 1, *, ttl: float | None = None) -> int:
        return await self._base.incr(self._key(key), amount, ttl=ttl)

    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool:
        return await self._base.set_if_absent(self._key(key), value, ttl=ttl)

    async def delete_if_equal(self, key: str, expected: bytes) -> bool:
        return await self._base.delete_if_equal(self._key(key), expected)

    def namespace(self, prefix: str) -> PrefixedStore:
        return PrefixedStore(self._base, f"{self._name}:{check_prefix(prefix)}")

    def __repr__(self) -> str:
        return f"PrefixedStore({self._base!r}, {self._name!r})"


__all__ = ["PrefixedStore"]
