"""``HealthModule.for_root``: options, the generated probe controller and the factory hooks.

``LaurenFactory.create`` calls :func:`install_health` in Phase 2 (one
:class:`~lauren._health._runtime.HealthRuntime` per app when a
``HealthModule`` is imported, registered as a global value provider),
:func:`compile_health` right after the Phase 6a discovery pass (check
validation runs whether or not the module is imported) and binds the runtime
to the app in Phase 7.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..decorators import PUBLIC_METADATA_KEY, controller, get, module, set_metadata
from ..exceptions import DecoratorUsageError, GuardConfigError, StartupError
from ..logging import Logger
from ..types import ExecutionContext, Request, Response
from ._indicators import memory_supported
from ._meta import HEALTH_CHECK_META, LIVE, READY, STARTUP
from ._runtime import HealthRuntime, compile_checks

if TYPE_CHECKING:
    from .._di import DIContainer
    from .._discovery import DiscoveryIndex

#: Attribute ``for_root`` sets on the generated module class.
HEALTH_OPTIONS_ATTR = "__lauren_health_options__"
DETAILS_POLICIES = ("never", "always", "when_authorized")
FORMATS = ("lauren", "ietf")
_HELPER = "HealthModule.for_root"


@dataclass(frozen=True, slots=True)
class HealthOptions:
    """The validated arguments of one ``HealthModule.for_root(...)`` call."""

    path: str
    details: str
    details_guard: type | None
    format: str
    event_loop_lag_ms: float | None
    disk_paths: tuple[tuple[str, int], ...]
    max_rss_bytes: int | None
    version: str | None
    report_status_code: bool
    degraded_status: int


class HealthModule:
    """Liveness, readiness and startup probes plus a full health report.

    Import the module :meth:`for_root` returns into your root module. The
    probes read the application's lifecycle state and the
    ``@health_check`` methods discovered on singleton providers.
    """

    @classmethod
    def for_root(
        cls,
        *,
        path: str = "/health",
        details: str = "never",
        details_guard: type | None = None,
        format: str = "lauren",
        event_loop_lag_ms: float | None = None,
        disk_paths: Mapping[str | os.PathLike[str], int] | None = None,
        max_rss_bytes: int | None = None,
        version: str | None = None,
        report_status_code: bool = True,
        degraded_status: int = 200,
    ) -> type:
        """Return a module serving ``{path}`` (report), ``{path}/live``, ``/ready`` and ``/startup``.

        ``details`` governs what the full report shows beyond check names and
        statuses: ``"never"`` (default), ``"always"`` or
        ``"when_authorized"`` — then ``details_guard`` (a guard class,
        resolved through DI) decides per request, and a denial only hides
        the details. ``format="ietf"`` answers ``application/health+json``
        (``pass`` / ``warn`` / ``fail``). ``event_loop_lag_ms``,
        ``disk_paths`` (``{path: min_free_bytes}``) and ``max_rss_bytes``
        enable the built-in indicators. ``version`` is added to the report.
        The report answers 503 when its aggregate is down unless
        ``report_status_code=False``; ``degraded_status`` is the status code
        for a degraded probe or report (default 200, so the instance stays in
        rotation). The routes are kept out of OpenAPI and carry
        ``PUBLIC_METADATA_KEY`` so global guards that honour it let probes
        through.
        """
        options = _make_options(
            path=path,
            details=details,
            details_guard=details_guard,
            format=format,
            event_loop_lag_ms=event_loop_lag_ms,
            disk_paths=disk_paths,
            max_rss_bytes=max_rss_bytes,
            version=version,
            report_status_code=report_status_code,
            degraded_status=degraded_status,
        )
        namespace = {
            "__doc__": "Generated HealthModule (liveness, readiness and startup probes).",
            "__module__": __name__,
            HEALTH_OPTIONS_ATTR: options,
        }
        generated: type = module(controllers=[_make_controller(options.path)])(
            type(f"HealthModule[{options.path}]", (), namespace)
        )
        return generated


def _usage(message: str, **detail: Any) -> DecoratorUsageError:
    return DecoratorUsageError(message, detail={"helper": _HELPER, **detail})


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _make_options(
    *,
    path: Any,
    details: Any,
    details_guard: Any,
    format: Any,
    event_loop_lag_ms: Any,
    disk_paths: Any,
    max_rss_bytes: Any,
    version: Any,
    report_status_code: Any,
    degraded_status: Any,
) -> HealthOptions:
    """Validate ``for_root``'s arguments (``DecoratorUsageError`` / ``GuardConfigError``)."""
    if not isinstance(path, str) or not path.startswith("/"):
        raise _usage(
            f'{_HELPER}(path={path!r}) must be a string starting with "/", e.g. "/health"', path=repr(path)
        )
    if details not in DETAILS_POLICIES:
        raise _usage(
            f'{_HELPER}(details={details!r}) is invalid; use "never", "always" or "when_authorized"',
            details=repr(details),
        )
    if details == "when_authorized" and details_guard is None:
        raise GuardConfigError(
            f'{_HELPER}(details="when_authorized") needs details_guard=<a guard class> to decide who sees details',
            detail={"helper": _HELPER, "reason": "missing_details_guard"},
        )
    if details_guard is not None:
        name = getattr(details_guard, "__name__", repr(details_guard))
        if not isinstance(details_guard, type) or not callable(getattr(details_guard, "can_activate", None)):
            raise GuardConfigError(
                f"{_HELPER}(details_guard={name}) must be a guard class defining 'can_activate(context)'",
                detail={"helper": _HELPER, "guard": name, "reason": "invalid_details_guard"},
            )
        if details != "when_authorized":
            raise GuardConfigError(
                f'{_HELPER}(details_guard={name}) only applies with details="when_authorized" '
                f"(got details={details!r})",
                detail={"helper": _HELPER, "guard": name, "reason": "unused_details_guard"},
            )
    if format not in FORMATS:
        raise _usage(f'{_HELPER}(format={format!r}) is invalid; use "lauren" or "ietf"', format=repr(format))
    if event_loop_lag_ms is not None and (not _is_number(event_loop_lag_ms) or event_loop_lag_ms <= 0):
        raise _usage(
            f"{_HELPER}(event_loop_lag_ms={event_loop_lag_ms!r}) must be None or a number of milliseconds > 0",
            event_loop_lag_ms=repr(event_loop_lag_ms),
        )
    disks: list[tuple[str, int]] = []
    if disk_paths is not None:
        if not isinstance(disk_paths, Mapping):
            raise _usage(
                f'{_HELPER}(disk_paths={disk_paths!r}) must map paths to minimum free bytes, e.g. {{"/": 1 << 30}}',
                disk_paths=repr(disk_paths),
            )
        for raw_path, min_free in disk_paths.items():
            if not isinstance(raw_path, (str, os.PathLike)) or not _is_int(min_free) or min_free < 0:
                raise _usage(
                    f"{_HELPER}(disk_paths=...) entry {raw_path!r}: {min_free!r} must map a path to an int >= 0",
                    disk_paths=repr(disk_paths),
                )
            disks.append((os.fspath(raw_path), min_free))
    if max_rss_bytes is not None:
        if not _is_int(max_rss_bytes) or max_rss_bytes <= 0:
            raise _usage(
                f"{_HELPER}(max_rss_bytes={max_rss_bytes!r}) must be None or an int > 0",
                max_rss_bytes=repr(max_rss_bytes),
            )
        if not memory_supported():
            raise _usage(
                f"{_HELPER}(max_rss_bytes=...) is not supported on this platform (no /proc or resource module); "
                "leave it None here",
                max_rss_bytes=repr(max_rss_bytes),
                reason="unsupported_platform",
            )
    if version is not None and not isinstance(version, str):
        raise _usage(f"{_HELPER}(version={version!r}) must be None or a string", version=repr(version))
    if not isinstance(report_status_code, bool):
        raise _usage(
            f"{_HELPER}(report_status_code={report_status_code!r}) must be a bool",
            report_status_code=repr(report_status_code),
        )
    if not _is_int(degraded_status) or not 200 <= degraded_status <= 599:
        raise _usage(
            f"{_HELPER}(degraded_status={degraded_status!r}) must be an HTTP status code between 200 and 599",
            degraded_status=repr(degraded_status),
        )
    return HealthOptions(
        path=path.rstrip("/") or "/",
        details=details,
        details_guard=details_guard,
        format=format,
        event_loop_lag_ms=float(event_loop_lag_ms) if event_loop_lag_ms is not None else None,
        disk_paths=tuple(disks),
        max_rss_bytes=max_rss_bytes,
        version=version,
        report_status_code=report_status_code,
        degraded_status=degraded_status,
    )


