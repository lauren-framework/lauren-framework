"""ASGI-level WebSocket runtime — gateway compilation + request dispatch.

This module is lauren's internal glue between the user-facing decorators
in :mod:`lauren.websockets` and the ASGI app produced by
:class:`lauren.LaurenFactory`. It owns three responsibilities:

1. **Gateway discovery** — walk the compiled module graph, pick every
   class that has its OWN :attr:`WS_CONTROLLER_META` marker (the
   framework-wide rule: no metadata inheritance), validate the
   ``@on_connect`` / ``@on_message`` / ``@on_disconnect`` hooks declared
   on that class, and produce an immutable :class:`CompiledGateway`
   record per gateway.

2. **Typed dispatch plan** — for every ``@on_message("x")`` we inspect
   the handler's signature once and build an :class:`Extraction` list
   that the request path replays without reflection. The ``body`` /
   ``Json[Model]`` extractor is special-cased to validate through a
   :class:`pydantic.TypeAdapter` so discriminated unions from
   :mod:`lauren.streaming` work out of the box.

3. **ASGI scope handling** — the HTTP runtime's ``__call__`` forwards
   ``websocket`` scopes to :func:`handle_websocket` here, which
   implements the full handshake → hook → dispatch → disconnect cycle.

Everything here is private; :mod:`lauren.websockets` exposes the user
surface (decorators, :class:`WebSocket`, :class:`BroadcastGroup`).
"""

from __future__ import annotations

import inspect as _inspect
import json as _jsonlib
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable

from ._di import DIContainer, INJECTABLE_META, InjectableMeta
from ._routing import Router, RouteEntry
from .exceptions import (
    MetadataInheritanceError,
    StartupError,
    UnresolvableParameterError,
)
from .extractors import (
    Extraction,
    _ParamSpec,
    FieldDescriptor,
    extract_parameter,
    parse_extractor_hint,
)
from .streaming import _build_adapter
from .types import Headers, Scope as DScope
from .websockets import (
    WebSocket,
    WebSocketDisconnect,
    WebSocketError,
    WebSocketValidationError,
    WS_CONTROLLER_META,
    WsControllerMeta,
    WsMessageMeta,
    discover_ws_hooks,
    is_ws_controller,
    own_ws_controller_meta,
)

from ._validation import is_pydantic_model as _is_pydantic_model  # noqa: F401

# NOTE: reflect._reader and reflect._composer are imported lazily (inside
# compile_gateways / handle_websocket) to avoid a circular import:
#   _ws_runtime → reflect._reader → reflect/__init__.py → reflect._context
#                                                        → _ws_runtime  ← cycle
# WsConnectionContext and WsUpgradeRequest are defined HERE (below) so that
# reflect._context can import them from this module without triggering the cycle.


# ---------------------------------------------------------------------------
# WebSocket connection context — passed to guards and interceptors.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WsUpgradeRequest:
    """Read-only view of the HTTP upgrade request that opened a WS connection.

    Mirrors the fields that guards most commonly access so that the same
    guard class works on both ``@controller`` (HTTP) and ``@ws_controller``
    (WebSocket) via duck-typing::

        async def can_activate(self, ctx) -> bool:
            return ctx.request.headers.get("x-api-key") == "valid"

    WS upgrades have no body, so ``body()``, ``text()``, ``json()``, and
    ``stream()`` are intentionally absent.
    """

    headers: Any  # lauren.types.Headers — case-insensitive
    path: str
    path_params: dict[str, str]
    query_string: str = ""
    client: Any | None = None  # lauren.types.ClientInfo | None
    method: str = "GET"  # WS upgrades are always GET requests


@dataclass(frozen=True, slots=True)
class WsConnectionContext:
    """Context object passed to guards and interceptors during a WS connection.

    Mirrors :class:`~lauren.types.ExecutionContext` so that the same guard
    works on both ``@controller`` (HTTP) and ``@ws_controller`` (WebSocket)::

        # Works for HTTP (ctx is ExecutionContext) and WS (ctx is WsConnectionContext).
        async def can_activate(self, ctx) -> bool:
            return ctx.request.headers.get("x-api-key") == "valid"

    The ``connection`` field gives guards direct access to the live
    :class:`WebSocket` object.  Guards that call ``await ctx.connection.close(code)``
    before returning ``False`` send a custom close reason; Lauren will *not*
    send a second close frame.
    """

    request: WsUpgradeRequest
    """Read-only upgrade request data (headers, path, etc.)."""

    connection: Any
    """The live :class:`WebSocket` object for this connection."""

    handler_class: type | None = None
    """The ``@ws_controller`` class handling this connection."""

    handler_func: Any | None = None
    """Always ``None`` during the connection-level check; present for
    protocol symmetry with :class:`~lauren.types.ExecutionContext`."""

    route_template: str | None = None
    """Matched path template, e.g. ``"/chat/{room_id}"``."""

    metadata: dict[str, Any] = field(default_factory=dict)
    """Key→value pairs from ``@set_metadata`` decorators on the gateway."""

    def get_metadata(self, key: str, default: Any = None) -> Any:
        return self.metadata.get(key, default)


# ---------------------------------------------------------------------------
# Compiled dispatch records. Produced once at startup; consulted at every
# frame. Immutable so the hot path stays free of locks and reflection.
# ---------------------------------------------------------------------------


