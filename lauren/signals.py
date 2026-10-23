"""Lifecycle signals — OS signal handlers *and* a typed event bus.

This module exposes two complementary surfaces that both happen to
deal with "signals" in the general sense:

1. **OS signal handlers** (:func:`install_signal_handlers`,
   :func:`wait_for_shutdown`) — wire ``SIGTERM`` / ``SIGINT`` to a
   graceful :meth:`LaurenApp.shutdown`. This has been around since
   the framework's first release and is used by standalone scripts
   and production containers that need a deterministic drain.

2. **Lifecycle event bus** (:class:`SignalBus`,
   :class:`LifecycleEvent` subclasses, the :func:`on` decorator) —
   emit typed events at well-known points in the request/app
   lifecycle so user code can plug in tracing, metrics, audit
   logging, or any other cross-cutting concern without touching the
   framework internals. Inspired by NestJS's ``LifecycleEvent`` and
   FastAPI's startup/shutdown hooks.

The two surfaces share the word "signal" but do not share state —
OS signals go through :mod:`signal`, lifecycle events through an
in-process pub/sub bus. They are in the same module because they
are in the same conceptual domain: "things that happen around the
app's running lifetime".

Event taxonomy
--------------

Five built-in events are fired by the framework:

* :class:`StartupBegin` — the first ``lifespan.startup`` message has
  arrived; the DI graph is compiled but no ``@post_construct`` hooks
  have run yet.
* :class:`StartupComplete` — every ``@post_construct`` hook has
  finished; the app is ready to accept traffic.
* :class:`RequestReceived` — a new HTTP request has been decoded;
  the router has NOT run yet.
* :class:`RequestComplete` — the request pipeline has produced the final
  response, which is sent right after the listeners return. Also fires on
  error paths, with the captured exception on the event.
* :class:`ShutdownBegin` — a ``lifespan.shutdown`` message arrived
  or a graceful-shutdown signal was received.

:class:`LifecycleStateChanged` accompanies every transition of
``app.lifecycle_state`` (:class:`AppLifecycleState`), and
:class:`HealthChanged` every status change of a ``lauren.health`` check.
Background tasks add :class:`BackgroundTaskStarted` /
:class:`BackgroundTaskComplete` / :class:`BackgroundTaskFailed`, and the
application event bus (:mod:`lauren.events`) adds
:class:`EventHandlerFailed`. Domain events themselves do **not** go through
this bus: they are delivered by :class:`~lauren.events.EventBus` to DI-aware
``@on_event`` handlers.

User code registers listeners via :meth:`SignalBus.on` (also
available as :func:`on` for convenience) and the framework emits
events via :meth:`SignalBus.emit` during the request lifecycle.
Listeners may be sync or async; errors in listeners are logged but
never propagate out of ``emit`` — observability must not break the
request path.
"""

from __future__ import annotations

import asyncio
import enum
import inspect as _inspect
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, TypeVar

from .logging import Logger

if TYPE_CHECKING:
    from ._asgi import LaurenApp
    from ._health._types import HealthStatus


DEFAULT_SIGNALS: tuple[int, ...] = (
    signal.SIGINT,
    signal.SIGTERM,
)


# ---------------------------------------------------------------------------
# Application lifecycle state
# ---------------------------------------------------------------------------


class AppLifecycleState(enum.Enum):
    """Where an application is in its lifetime, read from ``app.lifecycle_state``.

    ``CREATED`` (``LaurenFactory.create`` returned) → ``STARTING``
    (``startup()`` running) → ``READY`` (serving) → ``DRAINING``
    (termination requested: readiness fails, requests are still served) →
    ``STOPPED`` (``shutdown()`` finished). ``FAILED`` means ``startup()``
    raised. ``STOPPED`` and ``FAILED`` are terminal. Every transition emits a
    :class:`LifecycleStateChanged` on the app's :class:`SignalBus`.
    """

    CREATED = "created"
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    STOPPED = "stopped"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Typed lifecycle events
# ---------------------------------------------------------------------------


