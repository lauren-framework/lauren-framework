"""The hosted-service supervisor (private module).

It owns one task per hosted service: starts them in the providers'
topological order after ``@post_construct``, restarts failures with
exponential backoff and full jitter, emits ``HostedService*`` signals,
exposes status (``HostedServiceInfo``) and stops services in reverse start
order during shutdown — before ``@pre_destruct``, so every dependency is
alive for a service's whole graceful stop.
"""

from __future__ import annotations

import asyncio
import functools
import random
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..exceptions import LifecycleError
from ..logging import Logger
from ..signals import HostedServiceFailed, HostedServiceStarted, HostedServiceStopped, SignalBus
from ._clock import Clock
from ._context import ServiceContext, bound_logger
from ._decorators import HostedServiceMeta
from ._threads import run_in_daemon_thread

if TYPE_CHECKING:
    from .._di import Provider
    from ..context import OperationScope

#: Real seconds a cancelled service gets to unwind before it is abandoned.
_CANCEL_GRACE = 1.0


@dataclass(frozen=True, slots=True)
class HostedServiceInfo:
    """A snapshot of one hosted service.

    ``state`` is ``"pending"`` (not started yet, or ``hosted_services=False``),
    ``"disabled"`` (``enabled=False``), ``"running"``, ``"restarting"``
    (waiting out a backoff), ``"stopping"``, ``"stopped"`` (stopped or
    returned) or ``"failed"`` (crashed with no restart left). ``healthy`` is
    ``False`` only for a failed service with ``on_exhausted="fail_readiness"``
    — the signal a readiness probe acts on.
    """

    name: str
    state: str
    ready: bool
    restart_count: int
    last_error: str | None
    healthy: bool

    def as_dict(self) -> dict[str, Any]:
        """A JSON-friendly ``dict``."""
        return {
            "name": self.name,
            "state": self.state,
            "ready": self.ready,
            "restart_count": self.restart_count,
            "last_error": self.last_error,
            "healthy": self.healthy,
        }


class ServiceRunner:
    """One hosted service: its options, instance, task and state."""

    __slots__ = (
        "name",
        "meta",
        "provider",
        "is_coroutine",
        "instance",
        "ctx",
        "task",
        "state",
        "restart_count",
        "restarts",
        "last_error",
        "exhausted",
        "started_at",
        "finished",
    )

    def __init__(self, *, name: str, meta: HostedServiceMeta, provider: Provider, is_coroutine: bool) -> None:
        self.name = name
        self.meta = meta
        self.provider = provider
        self.is_coroutine = is_coroutine
        self.instance: Any = None
        self.ctx: ServiceContext | None = None
        self.task: asyncio.Task[None] | None = None
        self.state = "pending" if meta.enabled else "disabled"
        self.restart_count = 0
        self.restarts: deque[float] = deque()
        self.last_error: str | None = None
        self.exhausted = False
        self.started_at = 0.0
        self.finished = asyncio.Event()

    def info(self) -> HostedServiceInfo:
        return HostedServiceInfo(
            name=self.name,
            state=self.state,
            ready=bool(self.ctx is not None and self.ctx.ready and self.state == "running"),
            restart_count=self.restart_count,
            last_error=self.last_error,
            healthy=not (self.exhausted and self.meta.on_exhausted == "fail_readiness"),
        )


