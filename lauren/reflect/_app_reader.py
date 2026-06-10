"""App-level reflect readers — query a running LaurenApp's compiled dispatch table.

All functions accept any object that duck-types as a ``LaurenApp``; they access
``app._handlers``, ``app._ws_gateways``, and ``app._started`` via ``getattr``
so that no import of ``_asgi`` or ``_ws_runtime`` is needed at module load time.

Functions return an empty tuple / ``None`` when the app has not yet started
(``app._started`` is ``False``) rather than raising, so they are safe to call
in startup hooks or during testing.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from ._types import ReflectedRoute, ReflectedWsGateway, ReflectedWsMessage

if TYPE_CHECKING:
    pass

__all__ = [
    "get_all_routes",
    "get_all_ws_gateways",
    "get_route_metadata",
]


def get_all_routes(app: Any) -> tuple[ReflectedRoute, ...]:
    """Return all compiled HTTP routes from *app*.

    Reads from ``app._handlers`` (a ``dict[(method, path_template),
    CompiledHandler]`` populated during ``LaurenFactory.create()``).

    Returns an empty tuple when the app has not started yet.

    Example::

        app = LaurenFactory.create(AppModule)
        TestClient(app)  # triggers startup

        for route in get_all_routes(app):
            print(route.method, route.full_path, route.handler.__name__)
    """
    if not getattr(app, "_started", False):
        return ()
    handlers: dict[Any, Any] = getattr(app, "_handlers", {})
    result: list[ReflectedRoute] = []
    for (method, path_template), ch in handlers.items():
        rm = ch.route_meta
        result.append(
            ReflectedRoute(
                method=method,
                path=rm.path,
                full_path=path_template,
                summary=rm.summary,
                description=rm.description,
                response_model=rm.response_model,
                responses=dict(rm.responses),
                deprecated=rm.deprecated,
                operation_id=rm.operation_id,
                include_in_schema=rm.include_in_schema,
                tags=tuple(rm.tags),
                handler=ch.handler_fn,
            )
        )
    return tuple(result)


def get_all_ws_gateways(app: Any) -> tuple[ReflectedWsGateway, ...]:
    """Return all compiled WebSocket gateways from *app*.

    Reads from ``app._ws_gateways`` (a ``dict[path_template, CompiledGateway]``
    populated during ``LaurenFactory.create()``).

    Returns an empty tuple when the app has not started or has no WS gateways.

    Example::

        for gw in get_all_ws_gateways(app):
            print(gw.path_template, gw.guards)
    """
    if not getattr(app, "_started", False):
        return ()
    gateways: dict[str, Any] = getattr(app, "_ws_gateways", {})
    result: list[ReflectedWsGateway] = []
    for path_template, gw in gateways.items():
        messages: list[ReflectedWsMessage] = []
        for mm in getattr(gw, "message_metas", ()):
            # Look up handler_fn from the compiled messages dict; fall back to
            # None when the event has no dedicated compiled entry (shouldn't
            # happen in practice but is defensive).
            compiled_msgs: dict[str, Any] = getattr(gw, "messages", {})
            cm = compiled_msgs.get(mm.event)
            messages.append(
                ReflectedWsMessage(
                    event=mm.event,
                    payload_model=mm.payload_model,
                    summary=mm.summary,
                    description=mm.description,
                    handler=cm.handler_fn if cm is not None else None,
                )
            )
        result.append(
            ReflectedWsGateway(
                cls=gw.controller_cls,
                path_template=path_template,
                meta=gw.controller_meta,
                guards=tuple(getattr(gw, "guards", ())),
                interceptors=tuple(getattr(gw, "interceptors", ())),
                middlewares=tuple(getattr(gw, "middlewares", ())),
                messages=tuple(messages),
                owning_module=gw.owning_module,
            )
        )
    return tuple(result)


def get_route_metadata(app: Any, method: str, path: str) -> ReflectedRoute | None:
    """Return the :class:`~lauren.reflect.ReflectedRoute` for *(method, path)*,
    or ``None`` if the route is not registered or the app has not started.

    *path* must be the exact path template as registered (e.g.
    ``"/users/{id}"``), not a concrete URL (e.g. ``"/users/42"``).

    Example::

        route = get_route_metadata(app, "GET", "/users/{id}")
        if route is not None:
            print(route.handler.__name__, route.guards)
    """
    if not getattr(app, "_started", False):
        return None
    handlers: dict[Any, Any] = getattr(app, "_handlers", {})
    ch = handlers.get((method.upper(), path))
    if ch is None:
        return None
    rm = ch.route_meta
    return ReflectedRoute(
        method=method.upper(),
        path=rm.path,
        full_path=path,
        summary=rm.summary,
        description=rm.description,
        response_model=rm.response_model,
        responses=dict(rm.responses),
        deprecated=rm.deprecated,
        operation_id=rm.operation_id,
        include_in_schema=rm.include_in_schema,
        tags=tuple(rm.tags),
        handler=ch.handler_fn,
    )