@dataclass
class LifecycleEvent:
    """Base class for every event published on the :class:`SignalBus`.

    Subclasses carry event-specific context (the live ``Request``,
    the duration, the captured exception, ...). The base class only
    holds a monotonic timestamp so listeners can compute deltas
    without having to record wall-clock time themselves.
    """

    #: ``time.monotonic()`` reading at the moment the framework
    #: emitted the event. Use deltas between events rather than
    #: treating this as an absolute time \u2014 ``time.monotonic`` is
    #: arbitrary across process restarts.
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class StartupBegin(LifecycleEvent):
    """Emitted when the app receives its first ``lifespan.startup``.

    At this point the DI graph is compiled but ``@post_construct``
    hooks have NOT run yet. Listeners observing this event are
    typically interested in recording "app build completed" telemetry
    without depending on user services being constructed.
    """

    #: The live :class:`LaurenApp` \u2014 useful for reading the router
    #: table or the DI container's provider list.
    app: Any = None


@dataclass
class StartupComplete(LifecycleEvent):
    """Fired once every ``@post_construct`` hook has finished running.

    The app is fully ready to accept traffic at this point. Metrics
    pipelines typically record a "ready" counter here; health-check
    endpoints can be wired to flip a readiness flag.
    """

    app: Any = None
    #: Seconds elapsed between the matching :class:`StartupBegin` and
    #: this event \u2014 i.e. the duration of the lifecycle-hook phase.
    #: ``0.0`` if the framework could not measure (shouldn't happen
    #: in practice).
    duration_s: float = 0.0


@dataclass
class RequestReceived(LifecycleEvent):
    """Fired as soon as the ASGI scope has been parsed into a :class:`Request`.

    The router has NOT yet run, so ``request.get_matched_route()``
    returns ``None``. Listeners should avoid reading the body here
    unless they also intend to be the sole consumer \u2014 doing so
    would flip the body into buffered mode and defeat any downstream
    :class:`~lauren.extractors.ByteStream` handler.
    """

    #: The live incoming request. Safe to read path/method/headers;
    #: avoid calling ``.body()`` or iterating a ``ByteStream``.
    request: Any = None


@dataclass
class RequestComplete(LifecycleEvent):
    """Fired once the request pipeline has produced its final response.

    Emitted just *before* the response is written to the client (the
    status, headers and body are final at this point), so listeners add
    to the request's latency.

    Fires on both success and error paths. When the handler raised
    an exception that escaped the middleware chain, the exception
    instance is attached to :attr:`error` and :attr:`status`
    reflects the final HTTP status surfaced to the client.
    """

    request: Any = None
    #: The :class:`Response` object delivered to the client. May be
    #: an error envelope for failed requests.
    response: Any = None
    #: Final HTTP status code seen by the client.
    status: int = 0
    #: Request processing duration in seconds (monotonic).
    duration_s: float = 0.0
    #: The exception that was raised during handling, if any.
    #: ``None`` on success. Listeners can use this for error-budget
    #: tracking without having to hook a separate ``on_error`` path.
    error: BaseException | None = None


@dataclass
class ShutdownBegin(LifecycleEvent):
    """Fired when ``lifespan.shutdown`` arrives or a signal triggered.

    Listeners are typically used to flush buffers, close external
    connections, or emit a final heartbeat. The event does NOT
    block shutdown \u2014 listeners run concurrently with the framework's
    own ``@pre_destruct`` machinery.
    """

    app: Any = None


@dataclass
class LifecycleStateChanged(LifecycleEvent):
    """Emitted on every transition of ``app.lifecycle_state``.

    Transitions happen in ``startup()`` (``CREATED → STARTING``, then
    ``READY`` or ``FAILED``), in ``app.begin_draining()`` or at the start of
    ``shutdown()`` (``READY → DRAINING``) and at the end of ``shutdown()``
    (``→ STOPPED``). Listeners run in transition order.
    """

    app: Any = None
    #: The state before the transition.
    previous: AppLifecycleState | None = None
    #: The state after the transition (also ``app.lifecycle_state``).
    current: AppLifecycleState | None = None
    #: Free-form cause, e.g. ``"signal SIGTERM"`` from ``begin_draining(reason=...)``.
    reason: str = ""


@dataclass
class HealthChanged(LifecycleEvent):
    """Emitted when a health check's status, or the full report's aggregate status, changes.

    Fired by ``lauren.health`` whenever a check runs and its
    :class:`~lauren.health.HealthStatus` differs from the previous run
    (``previous`` is ``None`` the first time a check runs), and when a full
    report's aggregate status changes (``name`` is ``None``).
    """

    app: Any = None
    #: The check's name; ``None`` for the aggregate status of the full report.
    name: str | None = None
    previous: HealthStatus | None = None
    current: HealthStatus | None = None
    #: The check's details (including ``error`` / ``reason`` when it is down).
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class BackgroundTaskStarted(LifecycleEvent):
    """Emitted just before a background task begins executing."""

    task_id: str = ""
    func: Any = None  # Callable[..., Any]


