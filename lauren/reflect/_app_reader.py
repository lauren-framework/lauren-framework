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

from ._responses import route_responses
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
    names = _route_names(app, "http")
    result: list[ReflectedRoute] = []
    for (method, path_template), ch in handlers.items():
        result.append(_reflected(method, ch.path_template, ch, names.get((method, path_template))))
    return tuple(result)


def _route_names(app: Any, kind: str) -> dict[tuple[str, str], str | None]:
    """``(method, path template) → route name`` from the app's URL registry."""
    table = getattr(getattr(app, "_urls", None), "_table", None)
    if table is None:
        return {}
    return {
        (r.method, (getattr(r, "host", None) or "") + r.path_template): r.name
        for r in getattr(table, kind).routes
    }


def _reflected(method: str, path_template: str, ch: Any, name: str | None = None) -> ReflectedRoute:
    rm = ch.route_meta
    status_code, responses_typed, raises = route_responses(
        getattr(ch, "response_table", None),
        getattr(ch, "raises", ()),
        rm.responses,
        status_code=getattr(rm, "status_code", None),
    )
    return ReflectedRoute(
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
        status_code=status_code,
        responses_typed=responses_typed,
        raises=raises,
        guards=tuple(ch.guards),
        interceptors=tuple(ch.interceptors),
        middlewares=tuple(ch.middleware_chain),
        consumes=tuple(getattr(ch, "consumes", ())),
        produces=tuple(getattr(ch, "produces", ())),
        max_body_size=getattr(ch, "body_limit", None),
        host=ch.host,
        name=name,
    )


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
    names = _route_names(app, "ws")
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
                path_template=gw.path_template,
                meta=gw.controller_meta,
                guards=tuple(getattr(gw, "guards", ())),
                interceptors=tuple(getattr(gw, "interceptors", ())),
                middlewares=tuple(getattr(gw, "middlewares", ())),
                messages=tuple(messages),
                owning_module=gw.owning_module,
                host=gw.host,
                name=names.get(("WEBSOCKET", path_template)),
            )
        )
    return tuple(result)


def get_route_metadata(app: Any, method: str, path: str, *, host: str | None = None) -> ReflectedRoute | None:
    """Return the :class:`~lauren.reflect.ReflectedRoute` for *(method, path)*,
    or ``None`` if the route is not registered or the app has not started.

    *path* must be the exact path template as registered (e.g.
    ``"/users/{id}"``), not a concrete URL (e.g. ``"/users/42"``).
    For host-routed entries, pass the canonical host pattern as ``host=``;
    the returned ``full_path`` remains the path, not the internal lookup key.

    Example::

        route = get_route_metadata(app, "GET", "/users/{id}")
        if route is not None:
            print(route.handler.__name__, route.guards)
    """
    if not getattr(app, "_started", False):
        return None
    handlers: dict[Any, Any] = getattr(app, "_handlers", {})
    ch = handlers.get((method.upper(), (host or "") + path))
    if ch is None:
        return None
    return _reflected(
        method.upper(), path, ch, _route_names(app, "http").get((method.upper(), (host or "") + path))
    )
