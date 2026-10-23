"""The per-app ``EventBus``, the request-scoped ``EventPublisher`` and their factory hooks.

``LaurenFactory.create`` calls :func:`install_events` in Phase 2 (one bus per
app, registered as a global value provider next to the request-scoped
``EventPublisher`` factory), :func:`compile_events` right after the Phase 6a
discovery pass (handler validation, the immutable dispatch table) and
binds the bus to the app in Phase 7. ``LaurenApp.startup`` / ``shutdown``
drive the bus through its lifecycle states: deferred work emitted during
startup waits for READY, tracked tasks are drained (then cancelled) during
shutdown, and emitting after shutdown raises.

Dispatch never reflects: an emission looks the event class up in the
dispatch table (an ``__mro__`` walk, cached per class) and runs each
:class:`~lauren._events._plan.HandlerPlan` with precompiled parameter
lists. Inline handlers resolve dependencies in the publisher's scope — the
request (or WebSocket connection) for an ``EventPublisher``, otherwise one
``OperationScope`` per emission; each deferred handler run gets its own
``OperationScope``.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import functools
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextvars import ContextVar
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from .._di.custom import OptionalDep
from ..background import _BG_TASKS_ATTR, BackgroundTasks
from ..decorators import module
from ..exceptions import (
    DecoratorUsageError,
    LifecycleViolationError,
    MissingProviderError,
    StartupError,
)
from ..logging import Logger, LogLevel, LogRecord, NullLogger
from ..signals import EventHandlerFailed, SignalBus
from ..types import Request, Scope
from ._meta import BACKGROUND, INLINE, ON_EVENT_META, PROPAGATE
from ._plan import DispatchTable, HandlerPlan, compile_handler_plans
from ._types import EmitResult, EventContext, EventRecording, TransactionPhase, UnitOfWork, _unwrap_function

if TYPE_CHECKING:
    from .._asgi import LaurenApp
    from .._di import DIContainer
    from .._discovery import DiscoveryIndex
    from ..websockets import WebSocket

#: Attribute ``LaurenApp.handle`` sets on each ``Request``: the arena lease
#: holding the request's DI cache and framework values. ``Request.reset``
#: clears it with the rest of ``__dict__`` when the object is reused.
LEASE_ATTR = "_lauren_lease"
#: Attribute the WebSocket runtime sets on each connection's ``WebSocket``:
#: the connection's request-scoped DI cache.
WS_CACHE_ATTR = "_lauren_request_cache"

#: Event classes being delivered in the current task (outermost first), for
#: ``EventContext.depth`` and the re-entrancy guard. A ``ContextVar`` holds
#: per-task values, not shared state: tasks spawned by a handler inherit a
#: copy, and two apps never see each other's chains.
_CHAIN: ContextVar[tuple[type, ...]] = ContextVar("lauren_event_chain", default=())

_CREATED = "created"  # built, startup() not called: dispatch freely (lazy DI, like requests)
_STARTING = "starting"  # @post_construct hooks running: inline only on built singletons
_READY = "ready"
_DRAINING = "draining"  # shutdown: emission works, deferred work meets the drain deadline
_STOPPED = "stopped"  # emit() raises

DEFAULT_MAX_DEPTH = 16


def _check_options(max_depth: Any, drain_timeout: Any, *, where: str) -> None:
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 0:
        raise DecoratorUsageError(
            f"{where}(max_depth={max_depth!r}) must be an int >= 0",
            detail={"helper": where, "max_depth": repr(max_depth)},
        )
    if drain_timeout is not None and (
        not isinstance(drain_timeout, (int, float)) or isinstance(drain_timeout, bool) or drain_timeout < 0
    ):
        raise DecoratorUsageError(
            f"{where}(drain_timeout={drain_timeout!r}) must be None or a number of seconds >= 0",
            detail={"helper": where, "drain_timeout": repr(drain_timeout)},
        )


class _Frame:
    """Where an emission's inline handlers resolve dependencies.

    ``cache`` is the request-scoped instance cache (``None`` outside any
    scope), ``values`` the framework values; ``request`` is set for HTTP
    requests, whose background handlers go on the response's
    ``BackgroundTasks`` queue.
    """

    __slots__ = ("cache", "values", "in_request", "request")

    def __init__(
        self,
        cache: dict[Any, Any] | None,
        values: dict[Any, Any],
        *,
        in_request: bool = False,
        request: Request | None = None,
    ) -> None:
        self.cache = cache
        self.values = values
        self.in_request = in_request
        self.request = request


@dataclass(frozen=True, slots=True)
class _Deferred:
    """A deferred handler call waiting for (or running in) its task."""

    plan: HandlerPlan
    event: Any
    ctx: EventContext
    chain: tuple[type, ...]


class _EventLogger:
    """The app logger with a default ``context`` (``EventContext.log``)."""

    __slots__ = ("_inner", "_context", "level")

    def __init__(self, inner: Logger, context: str) -> None:
        self._inner = inner
        self._context = context
        self.level: LogLevel = getattr(inner, "level", LogLevel.INFO)

    def log_record(self, record: LogRecord) -> None:
        self._inner.log_record(record)

    def log(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.log(message, context=context or self._context, **extra)

    def info(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.info(message, context=context or self._context, **extra)

    def debug(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.debug(message, context=context or self._context, **extra)

    def verbose(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.verbose(message, context=context or self._context, **extra)

    def warn(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.warn(message, context=context or self._context, **extra)

    def warning(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.warning(message, context=context or self._context, **extra)

    def error(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._inner.error(message, context=context or self._context, **extra)


class EventBus:
    """The application's event bus: one per app, injectable everywhere as a singleton.

    ``emit()`` delivers an event to every ``@on_event`` handler subscribed
    to its class or a base class. Inline handlers run first, by priority,
    inside the call — sharing one ``OperationScope`` per emission for their
    request-scoped dependencies; then deferred (``"concurrent"`` /
    ``"background"``) handlers start as tracked tasks, each in its own
    ``OperationScope``. Use :class:`EventPublisher` instead to run inline
    handlers in the current request's scope.

    ``recording()``, ``drain()`` and ``disabled()`` are testing aids. A bus
    built directly (``EventBus()``) has no handlers, which makes it a
    convenient stand-in when unit-testing a service that publishes.
    """

    __slots__ = (
        "_max_depth",
        "_drain_timeout",
        "_logger",
        "_app",
        "_container",
        "_signals",
        "_table",
        "_state",
        "_tasks",
        "_pending",
        "_recordings",
        "_muted",
        "_loggers",
        "_unscoped",
    )

    def __init__(
        self,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        drain_timeout: float | None = None,
        logger: Logger | None = None,
    ) -> None:
        _check_options(max_depth, drain_timeout, where="EventBus")
        self._max_depth = max_depth
        self._drain_timeout = drain_timeout
        self._logger: Logger = logger or NullLogger()
        self._app: LaurenApp | None = None
        self._container: DIContainer | None = None
        self._signals: SignalBus | None = None
        self._table = DispatchTable()
        self._state = _CREATED
        self._tasks: set[asyncio.Task[Any]] = set()
        self._pending: list[_Deferred] = []
        self._recordings: list[EventRecording] = []
        self._muted: dict[int, int] = {}
        self._loggers: dict[type, Logger] = {}
        self._unscoped = _Frame(None, {})

    # -- Publishing ----------------------------------------------------------

    async def emit(self, event: object, *, wait: bool = False) -> EmitResult:
        """Deliver ``event`` to its handlers and return what happened.

        Inline handlers run sequentially by priority (descending), then
        discovery order. A ``"propagate"`` handler that raises stops the
        remaining inline handlers, schedules no deferred handler and
        re-raises here; an ``"isolate"`` failure is logged, signalled and
        listed in :attr:`EmitResult.errors`. After the inline handlers,
        deferred handlers start as tracked tasks; ``wait=True`` also awaits
        them. Raises :class:`~lauren.exceptions.LifecycleViolationError`
        after the app has shut down and :class:`RecursionError` when
        handlers re-emit deeper than ``max_depth``.
        """
        return await self._emit(event, None, wait)

    # -- Testing aids --------------------------------------------------------

    @contextlib.contextmanager
    def recording(self) -> Iterator[EventRecording]:
        """Capture emitted events and handler outcomes while the block runs.

        Recordings are per bus (per app) and may nest. Deferred handlers are
        recorded when they finish, so ``await bus.drain()`` inside the block
        before asserting on them.
        """
        recording = EventRecording()
        self._recordings.append(recording)
        try:
            yield recording
        finally:
            self._recordings.remove(recording)

    @contextlib.contextmanager
    def disabled(self, *handlers: str | Callable[..., Any]) -> Iterator[None]:
        """Mute handlers while the block runs: ``with bus.disabled(Mailer.send_welcome):``.

        Name each handler by its method (``Class.method`` attribute) or as a
        ``"Class.method"`` string. Raises ``ValueError`` for anything that is
        not a discovered handler of this app.
        """
        if not handlers:
            raise ValueError(
                "bus.disabled() needs at least one handler, e.g. bus.disabled(Mailer.send_welcome)"
            )
        keys = [key for handler in handlers for key in self._match_handler(handler)]
        for key in keys:
            self._muted[key] = self._muted.get(key, 0) + 1
        try:
            yield
        finally:
            for key in keys:
                remaining = self._muted[key] - 1
                if remaining:
                    self._muted[key] = remaining
                else:
                    del self._muted[key]

    async def drain(self, timeout: float | None = None) -> None:
        """Wait until no deferred handler task is running (tasks they spawn included).

        Raises :class:`TimeoutError` if tasks are still running after
        ``timeout`` seconds; they keep running.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self._tasks:
            remaining = None if deadline is None else max(0.0, deadline - loop.time())
            _done, pending = await asyncio.wait(tuple(self._tasks), timeout=remaining)
            if pending:
                raise TimeoutError(f"{len(pending)} event handler task(s) still running after {timeout}s")

    def handlers_for(self, event_type: type) -> tuple[str, ...]:
        """``"Class.method"`` of every handler an instance of ``event_type`` reaches, in dispatch order."""
        if not isinstance(event_type, type):
            raise TypeError(f"handlers_for() takes an event class, got {event_type!r}")
        return tuple(plan.name for plan in self._table.lookup(event_type))

    def __repr__(self) -> str:
        return (
            f"<EventBus {len(self._table.plans)} subscription(s), {self._state}, "
            f"{len(self._tasks)} running task(s)>"
        )

    # -- Dispatch ------------------------------------------------------------

    async def _emit(self, event: Any, frame: _Frame | None, wait: bool) -> EmitResult:
        if isinstance(event, type):
            raise TypeError(
                f"emit() takes an event instance, got the class {event.__name__}; write emit({event.__name__}(...))"
            )
        event_type = type(event)
        if self._state == _STOPPED:
            raise LifecycleViolationError(
                f"cannot emit {event_type.__name__}: the application has shut down",
                detail={"event_type": event_type.__name__, "reason": "app_stopped"},
            )
        chain = _CHAIN.get()
        if len(chain) > self._max_depth:
            path = " -> ".join(t.__name__ for t in (*chain, event_type))
            raise RecursionError(
                f"event re-entrancy depth {len(chain)} exceeds max_depth={self._max_depth}: {path} "
                "(handlers keep emitting events; raise the limit with EventsModule.for_root(max_depth=...) "
                "only if the chain is intended)"
            )
        for recording in self._recordings:
            recording.events.append(event)
        handlers = self._table.lookup(event_type)
        if self._muted and handlers:
            handlers = tuple(h for h in handlers if id(h.entry) not in self._muted)
        if not handlers:
            return EmitResult(event)
        inline = tuple(h for h in handlers if h.mode == INLINE)
        deferred = tuple(h for h in handlers if h.mode != INLINE)
        if self._state == _STARTING:
            self._check_startup(event_type, inline, wait=wait and bool(deferred))
        ctx = EventContext(
            event_id=uuid.uuid4().hex,
            emitted_at=datetime.now(timezone.utc),
            depth=len(chain),
            mode=INLINE,
            in_request=frame is not None and frame.in_request,
            log=self._log_for(event_type),
        )
        nested = (*chain, event_type)
        token = _CHAIN.set(nested)
        try:
            handled, errors = await self._run_inline(inline, event, ctx, frame) if inline else (0, [])
            spawned = self._schedule(deferred, event, ctx, frame, nested)
        finally:
            _CHAIN.reset(token)
        if wait and spawned:
            ok, failures = await self._await_deferred(spawned)
            handled += ok
            errors.extend(failures)
        return EmitResult(event, handled, len(deferred), tuple(errors))

    def _check_startup(self, event_type: type, inline: tuple[HandlerPlan, ...], *, wait: bool) -> None:
        """Before READY: inline handlers only on singletons whose ``@post_construct`` ran."""
        initialized = self._container._singletons_initialized if self._container is not None else set()
        for plan in inline:
            provider = plan.entry.provider
            if plan.needs_instance and (provider.scope != Scope.SINGLETON or provider.cls not in initialized):
                raise LifecycleViolationError(
                    f"cannot deliver {event_type.__name__} to {plan.name} during startup: "
                    f"{getattr(provider.cls, '__name__', provider.cls)} is not constructed yet. Emit after "
                    "startup (or from a @post_construct hook that runs later), or make the handler deferred "
                    "(mode='concurrent'), which runs once the app is ready",
                    detail={
                        "event_type": event_type.__name__,
                        "handler": plan.name,
                        "reason": "app_not_ready",
                    },
                )
        if wait:
            raise LifecycleViolationError(
                f"emit({event_type.__name__}, wait=True) during startup: deferred handlers only start once "
                "the app is ready, so there is nothing to wait for yet",
                detail={"event_type": event_type.__name__, "reason": "app_not_ready"},
            )

    async def _run_inline(
        self,
        handlers: tuple[HandlerPlan, ...],
        event: Any,
        ctx: EventContext,
        frame: _Frame | None,
    ) -> tuple[int, list[EventHandlerFailed]]:
        if frame is None:
            if any(h.needs_scope for h in handlers):
                async with self._operation_scope(type(event)) as scoped:
                    return await self._call_inline(handlers, event, ctx, scoped)
            frame = self._unscoped
        return await self._call_inline(handlers, event, ctx, frame)

    async def _call_inline(
        self,
        handlers: tuple[HandlerPlan, ...],
        event: Any,
        ctx: EventContext,
        frame: _Frame,
    ) -> tuple[int, list[EventHandlerFailed]]:
        handled = 0
        errors: list[EventHandlerFailed] = []
        for plan in handlers:
            try:
                await self._call(plan, event, ctx, frame)
            except Exception as exc:
                failure = self._failure(plan, event, exc)
                if plan.errors == PROPAGATE:
                    # The publisher sees the exception; do not log it twice.
                    await self._report(plan, failure, log=False)
                    raise
                await self._report(plan, failure, log=True)
                errors.append(failure)
            else:
                handled += 1
                self._record(plan, None)
        return handled, errors

    async def _call(self, plan: HandlerPlan, event: Any, ctx: EventContext, frame: _Frame) -> None:
        container = self._container
        if container is None:  # pragma: no cover - plans only exist on a compiled bus
            raise LifecycleViolationError("EventBus is not attached to an application")
        instance = None
        if plan.needs_instance:
            instance = await container._instantiate(plan.entry.provider, frame.cache, frame.values)
        kwargs: dict[str, Any] = {plan.event_param: event}
        for name in plan.context_params:
            kwargs[name] = ctx
        for name, token in plan.di_params:
            kwargs[name] = await container.resolve(
                token,
                request_cache=frame.cache,
                framework_values=frame.values,
                owning_module=plan.owning_module,
            )
        await plan.entry.plan.call(plan.entry.bind(instance), kwargs)

    @contextlib.asynccontextmanager
    async def _operation_scope(self, event_type: type) -> AsyncIterator[_Frame]:
        """A fresh ``OperationScope`` (``app.scope()``) for one emission or deferred call.

        Its ``_Frame`` is seeded as a value, so an ``EventPublisher`` resolved
        inside a handler joins the same scope.
        """
        app = self._app
        if app is None:  # pragma: no cover - plans only exist on a bound bus
            raise LifecycleViolationError("EventBus is not attached to an application")
        values: dict[Any, Any] = {}
        frame = _Frame(None, values)
        values[_Frame] = frame
        async with app.scope(f"event:{event_type.__name__}", values=values) as scope:
            # Resolve in the scope's own instance cache, so its request-scoped
            # instances are torn down (finalize_scope) when the scope exits.
            frame.cache = scope._cache
            yield frame

    def _schedule(
        self,
        handlers: tuple[HandlerPlan, ...],
        event: Any,
        ctx: EventContext,
        frame: _Frame | None,
        chain: tuple[type, ...],
    ) -> list[tuple[_Deferred, asyncio.Task[Any]]]:
        """Queue or start the deferred handlers; return the tasks started."""
        spawned: list[tuple[_Deferred, asyncio.Task[Any]]] = []
        contexts: dict[str, EventContext] = {}
        for plan in handlers:
            mode_ctx = contexts.get(plan.mode)
            if mode_ctx is None:
                mode_ctx = contexts[plan.mode] = replace(ctx, mode=plan.mode)
            job = _Deferred(plan, event, mode_ctx, chain)
            if plan.mode == BACKGROUND and frame is not None and frame.request is not None:
                # After the response: the request's own queue, as BackgroundTasks does.
                _background_tasks(frame.request).add_task(self._run_deferred, job)
            elif self._state == _STARTING:
                self._pending.append(job)  # released at READY
            else:
                spawned.append((job, self._spawn(job)))
        return spawned

    def _spawn(self, job: _Deferred) -> asyncio.Task[Any]:
        task = asyncio.get_running_loop().create_task(
            self._run_deferred(job, tracked=True), name=f"lauren-event:{job.plan.name}"
        )
        self._tasks.add(task)
        task.add_done_callback(functools.partial(self._task_done, job))
        return task

    def _task_done(self, job: _Deferred, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if task.cancelled():  # also covers a task cancelled before it ever ran
            self._report_cancelled(job.plan, job.event, asyncio.CancelledError())

    async def _run_deferred(self, job: _Deferred, *, tracked: bool = False) -> EventHandlerFailed | None:
        """Run one deferred handler; failures are reported, never raised (cancellation is).

        ``tracked`` runs report cancellation from their task's done callback;
        a run on a request's ``BackgroundTasks`` queue reports it here.
        """
        plan = job.plan
        token = _CHAIN.set(job.chain)
        try:
            if plan.needs_scope:
                async with self._operation_scope(type(job.event)) as frame:
                    await self._call(plan, job.event, job.ctx, frame)
            else:
                await self._call(plan, job.event, job.ctx, self._unscoped)
        except asyncio.CancelledError as exc:
            if not tracked:
                self._report_cancelled(plan, job.event, exc)
            raise
        except Exception as exc:
            failure = self._failure(plan, job.event, exc)
            await self._report(plan, failure, log=True)
            return failure
        else:
            self._record(plan, None)
            return None
        finally:
            _CHAIN.reset(token)

    async def _await_deferred(
        self, spawned: list[tuple[_Deferred, asyncio.Task[Any]]]
    ) -> tuple[int, list[EventHandlerFailed]]:
        """``wait=True``: await the tasks this emission started (without cancelling them on our own cancellation)."""
        await asyncio.wait([task for _job, task in spawned])
        ok = 0
        failures: list[EventHandlerFailed] = []
        for job, task in spawned:
            if task.cancelled():
                failures.append(self._failure(job.plan, job.event, asyncio.CancelledError()))
                continue
            result = task.result()
            if result is None:
                ok += 1
            else:
                failures.append(result)
        return ok, failures

    # -- Reporting -------------------------------------------------------------

    def _failure(self, plan: HandlerPlan, event: Any, exc: BaseException) -> EventHandlerFailed:
        return EventHandlerFailed(
            event_type=type(event), handler=plan.name, error=exc, mode=plan.mode, event=event
        )

    def _record(self, plan: HandlerPlan, failure: EventHandlerFailed | None) -> None:
        for recording in self._recordings:
            recording._record(plan.name, plan.entry.function, failure)

    async def _report(self, plan: HandlerPlan, failure: EventHandlerFailed, *, log: bool) -> None:
        self._record(plan, failure)
        if log:
            self._log_failure(plan, failure)
        if self._signals is not None:
            await self._signals.emit(failure)

    def _report_cancelled(self, plan: HandlerPlan, event: Any, exc: BaseException) -> None:
        """Report a cancelled deferred handler without awaiting (we are being cancelled)."""
        failure = self._failure(plan, event, exc)
        self._record(plan, failure)
        self._logger.warn(
            f"event handler {plan.name} was cancelled while handling {type(event).__name__}",
            context="EventBus",
            handler=plan.name,
            event=type(event).__name__,
            mode=plan.mode,
        )
        if self._signals is not None:
            self._signals.emit_sync(failure)

    def _log_failure(self, plan: HandlerPlan, failure: EventHandlerFailed) -> None:
        error = failure.error
        self._logger.error(
            f"event handler {plan.name} failed on {failure.event_type.__name__}: {type(error).__name__}: {error}",
            context="EventBus",
            handler=plan.name,
            event=failure.event_type.__name__,
            mode=plan.mode,
            error=type(error).__name__,
        )

    def _log_for(self, event_type: type) -> Logger:
        log = self._loggers.get(event_type)
        if log is None:
            log = self._loggers[event_type] = _EventLogger(self._logger, f"Event:{event_type.__name__}")
        return log

    def _match_handler(self, handler: str | Callable[..., Any]) -> list[int]:
        if isinstance(handler, str):
            keys = {id(plan.entry) for plan in self._table.plans if plan.name == handler}
        else:
            target = _unwrap_function(handler)
            keys = {id(plan.entry) for plan in self._table.plans if plan.entry.function is target}
        if not keys:
            raise ValueError(f"{handler!r} is not an enabled @on_event handler of this application")
        return sorted(keys)

    # -- Framework hooks (LaurenFactory.create / LaurenApp) --------------------

    def _install(self, table: DispatchTable, container: DIContainer) -> None:
        self._table = table
        self._container = container

    def _bind(self, app: LaurenApp) -> None:
        self._app = app
        self._container = app.container
        self._signals = app.signals
        self._logger = app.logger
        self._loggers.clear()

    def _on_startup(self) -> None:
        """``startup()`` began: until READY, deferred work queues and inline handlers need built singletons."""
        if self._state == _CREATED:
            self._state = _STARTING

    def _on_ready(self) -> None:
        """READY: start the deferred work queued during startup."""
        if self._state in (_CREATED, _STARTING):
            self._state = _READY
            pending, self._pending = self._pending, []
            for job in pending:
                self._spawn(job)

    async def _on_drain(self, timeout: float) -> None:
        """Shutdown, after the request drain: await tracked tasks, then cancel the rest."""
        if self._state == _STOPPED:
            return
        self._state = _DRAINING
        if not self._tasks:
            return
        budget = self._drain_timeout if self._drain_timeout is not None else timeout
        self._logger.log(
            f"Draining {len(self._tasks)} event handler task(s)",
            context="Shutdown",
            tasks=len(self._tasks),
            drain_timeout=budget,
        )
        try:
            await self.drain(budget)
        except TimeoutError:
            self._logger.warn(
                f"Event handler drain timeout after {budget}s; cancelling {len(self._tasks)} task(s)",
                context="Shutdown",
                tasks=len(self._tasks),
            )
            await self._cancel_tasks()

    async def _on_stopped(self) -> None:
        """End of shutdown: cancel stragglers; later emissions raise."""
        if self._state == _STOPPED:
            return
        self._state = _STOPPED
        pending, self._pending = self._pending, []
        for job in pending:  # startup never reached READY
            self._report_cancelled(job.plan, job.event, asyncio.CancelledError())
        await self._cancel_tasks()

    async def _cancel_tasks(self) -> None:
        tasks = tuple(self._tasks)
        if not tasks:
            return
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)