@dataclass
class BackgroundTaskComplete(LifecycleEvent):
    """Emitted after a background task finishes successfully."""

    task_id: str = ""
    func: Any = None
    duration_s: float = 0.0


@dataclass
class BackgroundTaskFailed(LifecycleEvent):
    """Emitted when a background task raises an exception."""

    task_id: str = ""
    func: Any = None
    error: BaseException | None = None


@dataclass
class EventHandlerFailed(LifecycleEvent):
    """Emitted when an ``@on_event`` handler raises, or is cancelled at shutdown.

    Fired by the application :class:`~lauren.events.EventBus` for every
    handler failure, whatever the handler's error policy: ``"isolate"``
    failures are also logged and returned in
    :attr:`EmitResult.errors <lauren.events.EmitResult.errors>`,
    ``"propagate"`` failures re-raise from ``emit()``. A deferred handler
    cancelled by the shutdown drain carries an :class:`asyncio.CancelledError`.
    """

    #: The class of the event being delivered.
    event_type: Any = None
    #: ``"Class.method"`` of the handler.
    handler: str = ""
    error: BaseException | None = None
    #: The handler's delivery mode: ``"inline"``, ``"concurrent"`` or ``"background"``.
    mode: str = ""
    #: The event instance itself.
    event: Any = None


# ---------------------------------------------------------------------------
# Listener registration types
# ---------------------------------------------------------------------------


#: A synchronous or asynchronous listener callable. Sync listeners
#: run inline on the event loop thread; async listeners are awaited
#: sequentially (never in parallel) so relative ordering across
#: events for a single listener is preserved.
Listener = Callable[[LifecycleEvent], Any | Awaitable[Any]]

E = TypeVar("E", bound=LifecycleEvent)


# ---------------------------------------------------------------------------
# The bus itself
# ---------------------------------------------------------------------------