def _make_controller(prefix: str) -> type:
    """A fresh probe controller per ``for_root`` call, so several mounts stay distinct classes."""

    @set_metadata(PUBLIC_METADATA_KEY, True)
    @controller(prefix)
    class HealthController:
        """Generated probe endpoints (``lauren.health``); kept out of OpenAPI."""

        def __init__(self, runtime: HealthRuntime) -> None:
            self._runtime = runtime

        @get("/", include_in_schema=False)
        async def report(self, ctx: ExecutionContext, request: Request) -> Response:
            return await self._runtime.report_response(ctx, request)

        @get("/live", include_in_schema=False)
        async def live(self) -> Response:
            return await self._runtime.probe_response(LIVE)

        @get("/ready", include_in_schema=False)
        async def ready(self) -> Response:
            return await self._runtime.probe_response(READY)

        @get("/startup", include_in_schema=False)
        async def startup(self) -> Response:
            return await self._runtime.probe_response(STARTUP)

    return HealthController


# ---------------------------------------------------------------------------
# Factory hooks
# ---------------------------------------------------------------------------


def _health_options(graph: Any) -> HealthOptions | None:
    found: list[HealthOptions] = []
    for mod in graph.modules:
        options = mod.__dict__.get(HEALTH_OPTIONS_ATTR)
        if options is not None and options not in found:
            found.append(options)
    if len(found) > 1:
        raise StartupError(
            f"{_HELPER}() is imported with different options ({', '.join(repr(o) for o in found)}); "
            "import one HealthModule per application",
            detail={"reason": "conflicting_health_options", "options": [repr(o) for o in found]},
        )
    return found[0] if found else None