class EventPublisher:
    """The request-scoped publisher: inline handlers join the current request's DI scope.

    Inject it (``events: EventPublisher``) into a route handler, a
    WebSocket hook, a request-scoped provider or an ``@on_event`` handler —
    it is provided by the framework. ``emit()`` behaves like
    :meth:`EventBus.emit`, except that inline handlers resolve
    ``Scope.REQUEST`` dependencies from the publisher's scope (the same
    ``DbSession`` as the controller), and ``"background"`` handlers in an
    HTTP request run after the response is sent, on the request's
    ``BackgroundTasks`` queue, each in a fresh ``OperationScope``. In a
    WebSocket connection there is no response, so background handlers run
    like concurrent ones.

    Resolving it anywhere else (a singleton, a job, ``app.scope()``) raises
    :class:`~lauren.exceptions.LifecycleViolationError` — inject
    :class:`EventBus` there.
    """

    __slots__ = ("_bus", "_frame", "_loop")

    def __init__(self, bus: EventBus, frame: _Frame) -> None:
        self._bus = bus
        self._frame = frame
        try:
            self._loop: asyncio.AbstractEventLoop | None = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = None

    @property
    def in_request(self) -> bool:
        """``True`` inside an HTTP request or a WebSocket connection."""
        return self._frame.in_request

    async def emit(
        self, event: object, *, wait: bool = False, after: TransactionPhase | None = None
    ) -> EmitResult:
        """Deliver ``event``; see :meth:`EventBus.emit`.

        ``after=TransactionPhase.COMMIT`` defers the whole emission until the
        active :class:`~lauren.events.UnitOfWork` (resolved from this scope)
        commits and drops it on rollback; the call then returns an empty
        :class:`EmitResult`, and the emission is later delivered like
        :meth:`EventBus.emit`. Without a ``UnitOfWork`` provider it raises
        :class:`~lauren.exceptions.LifecycleViolationError`.
        """
        if after is not None:
            return await self._emit_after(event, after)
        return await self._bus._emit(event, self._frame, wait)

    def emit_threadsafe(self, event: object) -> concurrent.futures.Future[EmitResult]:
        """Schedule :meth:`emit` from a worker thread (a sync handler) onto the app's event loop.

        Returns a :class:`concurrent.futures.Future`; call ``.result()`` to
        wait for the inline handlers (and to see their exceptions) before the
        sync handler returns — the request scope ends when it does.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise LifecycleViolationError(
                "EventPublisher.emit_threadsafe() needs the event loop the publisher was created on",
                detail={"reason": "no_event_loop"},
            )
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            raise RuntimeError(
                "emit_threadsafe() is for sync code running in a worker thread; on the event loop, "
                "write `await publisher.emit(event)`"
            )
        return asyncio.run_coroutine_threadsafe(self.emit(event), loop)

    async def _emit_after(self, event: Any, after: TransactionPhase) -> EmitResult:
        if after is not TransactionPhase.COMMIT:
            raise ValueError(f"after= expects TransactionPhase.COMMIT, got {after!r}")
        bus = self._bus
        event_type = type(event)
        if isinstance(event, type):
            raise TypeError(f"emit() takes an event instance, got the class {event.__name__}")
        if bus._state == _STOPPED:
            raise LifecycleViolationError(
                f"cannot emit {event_type.__name__}: the application has shut down",
                detail={"event_type": event_type.__name__, "reason": "app_stopped"},
            )
        unit: UnitOfWork | None = None
        if bus._container is not None:
            with contextlib.suppress(MissingProviderError):
                unit = await bus._container.resolve(
                    UnitOfWork, request_cache=self._frame.cache, framework_values=self._frame.values
                )
        if unit is None:
            raise LifecycleViolationError(
                f"emit({event_type.__name__}, after=TransactionPhase.COMMIT) needs a UnitOfWork in this "
                "scope; register a provider implementing on_commit() / on_rollback() (e.g. your request-scoped "
                "database session)",
                detail={"event_type": event_type.__name__, "reason": "no_unit_of_work"},
            )
        chain = _CHAIN.get()

        async def deliver() -> None:
            token = _CHAIN.set(chain)
            try:
                await bus._emit(event, None, False)
            finally:
                _CHAIN.reset(token)

        async def drop() -> None:
            bus._logger.verbose(
                f"dropped {event_type.__name__} emitted after commit: the unit of work rolled back",
                context="EventBus",
                event=event_type.__name__,
            )

        unit.on_commit(deliver)
        unit.on_rollback(drop)
        return EmitResult(event)


def _background_tasks(request: Request) -> BackgroundTasks:
    """The request's ``BackgroundTasks`` queue, created on first use like ``_invoke_handler`` does."""
    tasks: BackgroundTasks | None = request.state.get(_BG_TASKS_ATTR)
    if tasks is None:
        tasks = BackgroundTasks()
        setattr(request.state, _BG_TASKS_ATTR, tasks)
    return tasks


