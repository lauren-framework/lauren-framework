"""The per-app health runtime: compiled checks, execution, caching, probes and reports.

``compile_checks`` validates every discovered ``@health_check`` method at
startup (``LaurenFactory.create`` Phase 6a). A :class:`HealthRuntime` exists
only when the app imports ``HealthModule.for_root(...)``; it is registered as
a global value provider, receives the compiled checks plus the built-in
indicators, and is bound to the app in Phase 7. All of its state — results,
in-flight tasks, the event-loop sampler — belongs to that one app.

Running a check never reflects: the discovery pass compiled its
:class:`~lauren._discovery.CallPlan`. Each run is bounded by the check's
timeout; concurrent probes share one in-flight run per check
(single-flight); ``cache_ttl`` reuses the last result.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio.to_thread

from .._discovery import DiscoveredMethod
from .._events._bus import LEASE_ATTR
from ..exceptions import DuplicateBindingError, HTTPError, LifecycleConfigError, LifecycleViolationError
from ..signals import AppLifecycleState, HealthChanged, LifecycleEvent, LifecycleStateChanged
from ..types import ExecutionContext, Headers, Request, Response, Scope
from ._indicators import LoopLagMonitor, disk_probe, memory_probe
from ._meta import LIVE, READY, STARTUP
from ._types import HealthResult, HealthStatus, aggregate

if TYPE_CHECKING:
    from .._asgi import LaurenApp
    from .._di import DIContainer
    from ..context import OperationScope
    from ._module import HealthOptions

#: Names of the built-in indicators; user checks cannot take them.
RESERVED_NAMES = frozenset({"lifecycle", "event_loop", "memory"})
DISK_PREFIX = "disk:"
BUILTIN = "built-in"
#: Timeout of the built-in indicators, in seconds.
BUILTIN_TIMEOUT = 2.0

_SERVING = frozenset({AppLifecycleState.READY, AppLifecycleState.DRAINING})
_DEAD = frozenset({AppLifecycleState.FAILED, AppLifecycleState.STOPPED})
_IETF_STATUS = {HealthStatus.UP: "pass", HealthStatus.DEGRADED: "warn", HealthStatus.DOWN: "fail"}

# Sync checks run in a worker thread that a timeout abandons (a thread cannot
# be cancelled): ``abandon_on_cancel`` on anyio >= 4.1, ``cancellable`` on 4.0.
_run_sync: Callable[..., Any] = anyio.to_thread.run_sync
_ABANDON_ON_CANCEL: dict[str, bool] = (
    {"abandon_on_cancel": True}
    if "abandon_on_cancel" in inspect.signature(anyio.to_thread.run_sync).parameters
    else {"cancellable": True}  # pragma: no cover - anyio 4.0
)


@dataclass(frozen=True, slots=True)
class Check:
    """One compiled check: a discovered ``@health_check`` method or a built-in indicator."""

    name: str
    critical: bool
    timeout: float
    cache_ttl: float
    tags: frozenset[str]
    source: str  # "Class.method", or "built-in"
    entry: DiscoveredMethod | None = None
    probe: Callable[[], HealthResult] | None = None  # built-in indicators
    blocking: bool = False  # run ``probe`` in a worker thread


@dataclass(frozen=True, slots=True)
class Outcome:
    """The result of one run of a check."""

    status: HealthStatus
    details: dict[str, Any]
    latency_ms: float
    reason: str | None = None  # "timeout"
    error: str | None = None  # "ConnectionError: refused"


class _CheckState:
    """Per-check mutable state: last outcome, cache expiry, in-flight run."""

    __slots__ = ("outcome", "expires", "task", "status", "thread_run")

    def __init__(self) -> None:
        self.outcome: Outcome | None = None
        self.expires = 0.0
        self.task: asyncio.Task[Outcome] | None = None
        self.status: HealthStatus | None = None
        self.thread_run: _ThreadRun | None = None  # the last sync run handed to a worker thread


class _ThreadRun:
    """One sync run handed to a worker thread: did the thread start it, has it finished?

    A run abandoned by its timeout before a thread picked it up never calls
    the check; one abandoned after it started keeps its thread until it
    returns, and :attr:`busy` reports that.
    """

    __slots__ = ("_lock", "started", "finished", "abandoned")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.started = False
        self.finished = False
        self.abandoned = False

    @property
    def busy(self) -> bool:
        return self.started and not self.finished

    def begin(self) -> bool:
        """Called by the worker thread: ``False`` if the run was abandoned before it started."""
        with self._lock:
            if self.abandoned:
                return False
            self.started = True
            return True

    def abandon(self) -> None:
        with self._lock:
            self.abandoned = True


class _StillRunning(Exception):
    """A sync check's previous run outlived its timeout and still occupies a thread."""


