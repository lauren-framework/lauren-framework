"""Handler plans and the per-app dispatch table (startup compilation, private).

The shared discovery pass (``lauren._discovery``) finds every
``@on_event`` method and compiles its parameters into a ``CallPlan``.
:func:`compile_handler_plans` turns those entries into one
:class:`HandlerPlan` per subscription, validating the event parameter
against the subscribed type and failing ``LaurenFactory.create`` with
:class:`~lauren.exceptions.UnresolvableParameterError` on any wiring
mistake. :class:`DispatchTable` then answers "which handlers, in which
order" for an event class with one ``__mro__`` walk per class, cached.
"""

from __future__ import annotations

import inspect
import types
import typing
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from .._di import _unwrap_annotated
from .._discovery import DI, FRAMEWORK, DiscoveredMethod
from ..exceptions import UnresolvableParameterError
from ..types import Scope
from ._meta import OnEventMeta

_EMPTY = inspect.Parameter.empty
_NONE_TYPE = type(None)


@dataclass(frozen=True, slots=True)
class HandlerPlan:
    """One subscription of one discovered method, ready to dispatch without reflection."""

    index: int  # discovery order, the priority tie-breaker
    name: str  # "Class.method"
    event_type: type  # the subscribed class
    mode: str
    priority: int
    errors: str
    entry: DiscoveredMethod
    event_param: str
    context_params: tuple[str, ...]
    di_params: tuple[tuple[str, Any], ...]
    needs_instance: bool  # False for staticmethod / classmethod handlers
    needs_scope: bool  # a dependency or the instance itself may be request-scoped

    @property
    def owning_module(self) -> type | None:
        return self.entry.plan.owning_module


def compile_handler_plans(entries: Iterable[DiscoveredMethod]) -> tuple[HandlerPlan, ...]:
    """Validate every discovered ``@on_event`` method and expand it into plans.

    A method stacked with several ``@on_event`` (or inferring a union) gets
    one plan per subscribed class. ``enabled=False`` subscriptions are
    validated and then left out.
    """
    plans: list[HandlerPlan] = []
    index = 0
    for entry in entries:
        name = entry.plan.target
        event_param, annotation = _event_parameter(entry, name)
        context_params: list[str] = []
        di_params: list[tuple[str, Any]] = []
        for param in entry.plan.params[1:]:
            if param.kind == FRAMEWORK:
                context_params.append(param.name)
            elif param.kind == DI:
                di_params.append((param.name, param.token))
            elif not param.has_default:  # ARGUMENT: nothing the bus or DI can supply
                raise _error(
                    name,
                    param.name,
                    "unresolvable_dependency",
                    f"cannot resolve parameter {param.name!r} ({_describe(param.token)}); a handler takes the "
                    "event first, then EventContext and dependencies visible from its provider's module",
                    token=_describe(param.token),
                )
        needs_instance = entry.plan.binding == "instance"
        needs_scope = bool(di_params) or (needs_instance and entry.provider.scope != Scope.SINGLETON)
        metas: list[OnEventMeta] = list(entry.meta or ())
        for meta in metas:
            for event_type in _subscribed_types(meta, annotation, name, event_param):
                if meta.enabled:
                    plans.append(
                        HandlerPlan(
                            index=index,
                            name=name,
                            event_type=event_type,
                            mode=meta.mode,
                            priority=meta.priority,
                            errors=meta.errors,
                            entry=entry,
                            event_param=event_param,
                            context_params=tuple(context_params),
                            di_params=tuple(di_params),
                            needs_instance=needs_instance,
                            needs_scope=needs_scope,
                        )
                    )
                index += 1
    return tuple(plans)


def _event_parameter(entry: DiscoveredMethod, name: str) -> tuple[str, Any]:
    """The event parameter (first after ``self``) and its unwrapped annotation."""
    params = entry.plan.params
    if not params or params[0].kind == FRAMEWORK:
        raise _error(
            name,
            params[0].name if params else None,
            "missing_event_parameter",
            "the first parameter after self must receive the event, e.g. "
            "`async def handle(self, event: UserRegistered, ctx: EventContext)`",
        )
    first = params[0]  # ARGUMENT, or DI when the event class happens to be a provider
    return first.name, _unwrap_annotated(first.token)