#: Wildcard event name that catches any text frame without a dedicated
#: handler. Declared as a constant so ``WildcardMessage`` isn't a magic
#: string scattered across the dispatch logic.
WILDCARD_EVENT = "*"

#: Special event name for raw binary frames. A handler
#: ``@on_message("__binary__")`` receives the ``bytes`` payload directly
#: without JSON decoding.
BINARY_EVENT = "__binary__"


@dataclass
class CompiledMessage:
    """Per-event dispatch plan."""

    event: str
    handler_fn: Callable[..., Any]
    extractions: tuple[Extraction, ...]
    message_meta: WsMessageMeta
    #: Pydantic :class:`TypeAdapter` built for the handler's ``body``
    #: extractor inner type (if any). ``None`` when the handler doesn't
    #: declare a typed body — in which case the raw JSON dict is
    #: injected verbatim under the parameter name.
    payload_adapter: Any = None
    #: Name of the handler parameter that should receive the validated
    #: payload. Resolved once at compile time so the dispatcher can
    #: ``kwargs[body_param] = payload`` without extra lookup.
    payload_param: str | None = None


@dataclass
class CompiledGateway:
    """All the immutable metadata the runtime needs for one gateway."""

    controller_cls: type
    path_template: str
    param_names: tuple[str, ...]
    controller_meta: WsControllerMeta
    owning_module: type | None
    on_connect: Callable[..., Any] | None
    on_connect_extractions: tuple[Extraction, ...]
    on_disconnect: Callable[..., Any] | None
    on_disconnect_extractions: tuple[Extraction, ...]
    on_error: Callable[..., Any] | None
    on_error_extractions: tuple[Extraction, ...] = ()
    messages: dict[str, CompiledMessage] = field(default_factory=dict)
    #: All message metas in declaration order — preserved so AsyncAPI
    #: generators can enumerate every declared event even when several
    #: share the same handler function.
    message_metas: tuple[WsMessageMeta, ...] = ()
    #: Binding style per hook function — kept for introspection.
    #: Dispatch now uses ``raw_descriptors`` + ``__get__`` instead.
    bindings: dict[Any, str] = field(default_factory=dict)
    #: Raw descriptor from ``cls.__dict__[attr_name]`` for each hook
    #: function.  ``_run_hook`` calls ``descriptor.__get__(instance, cls)``
    #: so every binding style, including custom descriptors, works
    #: transparently without an explicit ``if/elif`` branch.
    raw_descriptors: dict[Any, Any] = field(default_factory=dict)
    #: Guard classes declared via ``@use_guards`` on the gateway class.
    #: Checked before ``@on_connect`` fires; rejection closes with 1008.
    guards: tuple[type, ...] = ()
    #: Interceptor classes declared via ``@use_interceptors`` on the class.
    #: Wrap the ``@on_connect`` lifecycle (before accept + after connect).
    interceptors: tuple[type, ...] = ()
    #: Middleware classes declared via ``@use_middlewares`` on the class.
    #: Stored for introspection / future WS middleware support.
    middlewares: tuple[type, ...] = ()


# ---------------------------------------------------------------------------
# Gateway compilation — called from ``LaurenFactory.create``'s Phase 5.
# ---------------------------------------------------------------------------