def compile_checks(entries: Iterable[DiscoveredMethod]) -> tuple[Check, ...]:
    """Validate every discovered ``@health_check`` method (startup only).

    Raises :class:`~lauren.exceptions.LifecycleConfigError` for a check on a
    provider that is not a singleton and
    :class:`~lauren.exceptions.DuplicateBindingError` for a name declared
    twice or taken by a built-in indicator. Parameters were already planned
    by the discovery pass (anything but a dependency of the provider's module
    fails there with :class:`~lauren.exceptions.UnresolvableParameterError`).
    """
    checks: list[Check] = []
    claimed: dict[str, str] = {}
    for entry in entries:
        source = entry.plan.target
        for meta in entry.meta or ():
            if entry.provider.scope != Scope.SINGLETON:
                scope = Scope(entry.provider.scope).name.lower()
                raise LifecycleConfigError(
                    f"@health_check {meta.name!r} on {source}: {entry.owner.__name__} is a {scope}-scoped "
                    "provider; health checks run on singletons, so declare it @injectable(scope=Scope.SINGLETON) "
                    "or move the check to a singleton",
                    detail={
                        "check": meta.name,
                        "provider": entry.owner.__name__,
                        "method": entry.attr_name,
                        "scope": scope,
                        "reason": "non_singleton_health_check",
                    },
                )
            _claim(meta.name, source, claimed)
            checks.append(
                Check(
                    name=meta.name,
                    critical=meta.critical,
                    timeout=meta.timeout,
                    cache_ttl=meta.cache_ttl,
                    tags=frozenset(meta.tags),
                    source=source,
                    entry=entry,
                )
            )
    return tuple(checks)


def _claim(name: str, source: str, claimed: dict[str, str]) -> None:
    if name in RESERVED_NAMES or name.startswith(DISK_PREFIX):
        raise DuplicateBindingError(
            f"health check name {name!r} ({source}) is reserved for a built-in indicator; choose another name",
            detail={"check": name, "providers": [source, BUILTIN], "reason": "reserved_health_check_name"},
        )
    if name in claimed:
        raise DuplicateBindingError(
            f"health check {name!r} is declared twice, on {claimed[name]} and {source}; "
            "check names are unique per application",
            detail={"check": name, "providers": [claimed[name], source], "reason": "duplicate_health_check"},
        )
    claimed[name] = source


def builtin_checks(options: HealthOptions, monitor: LoopLagMonitor | None) -> tuple[Check, ...]:
    """The built-in indicators ``options`` enables (event loop, disk space, memory)."""
    checks: list[Check] = []
    if monitor is not None:
        checks.append(
            Check(
                "event_loop",
                True,
                BUILTIN_TIMEOUT,
                0.0,
                frozenset({LIVE, READY}),
                BUILTIN,
                probe=monitor.result,
            )
        )
    for path, min_free in options.disk_paths:
        checks.append(
            Check(
                f"{DISK_PREFIX}{path}",
                True,
                BUILTIN_TIMEOUT,
                0.0,
                frozenset({READY}),
                BUILTIN,
                probe=disk_probe(path, min_free),
                blocking=True,
            )
        )
    if options.max_rss_bytes is not None:
        checks.append(
            Check(
                "memory",
                True,
                BUILTIN_TIMEOUT,
                0.0,
                frozenset({READY}),
                BUILTIN,
                probe=memory_probe(options.max_rss_bytes),
            )
        )
    return tuple(checks)


def _normalise(raw: Any) -> HealthResult:
    if raw is None or raw is True:
        return HealthResult.up()
    if raw is False:
        return HealthResult.down()
    if isinstance(raw, HealthResult):
        return raw
    raise TypeError(f"a health check returned {type(raw).__name__}; return None, a bool or a HealthResult")


def _describe_error(exc: BaseException) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