class Supervisor:
    """Starts, supervises and stops the hosted services of one app."""

    def __init__(
        self,
        runners: list[ServiceRunner],
        *,
        clock: Clock,
        logger: Logger,
        signals: SignalBus,
        scope_factory: Callable[..., OperationScope],
        resolve_instance: Callable[[Any], Any],
        enabled: bool = True,
        rng: random.Random | None = None,
    ) -> None:
        self._runners = runners
        self._clock = clock
        self._logger = logger
        self._signals = signals
        self._scope_factory = scope_factory
        self._resolve_instance = resolve_instance
        self._enabled = enabled
        self._rng = rng or random.Random()
        self._started: list[ServiceRunner] = []

    def statuses(self) -> tuple[HostedServiceInfo, ...]:
        return tuple(runner.info() for runner in self._runners)

    # -- Start ------------------------------------------------------------

    async def start(self) -> None:
        """Start every enabled service in order; await ``wait_ready`` services.

        A ``wait_ready`` service that does not call ``ctx.mark_ready()`` within
        ``start_timeout`` real seconds (or finishes before) stops every
        service started so far and raises :class:`~lauren.exceptions.LifecycleError`.
        """
        if not self._enabled:
            if self._runners:
                self._logger.log(
                    f"hosted_services=False: {len(self._runners)} hosted service(s) not started",
                    context="HostedService",
                )
            return
        loop = asyncio.get_running_loop()
        for runner in self._runners:
            if not runner.meta.enabled:
                continue
            runner.instance = await self._resolve_instance(runner.provider)
            runner.ctx = ServiceContext(
                name=runner.name,
                clock=self._clock,
                log=bound_logger(self._logger, f"Service:{runner.name}", service=runner.name),
                scope_factory=self._scope_factory,
            )
            runner.started_at = self._clock.monotonic()
            runner.state = "running"
            runner.task = loop.create_task(self._supervise(runner), name=f"lauren-hosted:{runner.name}")
            self._started.append(runner)
            self._logger.log(f"Hosted service {runner.name!r} started", context="HostedService")
            # Let run() begin before the next service starts, so services
            # enter run() in dependency order.
            await asyncio.sleep(0)
            if runner.meta.wait_ready:
                problem = await self._await_ready(runner)
                if problem is not None:
                    reason, message = problem
                    await self.stop()
                    raise LifecycleError(
                        f"hosted service {runner.name!r} {message}",
                        detail={
                            "service": runner.name,
                            "reason": reason,
                            "start_timeout": runner.meta.start_timeout,
                        },
                    )

    async def _await_ready(self, runner: ServiceRunner) -> tuple[str, str] | None:
        assert runner.ctx is not None
        ready = asyncio.ensure_future(runner.ctx._ready_event.wait())
        finished = asyncio.ensure_future(runner.finished.wait())
        try:
            await asyncio.wait(
                {ready, finished}, timeout=runner.meta.start_timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            ready.cancel()
            finished.cancel()
        if runner.ctx.ready:
            return None
        if runner.finished.is_set():
            return "exited_before_ready", "finished before calling ctx.mark_ready() (wait_ready=True)"
        return (
            "start_timeout",
            f"did not call ctx.mark_ready() within {runner.meta.start_timeout:g}s (wait_ready=True)",
        )

    # -- Supervision ------------------------------------------------------

    async def _supervise(self, runner: ServiceRunner) -> None:
        ctx = runner.ctx
        assert ctx is not None
        loop = asyncio.get_running_loop()
        bound_run = getattr(runner.instance, "run")
        meta = runner.meta
        try:
            while True:
                runner.state = "running"
                ctx._begin_attempt(runner.restart_count, loop)
                await self._signals.emit(HostedServiceStarted(name=runner.name))
                error: Exception | None = None
                try:
                    if runner.is_coroutine:
                        await bound_run(ctx)
                    else:
                        await run_in_daemon_thread(
                            functools.partial(bound_run, ctx), name=f"lauren-hosted:{runner.name}"
                        )
                except asyncio.CancelledError:
                    if ctx.stop_requested:
                        return  # the stop path cancelled us after stop_timeout
                    self._logger.warn(
                        f"hosted service {runner.name!r} was cancelled outside shutdown (did its event loop "
                        "close? Under TestClient, drive app.startup()/shutdown() in one loop or pass "
                        "hosted_services=False)",
                        context="HostedService",
                    )
                    raise
                except Exception as exc:
                    error = exc
                if ctx.stop_requested:
                    if error is not None:
                        await self._report_failure(runner, error, will_restart=False)
                    return
                if error is None and meta.restart != "always":
                    self._logger.log(f"Hosted service {runner.name!r} returned", context="HostedService")
                    return
                will_restart = meta.restart != "never" and self._may_restart(runner)
                if error is not None:
                    await self._report_failure(runner, error, will_restart=will_restart)
                if not will_restart:
                    runner.exhausted = True
                    self._logger.error(
                        f"hosted service {runner.name!r} will not restart "
                        f"(restart={meta.restart!r}, {len(runner.restarts)} restart(s) in the last "
                        f"{meta.restart_window:g}s); on_exhausted={meta.on_exhausted!r}",
                        context="HostedService",
                    )
                    return
                delay = self._backoff(runner)
                runner.state = "restarting"
                runner.restarts.append(self._clock.monotonic())
                runner.restart_count += 1
                self._logger.warn(
                    f"Restarting hosted service {runner.name!r} in {delay:.2f}s "
                    f"(restart {runner.restart_count})",
                    context="HostedService",
                )
                if not await ctx.sleep(delay):
                    return  # stop requested during the backoff
        finally:
            runner.state = "failed" if runner.exhausted else "stopped"
            runner.finished.set()
            await self._signals.emit(
                HostedServiceStopped(name=runner.name, duration_s=self._clock.monotonic() - runner.started_at)
            )

    async def _report_failure(self, runner: ServiceRunner, error: Exception, *, will_restart: bool) -> None:
        runner.last_error = f"{type(error).__name__}: {error}"
        self._logger.error(
            f"hosted service {runner.name!r} failed: {runner.last_error}",
            context="HostedService",
            service=runner.name,
            error=type(error).__name__,
        )
        await self._signals.emit(
            HostedServiceFailed(
                name=runner.name,
                error=error,
                restart_count=runner.restart_count,
                will_restart=will_restart,
            )
        )

    def _may_restart(self, runner: ServiceRunner) -> bool:
        """``True`` while fewer than ``max_restarts`` restarts fall inside ``restart_window``."""
        now = self._clock.monotonic()
        window = runner.meta.restart_window
        while runner.restarts and now - runner.restarts[0] > window:
            runner.restarts.popleft()
        return len(runner.restarts) < runner.meta.max_restarts

    def _backoff(self, runner: ServiceRunner) -> float:
        """Exponential backoff with full jitter: ``uniform(0, min(cap, base * 2**n))``."""
        base, cap = runner.meta.backoff
        ceiling = min(cap, base * (2 ** len(runner.restarts)))
        return self._rng.uniform(0.0, ceiling) if ceiling > 0 else 0.0

    # -- Stop -------------------------------------------------------------

    async def stop(self) -> None:
        """Stop started services in reverse start order (each bounded by its ``stop_timeout``)."""
        started, self._started = self._started, []
        for runner in reversed(started):
            await self._stop_one(runner)

    async def _stop_one(self, runner: ServiceRunner) -> None:
        task, ctx = runner.task, runner.ctx
        if task is None or ctx is None:
            return
        if not task.done():
            runner.state = "stopping"
        ctx._request_stop()
        if task.done():
            return
        timeout = runner.meta.stop_timeout
        await asyncio.wait({task}, timeout=timeout)
        if task.done():
            return
        self._logger.warn(
            f"hosted service {runner.name!r} did not stop within {timeout:g}s; cancelling it",
            context="HostedService",
        )
        task.cancel()
        await asyncio.wait({task}, timeout=_CANCEL_GRACE)
        if not runner.is_coroutine:
            self._logger.warn(
                f"hosted service {runner.name!r} runs on a thread that cannot be cancelled; the thread was "
                "abandoned (poll ctx.stop_requested to stop in time)",
                context="HostedService",
            )
        elif not task.done():  # pragma: no cover - a run() that swallows cancellation
            self._logger.error(
                f"hosted service {runner.name!r} ignored cancellation; abandoning it", context="HostedService"
            )


__all__ = ["HostedServiceInfo", "ServiceRunner", "Supervisor"]