def compile_gateways(
    module_graph: Any,
    container: DIContainer,
    *,
    ws_router: Router,
    logger: Any,
) -> dict[str, CompiledGateway]:
    """Walk ``module_graph``, build :class:`CompiledGateway` for every
    class that has its OWN ``@ws_controller`` marker.

    Returns a dict keyed by path template so the request-side lookup is
    a single router match plus a dict get. Gateways share the same
    :class:`DIContainer` as HTTP controllers so providers declared in
    the module graph are visible to both worlds uniformly.

    Raises :class:`StartupError` when a gateway's handlers can't be
    compiled (typically bad extractor annotations), and
    :class:`MetadataInheritanceError` if a class in
    ``module_graph.iter_controllers()`` inherits the ``@ws_controller``
    marker without re-declaring it explicitly on the subclass.
    """
    from .reflect._reader import (  # noqa: PLC0415
        reflect_guards,
        reflect_interceptors,
        reflect_middlewares,
    )

    gateways: dict[str, CompiledGateway] = {}
    for cls in _iter_ws_controllers(module_graph):
        ctrl_meta = own_ws_controller_meta(cls)
        hooks = discover_ws_hooks(cls)
        owning_module = module_graph.module_for_controller(cls)

        # Register the gateway as an injectable (REQUEST scope by
        # default — ``@ws_controller`` already sets that) so the
        # container can instantiate it per-connection.
        if cls not in {p.cls for p in container.all_providers()}:
            _ensure_injectable(cls)
            container.register(cls, owning_module=owning_module)

        # Reserve the path in the WebSocket router. We reuse the same
        # radix router as HTTP for param/wildcard semantics. The method
        # slot is the synthetic ``"WEBSOCKET"`` pseudo-verb so we can
        # register a route even though the router's HTTP_METHODS set
        # doesn't list it.
        path = ctrl_meta.path or ""
        entry = _add_ws_route(ws_router, path, cls)
        param_names = tuple(entry.param_names)

        # Compile @on_connect / @on_disconnect signatures. They share
        # the same extractor plan as HTTP handlers with one addition:
        # ``ws: WebSocket`` is auto-resolved by the dispatcher.
        on_connect_ext: tuple[Extraction, ...] = ()
        if hooks["on_connect"] is not None:
            on_connect_ext = _compile_ws_signature(
                cls,
                hooks["on_connect"],
                container,
                path_param_names=set(param_names),
                owning_module=owning_module,
                is_message_handler=False,
            )
        on_disconnect_ext: tuple[Extraction, ...] = ()
        if hooks["on_disconnect"] is not None:
            on_disconnect_ext = _compile_ws_signature(
                cls,
                hooks["on_disconnect"],
                container,
                path_param_names=set(param_names),
                owning_module=owning_module,
                is_message_handler=False,
            )

        # Compile @on_error if declared. Its signature is the same as
        # @on_message minus the payload parameter — the raised exception
        # arrives via the ``ws_error`` pseudo-source.
        on_error_extractions: tuple[Extraction, ...] = ()
        if hooks["on_error"] is not None:
            on_error_extractions = _compile_ws_signature(
                cls,
                hooks["on_error"],
                container,
                path_param_names=set(param_names),
                owning_module=owning_module,
                is_message_handler=False,
                is_error_handler=True,
            )

        # Compile every @on_message. One function can carry multiple
        # metas (stacked decorators) — we produce one CompiledMessage
        # per event alias so the dispatch map is flat.
        messages: dict[str, CompiledMessage] = {}
        all_metas: list[WsMessageMeta] = []
        for event, fn, meta in hooks["messages"]:
            if event in messages:
                raise StartupError(
                    f"duplicate @on_message({event!r}) on gateway {cls.__name__}",
                    detail={"gateway": cls.__name__, "event": event},
                )
            extractions = _compile_ws_signature(
                cls,
                fn,
                container,
                path_param_names=set(param_names),
                owning_module=owning_module,
                is_message_handler=True,
                is_binary_handler=(event == BINARY_EVENT),
            )
            payload_param, payload_adapter = _find_payload_adapter(
                extractions, binary=(event == BINARY_EVENT)
            )
            messages[event] = CompiledMessage(
                event=event,
                handler_fn=fn,
                extractions=extractions,
                message_meta=meta,
                payload_adapter=payload_adapter,
                payload_param=payload_param,
            )
            all_metas.append(meta)
            logger.log(
                f"Mapped {{WEBSOCKET {entry.path_template} #{event}}} → {cls.__name__}.{fn.__name__}",
                context="RouterExplorer",
                event=event,
                path=entry.path_template,
                handler=f"{cls.__name__}.{fn.__name__}",
            )

        compiled = CompiledGateway(
            controller_cls=cls,
            path_template=entry.path_template,
            param_names=param_names,
            controller_meta=ctrl_meta,
            owning_module=owning_module,
            on_connect=hooks["on_connect"],
            on_connect_extractions=on_connect_ext,
            on_disconnect=hooks["on_disconnect"],
            on_disconnect_extractions=on_disconnect_ext,
            on_error=hooks["on_error"],
            on_error_extractions=on_error_extractions,
            messages=messages,
            message_metas=tuple(all_metas),
            bindings=dict(hooks.get("bindings", {})),
            raw_descriptors=dict(hooks.get("raw_descriptors", {})),
            # Cross-cutting concern metadata — read from cls.__dict__ only
            # (own-class rule: no inheritance of guard/interceptor metadata).
            guards=reflect_guards(cls),
            interceptors=reflect_interceptors(cls),
            middlewares=reflect_middlewares(cls),
        )
        gateways[entry.path_template] = compiled
    return gateways


def _iter_ws_controllers(module_graph: Any) -> Iterable[type]:
    """Yield every class listed in a module's ``controllers`` that has
    its OWN :attr:`WS_CONTROLLER_META`.

    Gateways are stored in the same ``controllers`` slot as HTTP
    controllers — it's the decorator marker that tells them apart.
    Classes that merely *inherit* the marker (no re-decoration) trigger
    :class:`MetadataInheritanceError` via :func:`own_ws_controller_meta`
    when the compiler tries to pull their metadata out, which gives us
    a consistent explicit-opt-in story across the framework.
    """
    seen: set[type] = set()
    for cls in module_graph.iter_controllers():
        if cls in seen:
            continue
        seen.add(cls)
        if is_ws_controller(cls):
            yield cls
            continue
        # If a base class has the marker but this subclass doesn't,
        # own_ws_controller_meta (called later if the user DID mean to
        # register this as a gateway) would raise. But for ordinary
        # iteration we just skip — the class might be a plain HTTP
        # controller that happens to inherit from a WS base for shared
        # helpers, which is legitimate.
        for base in cls.__mro__[1:]:
            if WS_CONTROLLER_META in base.__dict__:
                # Heuristic: classes that inherit @ws_controller AND
                # redeclare any of the method hooks without re-applying
                # @ws_controller at the class level are almost certainly
                # a mistake. We err on the side of explicit: such
                # classes must opt in to be gateways.
                break


