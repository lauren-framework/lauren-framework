"""``JobContext`` and ``ServiceContext`` (private module, public through ``lauren.scheduling``)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from ..logging import Logger, LogLevel, LogRecord
from ._clock import Clock, wait_event_or_sleep

if TYPE_CHECKING:
    from ..context import OperationScope


class _BoundLogger:
    """The app logger with a default ``context=`` and extra fields bound."""

    __slots__ = ("_logger", "_context", "_extra")

    def __init__(self, logger: Logger, context: str, **extra: Any) -> None:
        self._logger = logger
        self._context = context
        self._extra = extra

    @property
    def level(self) -> LogLevel:
        return self._logger.level

    def log_record(self, record: LogRecord) -> None:
        self._logger.log_record(record)

    def _emit(self, method: str, message: str, context: str, extra: Mapping[str, Any]) -> None:
        getattr(self._logger, method)(message, context=context or self._context, **{**self._extra, **extra})

    def log(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("log", message, context, extra)

    def info(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("log", message, context, extra)

    def debug(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("debug", message, context, extra)

    def verbose(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("verbose", message, context, extra)

    def warn(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("warn", message, context, extra)

    def warning(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("warn", message, context, extra)

    def error(self, message: str, *, context: str = "", **extra: Any) -> None:
        self._emit("error", message, context, extra)


def bound_logger(logger: Logger, context: str, **extra: Any) -> Logger:
    """``logger`` with ``context=`` defaulted to ``context`` and ``extra`` added to every record."""
    return cast(Logger, _BoundLogger(logger, context, **extra))


class JobContext:
    """What a scheduled run knows about itself; declare a ``JobContext`` parameter to receive it.

    * ``name`` — the job name; ``attempt`` is always ``1`` (retries belong to a
      durable job queue, not the scheduler).
    * ``scheduled_at`` — the nominal fire time, an aware datetime in the job's
      time zone; ``fired_at`` — when the run actually started.
    * ``log`` — the app logger bound with ``context="Job:<name>"``.
    * ``cancelled`` — becomes ``True`` when the run times out or shutdown
      cancels it (sync jobs, which cannot be interrupted, should poll it).
    * ``scope`` — the run's :class:`~lauren.context.OperationScope` when the
      method takes dependency-injected parameters, else ``None``.

    A ``Scope.REQUEST`` provider constructed during the run may also declare a
    ``JobContext`` constructor parameter.
    """

    __slots__ = ("_name", "_scheduled_at", "_fired_at", "_log", "_cancelled", "_scope")

    def __init__(
        self,
        *,
        name: str,
        scheduled_at: datetime,
        fired_at: datetime,
        log: Logger,
    ) -> None:
        self._name = name
        self._scheduled_at = scheduled_at
        self._fired_at = fired_at
        self._log = log
        self._cancelled = False
        self._scope: OperationScope | None = None

    @property
    def name(self) -> str:
        return self._name

    @property
    def scheduled_at(self) -> datetime:
        return self._scheduled_at

    @property
    def fired_at(self) -> datetime:
        return self._fired_at

    @property
    def attempt(self) -> int:
        return 1

    @property
    def log(self) -> Logger:
        return self._log

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def scope(self) -> OperationScope | None:
        return self._scope

    def __repr__(self) -> str:
        return f"<JobContext {self._name!r} scheduled_at={self._scheduled_at.isoformat()}>"


class ServiceContext:
    """The handle a hosted service's ``run(ctx)`` receives.

    * ``stop_requested`` — ``True`` once shutdown asked the service to stop
      (thread-safe, so a sync ``run`` can poll it).
    * ``await sleep(seconds)`` — sleeps on the scheduler clock; returns
      ``False`` early when stop is requested, ``True`` otherwise.
    * ``await wait_stopped(timeout=None)`` — waits for the stop request;
      ``True`` when it arrived, ``False`` when ``timeout`` ran out.
    * ``mark_ready()`` — signals readiness (``@hosted_service(wait_ready=True)``
      makes startup wait for it); ``ready`` reads it back. Readiness resets
      when the service restarts.
    * ``name``, ``restart_count`` and ``log`` (the app logger bound with
      ``context="Service:<name>"``).
    * ``scope(name=None)`` — a new :class:`~lauren.context.OperationScope` for
      one iteration's ``Scope.REQUEST`` dependencies (the context is seeded
      into it, so such providers may take a ``ServiceContext`` parameter).
    """

    __slots__ = (
        "_name",
        "_clock",
        "_log",
        "_scope_factory",
        "_stop_requested",
        "_stop_event",
        "_ready",
        "_ready_event",
        "_loop",
        "_restart_count",
    )

    def __init__(
        self,
        *,
        name: str,
        clock: Clock,
        log: Logger,
        scope_factory: Callable[..., OperationScope],
    ) -> None:
        self._name = name
        self._clock = clock
        self._log = log
        self._scope_factory = scope_factory
        self._stop_requested = False
        self._stop_event = asyncio.Event()
        self._ready = False
        self._ready_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._restart_count = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def log(self) -> Logger:
        return self._log

    @property
    def restart_count(self) -> int:
        return self._restart_count

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    @property
    def ready(self) -> bool:
        return self._ready

    async def sleep(self, seconds: float) -> bool:
        """Sleep ``seconds``; return ``False`` as soon as stop is requested."""
        stopped = await wait_event_or_sleep(self._clock, self._stop_event, seconds)
        return not stopped

    async def wait_stopped(self, timeout: float | None = None) -> bool:
        """Wait for the stop request; ``False`` if ``timeout`` seconds pass first."""
        return await wait_event_or_sleep(self._clock, self._stop_event, timeout)

    def mark_ready(self) -> None:
        """Signal that the service is ready (safe to call from a sync service's thread)."""
        self._ready = True
        loop = self._loop
        if loop is None:
            self._ready_event.set()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._ready_event.set()
        else:
            try:
                loop.call_soon_threadsafe(self._ready_event.set)
            except RuntimeError:  # the loop is closed; nobody is waiting
                pass

    def scope(self, name: str | None = None) -> OperationScope:
        """Open a new DI scope for one unit of work (use it with ``async with``)."""
        return self._scope_factory(name or f"service:{self._name}", values={ServiceContext: self})

    # -- Supervisor side -------------------------------------------------

    def _begin_attempt(self, restart_count: int, loop: asyncio.AbstractEventLoop) -> None:
        self._restart_count = restart_count
        self._ready = False
        self._ready_event.clear()
        self._loop = loop

    def _request_stop(self) -> None:
        self._stop_requested = True
        self._stop_event.set()

    def __repr__(self) -> str:
        state = "stopping" if self._stop_requested else ("ready" if self._ready else "running")
        return f"<ServiceContext {self._name!r} {state}, restarts={self._restart_count}>"


__all__ = ["JobContext", "ServiceContext", "bound_logger"]