def _make_publisher(
    bus: EventBus,
    request: Request | None,
    websocket: WebSocket | None,
    frame: _Frame | None,
) -> EventPublisher:
    """The ``EventPublisher`` factory: join the scope the provider is being resolved in."""
    if frame is not None:  # an operation scope opened by the bus (inside an @on_event handler)
        return EventPublisher(bus, frame)
    if isinstance(request, Request):
        lease = request.__dict__.get(LEASE_ATTR)
        if lease is not None:
            return EventPublisher(
                bus, _Frame(lease.request_cache, lease.framework_values, in_request=True, request=request)
            )
    elif request is not None and websocket is None:
        # WebSocket hook parameters resolve through a Request-like adapter.
        websocket = getattr(request, "_ws", None)
    if websocket is not None:
        cache = getattr(websocket, "__dict__", {}).get(WS_CACHE_ATTR)
        if cache is not None:
            from ..websockets import WebSocket

            values: dict[Any, Any] = {WebSocket: websocket, type(websocket): websocket}
            return EventPublisher(bus, _Frame(cache, values, in_request=True))
    raise LifecycleViolationError(
        "EventPublisher is only available inside an HTTP request, a WebSocket connection or an @on_event "
        "handler; inject EventBus in singletons, jobs, commands and app.scope() blocks",
        detail={"reason": "no_request_scope"},
    )


