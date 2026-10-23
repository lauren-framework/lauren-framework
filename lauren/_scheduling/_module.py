"""``ScheduleModule`` and the factory integration (private module).

``LaurenFactory.create`` calls three functions from here:

* :func:`prepare_scheduling` (Phase 2c, before the DI compile) finds the
  ``ScheduleModule.for_root()`` config in the module graph and registers the
  app's :class:`SchedulerRegistry` as a global provider;
* :func:`compile_scheduling` (Phase 6a, right after discovery) validates
  every discovered job and hosted service — singleton scope, unique names,
  ``ScheduleModule`` present — and builds the job table;
* :meth:`CompiledScheduling.build` (Phase 7) creates the per-app runtime the
  ``LaurenApp`` owns. Apps without jobs, hosted services or a
  ``ScheduleModule`` get ``None`` and pay nothing.
"""

from __future__ import annotations

import inspect
import random
from dataclasses import dataclass, field
from datetime import tzinfo
from typing import TYPE_CHECKING, Any

from .._discovery import DiscoveryIndex
from ..decorators import module
from ..exceptions import DecoratorUsageError, DuplicateBindingError, LifecycleConfigError
from ..logging import Logger
from ..types import Scope
from ._clock import Clock, SystemClock
from ._cron import resolve_timezone, timezone_name
from ._decorators import (
    HOSTED_SERVICE_META,
    SCHEDULE_META,
    HostedServiceMeta,
    ScheduleMeta,
    validate_run_method,
)
from ._lock import InMemorySchedulerLock, SchedulerLock
from ._scheduler import Job, Scheduler, SchedulerRegistry
from ._supervisor import ServiceRunner, Supervisor

if TYPE_CHECKING:
    from .._asgi import LaurenApp
    from .._di import DIContainer, Provider

SCHEDULE_CONFIG_ATTR = "__lauren_schedule_config__"


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    """The options of one ``ScheduleModule.for_root(...)`` call."""

    timezone: tzinfo
    lock: SchedulerLock | None
    enabled: bool
    clock: Clock | None
    job_drain_timeout: float | None


class ScheduleModule:
    """Enables scheduled jobs: import ``ScheduleModule.for_root()`` once, in any module.

    Without it, an app whose providers carry ``@interval`` / ``@cron`` /
    ``@run_once`` fails in ``LaurenFactory.create`` with
    :class:`~lauren.exceptions.LifecycleConfigError` instead of silently never
    running them. Importing it also makes :class:`SchedulerRegistry`
    injectable everywhere.
    """

    @classmethod
    def for_root(
        cls,
        *,
        timezone: str | tzinfo = "UTC",
        lock: SchedulerLock | None = None,
        enabled: bool = True,
        clock: Clock | None = None,
        job_drain_timeout: float | None = None,
    ) -> type:
        """Return the module to put in ``@module(imports=[...])``.

        ``timezone`` is the default zone of ``@cron`` jobs (and of
        ``scheduled_at``). ``lock`` backs ``exclusive=True`` jobs (default: a
        per-process :class:`InMemorySchedulerLock`). ``enabled=False`` keeps
        discovery, validation and :meth:`SchedulerRegistry.trigger` but fires
        nothing. ``clock`` replaces the real clock (pass a fresh
        :class:`ManualClock` per app in tests; hosted services use it too).
        ``job_drain_timeout`` bounds how long shutdown waits for in-flight
        runs (default: what is left of ``drain_timeout``).
        """
        where = "ScheduleModule.for_root"
        zone = resolve_timezone(timezone, where=where)
        if zone is None:  # pragma: no cover - resolve_timezone only returns None for None
            raise DecoratorUsageError(f"{where}: timezone is required", detail={"helper": where})
        if lock is not None and not (
            callable(getattr(lock, "acquire", None)) and callable(getattr(lock, "release", None))
        ):
            raise DecoratorUsageError(
                f"{where}: lock= must implement SchedulerLock (async acquire(key, *, ttl) and release(key)), "
                f"got {lock!r}",
                detail={"helper": where, "reason": "invalid_lock"},
            )
        if clock is not None and not all(
            callable(getattr(clock, attr, None)) for attr in ("monotonic", "now", "sleep")
        ):
            raise DecoratorUsageError(
                f"{where}: clock= must implement Clock (monotonic(), now(), async sleep()), got {clock!r}",
                detail={"helper": where, "reason": "invalid_clock"},
            )
        if not isinstance(enabled, bool):
            raise DecoratorUsageError(
                f"{where}: enabled= must be True or False, got {enabled!r}",
                detail={"helper": where, "reason": "invalid_flag"},
            )
        if job_drain_timeout is not None and (
            isinstance(job_drain_timeout, bool)
            or not isinstance(job_drain_timeout, (int, float))
            or job_drain_timeout < 0
        ):
            raise DecoratorUsageError(
                f"{where}: job_drain_timeout= must be a number of seconds (0 or more), got {job_drain_timeout!r}",
                detail={"helper": where, "reason": "invalid_number"},
            )
        config = ScheduleConfig(
            timezone=zone,
            lock=lock,
            enabled=enabled,
            clock=clock,
            job_drain_timeout=None if job_drain_timeout is None else float(job_drain_timeout),
        )
        namespace = {
            "__doc__": "Generated ScheduleModule.",
            "__module__": __name__,
            SCHEDULE_CONFIG_ATTR: config,
        }
        generated = type(f"ScheduleModule[{timezone_name(zone)}]", (), namespace)
        return module()(generated)


