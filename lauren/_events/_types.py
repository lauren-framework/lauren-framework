"""Value types of the application event bus (re-exported by :mod:`lauren.events`)."""

from __future__ import annotations

import enum
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, TypeVar, runtime_checkable

from ..logging import Logger
from ..signals import EventHandlerFailed

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class EventContext:
    """Delivery details for one handler call, injected into a parameter annotated ``EventContext``.

    A handler declares it under any name, after the event parameter::

        @on_event()
        async def record(self, event: OrderPlaced, ctx: EventContext) -> None:
            ctx.log.log(f"order {event.order_id} ({ctx.event_id})")

    The request is deliberately not exposed: requests are pooled and
    reused, and deferred handlers outlive them. Put whatever a handler
    needs from the request into the event.
    """

    #: Random hex id, unique per ``emit()`` call (idempotency keys, audit rows).
    event_id: str
    #: When ``emit()`` was called (timezone-aware, UTC).
    emitted_at: datetime
    #: Re-entrancy depth: ``0`` for a top-level emission, ``1`` for an event
    #: emitted by a handler of that event, and so on.
    depth: int
    #: This handler's delivery mode: ``"inline"``, ``"concurrent"`` or ``"background"``.
    mode: str
    #: ``True`` when the event was published through an
    #: :class:`~lauren.events.EventPublisher` inside an HTTP request or a
    #: WebSocket connection.
    in_request: bool
    #: The application logger, with ``context="Event:<EventType>"`` by default.
    log: Logger


@dataclass(frozen=True, slots=True)
class EmitResult:
    """What one ``emit()`` call did.

    ``handled`` counts handlers that finished without an error during the
    call (inline handlers, plus deferred ones when ``wait=True``);
    ``scheduled`` counts deferred handlers queued or started; ``errors``
    holds an :class:`~lauren.signals.EventHandlerFailed` record for every
    ``"isolate"`` handler that failed during the call.
    """

    event: Any
    handled: int = 0
    scheduled: int = 0
    errors: tuple[EventHandlerFailed, ...] = ()

    @property
    def ok(self) -> bool:
        """``True`` when no handler failed during the call."""
        return not self.errors


class TransactionPhase(enum.Enum):
    """When a deferred emission (``EventPublisher.emit(after=...)``) is delivered."""

    #: After the active :class:`UnitOfWork` commits; dropped on rollback.
    COMMIT = "commit"


@runtime_checkable
class UnitOfWork(Protocol):
    """A transaction boundary that ``EventPublisher.emit(after=TransactionPhase.COMMIT)`` can wait for.

    Core defines only the contract; a database integration (for example a
    request-scoped session) implements it and is registered as a provider
    — either with ``provides=(UnitOfWork,)`` or simply by having both
    methods, since the protocol also matches structurally. The unit of
    work awaits each callback once: ``on_commit`` callbacks after a
    successful commit, ``on_rollback`` callbacks after a rollback.
    """

    def on_commit(self, callback: Callable[[], Awaitable[None]]) -> None: ...

    def on_rollback(self, callback: Callable[[], Awaitable[None]]) -> None: ...


class EventRecording:
    """Events and handler outcomes captured by :meth:`EventBus.recording() <lauren.events.EventBus.recording>`.

    Everything is recorded in order: ``events`` as they are emitted,
    ``calls`` and ``failures`` as handlers finish.
    """

    __slots__ = ("events", "calls", "failures", "_functions")

    def __init__(self) -> None:
        #: Every event emitted while recording, handled or not.
        self.events: list[Any] = []
        #: ``"Class.method"`` of every handler call that finished (succeeded or failed).
        self.calls: list[str] = []
        #: One record per failed handler call.
        self.failures: list[EventHandlerFailed] = []
        self._functions: list[Callable[..., Any]] = []

    def of(self, event_type: type[T]) -> list[T]:
        """The recorded events that are instances of ``event_type`` (subclasses included)."""
        return [event for event in self.events if isinstance(event, event_type)]

    def handler_calls(self, handler: str | Callable[..., Any]) -> int:
        """How many calls of ``handler`` finished: a ``"Class.method"`` name or the method itself."""
        if isinstance(handler, str):
            return sum(1 for name in self.calls if name == handler)
        target = _unwrap_function(handler)
        return sum(1 for fn in self._functions if fn is target)

    def _record(self, name: str, function: Callable[..., Any], failure: EventHandlerFailed | None) -> None:
        self.calls.append(name)
        self._functions.append(function)
        if failure is not None:
            self.failures.append(failure)

    def __repr__(self) -> str:
        return f"<EventRecording {len(self.events)} event(s), {len(self.calls)} handler call(s)>"


def _unwrap_function(handler: Any) -> Any:
    """The plain function behind a method, bound method, staticmethod or classmethod."""
    while True:
        inner = getattr(handler, "__func__", None)
        if inner is None:
            return handler
        handler = inner


__all__ = ["EmitResult", "EventContext", "EventRecording", "TransactionPhase", "UnitOfWork"]
