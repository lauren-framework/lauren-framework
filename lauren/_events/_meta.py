"""The ``@on_event`` decorator and its metadata (re-exported by :mod:`lauren.events`)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .._discovery import MarkerSpec, register_marker
from ..decorators import _reject_bare_usage
from ..exceptions import DecoratorUsageError
from ._types import EventContext

F = TypeVar("F")

#: Marker attribute: a list of :class:`OnEventMeta`, one per ``@on_event``.
ON_EVENT_META = "__lauren_on_event__"

INLINE = "inline"
CONCURRENT = "concurrent"
BACKGROUND = "background"
MODES = (INLINE, CONCURRENT, BACKGROUND)

PROPAGATE = "propagate"
ISOLATE = "isolate"
ERROR_POLICIES = (PROPAGATE, ISOLATE)


@dataclass(frozen=True, slots=True)
class OnEventMeta:
    """One ``@on_event(...)`` declaration (``errors`` already defaulted from ``mode``)."""

    event_type: type | None
    mode: str
    priority: int
    errors: str
    enabled: bool


def on_event(
    event_type: type | None = None,
    *,
    mode: str = INLINE,
    priority: int = 0,
    errors: str | None = None,
    enabled: bool = True,
) -> Callable[[F], F]:
    """Subscribe a provider method to an event class; it is discovered at startup.

    ``event_type=None`` infers the class from the annotation of the first
    parameter after ``self`` (a union subscribes to each member). The
    handler also receives instances of subclasses. Other parameters are an
    :class:`~lauren.events.EventContext` (any name) or dependencies the
    provider's module can resolve.

    ``mode`` is ``"inline"`` (awaited inside ``emit()``, part of the
    publisher's unit of work), ``"concurrent"`` (a tracked task started
    after the inline handlers succeed) or ``"background"`` (after the HTTP
    response when published through an ``EventPublisher`` in a request,
    otherwise like ``"concurrent"``). Higher ``priority`` runs first; ties
    keep discovery order. ``errors`` defaults to ``"propagate"`` for inline
    handlers (the exception aborts the emission and re-raises from
    ``emit()``) and ``"isolate"`` for deferred ones (logged, signalled as
    :class:`~lauren.signals.EventHandlerFailed`, returned in
    :attr:`EmitResult.errors <lauren.events.EmitResult.errors>`).
    ``enabled=False`` keeps the handler validated but never calls it.

    The decorator only records metadata and returns the method unchanged;
    stacking it subscribes the method to several events.
    """
    if event_type is not None and not isinstance(event_type, type):
        if callable(event_type):
            _reject_bare_usage("on_event", event_type)
        raise DecoratorUsageError(
            f"@on_event expects an event class, got {event_type!r}; write @on_event(UserRegistered) "
            "or @on_event() to infer the class from the event parameter's annotation",
            detail={"decorator": "on_event", "event_type": repr(event_type)},
        )
    if mode not in MODES:
        raise DecoratorUsageError(
            f'@on_event(mode={mode!r}) is invalid; use "inline", "concurrent" or "background"',
            detail={"decorator": "on_event", "mode": repr(mode)},
        )
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise DecoratorUsageError(
            f"@on_event(priority={priority!r}) must be an int (higher runs first)",
            detail={"decorator": "on_event", "priority": repr(priority)},
        )
    if errors is not None and errors not in ERROR_POLICIES:
        raise DecoratorUsageError(
            f'@on_event(errors={errors!r}) is invalid; use "propagate" or "isolate"',
            detail={"decorator": "on_event", "errors": repr(errors)},
        )
    if errors == PROPAGATE and mode != INLINE:
        raise DecoratorUsageError(
            f'@on_event(mode="{mode}", errors="propagate") is invalid: a {mode} handler runs after '
            'emit() has returned, so there is no caller to propagate to. Use errors="isolate" '
            '(the default for deferred modes) or mode="inline"',
            detail={"decorator": "on_event", "mode": mode, "errors": errors},
        )
    if not isinstance(enabled, bool):
        raise DecoratorUsageError(
            f"@on_event(enabled={enabled!r}) must be a bool",
            detail={"decorator": "on_event", "enabled": repr(enabled)},
        )
    meta = OnEventMeta(
        event_type=event_type,
        mode=mode,
        priority=priority,
        errors=errors if errors is not None else (PROPAGATE if mode == INLINE else ISOLATE),
        enabled=enabled,
    )

    def decorator(fn: F) -> F:
        # Above @staticmethod / @classmethod the marker goes on the function
        # itself, the canonical place discovery reads first.
        target: Any = fn.__func__ if isinstance(fn, (staticmethod, classmethod)) else fn
        if not callable(target):
            raise DecoratorUsageError(
                f"@on_event decorates a method, got {target!r}",
                detail={"decorator": "on_event", "target": repr(target)},
            )
        existing = list(getattr(target, ON_EVENT_META, None) or ())
        existing.append(meta)
        setattr(target, ON_EVENT_META, existing)
        return fn

    return decorator


# Handler methods are found by the shared startup discovery pass. The event
# parameter is left to this feature (``extra_params="collect"``); the bus
# compiler checks every other leftover parameter itself.
register_marker(MarkerSpec(ON_EVENT_META, framework_types=(EventContext,), extra_params="collect"))


__all__ = [
    "BACKGROUND",
    "CONCURRENT",
    "ERROR_POLICIES",
    "INLINE",
    "ISOLATE",
    "MODES",
    "ON_EVENT_META",
    "PROPAGATE",
    "OnEventMeta",
    "on_event",
]
