"""``@hosted_service``, ``@interval``, ``@cron`` and ``@run_once`` (private module).

The decorators only validate their arguments and attach metadata
(``__lauren_hosted_service__`` on the class's own ``__dict__``,
``__lauren_schedule__`` — a list, so stacking accumulates — on the
function). Discovery happens once, in ``LaurenFactory.create`` Phase 6a,
through the shared pass in ``lauren/_discovery.py``; the markers are
registered here, at import.
"""

from __future__ import annotations

import inspect
import math
from dataclasses import dataclass
from datetime import timedelta, tzinfo
from typing import Any, Callable, Literal, TypeVar

from .._di import INJECTABLE_META, InjectableMeta
from .._di._scope import register_framework_value
from .._discovery import MarkerSpec, register_marker
from ..decorators import _reject_bare_usage
from ..exceptions import DecoratorUsageError, LifecycleConfigError
from ..types import Scope
from ._context import JobContext, ServiceContext
from ._cron import CronExpression, resolve_timezone, timezone_name

SCHEDULE_META = "__lauren_schedule__"
HOSTED_SERVICE_META = "__lauren_hosted_service__"

Overlap = Literal["skip", "queue", "allow"]
Misfire = Literal["coalesce", "skip"]
IntervalMode = Literal["fixed_rate", "fixed_delay"]
Restart = Literal["never", "on-failure", "always"]
OnExhausted = Literal["log", "fail_readiness"]

_OVERLAPS = ("skip", "queue", "allow")
_MISFIRES = ("coalesce", "skip")
_MODES = ("fixed_rate", "fixed_delay")
_RESTARTS = ("never", "on-failure", "always")
_EXHAUSTED = ("log", "fail_readiness")

_MISSING: Any = object()
F = TypeVar("F")
C = TypeVar("C", bound=type)


@dataclass(frozen=True, slots=True)
class ScheduleMeta:
    """One schedule attached to a method by ``@interval`` / ``@cron`` / ``@run_once``.

    ``kind`` is ``"interval"``, ``"cron"`` or ``"once"``. ``seconds`` /
    ``initial_delay`` / ``mode`` describe an interval, ``cron`` / ``tz`` a cron
    schedule (``tz=None`` means the ``ScheduleModule`` time zone) and ``delay``
    a one-shot run. The remaining fields are the common job options.
    """

    kind: Literal["interval", "cron", "once"]
    name: str | None = None
    seconds: float | None = None
    initial_delay: float = 0.0
    mode: IntervalMode = "fixed_rate"
    cron: CronExpression | None = None
    tz: tzinfo | None = None
    delay: float = 0.0
    overlap: Overlap = "skip"
    max_instances: int = 1
    timeout: float | None = None
    jitter: float = 0.0
    misfire: Misfire = "coalesce"
    misfire_grace: float | None = None
    exclusive: bool = False
    enabled: bool = True

    def describe(self, default_tz: tzinfo | None = None) -> str:
        """A one-line, human-readable description of the schedule."""
        if self.kind == "interval":
            text = f"every {_fmt_seconds(self.seconds or 0.0)}"
            if self.mode == "fixed_delay":
                text += " after each run"
            if self.initial_delay:
                text += f", first after {_fmt_seconds(self.initial_delay)}"
            return text
        if self.kind == "cron":
            return f"cron {self.cron} ({timezone_name(self.tz or default_tz)})"
        return f"once, {_fmt_seconds(self.delay)} after startup"


@dataclass(frozen=True, slots=True)
class HostedServiceMeta:
    """Options of a ``@hosted_service(...)`` class (see the decorator for each field)."""

    name: str | None = None
    restart: Restart = "on-failure"
    max_restarts: int = 5
    restart_window: float = 300.0
    backoff: tuple[float, float] = (1.0, 30.0)
    on_exhausted: OnExhausted = "fail_readiness"
    wait_ready: bool = False
    start_timeout: float = 30.0
    stop_timeout: float = 10.0
    enabled: bool = True


def _fmt_seconds(value: float) -> str:
    return f"{value:g}s"


