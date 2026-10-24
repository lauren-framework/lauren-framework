"""Composing utilities — apply guard/interceptor chains in contexts where
Lauren's HTTP pipeline doesn't run (e.g. WebSocket connections).

These functions are used internally by :func:`~lauren._ws_runtime.handle_websocket`
and are also public so that extension authors (e.g. ``lauren-mcp``,
third-party WS transports) can build their own guard/interceptor wiring
without duplicating logic.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable

if TYPE_CHECKING:
    from ..types import CallHandler
    from .._ws_runtime import WsConnectionContext

__all__ = [
    "apply_guards",
    "apply_interceptors",
]

_logger = logging.getLogger(__name__)


async def apply_guards(
    guard_classes: tuple[type, ...],
    ctx: WsConnectionContext,
    *,
    container: Any,
    request_cache: dict[type, Any],
    framework_values: dict[type, Any],
    owning_module: type | None = None,
    on_denied: Callable[[type], Any] | None = None,
) -> bool:
    """Resolve and call each guard in order.

    Calls ``guard.can_activate(ctx)`` for every guard class.  Returns
    ``False`` on the first rejection and does **not** call subsequent guards
    (short-circuit).  Returns ``True`` when all guards pass.

    Guard exceptions are caught, logged at ERROR level, and treated as
    rejections so a broken guard never silently admits a connection.

    ``on_denied``, when given, is called with the guard class whose
    ``can_activate`` returned a falsy value (not for a guard that raised) —
    Lauren's WS runtime uses it to put the guard's ``deny_error`` code in the
    close reason.

    Parameters
    ----------
    guard_classes:
        Ordered tuple of guard types to check.
    ctx:
        :class:`~lauren.reflect.WsConnectionContext` built from the
        WebSocket upgrade request.
    container:
        Lauren's DI container for the current application.
    request_cache:
        Request-scoped instance cache shared across this connection.
    framework_values:
        ``{type: instance}`` map for DI short-circuit lookups.
    owning_module:
        The module that owns the gateway; used to enforce module-scoped
        provider visibility.
    """
    for guard_cls in guard_classes:
        raised = False
        try:
            guard = await container.resolve(
                guard_cls,
                request_cache=request_cache,
                framework_values=framework_values,
                owning_module=owning_module,
            )
            allowed: bool = await guard.can_activate(ctx)
        except Exception:
            _logger.exception(
                "Guard %r raised during WebSocket connection check — treating as rejection",
                guard_cls.__name__,
            )
            allowed = False
            raised = True

        if not allowed:
            _logger.debug(
                "WebSocket connection rejected by guard %r at %s",
                guard_cls.__name__,
                ctx.route_template,
            )
            if on_denied is not None and not raised:
                on_denied(guard_cls)
            return False

    return True


async def apply_interceptors(
    interceptor_classes: tuple[type, ...],
    handler: Callable[[], Awaitable[Any]],
    ctx: WsConnectionContext,
    *,
    container: Any,
    request_cache: dict[type, Any],
    framework_values: dict[type, Any],
    owning_module: type | None = None,
) -> Any:
    """Wrap *handler* with an interceptor chain and invoke it.

    Builds an outermost-first interceptor stack around *handler* (the
    innermost callable) and then calls the outermost interceptor.  Each
    interceptor receives ``ctx`` and a :class:`~lauren.types.CallHandler`
    whose ``handle()`` advances to the next layer.

    The return value is whatever the outermost interceptor returns —
    typically ``None`` for WS ``@on_connect`` hooks.

    Parameters
    ----------
    interceptor_classes:
        Ordered tuple of interceptor types (outermost first).
    handler:
        The innermost coroutine (e.g. the ``@on_connect`` hook).
    ctx:
        :class:`~lauren.reflect.WsConnectionContext` for this connection.
    container, request_cache, framework_values, owning_module:
        DI resolution parameters — same semantics as in :func:`apply_guards`.
    """
    from ..types import CallHandler  # noqa: PLC0415

    call_handler: "CallHandler" = CallHandler(handler)

    for inter_cls in reversed(interceptor_classes):
        inter_inst = await container.resolve(
            inter_cls,
            request_cache=request_cache,
            framework_values=framework_values,
            owning_module=owning_module,
        )
        call_handler = _layer(inter_inst, ctx, call_handler)

    return await call_handler.handle()


def _layer(interceptor: Any, ctx: Any, next_handler: "CallHandler") -> "CallHandler":
    """Wrap *next_handler* in one interceptor layer.

    A separate function so each layer's closure binds its own interceptor and
    successor — closures created inline in the loop would all see the last
    iteration's values and recurse into themselves.
    """
    from ..types import CallHandler  # noqa: PLC0415

    async def _invoke() -> Any:
        return await interceptor.intercept(ctx, next_handler)

    return CallHandler(_invoke)