def _subscribed_types(meta: OnEventMeta, annotation: Any, name: str, param: str) -> tuple[type, ...]:
    """The classes one ``@on_event`` subscribes to, checked against the annotation."""
    if meta.event_type is None:
        if annotation is _EMPTY:
            raise _error(
                name,
                param,
                "missing_event_parameter",
                f"@on_event() infers the event class from the annotation of {param!r}, which has none; "
                "annotate it or pass the class: @on_event(UserRegistered)",
            )
        inferred = _classes_of(annotation)
        if inferred is None:
            raise _error(
                name,
                param,
                "invalid_event_type",
                f"cannot infer an event class from the annotation {_describe(annotation)} of {param!r}; "
                "annotate it with a class (or a union of classes), or pass the class: @on_event(UserRegistered)",
                annotation=_describe(annotation),
            )
        return inferred
    event_type = meta.event_type
    accepted = _accepts(annotation, event_type)
    if accepted is None:
        raise _error(
            name,
            param,
            "invalid_event_type",
            f"the annotation {_describe(annotation)} of {param!r} is not a class, so it cannot be checked "
            f"against {event_type.__name__}; annotate it with {event_type.__name__} or one of its bases",
            event_type=event_type.__name__,
            annotation=_describe(annotation),
        )
    if not accepted:
        raise _error(
            name,
            param,
            "event_type_mismatch",
            f"subscribes to {event_type.__name__} but its parameter {param!r} is annotated "
            f"{_describe(annotation)}, which does not accept every {event_type.__name__}; annotate it "
            f"with {event_type.__name__} or a base class",
            event_type=event_type.__name__,
            annotation=_describe(annotation),
        )
    return (event_type,)


def _union_members(annotation: Any) -> tuple[Any, ...] | None:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return typing.get_args(annotation)
    return None


def _classes_of(annotation: Any) -> tuple[type, ...] | None:
    """The classes an inferred annotation names, or ``None`` when it is not class-shaped."""
    if annotation is typing.Any:  # a class since Python 3.11, but no event is an instance of it
        return None
    if isinstance(annotation, type):
        return (annotation,)
    members = _union_members(annotation)
    if members is None:
        return None
    classes = tuple(m for m in members if m is not _NONE_TYPE)
    if not classes or not all(isinstance(m, type) for m in classes):
        return None
    return classes


def _accepts(annotation: Any, event_type: type) -> bool | None:
    """Whether a parameter annotated ``annotation`` accepts every ``event_type``; ``None`` if unknowable."""
    if annotation is _EMPTY or annotation is typing.Any or annotation is object:
        return True
    candidates = (annotation,) if isinstance(annotation, type) else _union_members(annotation)
    if candidates is None:
        return None
    for candidate in candidates:
        if not isinstance(candidate, type):
            continue
        try:
            if issubclass(event_type, candidate):
                return True
        except TypeError:  # e.g. a non-runtime Protocol
            return None
    return False


def _describe(token: Any) -> str:
    if token is _EMPTY:
        return "<no annotation>"
    return str(getattr(token, "__name__", token))


def _error(
    name: str, param: str | None, reason: str, message: str, **extra: Any
) -> UnresolvableParameterError:
    return UnresolvableParameterError(
        f"@on_event handler {name}: {message}",
        detail={"handler": name, "target": name, "param": param, "reason": reason, **extra},
    )


class DispatchTable:
    """Handler plans by subscribed class, plus a per-event-class resolution cache.

    :meth:`lookup` merges the plans registered on every class of the
    event's ``__mro__`` (a method subscribed to several of them runs once,
    under its most specific subscription), sorts them by ``(-priority,
    index)`` and caches the tuple. The cache is per app and bounded by the
    number of event classes the program emits.
    """

    __slots__ = ("_by_type", "_resolved", "_plans")

    def __init__(self, plans: Iterable[HandlerPlan] = ()) -> None:
        self._plans: tuple[HandlerPlan, ...] = tuple(plans)
        by_type: dict[type, list[HandlerPlan]] = {}
        for plan in self._plans:
            by_type.setdefault(plan.event_type, []).append(plan)
        self._by_type: Mapping[type, tuple[HandlerPlan, ...]] = MappingProxyType(
            {event_type: tuple(group) for event_type, group in by_type.items()}
        )
        self._resolved: dict[type, tuple[HandlerPlan, ...]] = {}

    @property
    def plans(self) -> tuple[HandlerPlan, ...]:
        """Every enabled plan, in discovery order."""
        return self._plans

    @property
    def subscribed_types(self) -> tuple[type, ...]:
        """The classes at least one handler subscribes to, in first-subscription order."""
        return tuple(self._by_type)

    def lookup(self, event_type: type) -> tuple[HandlerPlan, ...]:
        """The plans an instance of ``event_type`` is delivered to, in dispatch order."""
        cached = self._resolved.get(event_type)
        if cached is not None:
            return cached
        seen: set[int] = set()
        merged: list[HandlerPlan] = []
        if self._by_type:
            for base in event_type.__mro__:
                for plan in self._by_type.get(base, ()):
                    key = id(plan.entry)
                    if key not in seen:
                        seen.add(key)
                        merged.append(plan)
        merged.sort(key=lambda plan: (-plan.priority, plan.index))
        resolved = tuple(merged)
        self._resolved[event_type] = resolved
        return resolved


__all__ = ["DispatchTable", "HandlerPlan", "compile_handler_plans"]
