"""The handler-facing :class:`Session` object.

A ``Session`` is a mutable, per-request, dict-like handle. It is what a
handler receives via ``session: Session`` injection (or reads off
``request.state.session``) and what it mutates. The session **engine**
(``_engine.py``) builds one per request from the inbound cookie and
persists it on the way out — but only when it is *dirty* (modified, new
with content, regenerated, or invalidated), so a pure read costs nothing
on the response path.

This is intentionally distinct from ``lauren_guards.Session`` (a frozen
auth store-row): this one is the request-scoped mutable mapping; that one
is an authentication record.
"""

from __future__ import annotations

from typing import Any, Callable, Iterator, MutableMapping

_SENTINEL: Any = object()


class Session(MutableMapping[str, Any]):
    """A mutable, dirty-tracked, per-request session mapping.

    Mutating the mapping (``session[k] = v``, ``del``, ``pop``,
    ``setdefault``, ``update``, ``clear``) flags the session modified so
    the engine knows to persist it. ``regenerate_id()`` issues a fresh id
    (session-fixation defence at login); ``invalidate()`` drops the
    server row and expires the cookie (logout).
    """

    __slots__ = (
        "_data",
        "_id",
        "_is_new",
        "_modified",
        "_invalidated",
        "_regenerated",
        "_new_id_factory",
    )

    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        id: str = "",
        is_new: bool = True,
        new_id_factory: Callable[[], str] | None = None,
    ) -> None:
        self._data: dict[str, Any] = dict(data or {})
        self._id = id
        self._is_new = is_new
        self._modified = False
        self._invalidated = False
        #: Set by ``regenerate_id()`` so the engine can revoke the prior
        #: cookie token (deny-list) when revocation is enabled.
        self._regenerated = False
        self._new_id_factory = new_id_factory or (lambda: "")

    # -- identity ------------------------------------------------------

    @property
    def id(self) -> str:
        """Opaque session id (server-side stores) or ``""`` for the
        cookie store before the first save."""
        return self._id

    @property
    def is_new(self) -> bool:
        """``True`` when no valid inbound session cookie was presented."""
        return self._is_new

    @property
    def is_modified(self) -> bool:
        """``True`` when any mutation, regeneration, or invalidation
        occurred this request."""
        return self._modified

    @property
    def is_invalidated(self) -> bool:
        return self._invalidated

    # -- mapping surface (writes are dirty-tracked) --------------------

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __setitem__(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._modified = True

    def __delitem__(self, key: str) -> None:
        del self._data[key]
        self._modified = True

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: object) -> bool:
        return key in self._data

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        flags = []
        if self._is_new:
            flags.append("new")
        if self._modified:
            flags.append("modified")
        if self._invalidated:
            flags.append("invalidated")
        return f"Session(id={self._id!r}, keys={list(self._data)}, {' '.join(flags) or 'clean'})"

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def setdefault(self, key: str, default: Any = None) -> Any:
        if key not in self._data:
            self._data[key] = default
            self._modified = True
        return self._data[key]

    def pop(self, key: str, default: Any = _SENTINEL) -> Any:
        if key in self._data:
            self._modified = True
            return self._data.pop(key)
        if default is _SENTINEL:
            raise KeyError(key)
        return default

    def update(self, *args: Any, **kwargs: Any) -> None:  # type: ignore[override]
        before = len(self._data)
        self._data.update(*args, **kwargs)
        # Conservatively mark modified whenever update was given anything.
        if args or kwargs or len(self._data) != before:
            self._modified = True

    def clear(self) -> None:
        if self._data:
            self._modified = True
        self._data.clear()

    # -- lifecycle -----------------------------------------------------

    def regenerate_id(self) -> None:
        """Issue a fresh session id while preserving the data.

        The canonical session-fixation defence: call this whenever the
        trust level of the session changes (e.g. right after a successful
        login). The engine deletes the old server-side row and re-sends
        the cookie with the new id.
        """
        self._id = self._new_id_factory()
        self._is_new = False
        self._modified = True
        self._regenerated = True

    def invalidate(self) -> None:
        """Drop the session: clears the data, deletes the server-side row
        on save, and expires the cookie on the response (logout)."""
        self._data.clear()
        self._invalidated = True
        self._modified = True

    # -- introspection -------------------------------------------------

    def as_dict(self) -> dict[str, Any]:
        """Return a shallow copy of the session data."""
        return dict(self._data)
