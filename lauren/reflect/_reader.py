"""Metadata readers — read decorator-attached annotations from any decorated
class without importing internal attribute-name strings directly.
"""

from __future__ import annotations

from typing import Any, NamedTuple, TYPE_CHECKING

from ._types import (
    ReflectedController,
    ReflectedModule,
    ReflectedRoute,
    ReflectedWsMessage,
)

if TYPE_CHECKING:
    from ..decorators import ControllerMeta, ModuleMeta
    from ..websockets import WsControllerMeta
    from .._di import InjectableMeta

__all__ = [
    # existing cross-cutting readers
    "reflect_guards",
    "reflect_interceptors",
    "reflect_middlewares",
    "reflect_all",
    "ReflectedMeta",
    # Phase 1 — class-level readers
    "reflect_controller",
    "reflect_module",
    "reflect_injectable",
    "reflect_ws_controller",
    "reflect_routes",
    "reflect_ws_messages",
    "reflect_exception_handlers",
    "get_controller_metadata",
    "get_module_metadata",
    # Phase 3 — user metadata + encoder
    "reflect_user_metadata",
    "reflect_encoder",
]

# ---------------------------------------------------------------------------
# Mirrored decorator-attribute constants.
# These strings are stable — they form part of Lauren's public decorator
# contract and are exported from lauren/decorators.py.  We mirror them here
# so reflect/_reader.py has no import-time dependency on the rest of the
# framework (avoiding any future circular-import risk).
# ---------------------------------------------------------------------------

_USE_GUARDS = "__lauren_use_guards__"
_USE_INTERCEPTORS = "__lauren_use_interceptors__"
_USE_MIDDLEWARES = "__lauren_use_middlewares__"
_CONTROLLER_META = "__lauren_controller__"
_MODULE_META = "__lauren_module__"
_ROUTE_META = "__lauren_route__"
_INJECTABLE_META = "__lauren_injectable__"
_WS_CONTROLLER_META = "__lauren_ws_controller__"
_WS_ON_MESSAGE = "__lauren_ws_on_message__"
_USE_EXCEPTION_HANDLERS = "__lauren_use_exception_handlers__"
_SET_METADATA = "__lauren_metadata__"
_USE_ENCODER = "__lauren_use_encoder__"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _join_path(*parts: str) -> str:
    """Join path segments, stripping redundant slashes and ensuring a leading /."""
    pieces = [p.strip("/") for p in parts if p.strip("/")]
    return "/" + "/".join(pieces) if pieces else "/"


# ---------------------------------------------------------------------------
# Cross-cutting concern readers (v1.6.0 — unchanged)
# ---------------------------------------------------------------------------


class ReflectedMeta(NamedTuple):
    """All cross-cutting metadata declared on a class via Lauren decorators."""

    guards: tuple[type, ...]
    interceptors: tuple[type, ...]
    middlewares: tuple[type, ...]


def reflect_guards(cls: type) -> tuple[type, ...]:
    """Return the guard classes declared on *cls* via ``@use_guards``.

    Reads from ``cls.__dict__`` only — matches Lauren's "metadata is never
    inherited" rule.  Returns an empty tuple when no guards are declared.

    Example::

        @use_guards(AuthGuard, RateLimitGuard)
        @ws_controller("/chat")
        class ChatGateway: ...

        reflect_guards(ChatGateway)   # → (AuthGuard, RateLimitGuard)
    """
    return tuple(cls.__dict__.get(_USE_GUARDS, ()))


def reflect_interceptors(cls: type) -> tuple[type, ...]:
    """Return the interceptor classes declared on *cls* via ``@use_interceptors``.

    Reads from ``cls.__dict__`` only.  Returns an empty tuple when none are
    declared.
    """
    return tuple(cls.__dict__.get(_USE_INTERCEPTORS, ()))


def reflect_middlewares(cls: type) -> tuple[type, ...]:
    """Return the middleware classes declared on *cls* via ``@use_middlewares``.

    Reads from ``cls.__dict__`` only.  Returns an empty tuple when none are
    declared.
    """
    return tuple(cls.__dict__.get(_USE_MIDDLEWARES, ()))


