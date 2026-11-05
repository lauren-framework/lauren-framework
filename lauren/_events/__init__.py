"""Application event bus internals (private; the public surface is :mod:`lauren.events`).

* ``_meta`` — ``@on_event`` and its marker, registered with the shared
  startup discovery pass (``lauren._discovery``) on import.
* ``_plan`` — handler validation into ``HandlerPlan`` objects and the
  per-app ``DispatchTable`` (an ``__mro__`` walk per event class, cached).
* ``_bus`` — ``EventBus`` (per app), ``EventPublisher`` (request-scoped),
  task tracking, the shutdown drain and the factory hooks
  (``install_events`` in Phase 2, ``compile_events`` after Phase 6a).
* ``_types`` — ``EventContext``, ``EmitResult``, ``EventRecording``,
  ``TransactionPhase`` and the ``UnitOfWork`` protocol.
"""

from __future__ import annotations

from ._bus import EventBus, EventPublisher, EventsModule, compile_events, install_events
from ._meta import ON_EVENT_META, OnEventMeta, on_event
from ._plan import DispatchTable, HandlerPlan, compile_handler_plans
from ._types import EmitResult, EventContext, EventRecording, TransactionPhase, UnitOfWork

__all__ = [
    "ON_EVENT_META",
    "DispatchTable",
    "EmitResult",
    "EventBus",
    "EventContext",
    "EventPublisher",
    "EventRecording",
    "EventsModule",
    "HandlerPlan",
    "OnEventMeta",
    "TransactionPhase",
    "UnitOfWork",
    "compile_events",
    "compile_handler_plans",
    "install_events",
    "on_event",
]