class SignalBus:
    """In-process pub/sub bus for :class:`LifecycleEvent` subclasses.

    The bus is owned by a :class:`LaurenApp` \u2014 there is one bus per
    application so multiple apps in the same process do not share
    listeners. The bus is thread-agnostic in that listener lookup
    is a plain dict read; concurrent emission from multiple
    coroutines is safe because each :meth:`emit` iterates its own
    listener list snapshot.

    Listener discipline
    -------------------

    * Listener errors are *logged but swallowed*. Observability code
      must not be able to break the request path.
    * Async listeners are awaited sequentially, not concurrently,
      so subsequent listeners see the side-effects of earlier ones
      in a deterministic order.
    * A listener registered for a base-class event receives every
      instance of that class, including subclasses \u2014 so
      ``bus.on(LifecycleEvent)`` is a firehose of every event.

    Basic usage::

        bus = app.signals  # SignalBus owned by the app

        @bus.on(RequestComplete)
        def log_slow(event: RequestComplete) -> None:
            if event.duration_s > 1.0:
                app.logger.warn(\"slow request\", path=event.request.path)
    """

    __slots__ = ("_listeners", "_logger")

    def __init__(self, logger: Logger | None = None) -> None:
        # Keyed on the exact event class registered. We emit by
        # walking the event's MRO so a listener on
        # ``LifecycleEvent`` (or any intermediate base class) picks
        # up every subclass event.
        self._listeners: dict[type, list[Listener]] = {}
        self._logger = logger

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def on(self, event_type: type[E]) -> Callable[[Listener], Listener]:
        """Register ``fn`` as a listener for ``event_type``.

        Returns the original callable so ``@bus.on(StartupComplete)``
        can decorate a function idiomatically. The callable may be
        sync or async; both work identically from the caller's
        perspective.

        A listener is registered at most once per ``(event_type, fn)``
        pair \u2014 re-registration is silently idempotent so module
        reloads during development do not create duplicate
        subscriptions.
        """

        def decorator(fn: Listener) -> Listener:
            listeners = self._listeners.setdefault(event_type, [])
            if fn not in listeners:
                listeners.append(fn)
            return fn

        return decorator

    def off(self, event_type: type[E], fn: Listener) -> bool:
        """Remove a previously-registered listener.

        Returns ``True`` when the listener was removed, ``False``
        when it wasn't registered in the first place. Never raises \u2014
        callers typically use this during test teardown where a
        missing listener is not a bug.
        """
        listeners = self._listeners.get(event_type)
        if not listeners:
            return False
        try:
            listeners.remove(fn)
        except ValueError:
            return False
        return True

    def listener_count(self, event_type: type[LifecycleEvent]) -> int:
        """Total listeners that would be invoked for ``event_type``.

        Walks the MRO the same way :meth:`emit` does, so a listener
        on ``LifecycleEvent`` is counted once for every concrete
        subclass. Primarily useful for tests and diagnostics.
        """
        total = 0
        for base in event_type.__mro__:
            total += len(self._listeners.get(base, ()))
            if base is LifecycleEvent:
                break
        return total

    def clear(self) -> None:
        """Remove every listener.

        Primarily useful between tests when the same ``SignalBus``
        is reused across cases.
        """
        self._listeners.clear()

    # ------------------------------------------------------------------
    # Emission
    # ------------------------------------------------------------------

    async def emit(self, event: LifecycleEvent) -> None:
        """Fire ``event`` to every registered listener.

        Listeners registered on any class in the event's MRO are
        invoked, in registration order within a class and
        most-specific-first across classes. Async listeners are
        awaited sequentially; sync listeners run inline.

        Exceptions inside listeners are logged through the bus's
        :class:`~lauren.logging.Logger` and then swallowed so a
        misbehaving listener can never break the request path.
        """
        # Fast path: if the bus has no listeners at all we skip the
        # MRO walk entirely. This matters because ``emit`` is called
        # on every request and the typical app starts with zero
        # listeners registered.
        if not self._listeners:
            return
        # Snapshot the listener list so a listener removing itself
        # or registering a sibling during emission doesn't mutate
        # the iteration we're performing.
        listeners: list[Listener] = []
        for base in type(event).__mro__:
            registered = self._listeners.get(base)
            if registered:
                listeners.extend(registered)
            if base is LifecycleEvent:
                break
        for fn in listeners:
            try:
                result = fn(event)
                if _inspect.isawaitable(result):
                    await result
            except Exception as exc:
                # Keep observability failures out of the request path.
                # We log through the bus's Logger if provided, else
                # fall back to the root stdlib logger so nothing is
                # silently lost.
                self._log_listener_error(fn, event, exc)

    def emit_sync(self, event: LifecycleEvent) -> None:
        """Synchronous variant of :meth:`emit`.

        Used from framework code paths that are not themselves async
        (startup / shutdown construction). Async listeners are
        scheduled on the running loop when one is available and
        otherwise skipped with a warning \u2014 we never create an event
        loop just to run a listener, because doing so would subtly
        break applications that rely on a specific loop policy.
        """
        if not self._listeners:
            return
        listeners: list[Listener] = []
        for base in type(event).__mro__:
            registered = self._listeners.get(base)
            if registered:
                listeners.extend(registered)
            if base is LifecycleEvent:
                break
        for fn in listeners:
            try:
                result = fn(event)
                if _inspect.isawaitable(result):
                    # Best-effort: schedule on the current loop if any.
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)  # type: ignore[arg-type]
                    except RuntimeError:
                        # No loop \u2014 drop the coroutine to avoid a
                        # "never awaited" warning storm; a sync
                        # callsite cannot meaningfully await here.
                        if hasattr(result, "close"):
                            result.close()
            except Exception as exc:
                self._log_listener_error(fn, event, exc)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _log_listener_error(self, fn: Listener, event: LifecycleEvent, exc: BaseException) -> None:
        """Record a listener failure without escalating it."""
        name = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", repr(fn))
        event_name = type(event).__name__
        if self._logger is not None:
            self._logger.error(
                f"signal listener {name} failed on {event_name}: {exc}",
                context="SignalBus",
                listener=name,
                event=event_name,
                error=type(exc).__name__,
            )
        else:  # pragma: no cover - fallback for tests without a logger
            import logging as _stdlib

            _stdlib.getLogger("lauren.signals").exception("signal listener %s failed on %s", name, event_name)


# ---------------------------------------------------------------------------
# Module-level convenience
# ---------------------------------------------------------------------------


_default_bus: SignalBus | None = None


