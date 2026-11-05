"""The shared request-scope finaliser (private).

An HTTP request, a WebSocket connection and an
:class:`~lauren.context.OperationScope` all end the same way: every
request-scoped instance created while the scope was open is torn down,
newest first. Each instance gets its ``@pre_destruct`` hook (when the provider
declares one) and then ``aclose()`` (a generator provider's teardown, or an
instance's own ``aclose`` method).

:func:`finalize_scope` is the only implementation of that teardown. It finds
each instance's provider through :meth:`DIContainer.instance_index`, a map
built once when the container compiles, so a request does not rebuild it.
"""

from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Any

from ..exceptions import DestructError
from . import _describe

if TYPE_CHECKING:
    from . import DIContainer, Provider

logger = logging.getLogger("lauren")

#: Types a consumer seeds into scopes with ``values=`` (an insertion-ordered
#: set). Like the discovery marker registry, it holds import-time
#: declarations of framework types, never per-application data.
_FRAMEWORK_VALUES: dict[Any, None] = {}


def register_framework_value(token: Any) -> Any:
    """Declare ``token`` as a value consumers seed into scopes (call at import).

    A feature that seeds a context object (``JobContext``, ``CommandContext``,
    …) declares its type here so ``DIContainer.compile`` accepts a provider
    constructor parameter of that type the way it accepts ``Request``: it is
    not a provider, and at run time the value comes from the scope's
    ``values=``. Resolving such a provider where the value was not seeded
    raises :class:`~lauren.exceptions.MissingProviderError`. Returns
    ``token``, so it also works as a class decorator.
    """
    _FRAMEWORK_VALUES.setdefault(token, None)
    return token


def framework_value_types() -> frozenset[Any]:
    """The types declared with :func:`register_framework_value`."""
    return frozenset(_FRAMEWORK_VALUES)


async def finalize_scope(
    container: DIContainer,
    cache: dict[Any, Any],
    *,
    origin: str = "request",
) -> list[DestructError]:
    """Tear down every instance in ``cache`` in reverse creation order.

    For each instance, newest first, run the provider's ``@pre_destruct`` hooks
    (every one it declares, in order), then call ``aclose()`` if the instance
    has one. A
    failing step is logged on the ``"lauren"`` logger, recorded as a
    :class:`~lauren.exceptions.DestructError` (its ``__cause__`` is the
    original exception) and teardown continues with the next step. Nothing
    is raised; the collected errors are returned.

    ``origin`` names the scope kind in log messages and in each error's
    ``detail`` (``"request"``, ``"websocket"``, ``"scope 'purge'"``). The keys
    are snapshotted first, so the caller's dict is not mutated.
    """
    if not cache:
        return []
    index = container.instance_index()
    errors: list[DestructError] = []
    for key, instance in [(k, cache[k]) for k in reversed(list(cache))]:
        provider = index.get(key)
        name = _instance_label(key, provider, instance)
        hooks = provider.pre_destruct_chain if provider is not None else ()
        for hook in hooks:
            try:
                result = getattr(instance, hook.__name__)()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.exception("Error in @pre_destruct on %s (%s teardown)", name, origin)
                errors.append(_teardown_error(name, origin, "pre_destruct", exc))
        aclose = getattr(instance, "aclose", None)
        if callable(aclose):
            try:
                result = aclose()
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:
                logger.exception("Error in aclose() on %s (%s teardown)", name, origin)
                errors.append(_teardown_error(name, origin, "aclose", exc))
    return errors


def _instance_label(key: Any, provider: Provider | None, instance: Any) -> str:
    """A readable name for a cached instance (its token, else its type)."""
    target = provider.cls if provider is not None else key
    if isinstance(target, int):  # a multi-binding slot is keyed by id(provider)
        target = type(instance)
    return _describe(target)


def _teardown_error(name: str, origin: str, stage: str, exc: Exception) -> DestructError:
    """Wrap a teardown failure, keeping the original as ``__cause__``."""
    step = "@pre_destruct" if stage == "pre_destruct" else "aclose()"
    error = DestructError(
        f"{step} on {name} failed during {origin} teardown: {exc}",
        detail={"class": name, "stage": stage, "origin": origin, "cause": str(exc)},
    )
    error.__cause__ = exc
    return error


__all__ = ["finalize_scope", "framework_value_types", "register_framework_value"]