def _add_ws_route(router: Router, path: str, cls: type) -> RouteEntry:
    """Register a placeholder HTTP entry so we reuse the radix tree.

    We register under the synthetic ``"WEBSOCKET"`` method. Because
    :class:`lauren._routing.Router` restricts ``add_route``'s method
    set, we bypass it by temporarily widening the allowed set for the
    registration only — cleanly contained here so the HTTP side never
    sees the pseudo-verb.
    """
    method = "WEBSOCKET"
    # Widen + restore the allowed-method frozenset around the add.
    original = Router.HTTP_METHODS
    Router.HTTP_METHODS = frozenset(original | {method})
    try:
        entry = router.add_route(
            method,
            path or "/",
            handler=lambda: None,  # placeholder; never invoked
            handler_class=cls,
            metadata={"ws_controller_cls": cls},
        )
    finally:
        Router.HTTP_METHODS = original
    return entry


def _ensure_injectable(cls: type) -> None:
    if INJECTABLE_META not in cls.__dict__:
        for base in cls.__mro__[1:]:
            if INJECTABLE_META in base.__dict__:
                raise MetadataInheritanceError(
                    f"{cls.__name__} inherits injectable metadata from "
                    f"{base.__name__} but isn't itself decorated. "
                    "Re-decorate the subclass with @ws_controller / "
                    "@injectable explicitly."
                )
        setattr(cls, INJECTABLE_META, InjectableMeta(scope=DScope.REQUEST))


# ---------------------------------------------------------------------------
# Signature compilation — tailored variant of
# ``_compile_handler_signature`` that knows about WebSocket-specific
# parameter shapes (``ws: WebSocket``) while still delegating to the
# standard extractor pipeline for everything else.
# ---------------------------------------------------------------------------