# ---------------------------------------------------------------------------
# Argument validation
# ---------------------------------------------------------------------------


def _usage(decorator: str, message: str, **detail: Any) -> DecoratorUsageError:
    label = decorator if "." in decorator else f"@{decorator}"
    return DecoratorUsageError(f"{label}: {message}", detail={"decorator": decorator, **detail})


def _number(decorator: str, option: str, value: Any, *, positive: bool, example: str) -> float:
    if isinstance(value, timedelta):
        value = value.total_seconds()
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise _usage(
            decorator,
            f"{option}= must be a number of seconds, got {value!r}; for example {example}",
            option=option,
            reason="invalid_number",
        )
    if value < 0 or (positive and value == 0):
        bound = "greater than 0" if positive else "0 or more"
        raise _usage(
            decorator,
            f"{option}= must be {bound}, got {value!r}; for example {example}",
            option=option,
            reason="out_of_range",
        )
    return float(value)


def _optional_positive(decorator: str, option: str, value: Any, example: str) -> float | None:
    if value is None:
        return None
    return _number(decorator, option, value, positive=True, example=example)


def _choice(decorator: str, option: str, value: Any, choices: tuple[str, ...]) -> Any:
    if value not in choices:
        allowed = ", ".join(repr(c) for c in choices)
        raise _usage(
            decorator,
            f"{option}= must be one of {allowed}, got {value!r}",
            option=option,
            reason="invalid_choice",
        )
    return value


