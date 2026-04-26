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
* :class:`RequestComplete` — the response has been fully sent. Also
  fires on error paths, with the captured exception on the event.
* :class:`ShutdownBegin` — a ``lifespan.shutdown`` message arrived
  or a graceful-shutdown signal was received.

User code registers listeners via :meth:`SignalBus.on` (also
available as :func:`on` for convenience) and the framework emits
events via :meth:`SignalBus.emit` during the request lifecycle.
Listeners may be sync or async; errors in listeners are logged but
never propagate out of ``emit`` — observability must not break the
request path.
"""

from __future__ import annotations

import asyncio
import inspect as _inspect
import signal
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Iterable, TypeVar

from .logging import Logger

if TYPE_CHECKING:
    from ._asgi import LaurenApp


DEFAULT_SIGNALS: tuple[int, ...] = (
    signal.SIGINT,
    signal.SIGTERM,
)


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
    """Fired after the response has been fully sent to the client.

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

    def _log_listener_error(
        self, fn: Listener, event: LifecycleEvent, exc: BaseException
    ) -> None:
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

            _stdlib.getLogger("lauren.signals").exception(
                "signal listener %s failed on %s", name, event_name
            )


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
    loop: asyncio.AbstractEventLoop | None = None,
) -> asyncio.Event:
    """Install async signal handlers that shut the app down gracefully.

    Returns an :class:`asyncio.Event` that fires once a handled signal has
    been received. Callers can ``await event.wait()`` to block until that
    moment (useful in a ``run_forever`` loop).

    The handlers are idempotent \u2014 delivering multiple signals never produces
    a second shutdown; the first wins and subsequent ones are logged.
    """
    loop = loop or asyncio.get_event_loop()
    event = asyncio.Event()
    logger = app.logger

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
        loop.create_task(app.shutdown(drain_timeout=drain_timeout))

    for sig in signals:
        try:
            loop.add_signal_handler(sig, _handler, sig)
        except (NotImplementedError, RuntimeError):
            # Windows or non-main-thread loops: fall back to signal.signal.
            try:
                signal.signal(sig, lambda s, f, _h=_handler: _h(s))
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
    # Lifecycle event types
    "LifecycleEvent",
    "StartupBegin",
    "StartupComplete",
    "RequestReceived",
    "RequestComplete",
    "ShutdownBegin",
    # Bus
    "SignalBus",
    "on",
    "get_default_bus",
    # OS signals (legacy)
    "DEFAULT_SIGNALS",
    "install_signal_handlers",
    "wait_for_shutdown",
]