# ---------------------------------------------------------------------------
# EventsModule (optional tuning)
# ---------------------------------------------------------------------------

EVENTS_OPTIONS_ATTR = "__lauren_events_options__"


@dataclass(frozen=True, slots=True)
class _EventsOptions:
    max_depth: int = DEFAULT_MAX_DEPTH
    drain_timeout: float | None = None


class EventsModule:
    """Optional tuning for the application event bus; events work without it.

    Import the module ``for_root`` returns into any module of the app.
    """

    @classmethod
    def for_root(cls, *, max_depth: int = DEFAULT_MAX_DEPTH, drain_timeout: float | None = None) -> type:
        """Return a module that configures the app's :class:`EventBus`.

        ``max_depth`` caps how deeply handlers may re-emit (a deeper chain
        raises :class:`RecursionError`). ``drain_timeout`` is how long
        shutdown waits for running deferred handlers before cancelling them;
        ``None`` uses what is left of ``shutdown(drain_timeout=...)`` after
        in-flight requests drained.
        """
        _check_options(max_depth, drain_timeout, where="EventsModule.for_root")
        namespace = {
            "__doc__": "Generated EventsModule (event bus options).",
            "__module__": __name__,
            EVENTS_OPTIONS_ATTR: _EventsOptions(max_depth, drain_timeout),
        }
        generated: type = module()(type("EventsModule[root]", (), namespace))
        return generated