def _compile_ws_signature(
    controller_cls: type,
    fn: Callable[..., Any],
    container: DIContainer,
    *,
    path_param_names: set[str],
    owning_module: type | None,
    is_message_handler: bool,
    is_error_handler: bool = False,
    is_binary_handler: bool = False,
) -> tuple[Extraction, ...]:
    """Build the extraction plan for a WebSocket hook method.

    Parameter handling rules (in order of precedence):

    1. ``self`` — skipped.
    2. Typed as :class:`WebSocket` — injected directly with source
       ``"websocket"``. The dispatcher supplies the live connection.
    3. Name matches a path parameter — auto-promoted to ``Path[T]``.
    4. Declared via a standard extractor marker (``Json[...]``,
       ``Query[...]``, ``Header[...]``, ``Depends[...]``, etc.) — the
       regular extractor pipeline takes over.
    5. Type matches a registered DI provider — resolved via ``depends``.
    6. Nothing else fits — :class:`UnresolvableParameterError`.

    The same :class:`Extraction` dataclass is reused so
    :func:`extract_parameter` handles actual value production; we just
    tag WebSocket-only sources (``"websocket"``) that ``_extract_raw``
    doesn't know about and resolve them ourselves in
    :func:`_bind_ws_kwargs`.
    """
    sig = _inspect.signature(fn)
    try:
        hints = _inspect.get_annotations(fn, eval_str=True)
    except Exception:
        hints = {}
    extractions: list[Extraction] = []

    # Both ``self`` (instance methods) and ``cls`` (classmethods) are
    # skipped; the dispatcher wires them in based on binding style.
    # Static methods don't name either, so the first user parameter
    # falls straight through.
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (
            _inspect.Parameter.VAR_POSITIONAL,
            _inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        ann = hints.get(name, param.annotation)
        default = param.default
        has_default = default is not _inspect.Parameter.empty

        # Rule 2: the live WebSocket connection.
        if ann is WebSocket or (isinstance(ann, type) and issubclass(ann, WebSocket)):
            extractions.append(
                Extraction(
                    name=name,
                    source="websocket",
                    inner_type=ann,
                    field_descriptor=None,
                    default=default,
                    has_default=has_default,
                )
            )
            continue

        # Rule 2b: @on_error hooks accept the raised exception under
        # any of the conventional parameter names. The dispatcher
        # forwards it via the ``ws_error`` pseudo-source.
        if is_error_handler and name in ("error", "exc", "exception"):
            extractions.append(
                Extraction(
                    name=name,
                    source="ws_error",
                    inner_type=ann if ann is not _inspect.Parameter.empty else Exception,
                    field_descriptor=None,
                    default=default,
                    has_default=has_default,
                )
            )
            continue

        # Rule 2c: binary @on_message handlers accept the raw bytes
        # payload under any parameter whose annotation is ``bytes``.
        # Declaring this shape explicitly keeps the rest of the
        # extractor dispatch generic — no bespoke marker needed for
        # the binary path.
        if is_binary_handler and (ann is bytes or ann is bytearray):
            extractions.append(
                Extraction(
                    name=name,
                    source="ws_binary",
                    inner_type=ann,
                    field_descriptor=None,
                    default=default,
                    has_default=has_default,
                )
            )
            continue

        # Parse as a standard extractor. This handles Json, Query, Path,
        # Header, Depends, Annotated[...], etc. We mirror
        # _compile_handler_signature but keep it smaller — no pipes, no
        # streaming, no ParamSpec default composition: ws handlers are
        # meant to be simple.
        source, inner, reads_body, marker_cls, fd, pipes = parse_extractor_hint(ann)

        default_fd: FieldDescriptor | None = None
        if has_default and isinstance(default, _ParamSpec):
            default_fd = default.field_descriptor
            default = default_fd.default if default_fd else ...
            has_default = default is not ...
        elif has_default and isinstance(default, FieldDescriptor):
            default_fd = default
            default = default_fd.default
            has_default = default_fd.default is not ...
        fd = fd or default_fd

        # Rule 3: auto-promote to Path[T] if the name is a known path
        # parameter — mirrors HTTP semantics so controller authors don't
        # need to remember the marker just to read a path var.
        if source is None and name in path_param_names:
            inner_type = ann if ann is not _inspect.Parameter.empty else str
            extractions.append(
                Extraction(
                    name=name,
                    source="path",
                    inner_type=inner_type,
                    field_descriptor=fd,
                    default=default,
                    has_default=has_default,
                )
            )
            continue

        # Rule 4: declared extractor marker — trust the parser.
        if source is not None:
            extractions.append(
                Extraction(
                    name=name,
                    source=source,
                    inner_type=inner,
                    field_descriptor=fd,
                    default=default,
                    has_default=has_default,
                    reads_body=reads_body,
                    marker_cls=marker_cls,
                    pipes=tuple(pipes),
                )
            )
            continue

        # Rule 5: DI fallback — accept both class tokens and function
        # tokens (function providers registered via ``@injectable()``
        # on a ``def``).
        if (
            isinstance(ann, type) or (callable(ann) and not isinstance(ann, type))
        ) and container.has_provider(ann, owning_module=owning_module):
            extractions.append(
                Extraction(
                    name=name,
                    source="depends",
                    inner_type=ann,
                    field_descriptor=fd,
                    default=default,
                    has_default=has_default,
                )
            )
            continue

        raise UnresolvableParameterError(
            f"Cannot resolve parameter {name!r} in {controller_cls.__name__}.{fn.__name__}",
            detail={
                "class": controller_cls.__name__,
                "handler": fn.__name__,
                "param": name,
            },
        )

    return tuple(extractions)


def _find_payload_adapter(
    extractions: tuple[Extraction, ...],
    *,
    binary: bool,
) -> tuple[str | None, Any]:
    """Pick the parameter that receives the inbound frame payload.

    For text/JSON frames (the common case) we look for a ``Json[...]``
    extractor and build a :class:`pydantic.TypeAdapter` from its inner
    type so validation supports both plain models and discriminated
    unions. For binary frames (``@on_message("__binary__")``) we look
    for a parameter of source ``"ws_binary"`` (a plain ``body: bytes``
    annotation in the handler, picked up by the WS signature compiler).
    """
    for ext in extractions:
        if binary and ext.source in ("ws_binary", "bytes"):
            return ext.name, None  # no adapter — bytes are passed as-is
        if (not binary) and ext.source == "json":
            adapter = _build_adapter(ext.inner_type)
            return ext.name, adapter
    return None, None


# ---------------------------------------------------------------------------
# Runtime dispatch. Invoked once per WebSocket connection from the
# ASGI ``__call__``. The function is written as a plain coroutine so the
# HTTP side can stay intact and this file can be skipped entirely when
# an app declares no gateways.
# ---------------------------------------------------------------------------


async def handle_websocket(
    app: Any,
    gateways: dict[str, CompiledGateway],
    ws_router: Router,
    scope: dict[str, Any],
    receive: Callable[[], Awaitable[dict[str, Any]]],
    send: Callable[[dict[str, Any]], Awaitable[None]],
    *,
    global_ws_guards: list[type] | None = None,
    global_ws_interceptors: list[type] | None = None,
) -> None:
    """ASGI-side WebSocket dispatch entry point.

    Flow:

    1. Await the initial ``websocket.connect`` message.
    2. Match the path against the WS router; if no gateway accepts the
       path, reject the handshake with close code 1008.
    3. Instantiate the gateway via DI (REQUEST scope).
    4. Run ``@on_connect`` if declared. If it returns normally and the
       socket hasn't been accepted yet, call ``ws.accept()``.
    5. Loop on incoming frames, dispatching each to the ``@on_message``
       plan. A frame with no matching event name routes to the
       wildcard handler (``"*"``) when present, or sends a structured
       error frame and keeps the connection open.
    6. On peer disconnect or server-initiated close, run
       ``@on_disconnect`` if declared — then finalize request-scoped
       providers (same as the HTTP dispatch path).
    """
    # Pull the opening ``websocket.connect`` message off the ASGI queue.
    opening = await receive()
    if opening.get("type") != "websocket.connect":
        # Some servers send a spurious message type. Close silently.
        await send({"type": "websocket.close", "code": 1011})
        return

    path = scope.get("path", "/")
    try:
        entry, params = ws_router.find("WEBSOCKET", path)
    except Exception:
        # Route miss: reject the handshake. 1008 = policy violation;
        # clients that care about the exact reason get the structured
        # code in the close frame.
        await send({"type": "websocket.close", "code": 1008})
        return
    gateway = gateways.get(entry.path_template)
    if gateway is None:
        await send({"type": "websocket.close", "code": 1008})
        return

    ws = WebSocket(
        scope=scope,
        receive=receive,
        send=send,
        path_template=entry.path_template,
        path_params=params,
        app_state=app._app_state,
        json_encoder=app._json_encoder,
    )

    request_cache: dict[type, Any] = {}
    container = app._container
    owning_module = gateway.owning_module

    # Resolve the gateway instance. REQUEST scope means the container
    # caches the instance inside ``request_cache`` so every hook on
    # this connection shares the same gateway object.
    controller = await container.resolve(
        gateway.controller_cls,
        request_cache=request_cache,
        framework_values={WebSocket: ws, type(ws): ws},
        owning_module=owning_module,
    )

    logger = app._logger

    async def _run_hook(
        fn: Callable[..., Any] | None,
        extractions: tuple[Extraction, ...],
        *,
        extra_values: dict[str, Any] | None = None,
    ) -> Any:
        """Bind extractor plan → kwargs and invoke one hook.

        ``extra_values`` lets the message dispatcher forward a pre-
        validated payload under the handler's body parameter, bypassing
        the generic extractor pipeline for the JSON frame since the
        payload came from the wire rather than an HTTP body.
        """
        if fn is None:
            return None
        kwargs = await _bind_ws_kwargs(
            ws,
            extractions,
            container,
            request_cache,
            owning_module,
            extra_values or {},
        )
        # Use the descriptor protocol: ``__get__(instance, cls)`` produces
        # the correctly bound callable for every binding style without an
        # explicit branch — staticmethod, classmethod, plain function, and
        # custom descriptors all behave correctly.
        _descriptor = gateway.raw_descriptors.get(fn)
        _bound = (
            _descriptor.__get__(controller, gateway.controller_cls)
            if _descriptor is not None
            else fn.__get__(controller, gateway.controller_cls)
        )
        result = _bound(**kwargs)
        if _inspect.isawaitable(result):
            result = await result
        return result

    # ---- Guards + @on_connect (+ interceptors) ----
    #
    # Effective chains: global guards run FIRST (same as HTTP), then
    # class-level guards.  Interceptors follow the same outermost-first
    # ordering used by the HTTP interceptor chain.
    #
    # Lazy imports here to avoid circular dependency at module load time:
    #   _ws_runtime → reflect._reader → reflect/__init__.py → reflect._context
    #               → _ws_runtime  [cycle!]
    from .reflect._composer import apply_guards, apply_interceptors  # noqa: PLC0415

    _effective_guards: tuple[type, ...] = tuple(global_ws_guards or ()) + gateway.guards
    _effective_interceptors: tuple[type, ...] = tuple(global_ws_interceptors or ()) + gateway.interceptors

    # Build a WsConnectionContext from the upgrade-request data so that
    # guards and interceptors receive the same duck-typed interface as
    # ExecutionContext.request (headers, path, method, …).
    _ws_ctx = WsConnectionContext(
        request=WsUpgradeRequest(
            headers=ws.headers,
            path=ws.path,
            path_params=dict(ws.path_params),
            query_string=scope.get("query_string", b"").decode("latin-1"),
            client=scope.get("client"),
        ),
        connection=ws,
        handler_class=gateway.controller_cls,
        route_template=gateway.path_template,
    )

    # Resolve the framework_values map for DI lookups within guards/interceptors.
    _fv: dict[type, Any] = {WebSocket: ws, type(ws): ws}

    try:
        # --- Guard check (before accepting the connection) ---
        if _effective_guards:
            _allowed = await apply_guards(
                _effective_guards,
                _ws_ctx,
                container=container,
                request_cache=request_cache,
                framework_values=_fv,
                owning_module=owning_module,
            )
            if not _allowed:
                await send({"type": "websocket.close", "code": 1008})
                await _finalize_scope(container, request_cache)
                return

        # --- @on_connect (optionally wrapped by interceptors) ---
        async def _run_connect() -> Any:
            """Run @on_connect + auto-accept; innermost of the interceptor chain."""
            await _run_hook(gateway.on_connect, gateway.on_connect_extractions)
            if ws.connection_state == ws.STATE_CONNECTING:
                await ws.accept()

        if _effective_interceptors:
            await apply_interceptors(
                _effective_interceptors,
                _run_connect,
                _ws_ctx,
                container=container,
                request_cache=request_cache,
                framework_values=_fv,
                owning_module=owning_module,
            )
        else:
            await _run_connect()

    except WebSocketError as exc:
        # Only send a close frame if the gateway hook didn't already close the
        # connection itself (e.g. ``await ws.close(...)`` followed by raising
        # ``WebSocketDisconnect``).  Sending a second ``websocket.close`` to an
        # already-rejected connection causes the underlying ASGI transport to
        # raise, which would propagate as an unhandled exception and corrupt
        # concurrent HTTP responses.
        if ws.connection_state != ws.STATE_CLOSED:
            try:
                await send({"type": "websocket.close", "code": exc.close_code})
            except Exception:
                pass  # transport already gone — nothing to do
        logger.warn(
            f"WebSocket handshake rejected: {exc.message}",
            context="WebSocket",
            path=path,
            error=type(exc).__name__,
        )
        await _finalize_scope(container, request_cache)
        return
    except Exception:
        # Any unexpected error during @on_connect → internal error
        # close. The hook machinery itself shouldn't leak exceptions to
        # the wire, so we log with traceback but send only the code.
        import logging as _logging

        _logging.getLogger("lauren").exception("Error in @on_connect")
        await send({"type": "websocket.close", "code": 1011})
        await _finalize_scope(container, request_cache)
        return

    # ---- Message dispatch loop ----

    disconnect_reason: WebSocketDisconnect | None = None
    try:
        while ws.connected:
            try:
                msg = await ws.receive()
            except WebSocketDisconnect as exc:
                disconnect_reason = exc
                break

            mtype = msg.get("type")
            if mtype != "websocket.receive":
                # Other ASGI message types are rare here; just ignore
                # and loop — the standard says only ``receive`` and
                # ``disconnect`` flow from client → server.
                continue

            try:
                if "bytes" in msg and msg["bytes"] is not None:
                    await _dispatch_binary(gateway, _run_hook, msg["bytes"], ws)
                elif "text" in msg and msg["text"] is not None:
                    await _dispatch_text(gateway, _run_hook, msg["text"], ws)
            except WebSocketDisconnect as exc:
                disconnect_reason = exc
                break
            except WebSocketError as exc:
                # Validation + protocol errors: reply with a structured
                # error frame; don't close unless the handler opted to.
                await _emit_error_frame(ws, exc)
            except Exception as exc:
                handled = False
                if gateway.on_error is not None:
                    try:
                        await _run_hook(
                            gateway.on_error,
                            gateway.on_error_extractions,
                            extra_values={
                                "__ws_error__": exc,
                            },
                        )
                        handled = True
                    except Exception:
                        import logging as _logging

                        _logging.getLogger("lauren").exception("Error in @on_error handler")
                if not handled:
                    import logging as _logging

                    _logging.getLogger("lauren").exception("Unhandled error in @on_message handler")
                    await _emit_error_frame(
                        ws,
                        WebSocketError(
                            "internal error",
                            detail={"type": type(exc).__name__},
                        ),
                    )
    finally:
        # ---- @on_disconnect + scope cleanup ----
        try:
            await _run_hook(
                gateway.on_disconnect,
                gateway.on_disconnect_extractions,
            )
        except Exception:
            import logging as _logging

            _logging.getLogger("lauren").exception("Error in @on_disconnect handler")
        # If the loop exited without peer disconnect, ensure we emitted
        # a close frame so the client doesn't hang.
        if ws.connected:
            try:
                await ws.close(code=1000)
            except Exception:
                pass
        await _finalize_scope(container, request_cache)

        # Publish the disconnect code on the app logger for observability.
        close_code = disconnect_reason.close_code if disconnect_reason is not None else ws.close_code
        logger.log(
            f"WebSocket closed {path} code={close_code}",
            context="WebSocket",
            path=path,
            code=close_code,
        )


async def _dispatch_text(
    gateway: CompiledGateway,
    run_hook: Callable[..., Awaitable[Any]],
    text: str,
    ws: WebSocket,
) -> None:
    """Parse a text frame, find the handler, run it."""
    try:
        frame = _jsonlib.loads(text)
    except _jsonlib.JSONDecodeError as e:
        raise WebSocketValidationError(
            f"invalid JSON frame: {e}",
            detail={"fragment": text[:120]},
        ) from e
    if not isinstance(frame, dict):
        raise WebSocketValidationError(
            "frame must be a JSON object with an 'event' field",
            detail={"received": type(frame).__name__},
        )
    event = frame.get("event")
    if not isinstance(event, str):
        raise WebSocketValidationError(
            "frame missing required 'event' field",
            detail={"frame_keys": list(frame.keys())},
        )
    compiled = gateway.messages.get(event) or gateway.messages.get(WILDCARD_EVENT)
    if compiled is None:
        raise WebSocketValidationError(
            f"no handler for event {event!r}",
            detail={"event": event, "known": list(gateway.messages.keys())},
        )
    payload = frame.get("data", frame)
    # ``data`` is the conventional payload key; we fall back to the
    # full frame so callers who embed their discriminator fields at
    # the top level don't have to wrap everything in ``data``.
    if compiled.payload_adapter is not None:
        try:
            payload = compiled.payload_adapter.validate_python(payload if event != payload else frame)
        except Exception as exc:
            _errors = getattr(exc, "errors", None)
            raise WebSocketValidationError(
                "validation error",
                detail={
                    "event": event,
                    "errors": _errors() if callable(_errors) else [str(exc)],
                },
            ) from exc
    extra: dict[str, Any] = {}
    if compiled.payload_param is not None:
        extra[compiled.payload_param] = payload
    await run_hook(
        compiled.handler_fn,
        compiled.extractions,
        extra_values=extra,
    )


async def _dispatch_binary(
    gateway: CompiledGateway,
    run_hook: Callable[..., Awaitable[Any]],
    data: bytes,
    ws: WebSocket,
) -> None:
    """Route a binary frame to ``@on_message("__binary__")``."""
    compiled = gateway.messages.get(BINARY_EVENT) or gateway.messages.get(WILDCARD_EVENT)
    if compiled is None:
        raise WebSocketValidationError(
            "no handler for binary frames",
            detail={"size": len(data)},
        )
    extra: dict[str, Any] = {}
    if compiled.payload_param is not None:
        extra[compiled.payload_param] = data
    await run_hook(
        compiled.handler_fn,
        compiled.extractions,
        extra_values=extra,
    )


async def _emit_error_frame(ws: WebSocket, exc: WebSocketError) -> None:
    """Send ``exc`` back to the client using the canonical envelope.

    Matches the HTTP error envelope (``{"error": {"code", "message",
    "detail"}}``) so clients that already parse one format don't need a
    second parser for WebSockets. Silent no-op if the socket is dead.
    """
    if not ws.connected:
        return
    try:
        await ws.send_json(exc.to_payload())
    except Exception:
        pass


async def _finalize_scope(
    container: DIContainer,
    request_cache: dict[type, Any],
) -> None:
    """Run @pre_destruct / aclose on every request-scoped provider.

    Mirrors the HTTP dispatcher's finalization so DB sessions /
    connection pools / user-defined request-scoped resources are torn
    down reliably after every connection — even if the handler raised.
    """
    providers_by_cls = {p.cls: p for p in container.all_providers()}
    for cls in reversed(list(request_cache.keys())):
        instance = request_cache[cls]
        provider = providers_by_cls.get(cls)
        if provider is not None and provider.pre_destruct is not None:
            try:
                bound = getattr(instance, provider.pre_destruct.__name__)
                res = bound()
                if _inspect.isawaitable(res):
                    await res
            except Exception:
                import logging as _logging

                _logging.getLogger("lauren").exception("Error in @pre_destruct on %s (ws)", cls.__name__)
        aclose = getattr(instance, "aclose", None)
        if callable(aclose):
            try:
                res = aclose()
                if _inspect.isawaitable(res):
                    await res
            except Exception:
                import logging as _logging

                _logging.getLogger("lauren").exception("Error in aclose on %s (ws)", cls.__name__)


async def _bind_ws_kwargs(
    ws: WebSocket,
    extractions: tuple[Extraction, ...],
    container: DIContainer,
    request_cache: dict[type, Any],
    owning_module: type | None,
    extra: dict[str, Any],
) -> dict[str, Any]:
    """Produce ``**kwargs`` for a WebSocket hook from its extractor plan.

    Inlines a handful of WS-specific sources (``websocket``, the pre-
    dispatched payload in ``extra``) and delegates everything else to
    :func:`extract_parameter`, which already knows how to handle path /
    query / header / cookie / Depends on a :class:`Request`-like
    object. To reuse that path we build a small adapter that exposes
    the WebSocket's path_params / headers / query_string / state in the
    shape the extractor expects.
    """
    kwargs: dict[str, Any] = {}
    request_like = _WsRequestAdapter(ws)
    ws_error = extra.get("__ws_error__")
    for ext in extractions:
        if ext.source == "websocket":
            kwargs[ext.name] = ws
            continue
        if ext.source == "ws_error":
            kwargs[ext.name] = ws_error
            continue
        if ext.source == "ws_binary":
            # Binary payload — pulled from the ``extra`` bucket under
            # the handler's declared parameter name (not a fixed key)
            # so the dispatcher can inject whatever the user named it.
            if ext.name in extra:
                kwargs[ext.name] = extra[ext.name]
            continue
        if ext.name in extra:
            kwargs[ext.name] = extra[ext.name]
            continue
        # ``json`` extractions are satisfied from ``extra`` above when
        # they carry the payload parameter name; if a user declared a
        # Json[...] parameter but we didn't find a payload for it
        # (e.g. an @on_connect hook), fall through and extract_parameter
        # will see an empty body and raise an appropriate error — the
        # same behaviour HTTP handlers get.
        kwargs[ext.name] = await extract_parameter(
            request_like,  # type: ignore[arg-type]
            ext,
            container=container,
            request_cache=request_cache,
            owning_module=owning_module,
        )
    return kwargs


class _WsRequestAdapter:
    """Minimal Request-alike view of a :class:`WebSocket`.

    :func:`extract_parameter` expects a :class:`~lauren.types.Request`-
    shaped object to pull path params, query strings, headers, cookies,
    and request state from. A WebSocket handshake carries all of those
    but not an HTTP body — this adapter surfaces them without the
    runtime having to duplicate every extractor.
    """

    __slots__ = ("_ws",)

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws

    @property
    def path_params(self) -> dict[str, str]:
        return self._ws.path_params

    @property
    def query_params(self) -> dict[str, list[str]]:
        from urllib.parse import parse_qsl

        qs = self._ws.query_string.decode("latin-1")
        out: dict[str, list[str]] = {}
        for k, v in parse_qsl(qs, keep_blank_values=True):
            out.setdefault(k, []).append(v)
        return out

    @property
    def headers(self) -> Headers:
        return self._ws.headers

    @property
    def cookies(self) -> dict[str, str]:
        header = self._ws.headers.get("cookie", "")
        out: dict[str, str] = {}
        if header:
            for pair in header.split(";"):
                if "=" in pair:
                    k, _, v = pair.partition("=")
                    out[k.strip()] = v.strip()
        return out

    @property
    def state(self) -> Any:
        return self._ws.state

    @property
    def app_state(self) -> Any:
        return self._ws.app_state

    async def body(self) -> bytes:  # pragma: no cover - handshake has no body
        return b""


__all__ = [
    "CompiledGateway",
    "CompiledMessage",
    "compile_gateways",
    "handle_websocket",
    "WILDCARD_EVENT",
    "BINARY_EVENT",
]
