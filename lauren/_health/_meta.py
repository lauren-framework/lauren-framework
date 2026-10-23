"""The ``@health_check`` decorator and its metadata (re-exported by :mod:`lauren.health`)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from .._discovery import MarkerSpec, register_marker
from ..exceptions import DecoratorUsageError

F = TypeVar("F")

#: Marker attribute: a list of :class:`HealthCheckMeta`, one per ``@health_check``.
HEALTH_CHECK_META = "__lauren_health_check__"

#: Probe tags: which endpoint a check feeds (every check is in the full report).
LIVE = "live"
READY = "ready"
STARTUP = "startup"
TAGS = (LIVE, READY, STARTUP)


@dataclass(frozen=True, slots=True)
class HealthCheckMeta:
    """One ``@health_check(...)`` declaration."""

    name: str
    critical: bool
    timeout: float
    cache_ttl: float
    tags: tuple[str, ...]


def _fail(message: str, **detail: Any) -> DecoratorUsageError:
    return DecoratorUsageError(message, detail={"decorator": "health_check", **detail})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def health_check(
    name: str,
    *,
    critical: bool = True,
    timeout: float = 2.0,
    cache_ttl: float = 0.0,
    tags: tuple[str, ...] = (READY,),
) -> Callable[[F], F]:
    """Mark a provider method as a health check; it is discovered at startup.

    The provider must be a ``Scope.SINGLETON`` injectable; the method takes
    no arguments besides ``self`` other than dependencies its module can
    resolve. It returns ``None`` / ``True`` (up), ``False`` (down) or a
    :class:`~lauren.health.HealthResult`; raising counts as down. Sync
    methods run in a worker thread. Every run is bounded by ``timeout``
    seconds (a timeout is down with ``reason: "timeout"``) and, with
    ``cache_ttl > 0``, its result is reused for that many seconds.

    ``tags`` select the probes the check feeds: ``"ready"`` (default),
    ``"live"`` (rarely right: a dependency outage then restarts the
    process) and ``"startup"``. A ``critical=False`` check that is down
    degrades the report instead of failing readiness. Names are unique per
    application. The decorator only records metadata and returns the method
    unchanged; stacking it registers several checks.
    """
    if not isinstance(name, str):
        if callable(name):
            raise _fail(
                '@health_check needs a name: write @health_check("database"), not a bare @health_check',
                target=getattr(name, "__qualname__", repr(name)),
            )
        raise _fail(f"@health_check name must be a string, got {name!r}", name=repr(name))
    if not name.strip():
        raise _fail(
            '@health_check name must be a non-empty string, e.g. @health_check("database")', name=name
        )
    if not isinstance(critical, bool):
        raise _fail(f"@health_check(critical={critical!r}) must be a bool", critical=repr(critical))
    if not _is_number(timeout) or timeout <= 0:
        raise _fail(
            f"@health_check(timeout={timeout!r}) must be a number of seconds > 0", timeout=repr(timeout)
        )
    if not _is_number(cache_ttl) or cache_ttl < 0:
        raise _fail(
            f"@health_check(cache_ttl={cache_ttl!r}) must be a number of seconds >= 0",
            cache_ttl=repr(cache_ttl),
        )
    if isinstance(tags, str) or not isinstance(tags, (tuple, list, frozenset, set)):
        raise _fail(
            f'@health_check(tags={tags!r}) must be a tuple of tags, e.g. tags=("ready",)', tags=repr(tags)
        )
    unknown = [tag for tag in tags if tag not in TAGS]
    if unknown:
        raise _fail(
            f"@health_check(tags={tuple(tags)!r}) has unknown tag(s) {unknown!r}; use 'live', 'ready' or 'startup'",
            tags=repr(tuple(tags)),
        )
    meta = HealthCheckMeta(
        name=name,
        critical=critical,
        timeout=float(timeout),
        cache_ttl=float(cache_ttl),
        tags=tuple(tag for tag in TAGS if tag in tags),
    )

    def decorator(fn: F) -> F:
        target: Any = fn.__func__ if isinstance(fn, (staticmethod, classmethod)) else fn
        if not callable(target) or isinstance(target, type):
            raise _fail(f"@health_check decorates a method, got {target!r}", target=repr(target))
        existing = list(getattr(target, HEALTH_CHECK_META, None) or ())
        existing.append(meta)
        setattr(target, HEALTH_CHECK_META, existing)
        return fn

    return decorator


# Check methods are found by the shared startup discovery pass; parameters
# other than dependencies of the provider's module fail startup there.
register_marker(MarkerSpec(HEALTH_CHECK_META))


__all__ = ["HEALTH_CHECK_META", "LIVE", "READY", "STARTUP", "TAGS", "HealthCheckMeta", "health_check"]