def install_health(graph: Any, container: DIContainer) -> HealthRuntime | None:
    """Phase 2: build the app's runtime when a ``HealthModule`` is imported (else ``None``).

    The details guard is registered like a global guard when no module
    provides it, so it resolves through DI.
    """
    options = _health_options(graph)
    if options is None:
        return None
    runtime = HealthRuntime(options)
    container.register_value(HealthRuntime, runtime)
    guard = options.details_guard
    if guard is not None and guard not in {p.cls for p in container.all_providers()}:
        from .._asgi import _ensure_injectable  # ``_asgi`` imports this module

        _ensure_injectable(guard)
        container.register(guard)
    return runtime


def compile_health(
    runtime: HealthRuntime | None, discovery: DiscoveryIndex, container: DIContainer, *, logger: Logger
) -> None:
    """Phase 6a: validate every ``@health_check`` method and install the checks in the runtime."""
    checks = compile_checks(discovery.methods_for(HEALTH_CHECK_META))
    if runtime is None:
        return
    runtime._install(checks, container)
    logger.verbose(
        f"Health checks compiled: {', '.join(c.name for c in runtime.checks) or 'none'} "
        f"(probes at {runtime.options.path})",
        context="Health",
    )


__all__ = [
    "DETAILS_POLICIES",
    "FORMATS",
    "HEALTH_OPTIONS_ATTR",
    "HealthModule",
    "HealthOptions",
    "compile_health",
    "install_health",
]