def _events_options(graph: Any) -> _EventsOptions:
    found: list[_EventsOptions] = []
    for mod in graph.modules:
        options = mod.__dict__.get(EVENTS_OPTIONS_ATTR)
        if options is not None and options not in found:
            found.append(options)
    if len(found) > 1:
        raise StartupError(
            "EventsModule.for_root() is imported with different options "
            f"({', '.join(repr(o) for o in found)}); configure the event bus once",
            detail={"reason": "conflicting_events_options", "options": [repr(o) for o in found]},
        )
    return found[0] if found else _EventsOptions()


# ---------------------------------------------------------------------------
# Factory hooks
# ---------------------------------------------------------------------------


def install_events(graph: Any, container: DIContainer) -> EventBus:
    """Phase 2: build the app's bus and register ``EventBus`` / ``EventPublisher`` globally.

    A binding the application registered itself for either token wins.
    """
    from ..websockets import WebSocket

    options = _events_options(graph)
    bus = EventBus(max_depth=options.max_depth, drain_timeout=options.drain_timeout)
    if not container.has_provider(EventBus):
        container.register_value(EventBus, bus)
    if not container.has_provider(EventPublisher):
        container.register_factory(
            EventPublisher,
            _make_publisher,
            inject=(EventBus, OptionalDep(Request), OptionalDep(WebSocket), OptionalDep(_Frame)),
            scope=Scope.REQUEST,
        )
    return bus


def compile_events(
    bus: EventBus, discovery: DiscoveryIndex, container: DIContainer, *, logger: Logger
) -> None:
    """Phase 6a: validate every ``@on_event`` handler and install the dispatch table."""
    table = DispatchTable(compile_handler_plans(discovery.methods_for(ON_EVENT_META)))
    bus._install(table, container)
    if table.plans:
        logger.verbose(
            f"Event handlers compiled: {len(table.plans)} subscription(s) on "
            f"{len(table.subscribed_types)} event class(es)",
            context="EventBus",
        )
        for event_type in table.subscribed_types:
            logger.verbose(
                f"{event_type.__name__} -> "
                + ", ".join(f"{p.name} [{p.mode}, priority={p.priority}]" for p in table.lookup(event_type)),
                context="EventBus",
            )


__all__ = [
    "EventBus",
    "EventPublisher",
    "EventsModule",
    "compile_events",
    "install_events",
]