def reflect_all(cls: type) -> ReflectedMeta:
    """Return all cross-cutting metadata declared on *cls*.

    Equivalent to calling all three individual readers and bundling the
    results into a :class:`ReflectedMeta` named tuple.

    Example::

        meta = reflect_all(MyGateway)
        meta.guards          # tuple[type, ...]
        meta.interceptors    # tuple[type, ...]
        meta.middlewares     # tuple[type, ...]
    """
    return ReflectedMeta(
        guards=reflect_guards(cls),
        interceptors=reflect_interceptors(cls),
        middlewares=reflect_middlewares(cls),
    )


# ---------------------------------------------------------------------------
# Phase 1 — static class-level readers
# ---------------------------------------------------------------------------


def reflect_controller(cls: type) -> ControllerMeta | None:
    """Return the :class:`~lauren.decorators.ControllerMeta` for *cls*, or
    ``None`` if the class is not decorated with ``@controller()``.

    Reads from ``cls.__dict__`` only — subclasses that do not re-decorate
    always return ``None``.
    """
    return cls.__dict__.get(_CONTROLLER_META)


def reflect_module(cls: type) -> ModuleMeta | None:
    """Return the :class:`~lauren.decorators.ModuleMeta` for *cls*, or
    ``None`` if the class is not decorated with ``@module()``.

    Reads from ``cls.__dict__`` only.
    """
    return cls.__dict__.get(_MODULE_META)


def reflect_injectable(cls: type) -> InjectableMeta | None:
    """Return the :class:`~lauren._di.InjectableMeta` for *cls*, or
    ``None`` if the class is not decorated with ``@injectable()``.

    Reads from ``cls.__dict__`` only.
    """
    return cls.__dict__.get(_INJECTABLE_META)


def reflect_ws_controller(cls: type) -> WsControllerMeta | None:
    """Return the :class:`~lauren.websockets.WsControllerMeta` for *cls*, or
    ``None`` if the class is not decorated with ``@ws_controller()``.

    Reads from ``cls.__dict__`` only.
    """
    return cls.__dict__.get(_WS_CONTROLLER_META)


def reflect_routes(cls: type) -> tuple[ReflectedRoute, ...]:
    """Return all HTTP routes declared on *cls* via ``@get``, ``@post``, etc.

    The :attr:`~ReflectedRoute.full_path` of each route combines the
    controller prefix (from ``@controller(prefix)``) with the route-relative
    path.  For classes without ``@controller`` the prefix is treated as
    empty, so ``full_path`` equals ``path``.

    Returns an empty tuple for undecorated classes or classes with no route
    methods.  Reads only from ``cls.__dict__`` — inherited routes from a
    parent class are NOT included.

    Example::

        @controller("/users")
        class UserController:
            @get("/{id}")
            async def get_user(self, id: Path[int]): ...

        routes = reflect_routes(UserController)
        routes[0].method     # "GET"
        routes[0].path       # "/{id}"
        routes[0].full_path  # "/users/{id}"
    """
    ctrl_meta = cls.__dict__.get(_CONTROLLER_META)
    prefix: str = ctrl_meta.prefix if ctrl_meta is not None else ""

    result: list[ReflectedRoute] = []
    for val in cls.__dict__.values():
        route_metas: list[Any] | None = getattr(val, _ROUTE_META, None)
        if not route_metas:
            continue
        for rm in route_metas:
            result.append(
                ReflectedRoute(
                    method=rm.method,
                    path=rm.path,
                    full_path=_join_path(prefix, rm.path),
                    summary=rm.summary,
                    description=rm.description,
                    response_model=rm.response_model,
                    responses=dict(rm.responses),
                    deprecated=rm.deprecated,
                    operation_id=rm.operation_id,
                    include_in_schema=rm.include_in_schema,
                    tags=tuple(rm.tags),
                    handler=val,
                )
            )
    return tuple(result)


def reflect_ws_messages(cls: type) -> tuple[ReflectedWsMessage, ...]:
    """Return all ``@on_message`` declarations on *cls*.

    Reads only from ``cls.__dict__`` — inherited messages are NOT included.
    Returns an empty tuple when *cls* has no ``@on_message`` methods.
    """
    result: list[ReflectedWsMessage] = []
    for val in cls.__dict__.values():
        msg_metas: list[Any] | None = getattr(val, _WS_ON_MESSAGE, None)
        if not msg_metas:
            continue
        for mm in msg_metas:
            result.append(
                ReflectedWsMessage(
                    event=mm.event,
                    payload_model=mm.payload_model,
                    summary=mm.summary,
                    description=mm.description,
                    handler=val,
                )
            )
    return tuple(result)


