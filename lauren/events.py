"""Application events: typed domain events delivered to DI-aware ``@on_event`` handlers.

One module announces that something happened; others react without being
imported by it. Events are plain classes (frozen dataclasses work well),
handlers are methods on providers, discovered and validated in
``LaurenFactory.create``::

    from dataclasses import dataclass

    from lauren import injectable
    from lauren.events import EventBus, on_event


    @dataclass(frozen=True)
    class UserRegistered:
        user_id: str
        email: str


    @injectable()
    class UserService:
        def __init__(self, events: EventBus) -> None:
            self._events = events

        async def register(self, user_id: str, email: str) -> None:
            await self._events.emit(UserRegistered(user_id, email))


    @injectable()
    class WelcomeMailer:
        @on_event(mode="background")  # the class comes from the annotation
        async def send_welcome(self, event: UserRegistered) -> None: ...

A handler on a base class receives every subclass. ``"inline"`` handlers
(the default) run inside ``emit()`` and propagate their errors, so they are
part of the publisher's unit of work; ``"concurrent"`` and ``"background"``
handlers run afterwards, only if every inline handler succeeded. Inject
:class:`EventPublisher` instead of :class:`EventBus` in request code to run
inline handlers in the request's DI scope and background handlers after the
response.

This bus is separate from :class:`~lauren.signals.SignalBus`, which carries
the framework's lifecycle telemetry (and the
:class:`~lauren.signals.EventHandlerFailed` signal).
"""

from __future__ import annotations

from ._events import (
    EmitResult,
    EventBus,
    EventContext,
    EventPublisher,
    EventRecording,
    EventsModule,
    TransactionPhase,
    UnitOfWork,
    on_event,
)
from .signals import EventHandlerFailed

__all__ = [
    "EmitResult",
    "EventBus",
    "EventContext",
    "EventHandlerFailed",
    "EventPublisher",
    "EventRecording",
    "EventsModule",
    "TransactionPhase",
    "UnitOfWork",
    "on_event",
]
