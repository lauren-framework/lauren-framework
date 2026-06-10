"""``lauren.reflect`` — Unified cross-cutting concerns for HTTP and WebSocket.

This module provides:

* **Metadata readers** — read guard/interceptor/middleware annotations from
  any decorated class without hard-coding internal attribute name strings.
* **``WsConnectionContext``** — a first-class context object passed to guards
  and interceptors during a WebSocket connection upgrade, mirroring
  :class:`~lauren.types.ExecutionContext` for HTTP so that the same guard
  class works on both transports.
* **Composing utilities** — :func:`apply_guards` and :func:`apply_interceptors`
  are used internally by Lauren's WS runtime and are also public so that
  extension authors can build their own WS transports.

Typical usage::

    from lauren import use_guards, ws_controller, injectable, Scope
    from lauren.reflect import WsConnectionContext

    @injectable(scope=Scope.SINGLETON)
    class ApiKeyGuard:
        # Works on both @controller (HTTP) and @ws_controller (WS)
        # because both context types expose ctx.request.headers
        async def can_activate(self, ctx) -> bool:
            return ctx.request.headers.get("x-api-key") == "valid"

    @use_guards(ApiKeyGuard)
    @ws_controller("/mcp/ws")
    class McpGateway:
        ...
"""

from __future__ import annotations

from ._composer import apply_guards, apply_interceptors
from ._context import WsConnectionContext, WsUpgradeRequest
from ._reader import ReflectedMeta, reflect_all, reflect_guards, reflect_interceptors, reflect_middlewares

__all__ = [
    # Context types
    "WsConnectionContext",
    "WsUpgradeRequest",
    # Readers
    "reflect_guards",
    "reflect_interceptors",
    "reflect_middlewares",
    "reflect_all",
    "ReflectedMeta",
    # Composing utilities
    "apply_guards",
    "apply_interceptors",
]