def reflect_exception_handlers(cls_or_fn: Any) -> tuple[Any, ...]:
    """Return the exception handler classes/functions attached via
    ``@use_exception_handlers``.

    For class targets reads from ``cls.__dict__`` only (own-class rule).
    For function/method targets reads via ``getattr`` (same storage used by
    the ``@use_exception_handlers`` decorator for function targets).
    Returns an empty tuple when none are declared.

    Example::

        @use_exception_handlers(DomainErrorHandler)
        @controller("/api")
        class ApiController: ...

        reflect_exception_handlers(ApiController)  # → (DomainErrorHandler,)
    """
    if isinstance(cls_or_fn, type):
        return tuple(cls_or_fn.__dict__.get(_USE_EXCEPTION_HANDLERS, ()))
    return tuple(getattr(cls_or_fn, _USE_EXCEPTION_HANDLERS, ()))


# ---------------------------------------------------------------------------
# Structured getters — return rich result types or None
# ---------------------------------------------------------------------------


def get_controller_metadata(cls: type) -> ReflectedController | None:
    """Return a :class:`ReflectedController` for *cls*, or ``None`` if
    *cls* is not a ``@controller``-decorated class.

    The returned object bundles the :class:`~lauren.decorators.ControllerMeta`,
    all cross-cutting concern metadata, and a tuple of
    :class:`ReflectedRoute` entries.

    Example::

        meta = get_controller_metadata(UserController)
        if meta is None:
            raise RuntimeError("not a controller")
        meta.meta.prefix      # "/users"
        meta.routes           # tuple[ReflectedRoute, ...]
        meta.guards           # tuple[type, ...]
    """
    ctrl_meta = cls.__dict__.get(_CONTROLLER_META)
    if ctrl_meta is None:
        return None
    return ReflectedController(
        cls=cls,
        meta=ctrl_meta,
        guards=reflect_guards(cls),
        interceptors=reflect_interceptors(cls),
        middlewares=reflect_middlewares(cls),
        exception_handlers=reflect_exception_handlers(cls),
        routes=reflect_routes(cls),
    )


def get_module_metadata(cls: type) -> ReflectedModule | None:
    """Return a :class:`ReflectedModule` for *cls*, or ``None`` if *cls* is
    not a ``@module``-decorated class.

    Example::

        meta = get_module_metadata(AppModule)
        if meta is None:
            raise RuntimeError("not a module")
        meta.meta.controllers   # tuple[type, ...]
        meta.meta.providers     # tuple[type, ...]
    """
    mod_meta = cls.__dict__.get(_MODULE_META)
    if mod_meta is None:
        return None
    return ReflectedModule(cls=cls, meta=mod_meta)


# ---------------------------------------------------------------------------
# Phase 3 — user metadata + encoder
# ---------------------------------------------------------------------------


def reflect_user_metadata(
    obj: Any,
    key: str | None = None,
    default: Any = None,
) -> Any:
    """Return ``@set_metadata`` values from *obj* (a class or function).

    When *key* is ``None`` the full metadata dict is returned (a copy).
    When *key* is provided the value for that key is returned, or *default*
    if the key is absent.

    Reads from ``obj.__dict__`` for classes (own-class rule) and from
    ``getattr`` for functions.

    Example::

        @set_metadata("rate_limit", 100)
        @controller("/api")
        class ApiController: ...

        reflect_user_metadata(ApiController)                   # {"rate_limit": 100}
        reflect_user_metadata(ApiController, "rate_limit")     # 100
        reflect_user_metadata(ApiController, "missing", 0)    # 0
    """
    if isinstance(obj, type):
        meta: dict[str, Any] = dict(obj.__dict__.get(_SET_METADATA, {}))
    else:
        meta = dict(getattr(obj, _SET_METADATA, {}))
    if key is None:
        return meta
    return meta.get(key, default)


def reflect_encoder(cls_or_fn: Any) -> Any | None:
    """Return the ``@use_encoder`` instance attached to *cls_or_fn*, or
    ``None`` when not set.

    For class targets reads from ``cls.__dict__`` only.
    For function/method targets reads via ``getattr``.

    Example::

        @use_encoder(OrjsonEncoder())
        @controller("/fast")
        class FastController: ...

        reflect_encoder(FastController)  # OrjsonEncoder instance
    """
    if isinstance(cls_or_fn, type):
        return cls_or_fn.__dict__.get(_USE_ENCODER)
    return getattr(cls_or_fn, _USE_ENCODER, None)