def get_default_bus() -> SignalBus:
    """Return (and lazily create) the process-wide default bus.

    Apps that don't care about multi-app isolation can register on
    this bus and every :class:`LaurenApp` built without an explicit
    bus will copy its listeners at construction time. Multi-app
    setups should create an explicit :class:`SignalBus` and pass it
    via ``LaurenFactory.create(signals=...)``.
    """
    global _default_bus
    if _default_bus is None:
        _default_bus = SignalBus()
    return _default_bus


def on(event_type: type[E]) -> Callable[[Listener], Listener]:
    """Decorator that registers against the process-wide default bus.

    Convenience shortcut so user code can write ``@on(RequestComplete)``
    at module level without threading an app instance around. When a
    :class:`LaurenApp` is later constructed, its bus is seeded with
    the default bus's listeners.
    """
    return get_default_bus().on(event_type)


# ---------------------------------------------------------------------------
# OS-signal handlers (kept from the original module)
# ---------------------------------------------------------------------------


def install_signal_handlers(
    app: "LaurenApp",
    *,
    signals: Iterable[int] = DEFAULT_SIGNALS,
    drain_timeout: float = 10.0,
    shutdown_delay: float = 0.0,
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Event:
    """Install async signal handlers that shut the app down gracefully.

    On the first handled signal the handler calls ``app.begin_draining()``
    (``READY → DRAINING``: readiness probes answer 503 while requests are
    still served), waits ``shutdown_delay`` seconds so load balancers polling
    readiness notice, then schedules ``app.shutdown(drain_timeout=...)``.

    Returns an :class:`asyncio.Event` that fires once a handled signal has
    been received. Callers can ``await event.wait()`` to block until that
    moment (useful in a ``run_forever`` loop).

    The handlers are idempotent \u2014 delivering multiple signals never produces
    a second shutdown; the first wins and subsequent ones are logged.
    """
    if isinstance(shutdown_delay, bool) or not isinstance(shutdown_delay, (int, float)) or shutdown_delay < 0:
        raise ValueError(f"shutdown_delay must be a number of seconds >= 0, got {shutdown_delay!r}")
    loop = loop or asyncio.get_event_loop()
    event = asyncio.Event()
    logger = app.logger
    tasks: set[asyncio.Task[Any]] = set()

    def _shutdown() -> None:
        task = loop.create_task(app.shutdown(drain_timeout=drain_timeout))
        tasks.add(task)  # keep a reference until it finishes
        task.add_done_callback(tasks.discard)

    def _handler(signum: int) -> None:
        sig_name = signal.Signals(signum).name
        if event.is_set():
            logger.warn(
                f"Signal {sig_name} received during shutdown; ignoring.",
                context="Shutdown",
                signal=sig_name,
            )
            return
        logger.log(
            f"Signal {sig_name} received \u2014 beginning graceful shutdown",
            context="Shutdown",
            signal=sig_name,
        )
        event.set()
        app.begin_draining(reason=f"signal {sig_name}")
        if shutdown_delay > 0:
            logger.log(
                f"Shutdown starts in {shutdown_delay}s (readiness now fails)",
                context="Shutdown",
                shutdown_delay=shutdown_delay,
            )
            loop.call_later(shutdown_delay, _shutdown)
        else:
            _shutdown()

    for sig in signals:
        try:
            loop.add_signal_handler(sig, _handler, sig)
        except (NotImplementedError, RuntimeError):
            # Windows or non-main-thread loops: fall back to signal.signal.
            try:
                signal.signal(sig, lambda s, f, _h=_handler: _h(s))  # type: ignore[misc]
            except (ValueError, OSError):  # pragma: no cover - platform-dep
                logger.warn(
                    f"Could not install handler for {signal.Signals(sig).name}",
                    context="Shutdown",
                )
    return event


async def wait_for_shutdown(event: asyncio.Event) -> None:
    """Convenience: ``await wait_for_shutdown(event)`` to block until set."""
    await event.wait()


__all__ = [
    # Lifecycle state
    "AppLifecycleState",
    # Lifecycle event types
    "LifecycleEvent",
    "LifecycleStateChanged",
    "HealthChanged",
    "StartupBegin",
    "StartupComplete",
    "RequestReceived",
    "RequestComplete",
    "ShutdownBegin",
    "BackgroundTaskStarted",
    "BackgroundTaskComplete",
    "BackgroundTaskFailed",
    "EventHandlerFailed",
    # Bus
    "SignalBus",
    "on",
    "get_default_bus",
    # OS signals (legacy)
    "DEFAULT_SIGNALS",
    "install_signal_handlers",
    "wait_for_shutdown",
]
