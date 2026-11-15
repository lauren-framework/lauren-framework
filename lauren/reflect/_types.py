"""Immutable result types returned by the structured reflect readers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    pass


__all__ = [
    "ReflectedResponse",
    "ReflectedRoute",
    "ReflectedWsMessage",
    "ReflectedController",
    "ReflectedModule",
    "ReflectedWsGateway",
    "ReflectedError",
    "ReflectedComposition",
]


@dataclass(frozen=True, slots=True)
class ReflectedResponse:
    """One documented response of a route: a declared success, a ``@raises`` error or a manual entry.

    Returned in :attr:`ReflectedRoute.responses_typed`.
    """

    status: int
    """The HTTP status."""

    body_type: Any
    """The declared body type; ``None`` for no body, :class:`~lauren.types.Response` for a
    passthrough, ``typing.Any`` when undeclared, the error class for a ``@raises`` entry."""

    description: str | None
    """The description (``Status(description=...)``, ``@raises(description=...)``, ``responses=``)."""

    source: Literal["declared", "raises", "manual"]
    """``"declared"``: ``status_code=`` / return annotation / ``response_model=``;
    ``"raises"``: ``@raises``; ``"manual"``: the free-form ``responses=`` dict."""


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

    status_code: int = 200
    """The effective success status: ``status_code=`` / ``Status(...)`` for plain values,
    else the first declared response (``-> None`` → 204, ``-> Created[T]`` → 201); 200
    when undeclared."""

    responses_typed: tuple[ReflectedResponse, ...] = ()
    """Every documented response: declared successes, ``@raises`` errors and ``responses=`` entries."""

    raises: tuple[type, ...] = ()
    """The ``@raises`` error classes (the method's, then the controller's own)."""

    guards: tuple[type, ...] = ()
    """The compiled route and controller guards (not global guards)."""

    interceptors: tuple[type, ...] = ()
    """The compiled route and controller interceptors (not globals)."""

    middlewares: tuple[type, ...] = ()
    """The compiled route and controller middleware chain (not globals)."""
    consumes: tuple[str, ...] = ()
    """Accepted request media types: ``consumes=`` (route, else controller). From
    :func:`~lauren.reflect.get_all_routes`, the effective list — including the types
    inferred from body parameters (``Json[T]`` → ``application/json``, …)."""

    produces: tuple[str, ...] = ()
    """Response media types: ``produces=`` (route, else controller). From
    :func:`~lauren.reflect.get_all_routes`, the effective list (``ContentConfig.default_produces``
    when undeclared)."""

    host: str | None = None
    """Canonical host pattern, or ``None`` for a host-independent route."""

    max_body_size: int | None = None
    """The route's body limit in bytes (route, else controller); ``None`` means the app's
    ``max_body_size``."""
    name: str | None = None
    """The route's full name for URL building (``"users.show"``, or the derived
    ``"UserController.show"``); ``None`` from :func:`~lauren.reflect.reflect_routes`
    (names are assigned at startup) and for unnamed routes."""


@dataclass(frozen=True, slots=True)
class ReflectedWsMessage:
    """A single ``@on_message("event")`` declaration on a WS gateway.

    Returned by :func:`~lauren.reflect.reflect_ws_messages` and
    :func:`~lauren.reflect.get_all_ws_gateways`.
    """

    event: str
    payload_model: Any | None
    """Reserved: always ``None`` today (``@on_message`` does not record it);
    read the handler's ``Json[...]`` annotation for the payload type."""
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
    host: str | None = None
    name: str | None = None
    """The gateway's full name for ``ws_url_for`` (``None`` when ambiguous)."""


@dataclass(frozen=True, slots=True)
class ReflectedError:
    """One error class of the catalog — framework or application.

    Returned by :func:`~lauren.reflect.error_catalog`.
    """

    cls: type
    """The error class (a :class:`~lauren.exceptions.LaurenError` subclass)."""

    code: str
    """The class-level stable ``code``."""

    status_code: int | None
    """The HTTP status for ``HTTPError`` subclasses; ``None`` for startup, lifecycle and WebSocket errors."""

    category: Literal["startup", "http", "lifecycle", "websocket", "other"]
    """``"other"`` is ``LaurenError`` itself and direct subclasses outside the four families."""

    problem_type: str | None
    """The class's RFC 9457 ``problem_type``, if it declares one."""

    problem_title: str | None
    """The class's RFC 9457 ``problem_title``, if it declares one."""

    summary: str
    """The first paragraph of the class's own docstring, on one line (``""`` without one)."""


@dataclass(frozen=True, slots=True)
class ReflectedComposition:
    """One ``compose(...)`` application recorded on a class or function.

    Returned by :func:`~lauren.reflect.reflect_composed`, in application order.
    """

    name: str | None
    """The ``name=`` given to :func:`~lauren.compose` (``None`` when unnamed)."""

    decorators: tuple[str, ...]
    """A label per composed decorator, in written order (e.g. ``"use_guards"``,
    ``"app.roles('admin')"``, or a nested composition's name)."""
