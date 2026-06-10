"""``lauren.reflect`` — Metadata introspection and WS cross-cutting concerns.

This module provides:

* **Cross-cutting readers** — read guard / interceptor / middleware annotations
  from any decorated class without hard-coding internal attribute strings.
* **Static class readers** — read :class:`~lauren.decorators.ControllerMeta`,
  :class:`~lauren.decorators.ModuleMeta`, route metadata, WS message metadata,
  exception-handler lists, user metadata, and encoder instances.
* **App-level readers** — query a running :class:`~lauren.LaurenApp`'s compiled
  route and WS gateway tables after startup.
* **``WsConnectionContext``** — context object passed to guards and interceptors
  during a WebSocket connection upgrade, mirroring
  :class:`~lauren.types.ExecutionContext` for HTTP.
* **Composing utilities** — :func:`apply_guards` and :func:`apply_interceptors`
  are used internally by Lauren's WS runtime and are also public for extension
  authors.

Typical usage::

    from lauren import use_guards, ws_controller, injectable, Scope
    from lauren.reflect import WsConnectionContext, reflect_routes

    @injectable(scope=Scope.SINGLETON)
    class ApiKeyGuard:
        async def can_activate(self, ctx) -> bool:
            return ctx.request.headers.get("x-api-key") == "valid"

    @use_guards(ApiKeyGuard)
    @ws_controller("/mcp/ws")
    class McpGateway:
        ...

    # Enumerate all HTTP routes declared on a controller (no app required):
    from lauren import controller, get
    from lauren.reflect import reflect_routes

    @controller("/users")
    class UserController:
        @get("/{id}")
        async def get_user(self): ...

    for r in reflect_routes(UserController):
        print(r.method, r.full_path)   # GET /users/{id}
"""

from __future__ import annotations

from ._app_reader import get_all_routes, get_all_ws_gateways, get_route_metadata
from ._composer import apply_guards, apply_interceptors
from ._context import WsConnectionContext, WsUpgradeRequest
from ._reader import (
    # cross-cutting (v1.6.0)
    ReflectedMeta,
    reflect_all,
    reflect_guards,
    reflect_interceptors,
    reflect_middlewares,
    # Phase 1 — static class readers
    reflect_controller,
    reflect_module,
    reflect_injectable,
    reflect_ws_controller,
    reflect_routes,
    reflect_ws_messages,
    reflect_exception_handlers,
    get_controller_metadata,
    get_module_metadata,
    # Phase 3 — user metadata + encoder
    reflect_user_metadata,
    reflect_encoder,
)
from ._types import (
    ReflectedController,
    ReflectedModule,
    ReflectedRoute,
    ReflectedWsGateway,
    ReflectedWsMessage,
)

__all__ = [
    # Context types
    "WsConnectionContext",
    "WsUpgradeRequest",
    # Cross-cutting readers
    "reflect_guards",
    "reflect_interceptors",
    "reflect_middlewares",
    "reflect_all",
    "ReflectedMeta",
    # Static class readers
    "reflect_controller",
    "reflect_module",
    "reflect_injectable",
    "reflect_ws_controller",
    "reflect_routes",
    "reflect_ws_messages",
    "reflect_exception_handlers",
    "get_controller_metadata",
    "get_module_metadata",
    # User metadata + encoder
    "reflect_user_metadata",
    "reflect_encoder",
    # App-level readers
    "get_all_routes",
    "get_all_ws_gateways",
    "get_route_metadata",
    # Result types
    "ReflectedRoute",
    "ReflectedWsMessage",
    "ReflectedController",
    "ReflectedModule",
    "ReflectedWsGateway",
    # Composing utilities
    "apply_guards",
    "apply_interceptors",
]
