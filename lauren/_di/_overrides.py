"""Build-time provider and module overrides — the seam behind ``lauren.testing.Test``.

An :class:`OverridePlan` describes replacements that apply to **one** app
build: provider rows to swap (keyed by the DI token being replaced) and
modules to substitute in the module graph. ``LaurenFactory.create`` reads the
plan that is active in the current context (see :func:`active_override_plan`)
and hands it to the :class:`~lauren._di.DIContainer` and the
:class:`~lauren._modules.ModuleGraph` it builds. Nothing else consults it, so
production builds — which never activate a plan — pay nothing.

The plan is activated by :meth:`lauren.testing.TestingModuleBuilder.compile`
through :func:`building_with` for the duration of one synchronous
``LaurenFactory.create`` call. A :class:`~contextvars.ContextVar` scopes it to
that call (and thread / task), so concurrent builds — and other apps in the
same process — never see each other's overrides.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

from ..types import Scope

#: The override kinds a :class:`ProviderOverride` can carry.
OVERRIDE_KINDS: frozenset[str] = frozenset({"value", "values", "class", "factory", "existing"})


@dataclass(frozen=True)
class ProviderOverride:
    """Replacement for every registration of ``token`` in one app build.

    ``kind`` selects the payload: ``"value"`` (``value``), ``"values"``
    (``values`` — replaces a whole multi-binding set), ``"class"``
    (``use_class``), ``"factory"`` (``factory`` + ``injects``) or
    ``"existing"`` (an alias to ``existing``). ``scope`` is ``None`` when the
    override should inherit the replaced provider's scope. An ``optional``
    override that never matches a registration is ignored instead of failing
    the build.
    """

    token: Any
    kind: str
    value: Any = None
    values: tuple[Any, ...] = ()
    use_class: type | None = None
    factory: Callable[..., Any] | None = None
    injects: tuple[Any, ...] = ()
    existing: Any = None
    scope: Scope | None = None
    optional: bool = False
    #: What the override replaces, for error messages ("provider", "guard", …).
    role: str = "provider"


class OverridePlan:
    """Immutable set of provider overrides and module substitutions for one build."""

    __slots__ = ("providers", "modules")

    def __init__(
        self,
        providers: Mapping[Any, ProviderOverride] | None = None,
        modules: Mapping[type, type] | None = None,
    ) -> None:
        self.providers: dict[Any, ProviderOverride] = dict(providers or {})
        self.modules: dict[type, type] = dict(modules or {})

    def get(self, token: Any) -> ProviderOverride | None:
        """Return the override registered for ``token``, if any."""
        try:
            return self.providers.get(token)
        except TypeError:  # unhashable token — cannot have been overridden
            return None


_ACTIVE_PLAN: ContextVar[OverridePlan | None] = ContextVar("lauren_override_plan", default=None)


def active_override_plan() -> OverridePlan | None:
    """Return the plan activated for the build running in this context, if any."""
    return _ACTIVE_PLAN.get()


@contextlib.contextmanager
def building_with(plan: OverridePlan) -> Iterator[None]:
    """Activate ``plan`` for the ``LaurenFactory.create`` call made inside the block."""
    reset_token = _ACTIVE_PLAN.set(plan)
    try:
        yield
    finally:
        _ACTIVE_PLAN.reset(reset_token)


__all__ = [
    "OVERRIDE_KINDS",
    "OverridePlan",
    "ProviderOverride",
    "active_override_plan",
    "building_with",
]
