"""Health value types: ``HealthStatus``, ``HealthResult`` and the aggregation rule."""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any


class HealthStatus(enum.Enum):
    """The status of one check, or of a whole probe or report."""

    UP = "up"
    DEGRADED = "degraded"
    DOWN = "down"


@dataclass(frozen=True, slots=True)
class HealthResult:
    """What a ``@health_check`` method may return: a status plus details for the report.

    Build one with :meth:`up`, :meth:`degraded` or :meth:`down`, passing the
    details as keyword arguments (``HealthResult.degraded(latency_ms=612.0,
    reason="slow")``). Details appear in the full report only when the
    details policy allows it; probes never show them. A check may also
    return ``None`` / ``True`` (up) or ``False`` (down), or raise (down).
    """

    status: HealthStatus
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", HealthStatus(self.status))
        if not isinstance(self.details, Mapping):
            raise TypeError(f"HealthResult details must be a mapping, got {type(self.details).__name__}")
        object.__setattr__(self, "details", dict(self.details))

    @classmethod
    def up(cls, **details: Any) -> HealthResult:
        """The check passed."""
        return cls(HealthStatus.UP, details)

    @classmethod
    def degraded(cls, **details: Any) -> HealthResult:
        """The check works but not well (slow, partially available); probes still pass."""
        return cls(HealthStatus.DEGRADED, details)

    @classmethod
    def down(cls, **details: Any) -> HealthResult:
        """The check failed."""
        return cls(HealthStatus.DOWN, details)


def aggregate(outcomes: Iterable[tuple[bool, HealthStatus]]) -> HealthStatus:
    """Combine ``(critical, status)`` pairs into one status.

    Any critical ``DOWN`` makes the aggregate ``DOWN``; otherwise any ``DOWN``
    (non-critical) or ``DEGRADED`` makes it ``DEGRADED``; otherwise (no
    checks included) it is ``UP``.
    """
    result = HealthStatus.UP
    for critical, status in outcomes:
        if status is HealthStatus.DOWN and critical:
            return HealthStatus.DOWN
        if status is not HealthStatus.UP:
            result = HealthStatus.DEGRADED
    return result


__all__ = ["HealthResult", "HealthStatus", "aggregate"]