# ---------------------------------------------------------------------------
# Phase 2c
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class PreparedScheduling:
    """What Phase 2c found: the ``ScheduleModule`` config and the app's registry."""

    config: ScheduleConfig | None = None
    registry: SchedulerRegistry | None = None


def prepare_scheduling(graph: Any, container: DIContainer) -> PreparedScheduling:
    """Phase 2c: register the app's :class:`SchedulerRegistry` when ``ScheduleModule`` is imported."""
    found = [
        (mod, mod.__dict__[SCHEDULE_CONFIG_ATTR])
        for mod in graph.modules
        if SCHEDULE_CONFIG_ATTR in mod.__dict__
    ]
    if not found:
        return PreparedScheduling()
    if len(found) > 1:
        raise LifecycleConfigError(
            "ScheduleModule.for_root() is imported more than once "
            f"({len(found)} configurations); import it once, typically in the root module",
            detail={"reason": "duplicate_schedule_module", "count": len(found)},
        )
    registry = SchedulerRegistry()
    container.register_value(SchedulerRegistry, registry, owning_module=None)
    return PreparedScheduling(config=found[0][1], registry=registry)


# ---------------------------------------------------------------------------
# Phase 6a
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CompiledScheduling:
    """Validated jobs and hosted services, ready to become a runtime in Phase 7."""

    prepared: PreparedScheduling
    jobs: list[Job] = field(default_factory=list)
    services: list[tuple[str, HostedServiceMeta, Provider, bool]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.jobs and not self.services and self.prepared.config is None

    def build(
        self,
        app: LaurenApp,
        *,
        order: list[Provider],
        hosted_services: bool,
        scheduler: bool,
    ) -> SchedulingRuntime | None:
        """Phase 7: create the app's scheduler / supervisor (``None`` when nothing is declared)."""
        if self.empty:
            return None
        config = self.prepared.config
        clock: Clock = (config.clock if config is not None else None) or SystemClock()
        container = app.container

        async def resolve_instance(provider: Provider) -> Any:
            return await container._instantiate(provider, None, {})  # noqa: SLF001 - honours multi-binding keys

        rng = random.Random()
        position = {id(provider): index for index, provider in enumerate(order)}
        services = sorted(self.services, key=lambda item: position.get(id(item[2]), len(position)))
        supervisor = None
        if services:
            supervisor = Supervisor(
                [ServiceRunner(name=n, meta=m, provider=p, is_coroutine=c) for n, m, p, c in services],
                clock=clock,
                logger=app.logger,
                signals=app.signals,
                scope_factory=app.scope,
                resolve_instance=resolve_instance,
                enabled=hosted_services,
                rng=rng,
            )
        sched = None
        if config is not None:
            sched = Scheduler(
                clock=clock,
                lock=config.lock or InMemorySchedulerLock(clock=clock),
                timezone=config.timezone,
                enabled=config.enabled and scheduler,
                logger=app.logger,
                signals=app.signals,
                scope_factory=app.scope,
                resolve_instance=resolve_instance,
                rng=rng,
            )
            for job in self.jobs:
                sched.add(job)
            assert self.prepared.registry is not None
            self.prepared.registry._bind(sched, supervisor)
        return SchedulingRuntime(
            supervisor=supervisor,
            scheduler=sched,
            logger=app.logger,
            job_drain_timeout=config.job_drain_timeout if config is not None else None,
        )


def compile_scheduling(discovery: DiscoveryIndex, prepared: PreparedScheduling) -> CompiledScheduling:
    """Phase 6a: validate discovered jobs and hosted services; raise on any misuse."""
    compiled = CompiledScheduling(prepared=prepared)
    entries = discovery.methods_for(SCHEDULE_META)
    if entries and prepared.config is None:
        entry = entries[0]
        first: ScheduleMeta = entry.meta[0]
        target = f"{entry.owner.__name__}.{entry.attr_name}"
        decorator = {"interval": "interval", "cron": "cron", "once": "run_once"}[first.kind]
        raise LifecycleConfigError(
            f"{target} is scheduled (@{decorator}) but scheduling is not enabled; import "
            "ScheduleModule.for_root() (or ScheduleModule.for_root(enabled=False) to disable explicitly)",
            detail={"target": target, "decorator": decorator, "reason": "scheduling_not_enabled"},
        )
    names: dict[str, str] = {}
    timezone = prepared.config.timezone if prepared.config is not None else None
    for entry in entries:
        target = f"{_class_label(entry.owner)}.{entry.attr_name}"
        _require_singleton(entry.provider, target, "scheduled job")
        metas = [
            m
            for m in (entry.meta if isinstance(entry.meta, list) else [entry.meta])
            if isinstance(m, ScheduleMeta)
        ]
        for index, meta in enumerate(metas, start=1):
            name = meta.name or (target if len(metas) == 1 else f"{target}#{index}")
            _claim_name(names, name, target, "scheduled jobs", "duplicate_job_name")
            compiled.jobs.append(
                Job(
                    name=name,
                    meta=meta,
                    tz=meta.tz or timezone,  # type: ignore[arg-type]  # config is set when jobs exist
                    target=target,
                    entry=entry,
                )
            )
    service_names: dict[str, str] = {}
    for provider, meta in discovery.classes_for(HOSTED_SERVICE_META):
        cls = provider.factory if provider.provider_kind == "class" else provider.cls
        assert isinstance(cls, type)
        target = _class_label(cls)
        _require_singleton(provider, target, "hosted service")
        run = validate_run_method(cls)
        name = meta.name or target
        _claim_name(service_names, name, target, "hosted services", "duplicate_service_name")
        compiled.services.append((name, meta, provider, inspect.iscoroutinefunction(run)))
    return compiled


def _class_label(cls: type) -> str:
    """The class's qualified name without any ``<locals>`` prefix (default job / service names)."""
    return cls.__qualname__.rsplit("<locals>.", 1)[-1]


def _require_singleton(provider: Provider, target: str, what: str) -> None:
    if provider.scope != Scope.SINGLETON:
        raise LifecycleConfigError(
            f"{target} is a {what} but its provider is {provider.scope.label}-scoped; {what}s run on a "
            "singleton instance, so declare it @injectable() (Scope.SINGLETON)",
            detail={"target": target, "scope": provider.scope.label, "reason": "not_singleton"},
        )


def _claim_name(names: dict[str, str], name: str, target: str, what: str, reason: str) -> None:
    other = names.get(name)
    if other is not None:
        raise DuplicateBindingError(
            f"two {what} are named {name!r} ({other} and {target}); pass name= to tell them apart",
            detail={"name": name, "targets": [other, target], "reason": reason},
        )
    names[name] = target


# ---------------------------------------------------------------------------
# Runtime (owned by LaurenApp)
# ---------------------------------------------------------------------------


class SchedulingRuntime:
    """Hosted services and the scheduler of one app, driven by ``startup()`` / ``shutdown()``."""

    __slots__ = ("supervisor", "scheduler", "_logger", "_job_drain_timeout")

    def __init__(
        self,
        *,
        supervisor: Supervisor | None,
        scheduler: Scheduler | None,
        logger: Logger,
        job_drain_timeout: float | None,
    ) -> None:
        self.supervisor = supervisor
        self.scheduler = scheduler
        self._logger = logger
        self._job_drain_timeout = job_drain_timeout

    async def start(self) -> None:
        """Startup steps 3-4: start hosted services in order, then arm the scheduler."""
        if self.supervisor is not None:
            await self.supervisor.start()
        if self.scheduler is not None:
            await self.scheduler.arm()

    def begin_firing(self) -> None:
        """Startup step 6 (``READY``): the scheduler starts firing."""
        sched = self.scheduler
        if sched is None:
            return
        sched.begin()
        exclusive = [job.name for job in sched.job_table() if job.meta.exclusive]
        if sched.firing and exclusive and isinstance(sched.lock, InMemorySchedulerLock):
            self._logger.log(
                f"{len(exclusive)} exclusive job(s) use the in-memory scheduler lock: exclusivity is per "
                "process, so with several workers each one runs them (share a store-backed SchedulerLock or "
                "run scheduled work in one process)",
                context="Scheduler",
                jobs=exclusive,
            )

    def stop_firing(self) -> None:
        """Shutdown step 1: no new runs start; hosted services keep running."""
        if self.scheduler is not None:
            self.scheduler.stop_firing()

    async def stop(self, remaining: float) -> None:
        """Shutdown steps 3-4: await (then cancel) in-flight runs, stop services in reverse order."""
        if self.scheduler is not None:
            timeout = self._job_drain_timeout if self._job_drain_timeout is not None else remaining
            await self.scheduler.drain(max(0.0, timeout))
        if self.supervisor is not None:
            await self.supervisor.stop()


__all__ = [
    "SCHEDULE_CONFIG_ATTR",
    "CompiledScheduling",
    "PreparedScheduling",
    "ScheduleConfig",
    "ScheduleModule",
    "SchedulingRuntime",
    "compile_scheduling",
    "prepare_scheduling",
]