def _flag(decorator: str, option: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise _usage(
            decorator, f"{option}= must be True or False, got {value!r}", option=option, reason="invalid_flag"
        )
    return value


def _job_name(decorator: str, value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise _usage(
            decorator,
            f"name= must be a non-empty string, got {value!r}",
            option="name",
            reason="invalid_name",
        )
    return value


def _common(
    decorator: str,
    *,
    name: Any,
    overlap: Any,
    max_instances: Any,
    timeout: Any,
    jitter: Any,
    misfire: Any,
    misfire_grace: Any,
    exclusive: Any,
    enabled: Any,
) -> dict[str, Any]:
    overlap = _choice(decorator, "overlap", overlap, _OVERLAPS)
    if max_instances is None:
        max_instances = 3 if overlap == "allow" else 1
    if isinstance(max_instances, bool) or not isinstance(max_instances, int) or max_instances < 1:
        raise _usage(
            decorator,
            f"max_instances= must be an integer of at least 1, got {max_instances!r}",
            option="max_instances",
            reason="out_of_range",
        )
    return {
        "name": _job_name(decorator, name),
        "overlap": overlap,
        "max_instances": max_instances,
        "timeout": _optional_positive(decorator, "timeout", timeout, "timeout=300"),
        "jitter": _number(decorator, "jitter", jitter, positive=False, example="jitter=5"),
        "misfire": _choice(decorator, "misfire", misfire, _MISFIRES),
        "misfire_grace": _optional_positive(decorator, "misfire_grace", misfire_grace, "misfire_grace=30"),
        "exclusive": _flag(decorator, "exclusive", exclusive),
        "enabled": _flag(decorator, "enabled", enabled),
    }


def _attach(decorator: str, target: Any, meta: ScheduleMeta) -> None:
    function = target.__func__ if isinstance(target, (staticmethod, classmethod)) else target
    if isinstance(target, type) or not callable(function):
        raise _usage(
            decorator,
            f"decorate a method of an @injectable() class, not {getattr(target, '__qualname__', repr(target))}",
            reason="not_a_method",
        )
    if inspect.isasyncgenfunction(function) or inspect.isgeneratorfunction(function):
        raise _usage(
            decorator,
            f"{getattr(function, '__qualname__', function)} is a generator; a scheduled job must be a plain "
            "or async method",
            reason="generator_job",
        )
    existing = getattr(target, SCHEDULE_META, None) or []
    setattr(target, SCHEDULE_META, [*existing, meta])


# ---------------------------------------------------------------------------
# Scheduled methods
# ---------------------------------------------------------------------------


def interval(
    seconds: Any = _MISSING,
    *,
    initial_delay: float | timedelta = 0.0,
    mode: IntervalMode = "fixed_rate",
    name: str | None = None,
    overlap: Overlap = "skip",
    max_instances: int | None = None,
    timeout: float | timedelta | None = None,
    jitter: float | timedelta = 0.0,
    misfire: Misfire = "coalesce",
    misfire_grace: float | timedelta | None = None,
    exclusive: bool = False,
    enabled: bool = True,
) -> Callable[[F], F]:
    """Run the decorated singleton method every ``seconds``.

    ``mode="fixed_rate"`` fires on a grid anchored at startup (``READY``) plus
    ``initial_delay``, so the first run is ``initial_delay`` seconds after
    startup; ``"fixed_delay"`` waits ``seconds`` after each run completes.
    ``seconds`` / ``initial_delay`` / ``timeout`` / ``jitter`` /
    ``misfire_grace`` take seconds or a :class:`~datetime.timedelta`. See
    :func:`cron` for the common options. Requires ``ScheduleModule.for_root()``.
    """
    if seconds is not _MISSING:
        _reject_bare_usage("interval", seconds)
    if seconds is _MISSING:
        raise _usage(
            "interval", "pass the period, for example @interval(seconds=60)", reason="missing_seconds"
        )
    period = _number("interval", "seconds", seconds, positive=True, example="@interval(seconds=60)")
    meta = ScheduleMeta(
        kind="interval",
        seconds=period,
        initial_delay=_number(
            "interval", "initial_delay", initial_delay, positive=False, example="initial_delay=5"
        ),
        mode=_choice("interval", "mode", mode, _MODES),
        **_common(
            "interval",
            name=name,
            overlap=overlap,
            max_instances=max_instances,
            timeout=timeout,
            jitter=jitter,
            misfire=misfire,
            misfire_grace=misfire_grace,
            exclusive=exclusive,
            enabled=enabled,
        ),
    )

    def _apply(target: F) -> F:
        _attach("interval", target, meta)
        return target

    return _apply


def cron(
    expression: Any = _MISSING,
    *,
    tz: str | tzinfo | None = None,
    name: str | None = None,
    overlap: Overlap = "skip",
    max_instances: int | None = None,
    timeout: float | timedelta | None = None,
    jitter: float | timedelta = 0.0,
    misfire: Misfire = "coalesce",
    misfire_grace: float | timedelta | None = None,
    exclusive: bool = False,
    enabled: bool = True,
) -> Callable[[F], F]:
    """Run the decorated singleton method on a cron schedule.

    ``expression`` has five fields (``minute hour day-of-month month
    day-of-week``) or six with a leading seconds field, or is a macro such as
    ``"@daily"``; it is validated here (:class:`~lauren.scheduling.CronExpression`).
    ``tz`` is an IANA zone (default: the ``ScheduleModule`` time zone).

    Common options (``@interval`` and ``@run_once`` take them too): ``name``
    (default ``Class.method``, unique app-wide); ``overlap`` — ``"skip"`` a
    fire while the previous run is still going, ``"queue"`` one run after it,
    or ``"allow"`` concurrent runs; ``max_instances`` (1, or 3 with
    ``"allow"``); ``timeout`` in seconds (the run is cancelled and reported as
    a ``TimeoutError``); ``jitter`` — a random ``0..jitter`` second delay per
    fire; ``misfire`` — when the scheduler wakes more than ``misfire_grace``
    seconds late, ``"coalesce"`` runs once now and ``"skip"`` waits for the next
    fire; ``exclusive`` — take the ``SchedulerLock`` for each fire so one
    worker runs it; ``enabled`` — a static switch.
    """
    if expression is not _MISSING:
        _reject_bare_usage("cron", expression)
    if expression is _MISSING:
        raise _usage(
            "cron", "pass an expression, for example @cron('0 3 * * *')", reason="missing_expression"
        )
    parsed = expression if isinstance(expression, CronExpression) else CronExpression(expression)
    zone = resolve_timezone(tz, where="@cron") if tz is not None else parsed.tz
    meta = ScheduleMeta(
        kind="cron",
        cron=parsed.with_tz(None) if parsed.tz is not None else parsed,
        tz=zone,
        **_common(
            "cron",
            name=name,
            overlap=overlap,
            max_instances=max_instances,
            timeout=timeout,
            jitter=jitter,
            misfire=misfire,
            misfire_grace=misfire_grace,
            exclusive=exclusive,
            enabled=enabled,
        ),
    )

    def _apply(target: F) -> F:
        _attach("cron", target, meta)
        return target

    return _apply


def run_once(
    delay: Any = 0.0,
    *,
    name: str | None = None,
    overlap: Overlap = "skip",
    max_instances: int | None = None,
    timeout: float | timedelta | None = None,
    jitter: float | timedelta = 0.0,
    misfire: Misfire = "coalesce",
    misfire_grace: float | timedelta | None = None,
    exclusive: bool = False,
    enabled: bool = True,
) -> Callable[[F], F]:
    """Run the decorated singleton method once, ``delay`` seconds after startup (``READY``).

    The one-shot counterpart of :func:`interval` (NestJS ``@Timeout``). Takes
    the common options described on :func:`cron`.
    """
    _reject_bare_usage("run_once", delay)
    meta = ScheduleMeta(
        kind="once",
        delay=_number("run_once", "delay", delay, positive=False, example="@run_once(delay=10)"),
        **_common(
            "run_once",
            name=name,
            overlap=overlap,
            max_instances=max_instances,
            timeout=timeout,
            jitter=jitter,
            misfire=misfire,
            misfire_grace=misfire_grace,
            exclusive=exclusive,
            enabled=enabled,
        ),
    )

    def _apply(target: F) -> F:
        _attach("run_once", target, meta)
        return target

    return _apply


# ---------------------------------------------------------------------------
# Hosted services
# ---------------------------------------------------------------------------

_RUN_SIGNATURE = "async def run(self, ctx: ServiceContext) -> None"


def hosted_service(
    *args: Any,
    name: str | None = None,
    restart: Restart = "on-failure",
    max_restarts: int = 5,
    restart_window: float | timedelta = 300.0,
    backoff: tuple[float, float] = (1.0, 30.0),
    on_exhausted: OnExhausted = "fail_readiness",
    wait_ready: bool = False,
    start_timeout: float | timedelta = 30.0,
    stop_timeout: float | timedelta = 10.0,
    enabled: bool = True,
) -> Callable[[C], C]:
    """Mark a class as a hosted service: its ``run(ctx)`` runs for the app's lifetime.

    The framework starts ``run`` after every ``@post_construct`` hook (in the
    providers' topological order), supervises it, and stops services in
    reverse order during shutdown — after requests drain and *before*
    ``@pre_destruct``. The class must define ``async def run(self, ctx)``
    (or a plain ``def``, run on a daemon thread; it must poll
    ``ctx.stop_requested``) and be listed in a module's ``providers``. It is
    made a singleton ``@injectable`` unless it carries its own ``@injectable``.

    ``restart`` is ``"never"``, ``"on-failure"`` or ``"always"`` (also after a
    normal return); at most ``max_restarts`` restarts within
    ``restart_window`` seconds, waiting a random ``0..min(cap, base * 2**n)``
    seconds before the n-th (``backoff=(base, cap)``). When restarts run out,
    ``on_exhausted="fail_readiness"`` marks the service unhealthy and
    ``"log"`` only logs. ``wait_ready=True`` makes startup wait up to
    ``start_timeout`` seconds for ``ctx.mark_ready()``. On shutdown the
    service has ``stop_timeout`` seconds to return before it is cancelled.
    ``enabled=False`` keeps it from starting. Must be called with parentheses.
    """
    if args:
        _reject_bare_usage("hosted_service", args[0])
        raise _usage(
            "hosted_service", "takes keyword arguments only, for example @hosted_service(restart='always')"
        )
    if isinstance(backoff, (list, tuple)) and len(backoff) == 2:
        base = _number("hosted_service", "backoff", backoff[0], positive=False, example="backoff=(1.0, 30.0)")
        cap = _number("hosted_service", "backoff", backoff[1], positive=False, example="backoff=(1.0, 30.0)")
        if cap < base:
            raise _usage(
                "hosted_service",
                f"backoff=(base, cap) needs cap >= base, got {backoff!r}; for example backoff=(1.0, 30.0)",
                option="backoff",
                reason="invalid_backoff",
            )
    else:
        raise _usage(
            "hosted_service",
            f"backoff= must be a (base, cap) pair of seconds, got {backoff!r}; for example backoff=(1.0, 30.0)",
            option="backoff",
            reason="invalid_backoff",
        )
    if isinstance(max_restarts, bool) or not isinstance(max_restarts, int) or max_restarts < 0:
        raise _usage(
            "hosted_service",
            f"max_restarts= must be an integer of at least 0, got {max_restarts!r}",
            option="max_restarts",
            reason="out_of_range",
        )
    meta = HostedServiceMeta(
        name=_job_name("hosted_service", name),
        restart=_choice("hosted_service", "restart", restart, _RESTARTS),
        max_restarts=max_restarts,
        restart_window=_number(
            "hosted_service", "restart_window", restart_window, positive=True, example="restart_window=300"
        ),
        backoff=(base, cap),
        on_exhausted=_choice("hosted_service", "on_exhausted", on_exhausted, _EXHAUSTED),
        wait_ready=_flag("hosted_service", "wait_ready", wait_ready),
        start_timeout=_number(
            "hosted_service", "start_timeout", start_timeout, positive=True, example="start_timeout=30"
        ),
        stop_timeout=_number(
            "hosted_service", "stop_timeout", stop_timeout, positive=False, example="stop_timeout=10"
        ),
        enabled=_flag("hosted_service", "enabled", enabled),
    )

    def _apply(cls: C) -> C:
        if not isinstance(cls, type):
            raise _usage("hosted_service", f"decorate a class, not {cls!r}", reason="not_a_class")
        validate_run_method(cls)
        setattr(cls, HOSTED_SERVICE_META, meta)
        if INJECTABLE_META not in cls.__dict__:
            setattr(cls, INJECTABLE_META, InjectableMeta(scope=Scope.SINGLETON))
        return cls

    return _apply


def validate_run_method(cls: type) -> Callable[..., Any]:
    """Check that ``cls`` defines ``run(self, ctx)``; return the function.

    Raises :class:`~lauren.exceptions.LifecycleConfigError` with the expected
    signature otherwise.
    """

    def _fail(problem: str) -> LifecycleConfigError:
        return LifecycleConfigError(
            f"@hosted_service() class {cls.__name__} {problem}; define `{_RUN_SIGNATURE}`",
            detail={"class": cls.__name__, "reason": "invalid_run_method"},
        )

    raw = inspect.getattr_static(cls, "run", None)
    if raw is None:
        raise _fail("has no run() method")
    if isinstance(raw, (staticmethod, classmethod)) or not inspect.isfunction(raw):
        raise _fail("must define run() as a regular instance method")
    if inspect.isasyncgenfunction(raw) or inspect.isgeneratorfunction(raw):
        raise _fail("defines run() as a generator")
    params = list(inspect.signature(raw).parameters.values())[1:]
    positional = [
        p
        for p in params
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    required = [p for p in positional if p.default is inspect.Parameter.empty]
    has_varargs = any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in params)
    required_kw = [
        p for p in params if p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    ]
    if required_kw or len(required) > 1 or not (positional or has_varargs):
        raise _fail("must take exactly one argument besides self (the ServiceContext)")
    return raw


# ---------------------------------------------------------------------------
# Discovery hooks (import-time declarations)
# ---------------------------------------------------------------------------

register_marker(MarkerSpec(SCHEDULE_META, kind="method", framework_types=(JobContext,)))
register_marker(MarkerSpec(HOSTED_SERVICE_META, kind="class"))
register_framework_value(JobContext)
register_framework_value(ServiceContext)


__all__ = [
    "HOSTED_SERVICE_META",
    "SCHEDULE_META",
    "HostedServiceMeta",
    "ScheduleMeta",
    "cron",
    "hosted_service",
    "interval",
    "run_once",
    "validate_run_method",
]
