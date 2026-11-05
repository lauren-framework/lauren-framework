# Health

Liveness, readiness and startup probes (`lauren.health`) and the application
lifecycle state they follow. See the [Health & Readiness Probes guide](../guides/health-checks.md)
for probe semantics, the details policy and draining.

## Module

::: lauren.health.HealthModule

## Checks

::: lauren.health.health_check

::: lauren.health.HealthResult

::: lauren.health.HealthStatus

## Lifecycle state and signals

`AppLifecycleState`, `LifecycleStateChanged` and `HealthChanged` are documented
with the other lifecycle events in the [Signals reference](signals.md#lifecycle-state-and-health-events).
