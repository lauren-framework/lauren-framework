"""DI scopes for work that is not an HTTP request or a WebSocket connection.

An :class:`OperationScope` is the request scope without a request. Open one
with :meth:`LaurenApp.scope() <lauren.LaurenApp.scope>` around a script, a job
run or any other unit of work that resolves ``Scope.REQUEST`` providers::

    async with app.scope("backfill") as scope:
        db = await scope.resolve(DbSession)          # one instance per scope
        assert db is await scope.resolve(DbSession)
    # exit: @pre_destruct, then aclose(), newest instance first

Scopes are cheap (one dict plus teardown, the same as an HTTP request), do not
nest or inherit from each other, and are owned by the app's container, so two
apps in one process never share instances.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType, TracebackType
from typing import TYPE_CHECKING, Any

from ._di._scope import finalize_scope
from .exceptions import DestructError, LifecycleViolationError

if TYPE_CHECKING:
    from ._di import DIContainer

_PENDING = "pending"
_ACTIVE = "active"
_CLOSED = "closed"


class OperationScope:
    """A named DI scope whose ``Scope.REQUEST`` providers live as long as the scope.

    Obtain one from :meth:`LaurenApp.scope() <lauren.LaurenApp.scope>` and use it
    as an async context manager; it is single-use.

    * ``Scope.REQUEST`` instances are cached per scope; singletons come from the
      container; transients are new on every resolution and are not tracked.
    * ``values`` seeds the scope's framework values: a token found there is
      returned as-is, both from :meth:`resolve` and for a provider's constructor
      parameter of that type.
    * On exit, every request-scoped instance is torn down in reverse creation
      order — its ``@pre_destruct`` hook, then ``aclose()`` (generator
      providers' teardown) — with the same finaliser HTTP requests use.
      Teardown errors are logged and collected in :attr:`teardown_errors`;
      they are never raised, and an exception from the ``async with`` body
      propagates unchanged.

    Several coroutines of one operation may share a scope. As within one HTTP
    request, two of them resolving the same request-scoped token for the
    first time concurrently may each construct an instance.
    """

    __slots__ = ("_name", "_container", "_cache", "_values", "_state", "_teardown_errors")

    def __init__(
        self,
        container: DIContainer,
        name: str = "operation",
        *,
        values: Mapping[Any, Any] | None = None,
    ) -> None:
        self._name = name
        self._container = container
        self._cache: dict[Any, Any] = {}
        self._values: dict[Any, Any] = dict(values or {})
        self._state = _PENDING
        self._teardown_errors: tuple[DestructError, ...] = ()

    @property
    def name(self) -> str:
        """The label given to :meth:`LaurenApp.scope`, used in logs and errors."""
        return self._name

    @property
    def values(self) -> Mapping[Any, Any]:
        """A read-only view of the framework values seeded with ``values=``."""
        return MappingProxyType(self._values)

    @property
    def active(self) -> bool:
        """``True`` between entering and leaving the ``async with`` block."""
        return self._state == _ACTIVE

    @property
    def teardown_errors(self) -> tuple[DestructError, ...]:
        """Teardown failures collected when the scope closed (empty until then).

        Each entry is a :class:`~lauren.exceptions.DestructError` whose
        ``__cause__`` is the exception the hook raised; ``detail`` carries
        ``class``, ``stage`` (``"pre_destruct"`` or ``"aclose"``), ``origin``
        and ``cause``.
        """
        return self._teardown_errors

    async def resolve(self, token: Any, *, owning_module: type | None = None) -> Any:
        """Resolve ``token`` within this scope.

        Visibility is global by default (``owning_module=None``); pass a module
        class to resolve only what that module can see, as its own providers
        would. Raises :class:`~lauren.exceptions.LifecycleViolationError`
        outside the ``async with`` block.
        """
        if self._state != _ACTIVE:
            raise LifecycleViolationError(
                f"OperationScope {self._name!r} is {self._state}; resolve() works only inside "
                f"`async with app.scope(...) as scope:`",
                detail={"scope": self._name, "state": self._state},
            )
        return await self._container.resolve(
            token,
            request_cache=self._cache,
            framework_values=self._values,
            owning_module=owning_module,
        )

    async def __aenter__(self) -> OperationScope:
        if self._state != _PENDING:
            raise LifecycleViolationError(
                f"OperationScope {self._name!r} was already entered; scopes are single-use, "
                "open a new one with app.scope()",
                detail={"scope": self._name, "state": self._state},
            )
        self._state = _ACTIVE
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._state = _CLOSED
        cache, self._cache = self._cache, {}
        errors = await finalize_scope(self._container, cache, origin=f"scope {self._name!r}")
        self._teardown_errors = tuple(errors)

    def __repr__(self) -> str:
        return f"<OperationScope {self._name!r} {self._state}, {len(self._cache)} request-scoped instance(s)>"


__all__ = ["OperationScope"]