class HealthRuntime:
    """One application's health checks: execution, cache, single-flight, probes and reports."""

    __slots__ = (
        "_options",
        "_app",
        "_container",
        "_monitor",
        "_checks",
        "_by_tag",
        "_states",
        "_aggregate",
        "_clock",
    )

    def __init__(self, options: HealthOptions) -> None:
        self._options = options
        self._app: LaurenApp | None = None
        self._container: DIContainer | None = None
        self._monitor = (
            LoopLagMonitor(options.event_loop_lag_ms) if options.event_loop_lag_ms is not None else None
        )
        self._checks: tuple[Check, ...] = ()
        self._by_tag: dict[str, tuple[Check, ...]] = {LIVE: (), READY: (), STARTUP: ()}
        self._states: dict[str, _CheckState] = {}
        self._aggregate: HealthStatus | None = None
        #: Monotonic clock for ``cache_ttl``; tests substitute a fake one.
        self._clock: Callable[[], float] = time.monotonic

    @property
    def options(self) -> HealthOptions:
        return self._options

    @property
    def checks(self) -> tuple[Check, ...]:
        """Every check the report runs: discovered checks first, then built-in indicators."""
        return self._checks

    @property
    def monitor(self) -> LoopLagMonitor | None:
        """The event-loop lag sampler (``None`` unless ``event_loop_lag_ms`` is set)."""
        return self._monitor

    # -- Factory hooks ---------------------------------------------------------

    def _install(self, checks: tuple[Check, ...], container: DIContainer) -> None:
        self._checks = checks + builtin_checks(self._options, self._monitor)
        self._by_tag = {
            tag: tuple(c for c in self._checks if tag in c.tags) for tag in (LIVE, READY, STARTUP)
        }
        self._states = {check.name: _CheckState() for check in self._checks}
        self._container = container

    def _bind(self, app: LaurenApp) -> None:
        self._app = app
        self._container = app.container
        if self._monitor is not None:
            app.signals.on(LifecycleStateChanged)(self._on_lifecycle)

    def _on_lifecycle(self, event: LifecycleEvent) -> None:
        """Run the event-loop sampler while the app is ``READY``."""
        if (
            not isinstance(event, LifecycleStateChanged)
            or event.app is not self._app
            or self._monitor is None
        ):
            return
        if event.current is AppLifecycleState.READY:
            self._monitor.ensure_running()
        else:
            self._monitor.stop()

    def _require_app(self) -> LaurenApp:
        if self._app is None:
            raise LifecycleViolationError(
                "the health runtime is not bound to an application",
                detail={"reason": "unbound_health_runtime"},
            )
        return self._app

    # -- Probes and the report -------------------------------------------------

    async def probe(self, kind: str) -> tuple[int, dict[str, Any]]:
        """Evaluate the ``"live"``, ``"ready"`` or ``"startup"`` probe: ``(http_status, body)``.

        Checks run only while the app is ``READY`` or ``DRAINING`` (their
        singletons exist); the lifecycle state alone decides otherwise.
        """
        state = self._require_app().lifecycle_state
        if kind == LIVE:
            failed, run = state in _DEAD, state in _SERVING
        elif kind == STARTUP:
            failed, run = state not in _SERVING, True
        else:
            failed, run = state is not AppLifecycleState.READY, True
        checks = self._by_tag[kind]
        pairs: list[tuple[bool, HealthStatus]] = []
        if not failed and run and checks:
            self._keep_sampling(state)
            for check, (outcome, _cached) in zip(checks, await self._run(checks)):
                pairs.append((check.critical, outcome.status))
                if outcome.status is HealthStatus.DOWN and (kind != READY or check.critical):
                    failed = True
        status = HealthStatus.DOWN if failed else aggregate(pairs)
        return self._status_code(status, failed=failed), {"status": self._status_value(status)}

    async def report(self, *, show_details: bool) -> tuple[int, dict[str, Any]]:
        """Run every check and build the full report: ``(http_status, body)``."""
        app = self._require_app()
        state = app.lifecycle_state
        pairs: list[tuple[bool, HealthStatus]] = [
            (True, HealthStatus.UP if state is AppLifecycleState.READY else HealthStatus.DOWN)
        ]
        entries: dict[str, Any] = {}
        if state in _SERVING and self._checks:
            self._keep_sampling(state)
            for check, (outcome, cached) in zip(self._checks, await self._run(self._checks)):
                pairs.append((check.critical, outcome.status))
                entries[check.name] = self._entry(check, outcome, cached, show_details)
        status = aggregate(pairs)
        previous, self._aggregate = self._aggregate, status
        if previous is not status:
            await self._changed(None, previous, status, {"lifecycle": state.value})
        payload: dict[str, Any] = {
            "status": self._status_value(status),
            "lifecycle": state.value,
            "checks": entries,
        }
        if self._options.version is not None:
            payload["version"] = self._options.version
        failed = status is HealthStatus.DOWN and self._options.report_status_code
        return self._status_code(status, failed=failed), payload

    async def details_allowed(self, ctx: ExecutionContext, request: Request) -> bool:
        """Apply the details policy; ``"when_authorized"`` asks the details guard.

        A guard that denies — returning a falsy value or raising an
        :class:`~lauren.exceptions.HTTPError` — hides the details; it never
        turns the report into an error response.
        """
        policy = self._options.details
        if policy != "when_authorized":
            return policy == "always"
        container = self._container
        guard_cls = self._options.details_guard
        if container is None or guard_cls is None:  # pragma: no cover - validated by for_root / bind
            return False
        lease = request.__dict__.get(LEASE_ATTR)
        guard = await container.resolve(
            guard_cls,
            request_cache=lease.request_cache if lease is not None else None,
            framework_values=lease.framework_values if lease is not None else None,
        )
        try:
            return bool(await guard.can_activate(ctx))
        except HTTPError:
            return False

    async def probe_response(self, kind: str) -> Response:
        return self._respond(*await self.probe(kind))

    async def report_response(self, ctx: ExecutionContext, request: Request) -> Response:
        show = await self.details_allowed(ctx, request)
        return self._respond(*await self.report(show_details=show))

    def _respond(self, status_code: int, payload: dict[str, Any]) -> Response:
        if self._options.format == "ietf":
            media_type = "application/health+json"
            payload = self._to_ietf(payload)
        else:
            media_type = "application/json"
        body = self._require_app().json_encoder.encode_compact(payload)
        return Response(
            body, status=status_code, media_type=media_type, headers=Headers([("cache-control", "no-store")])
        )

    def _status_value(self, status: HealthStatus) -> str:
        return _IETF_STATUS[status] if self._options.format == "ietf" else status.value

    def _status_code(self, status: HealthStatus, *, failed: bool) -> int:
        if failed:
            return 503
        if status is HealthStatus.DEGRADED:
            return self._options.degraded_status
        return 200

    def _entry(self, check: Check, outcome: Outcome, cached: bool, show_details: bool) -> dict[str, Any]:
        entry: dict[str, Any] = {"status": self._status_value(outcome.status)}
        if not show_details:
            return entry
        entry["critical"] = check.critical
        entry["latency_ms"] = round(outcome.latency_ms, 3)
        for key, value in outcome.details.items():
            if key not in ("status", "critical", "cached"):
                entry[key] = value
        if outcome.reason is not None:
            entry["reason"] = outcome.reason
        if outcome.error is not None:
            entry["error"] = outcome.error
        if cached:
            entry["cached"] = True
        return entry

    @staticmethod
    def _to_ietf(payload: dict[str, Any]) -> dict[str, Any]:
        """``application/health+json``: each check becomes a one-element list; ``error`` becomes ``output``."""
        checks = payload.get("checks")
        if checks is None:
            return payload
        converted: dict[str, Any] = {}
        for name, entry in checks.items():
            item = dict(entry)
            if "error" in item:
                item["output"] = item.pop("error")
            converted[name] = [item]
        return {**payload, "checks": converted}

    def _keep_sampling(self, state: AppLifecycleState) -> None:
        """Make sure the event-loop sampler runs on the current loop (each loop has its own)."""
        if self._monitor is not None and state is AppLifecycleState.READY:
            self._monitor.ensure_running()

    # -- Running checks --------------------------------------------------------

    async def _run(self, checks: tuple[Check, ...]) -> list[tuple[Outcome, bool]]:
        return list(await asyncio.gather(*(self._outcome(check) for check in checks)))

    async def _outcome(self, check: Check) -> tuple[Outcome, bool]:
        """``(outcome, cached)``: a fresh cached result, or the (shared) in-flight run."""
        state = self._states[check.name]
        if check.cache_ttl > 0 and state.outcome is not None and self._clock() < state.expires:
            return state.outcome, True
        loop = asyncio.get_running_loop()
        task = state.task
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self._execute(check, state), name=f"lauren-health:{check.name}")
            state.task = task
        # A probe request that goes away must not cancel a run other probes await.
        return await asyncio.shield(task), False

    async def _execute(self, check: Check, state: _CheckState) -> Outcome:
        started = time.perf_counter()
        reason: str | None = None
        error: str | None = None
        try:
            async with asyncio.timeout(check.timeout) as deadline:
                raw = await self._call(check, state)
            result = _normalise(raw)
        except TimeoutError as exc:
            result = HealthResult.down()
            if deadline.expired():
                reason = "timeout"
            else:
                error = _describe_error(exc)
        except _StillRunning:
            result, reason, error = HealthResult.down(), "timeout", "the previous run is still running"
        except Exception as exc:
            result, error = HealthResult.down(), _describe_error(exc)
        outcome = Outcome(
            result.status, dict(result.details), (time.perf_counter() - started) * 1000.0, reason, error
        )
        state.outcome = outcome
        state.expires = self._clock() + check.cache_ttl
        previous, state.status = state.status, outcome.status
        if previous is not outcome.status:
            details = dict(outcome.details)
            if reason is not None:
                details["reason"] = reason
            if error is not None:
                details["error"] = error
            await self._changed(check.name, previous, outcome.status, details)
        return outcome

    async def _call(self, check: Check, state: _CheckState) -> Any:
        if check.probe is not None:
            if check.blocking:
                return await self._in_thread(state, check.probe, {})
            return check.probe()
        entry = check.entry
        container = self._container
        if entry is None or container is None:  # pragma: no cover - a Check has one or the other
            raise LifecycleViolationError(
                f"health check {check.name!r} cannot run", detail={"check": check.name}
            )
        instance = None
        if entry.plan.binding == "instance":
            instance = await container._instantiate(entry.provider, None, {})
        if entry.plan.needs_scope:
            async with self._require_app().scope(f"health:{check.name}") as scope:
                return await self._invoke(entry, state, instance, scope)
        return await self._invoke(entry, state, instance, None)

    async def _invoke(
        self, entry: DiscoveredMethod, state: _CheckState, instance: Any, scope: OperationScope | None
    ) -> Any:
        if entry.plan.is_coroutine:
            return await entry.invoke(instance, scope)
        kwargs = await entry.plan.build_kwargs(scope)
        return await self._in_thread(state, entry.bind(instance), kwargs)

    async def _in_thread(self, state: _CheckState, fn: Callable[..., Any], kwargs: Mapping[str, Any]) -> Any:
        """Run a sync check in a worker thread that its timeout can abandon.

        A thread cannot be cancelled, so a hung check keeps its thread; until
        it returns, later runs fail fast instead of piling up more threads.
        """
        previous = state.thread_run
        if previous is not None and previous.busy:
            raise _StillRunning
        record = state.thread_run = _ThreadRun()

        def run() -> Any:
            if not record.begin():
                return None  # abandoned while waiting for a thread: do not call the check late
            try:
                return fn(**kwargs)
            finally:
                record.finished = True

        try:
            return await _run_sync(run, **_ABANDON_ON_CANCEL)
        except BaseException:
            record.abandon()
            raise

    async def _changed(
        self, name: str | None, previous: HealthStatus | None, current: HealthStatus, details: dict[str, Any]
    ) -> None:
        app = self._require_app()
        label = f"health check {name!r}" if name is not None else "health"
        message = f"{label}: {previous.value if previous else 'unknown'} -> {current.value}"
        if current is HealthStatus.DOWN:
            app.logger.warn(message, context="Health", check=name, status=current.value)
        else:
            app.logger.verbose(message, context="Health", check=name, status=current.value)
        await app.signals.emit(
            HealthChanged(app=app, name=name, previous=previous, current=current, details=details)
        )


__all__ = [
    "BUILTIN",
    "Check",
    "HealthRuntime",
    "Outcome",
    "RESERVED_NAMES",
    "builtin_checks",
    "compile_checks",
]
