"""Health checks and probes (private; the public surface is :mod:`lauren.health`).

* ``_types`` — ``HealthStatus``, ``HealthResult`` and the aggregation rule.
* ``_meta`` — ``@health_check`` and its marker, registered with the shared
  startup discovery pass (``lauren._discovery``) on import.
* ``_indicators`` — built-in indicators: event-loop lag sampler, disk space,
  process memory.
* ``_runtime`` — check validation (``compile_checks``) and the per-app
  ``HealthRuntime``: execution with timeouts, single-flight, caching, probe
  and report evaluation, ``HealthChanged`` signals.
* ``_module`` — ``HealthModule.for_root``, the generated probe controller and
  the factory hooks (``install_health`` in Phase 2, ``compile_health`` after
  Phase 6a).
"""

from __future__ import annotations

from ._meta import HEALTH_CHECK_META, HealthCheckMeta, health_check
from ._module import HealthModule, HealthOptions, compile_health, install_health
from ._runtime import HealthRuntime
from ._types import HealthResult, HealthStatus, aggregate

__all__ = [
    "HEALTH_CHECK_META",
    "HealthCheckMeta",
    "HealthModule",
    "HealthOptions",
    "HealthResult",
    "HealthRuntime",
    "HealthStatus",
    "aggregate",
    "compile_health",
    "health_check",
    "install_health",
]
