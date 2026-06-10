"""Immutable result types returned by the structured reflect readers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    pass


__all__ = [
    "ReflectedRoute",
    "ReflectedWsMessage",
    "ReflectedController",
    "ReflectedModule",
    "ReflectedWsGateway",
]


@dataclass(frozen=True, slots=True)
class ReflectedRoute:
    """A single HTTP route declared via ``@get``, ``@post``, etc.

    Returned by :func:`~lauren.reflect.reflect_routes` and
    :func:`~lauren.reflect.get_all_routes`.
    """

    method: str
    """HTTP method in upper-case, e.g. ``"GET"``."""

    path: str
    """Route-relative path as written in the decorator, e.g. ``"/{id}"``."""

    full_path: str
    """Absolute path with the controller prefix folded in, e.g. ``"/users/{id}"``."""

    summary: str | None
    description: str | None
    response_model: Any | None
    responses: dict[int, Any]
    deprecated: bool
    operation_id: str | None
    include_in_schema: bool
    tags: tuple[str, ...]

    handler: Any
    """The unbound method function that handles this route."""


@dataclass(frozen=True, slots=True)
class ReflectedWsMessage:
    """A single ``@on_message("event")`` declaration on a WS gateway.

    Returned by :func:`~lauren.reflect.reflect_ws_messages` and
    :func:`~lauren.reflect.get_all_ws_gateways`.
    """

    event: str
    payload_model: Any | None
    summary: str | None
    description: str | None
    handler: Any
    """The unbound method function that handles this event."""


@dataclass(frozen=True, slots=True)
class ReflectedController:
    """All metadata for a ``@controller``-decorated class.

    Returned by :func:`~lauren.reflect.get_controller_metadata`.
    Returns ``None`` when the class is not a ``@controller``.
    """

    cls: type
    meta: Any
    """The raw :class:`~lauren.decorators.ControllerMeta` instance."""

    guards: tuple[type, ...]
    interceptors: tuple[type, ...]
    middlewares: tuple[type, ...]
    exception_handlers: tuple[Any, ...]
    routes: tuple[ReflectedRoute, ...]


@dataclass(frozen=True, slots=True)
class ReflectedModule:
    """All metadata for a ``@module``-decorated class.

    Returned by :func:`~lauren.reflect.get_module_metadata`.
    Returns ``None`` when the class is not a ``@module``.
    """

    cls: type
    meta: Any
    """The raw :class:`~lauren.decorators.ModuleMeta` instance."""


@dataclass(frozen=True, slots=True)
class ReflectedWsGateway:
    """All metadata for a ``@ws_controller``-decorated class, post-startup.

    Returned by :func:`~lauren.reflect.get_all_ws_gateways`.
    """

    cls: type
    path_template: str
    meta: Any
    """The raw :class:`~lauren.websockets.WsControllerMeta` instance."""

    guards: tuple[type, ...]
    interceptors: tuple[type, ...]
    middlewares: tuple[type, ...]
    messages: tuple[ReflectedWsMessage, ...]
    owning_module: type | None
