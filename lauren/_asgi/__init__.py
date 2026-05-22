"""ASGI runtime — LaurenApp and LaurenFactory."""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable

import anyio.to_thread

from .._typing import resolve_type_hints

from .._arena import RequestArena
from .._di import INJECTABLE_META, DIContainer, InjectableMeta
from .._di.custom import CustomProvider
from ..serialization import (
    JSONEncoder,
    StdlibJSONEncoder,
    get_active_encoder,
)
from ..signals import (
    RequestComplete,
    RequestReceived,
    ShutdownBegin,
    SignalBus,
    StartupBegin,
    StartupComplete,
    get_default_bus,
)
from ..background import BackgroundTasks as _BackgroundTasks, _BG_TASKS_ATTR
from .._lifecycle import LifecycleScheduler
from .._modules import ModuleGraph
from .._routing import RouteEntry, Router
from ..decorators import (
    CONTROLLER_META,
    EXCEPTION_HANDLER_META,
    ROUTE_META,
    SET_METADATA,
    USE_ENCODER,
    USE_EXCEPTION_HANDLERS,
    USE_GUARDS,
    USE_INTERCEPTORS,
    USE_MIDDLEWARES,
    ControllerMeta,
    ExceptionHandlerMeta,
    RouteMeta,
)
from ..exceptions import (
    ExceptionHandlerConfigError,
    ForbiddenError,
    HTTPError,
    LaurenError,
    LifecycleViolationError,
    MethodNotAllowedError,
    RouteNotFoundError,
    StartupError,
)
from ..extractors import (
    FieldDescriptor,
    Extraction,
    _ParamSpec,
    _is_pydantic_model_type,
    _is_implicit_query_type,
    _is_struct_type,
    _peel_optional,
    extract_parameter,
    is_pipe,
    parse_extractor_hint,
)
from ..streaming import (
    FORMAT_TO_MEDIA_TYPE,
    _build_adapter,
    extract_streaming_item_type,
    negotiate_stream_format,
)
from ..sse import EventStream
from ..logging import (
    Logger,
    LogLevel,
    NullLogger,
    format_duration_ms,
)
from ..types import (
    AppState,
    CallHandler,
    CallNext,
    ClientInfo,
    ExecutionContext,
    Headers,
    Request,
    Response,
    Scope as DScope,
    ServerInfo,
)

logger = logging.getLogger("lauren")


# ---------------------------------------------------------------------------
# Compiled handler
# ---------------------------------------------------------------------------


@dataclass
class CompiledHandler:
    controller_cls: type
    handler_fn: Callable[..., Any]
    route_meta: RouteMeta
    path_template: str
    extractions: tuple[Extraction, ...]
    middleware_chain: tuple[type, ...]
    guards: tuple[type, ...]
    metadata: dict[str, Any]
    #: Effective exception-handler chain for this route, ordered route
    #: → controller (globals are appended at dispatch time so that the
    #: same compiled handler can be reused under different global
    #: configurations during testing). Each entry is a class or function
    #: previously decorated with ``@exception_handler`` and is resolved
    #: through the DI container at dispatch time.
    exception_handlers: tuple[Any, ...] = ()
    #: The module class that declares the controller. Controls which
    #: providers ``Depends[X]`` and auto-DI parameters can reach.
    owning_module: type | None = None
    #: Item type ``T`` extracted from a ``-> StreamingResponse[T]`` return
    #: annotation, or ``None`` when the handler does not stream. The
    #: dispatcher consults this field to pick the streaming serializer
    #: path; the OpenAPI generator consults it to emit ``x-streaming``.
    streaming_item_type: Any = None
    #: Binding style that tells the dispatcher how to invoke ``handler_fn``.
    #: One of ``"instance"`` (normal method, pass ``controller`` first),
    #: ``"classmethod"`` (pass ``controller_cls`` first), or ``"static"``
    #: (no receiver). Kept for introspection; dispatch now uses
    #: ``raw_descriptor.__get__`` instead of branching on this string.
    binding: str = "instance"
    #: ``True`` when ``handler_fn`` is a coroutine function (``async def``).
    #: Detected once at startup; the dispatcher uses this to decide whether
    #: to ``await`` directly or to offload to a thread pool via
    #: ``asyncio.to_thread`` so that blocking sync handlers do not stall the
    #: event loop.
    is_coroutine: bool = False
    #: Raw descriptor from ``cls.__dict__[attr_name]`` — the entry *before*
    #: ``_unwrap_handler_descriptor`` strips ``staticmethod`` /
    #: ``classmethod`` wrappers.  At dispatch time the runtime calls
    #: ``raw_descriptor.__get__(instance, cls)`` to obtain a bound callable,
    #: delegating to Python's descriptor protocol for every binding style
    #: including arbitrary custom descriptors that implement ``__get__``.
    #: ``None`` only for built-in synthetic handlers that bypass the MRO
    #: walk (e.g. the auto-generated OpenAPI docs endpoints).
    raw_descriptor: Any = None
    #: Ordered tuple of interceptor classes applied to this route.
    #: Encodes controller-level (class-then-method) chains; global
    #: interceptors are prepended at dispatch time.
    interceptors: tuple[type, ...] = ()
    #: Per-route JSON encoder set by ``@use_encoder(enc)``.  ``None``
    #: means fall through to the app-level encoder (``LaurenApp._json_encoder``).
    #: Method-level wins over controller-level; both win over app-level.
    encoder: Any = None


def _compile_handler_signature(
    controller_cls: type,
    fn: Callable[..., Any],
    container: DIContainer,
    path_param_names: set[str] | None = None,
    owning_module: type | None = None,
) -> tuple[tuple[Extraction, ...], list[str]]:
    """Introspect a handler and produce its extraction plan.

    Handles three metadata-placement styles and combines them:

    1. ``Annotated[Path[int], PathField(...), pipe(...)]`` — metadata
       bundled inside the type hint.
    2. ``name: Path[int] = PathField(...) & pipe(...)`` — field descriptor
       and pipes composed via ``&`` as the default.
    3. ``name: Path[int] = PathField(default=...)`` — the classic form
       already supported by existing tests.

    Annotation-side pipes run before default-side pipes (so the ordering
    in source code matches left-to-right visual reading).
    """
    sig = inspect.signature(fn)
    hints = _safe_type_hints(fn)
    path_param_names = path_param_names or set()
    extractions: list[Extraction] = []
    param_names: list[str] = []
    # Classmethod handlers have ``cls`` as their first argument — skip it
    # along with ``self`` so the DI plan starts at the first user
    # parameter regardless of binding style.
    for name, param in sig.parameters.items():
        if name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        ann = hints.get(name, param.annotation)
        source, inner, reads_body, marker_cls, ann_fd, ann_pipes = parse_extractor_hint(ann)
        default = param.default
        has_default = default is not inspect.Parameter.empty

        # Pull FieldDescriptor / pipes out of the default. Accepted shapes:
        #   • ``_ParamSpec`` — a ``PathField(...) | pipe(fn) | PipeClass`` chain
        #   • ``FieldDescriptor`` — a bare ``PathField(...)`` / ``QueryField(...)``
        #   • any callable carrying ``__lauren_pipe__`` — sugar for a single-pipe chain
        default_fd: FieldDescriptor | None = None
        default_pipes: tuple[Any, ...] = ()
        if has_default and isinstance(default, _ParamSpec):
            default_fd = default.field_descriptor
            default_pipes = default.pipes
            default = default_fd.default if default_fd else ...
            has_default = default is not ...
        elif has_default and isinstance(default, FieldDescriptor):
            default_fd = default
            default = default_fd.default
            has_default = default_fd.default is not ...
        elif has_default and is_pipe(default):
            default_pipes = (default,)
            default = inspect.Parameter.empty  # no real default value
            has_default = False

        # Reconcile field descriptors — at most one allowed between
        # annotation and default.
        fd: FieldDescriptor | None
        if ann_fd is not None and default_fd is not None:
            from ..exceptions import UnresolvableParameterError

            raise UnresolvableParameterError(
                f"Parameter {name!r} in {controller_cls.__name__}.{fn.__name__} "
                "has FieldDescriptor both in the annotation and as the default; "
                "pick one place.",
                detail={
                    "class": controller_cls.__name__,
                    "handler": fn.__name__,
                    "param": name,
                },
            )
        fd = ann_fd or default_fd
        # If the FieldDescriptor carries a default value and the parameter
        # itself has no plain default, surface the descriptor's default.
        if fd is not None and not has_default and fd.default is not ...:
            default = fd.default
            has_default = True

        pipes = ann_pipes + default_pipes

        # Auto-promote bare parameters whose name matches a path variable.
        if source is None and name in path_param_names:
            inner_type = ann if ann is not inspect.Parameter.empty else str
            extractions.append(
                Extraction(
                    name=name,
                    source="path",
                    inner_type=inner_type,
                    field_descriptor=fd,
                    default=default,
                    has_default=has_default,
                    pipes=pipes,
                )
            )
            param_names.append(name)
            continue
        if source is None:
            # Could be Request or app-state / DI dep
            if ann is Request or (isinstance(ann, type) and issubclass(ann, Request)):
                extractions.append(
                    Extraction(
                        name=name,
                        source="request",
                        inner_type=ann,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue
            # ExecutionContext injection — short-circuited at dispatch time.
            if ann is ExecutionContext or (isinstance(ann, type) and issubclass(ann, ExecutionContext)):
                extractions.append(
                    Extraction(
                        name=name,
                        source="execution_context",
                        inner_type=ann,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue
            # BackgroundTasks parameter — detected and short-circuited at dispatch time.
            if ann is _BackgroundTasks or (isinstance(ann, type) and issubclass(ann, _BackgroundTasks)):
                extractions.append(
                    Extraction(
                        name=name,
                        source="background_tasks",
                        inner_type=ann,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue
            # Resolve via DI — restricted to providers visible to the
            # controller's declaring module when one is known. Accepts
            # class tokens, function tokens (function providers
            # registered via ``@injectable()`` on a ``def``), and
            # ``list[T]`` tokens that expand to every multi-bound
            # provider of ``T`` at resolution time. ``has_provider``
            # handles the ``list[T]`` shape internally so the compiler
            # only has to decide whether the token *looks* resolvable.
            from .._di import _extract_inject_token, _multi_binding_element_type

            # ``Annotated[T, Inject("X")]`` overrides the type-as-token
            # convention. When the marker is present we resolve against
            # the user-supplied token rather than the bare annotation —
            # this is how non-class tokens (strings, Token instances)
            # reach into route handler signatures.
            inject_token = _extract_inject_token(ann)
            if inject_token is not None and container.has_provider(inject_token, owning_module=owning_module):
                extractions.append(
                    Extraction(
                        name=name,
                        source="depends",
                        inner_type=inject_token,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue

            is_multi_list = _multi_binding_element_type(ann) is not None
            looks_like_di_token = (
                isinstance(ann, type) or (callable(ann) and not isinstance(ann, type)) or is_multi_list
            )
            if looks_like_di_token and container.has_provider(ann, owning_module=owning_module):
                extractions.append(
                    Extraction(
                        name=name,
                        source="depends",
                        inner_type=ann,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue

            # ---------------------------------------------------------------------------
            # Implicit parameter detection: auto-promote bare (un-marked) parameters
            # that the user did not wrap in an extractor marker and that are not
            # resolvable via DI.
            #
            # Rules (in priority order):
            #   1. Pydantic BaseModel (possibly Optional[Model]) → JSON body.
            #      Rationale: a model type carries field-level schema, so the natural
            #      extraction point is the request body.
            #   2. Scalar type (int, str, float, bool, bytes, list[scalar], …) or bare
            #      unannotated parameter → query string parameter.
            #      Rationale: primitives are cheapest to pass as query params; this
            #      mirrors FastAPI and other typed-route frameworks.
            #   3. Everything else → raise (preserve old behaviour so that unregistered
            #      DI tokens and multi-binding patterns still fail loudly at startup).
            #
            # Both promotions can be overridden with explicit markers:
            # ``Query[MyModel]`` to pull model fields from the query string, or
            # ``Json[int]`` if a scalar should come from the body.
            # ---------------------------------------------------------------------------
            if _is_pydantic_model_type(ann):
                # Peel Optional[Model] so the extraction layer sees the plain
                # model type (not Optional[Model]) when calling _validate_json.
                body_inner, _ = _peel_optional(ann)
                extractions.append(
                    Extraction(
                        name=name,
                        source="json",
                        inner_type=body_inner,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        reads_body=True,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue
            if _is_struct_type(ann):
                # msgspec.Struct / dataclass — auto-promote to JSON body,
                # mirroring the Pydantic model behaviour above.
                body_inner, _ = _peel_optional(ann)
                extractions.append(
                    Extraction(
                        name=name,
                        source="json",
                        inner_type=body_inner,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        reads_body=True,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue
            if _is_implicit_query_type(ann):
                inner_type = ann
                extractions.append(
                    Extraction(
                        name=name,
                        source="query",
                        inner_type=inner_type,
                        field_descriptor=fd,
                        default=default,
                        has_default=has_default,
                        pipes=pipes,
                    )
                )
                param_names.append(name)
                continue
            from ..exceptions import UnresolvableParameterError

            raise UnresolvableParameterError(
                f"Cannot resolve parameter {name!r} in {controller_cls.__name__}.{fn.__name__}",
                detail={
                    "class": controller_cls.__name__,
                    "handler": fn.__name__,
                    "param": name,
                },
            )
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
                pipes=pipes,
            )
        )
        # No startup restriction on instance-method extractors: both
        # @injectable (DI-resolved) and plain (no-arg cache) forms are valid.
        param_names.append(name)
    return tuple(extractions), param_names


def _has_unresolved_hints(hints: dict[str, Any]) -> bool:
    """Return True when any resolved hint still contains a
    :class:`typing.ForwardRef`.

    Used by :func:`_safe_type_hints` to decide whether the stdlib fast
    path produced a usable result or whether we should retry with the
    calling-frame stack layered on top. Mirrors the equivalent helper
    in :mod:`lauren._di`.
    """
    import typing as _typing

    def _walk(ann: Any) -> bool:
        if isinstance(ann, _typing.ForwardRef):
            return True
        for arg in _typing.get_args(ann):
            if _walk(arg):
                return True
        return False

    return any(_walk(v) for v in hints.values())


def _safe_type_hints(fn: Callable[..., Any]) -> dict[str, Any]:
    """Resolve handler annotations, tolerating unresolved forward refs.

    Mirrors :func:`lauren._di._safe_class_hints`: when the plain
    ``get_type_hints`` call fails (usually because the handler lives in
    a nested scope and its annotation strings reference function-local
    names that module globals can't see), walk the calling frame stack
    to collect local namespaces and retry. Falls back to raw annotations
    only as a last resort — matching the DI container's behaviour so
    both paths surface the same name-resolution surface.
    """
    try:
        hints = resolve_type_hints(fn, include_extras=True)
        if not _has_unresolved_hints(hints):
            return hints
    except Exception:
        hints = {}
    import sys as _sys

    frame = _sys._getframe(1)
    merged_locals: dict[str, Any] = {}
    while frame is not None:
        for k, v in frame.f_locals.items():
            merged_locals.setdefault(k, v)
        frame = frame.f_back  # type: ignore[assignment]
    globalns: dict[str, Any] = dict(getattr(fn, "__globals__", {}) or {})
    try:
        return resolve_type_hints(
            fn,
            globalns=globalns,
            localns=merged_locals,
            include_extras=True,
        )
    except Exception:
        pass
    # Last resort: inspect.get_annotations(eval_str=True) evaluates PEP 563
    # string annotations (from __future__ import annotations) using the
    # function's own globals, so handler files can freely use that import
    # without breaking extractor hint resolution.
    try:
        import inspect as _inspect

        evaled = _inspect.get_annotations(fn, eval_str=True)
        if evaled:
            return evaled
    except Exception:
        pass
    return hints or {}


def _unwrap_handler_descriptor(
    raw: Any,
) -> tuple[Callable[..., Any] | None, str]:
    """Normalize a ``cls.__dict__`` entry to ``(callable, binding)``.

    Returns the underlying function plus the binding style the dispatch
    loop should use to invoke it:

    * plain function  → ``(fn,  "instance")``
    * ``staticmethod``  → ``(fn,  "static")``
    * ``classmethod``   → ``(fn,  "classmethod")``

    Non-callable or unrecognised descriptors produce ``(None, ...)``
    so callers can cheaply filter them out.

    Marker lookup is bi-directional: a route decorator may run either
    *above* ``@staticmethod`` / ``@classmethod`` (marker lands on the
    descriptor) or *below* them (marker lands on the raw function).
    We propagate any markers we find on the descriptor onto ``__func__``
    so the downstream signature / ``getattr`` machinery sees them in a
    single canonical place. Applied markers include route / middleware
    / guard / metadata / WebSocket hook attributes; the helper works
    from a conservative allow-list rather than ``vars()`` on the
    descriptor because ``staticmethod`` / ``classmethod`` objects
    accept almost any attribute without complaint.
    """
    _MARKER_ATTRS = (
        "__lauren_route__",
        "__lauren_use_middlewares__",
        "__lauren_use_guards__",
        "__lauren_use_interceptors__",
        "__lauren_metadata__",
        "__lauren_ws_on_connect__",
        "__lauren_ws_on_disconnect__",
        "__lauren_ws_on_message__",
        "__lauren_ws_on_error__",
        "__lauren_post_construct__",
        "__lauren_pre_destruct__",
    )

    def _merge_markers(descriptor: Any, func: Any) -> None:
        for marker in _MARKER_ATTRS:
            if hasattr(descriptor, marker) and not hasattr(func, marker):
                try:
                    setattr(func, marker, getattr(descriptor, marker))
                except (AttributeError, TypeError):
                    pass

    if isinstance(raw, staticmethod):
        fn = raw.__func__
        if callable(fn):
            _merge_markers(raw, fn)
            return fn, "static"
        return None, "static"
    if isinstance(raw, classmethod):
        fn = raw.__func__
        if callable(fn):
            _merge_markers(raw, fn)
            return fn, "classmethod"
        return None, "classmethod"
    if callable(raw):
        return raw, "instance"
    # Non-callable custom descriptor: use __wrapped__ (set by functools.wraps)
    # for signature and route-metadata inspection; dispatch goes through __get__.
    _wrapped = getattr(raw, "__wrapped__", None)
    if hasattr(raw, "__get__") and callable(_wrapped):
        return _wrapped, "instance"
    return None, "instance"


def _normalize_path(*parts: str) -> str:
    pieces: list[str] = []
    for p in parts:
        if not p:
            continue
        stripped = p.strip("/")
        if stripped:
            pieces.append(stripped)
    joined = "/".join(pieces)
    return "/" + joined if joined else "/"


# ---------------------------------------------------------------------------
# LaurenApp — the ASGI app
# ---------------------------------------------------------------------------


class LaurenApp:
    """A compiled, ready-to-serve ASGI application.

    Instances of this class are produced exclusively by :meth:`LaurenFactory.create`.
    """

    def __init__(
        self,
        *,
        router: Router,
        container: DIContainer,
        module_graph: ModuleGraph,
        lifecycle: LifecycleScheduler,
        compiled_handlers: dict[tuple[str, str], CompiledHandler],
        global_middlewares: list[type],
        app_state: AppState,
        strict_lifecycle: bool = True,
        max_body_size: int = 1_048_576,
        logger: Logger | None = None,
        ws_router: Router | None = None,
        ws_gateways: dict[str, Any] | None = None,
        arena: RequestArena | None = None,
        signals: SignalBus | None = None,
        error_format: str = "default",
        global_guards: list[type] | None = None,
        global_exception_handlers: list[Any] | None = None,
        global_interceptors: list[type] | None = None,
        global_providers: list[Any] | None = None,
    ) -> None:
        self._router = router
        self._container = container
        self._module_graph = module_graph
        self._lifecycle = lifecycle
        self._handlers = compiled_handlers
        self._global_middlewares = list(global_middlewares)
        self._global_guards = list(global_guards or [])
        self._global_exception_handlers = list(global_exception_handlers or [])
        self._global_interceptors: list[type] = list(global_interceptors or [])
        self._global_providers: list[Any] = list(global_providers or [])
        self._app_state = app_state
        self._strict_lifecycle = strict_lifecycle
        self._max_body_size = max_body_size
        self._running = False
        self._in_flight: set[asyncio.Task[Any]] = set()
        self._started = False
        self._shutdown_event = asyncio.Event()
        self._shutdown_running = False
        self._shutdown_complete = asyncio.Event()
        self._on_shutdown_callbacks: list[Callable[[], Any]] = []
        self._logger: Logger = logger or NullLogger()
        # WebSocket surface — optional; when no gateways are declared
        # the ASGI ``__call__`` path that handles ``websocket`` scopes
        # short-circuits with a 1008 close so the HTTP runtime stays
        # untouched for pure-HTTP apps.
        self._ws_router: Router | None = ws_router
        self._ws_gateways: dict[str, Any] = dict(ws_gateways or {})
        # The per-app arena pools request containers and ``Request``
        # instances to reduce GC churn under load. A user may pass a
        # pre-built arena (e.g. with an increased capacity for busy
        # apps); otherwise the default 256-slot arena is sufficient for
        # most workloads. Passing ``arena=RequestArena(capacity=0)``
        # disables pooling — handy for tests and for A/B measurements.
        self._arena: RequestArena = arena or RequestArena()
        # JSON encoder is captured by reference at build time so the
        # hot path never re-looks it up. Defaults to the process-wide
        # encoder, which starts as the stdlib one and can be swapped
        # via :func:`lauren.serialization.set_active_encoder` for
        # contexts outside a LaurenApp build.
        self._json_encoder: JSONEncoder = StdlibJSONEncoder()
        # Lifecycle event bus. If the caller doesn't supply one we
        # build a fresh per-app bus seeded from the process-wide
        # default — this lets users write ``@on(RequestComplete)`` at
        # module level and still have their listeners wired into
        # whatever app ends up being built. Multi-app setups get
        # isolation by passing an explicit ``SignalBus`` to
        # ``LaurenFactory.create``.
        if signals is not None:
            self._signals = signals
        else:
            self._signals = SignalBus(logger=self._logger)
            # Copy listeners from the default bus so module-level
            # ``@on`` decorators take effect.
            default = get_default_bus()
            for evt_type, fns in default._listeners.items():  # noqa: SLF001
                for fn in fns:
                    self._signals.on(evt_type)(fn)
        # Error envelope format. ``"default"`` emits lauren's classic
        # ``{"error": {"code", "message", "detail"}}`` shape;
        # ``"rfc7807"`` emits RFC 7807 Problem Details with the
        # ``application/problem+json`` content type. Anything else
        # falls back to ``default`` with a log warning rather than
        # raising — this is a user-facing response shape, not a
        # startup-critical invariant.
        if error_format not in ("default", "rfc7807"):
            self._logger.warn(
                f"unknown error_format {error_format!r}; using 'default'",
                context="LaurenApp",
            )
            error_format = "default"
        self._error_format = error_format
        # Mounted sub-applications, ordered by descending prefix length so the
        # most-specific prefix wins when multiple prefixes could match.
        self._mounts: list[tuple[str, Any]] = []

    # -- Introspection -----------------------------------------------------

    @property
    def router(self) -> Router:
        return self._router

    @property
    def container(self) -> DIContainer:
        return self._container

    @property
    def app_state(self) -> AppState:
        return self._app_state

    @property
    def module_graph(self) -> ModuleGraph:
        return self._module_graph

    @property
    def arena(self) -> RequestArena:
        """The per-app :class:`RequestArena` pool.

        Exposed primarily for observability (``app.arena.stats``) and
        for tests that want to drain or reconfigure the pool. The
        dispatcher consults the arena on every request; user code
        should treat it as read-only.
        """
        return self._arena

    @property
    def json_encoder(self) -> JSONEncoder:
        """The active JSON encoder for this application.

        Pinned at build time by :meth:`LaurenFactory.create` so the
        dispatcher's hot path reads the reference once rather than
        looking it up per request. User code that wants a custom
        serialization pipeline should pass ``json_encoder=...`` to
        the factory.
        """
        return self._json_encoder

    @property
    def signals(self) -> SignalBus:
        """The per-app :class:`SignalBus` for lifecycle events.

        Register listeners with ``@app.signals.on(RequestComplete)``.
        Multi-app deployments get automatic isolation because each
        :class:`LaurenApp` owns its own bus.
        """
        return self._signals

    @property
    def error_format(self) -> str:
        """``'default'`` or ``'rfc7807'``. See
        :meth:`LaurenFactory.create` for the configuration hook."""
        return self._error_format

    def routes(self) -> list[RouteEntry]:
        return self._router.routes()

    def openapi(self) -> dict[str, Any]:
        from ._openapi import generate_openapi

        return generate_openapi(self)

    @property
    def logger(self) -> Logger:
        """The :class:`Logger` installed for this application."""
        return self._logger

    def on_shutdown(self, callback: Callable[[], Any]) -> Callable[[], Any]:
        """Register a callback to run during :meth:`shutdown`.

        Complements ``@pre_destruct`` by letting callers attach arbitrary
        cleanup coroutines (or plain callables) without needing to express
        them as DI-scoped providers. Usable as a decorator::

            @app.on_shutdown
            async def flush_buffers() -> None:
                ...

        Callbacks run in reverse registration order (LIFO) after in-flight
        requests have drained and **before** ``@pre_destruct`` hooks, so
        they can use the DI graph if needed.
        """
        self._on_shutdown_callbacks.append(callback)
        return callback

    # -- Sub-application mounting ------------------------------------------

    def mount(self, path: str, app: Any) -> None:
        """Mount an ASGI sub-application at *path*.

        All requests whose path starts with *path* (or equals it exactly) are
        forwarded to *app* after stripping the prefix.  The stripped portion
        is appended to ``scope["root_path"]`` so the sub-application can
        reconstruct absolute URLs correctly.

        Mounts are checked in descending prefix-length order so a more-specific
        prefix (``/api/v2``) always wins over a shorter one (``/api``).

        Example::

            app = LaurenFactory.create(AppModule)
            app.mount("/legacy", legacy_asgi_app)
            app.mount("/files", static_files_app)

        The same result can be achieved at build time via
        ``LaurenFactory.create(..., mounts={"/legacy": legacy_asgi_app})``.
        """
        if not path:
            raise ValueError("mount path must not be empty")
        if not path.startswith("/"):
            path = "/" + path
        # Normalise: drop trailing slashes so prefix matching is uniform.
        path = path.rstrip("/")
        self._mounts.append((path, app))
        # Keep longest prefix first — stable sort preserves insertion order
        # among equal-length prefixes.
        self._mounts.sort(key=lambda t: len(t[0]), reverse=True)

    @property
    def mounts(self) -> list[tuple[str, Any]]:
        """Read-only snapshot of ``(path_prefix, sub_app)`` pairs."""
        return list(self._mounts)

    # -- Startup / Shutdown ------------------------------------------------

    async def startup(self) -> None:
        if self._started:
            if self._strict_lifecycle:
                raise LifecycleViolationError("startup called twice")
            return
        t0 = time.perf_counter()
        # Emit StartupBegin *before* @post_construct runs so listeners
        # see the "DI graph built, about to come online" moment. The
        # bus contract is that listener errors never propagate, so a
        # misbehaving observer cannot prevent startup.
        await self._signals.emit(StartupBegin(app=self))
        self._logger.log("Running @post_construct hooks", context="Lifecycle")
        await self._lifecycle.run_post_construct()
        self._app_state.seal()
        self._started = True
        self._running = True
        duration = time.perf_counter() - t0
        self._logger.log(
            f"Application ready ({format_duration_ms(duration)})",
            context="LaurenApp",
            routes=len(self._router.routes()),
        )
        # StartupComplete marks the app as ready for traffic.
        await self._signals.emit(StartupComplete(app=self, duration_s=duration))

    async def shutdown(self, *, drain_timeout: float = 10.0) -> None:
        """Gracefully stop the application.

        Steps (each logged as an event):

        1. Mark the app not-running — no new request scheduling.
        2. Drain in-flight requests (up to ``drain_timeout`` seconds).
        3. Invoke user-registered ``on_shutdown`` callbacks in reverse order.
        4. Run ``@pre_destruct`` hooks in reverse topological order.

        Idempotent: concurrent or repeated calls return as soon as the first
        shutdown has completed.
        """
        if self._shutdown_running:
            await self._shutdown_complete.wait()
            return
        self._shutdown_running = True
        self._running = False
        self._logger.log(
            f"Shutdown initiated (drain_timeout={drain_timeout}s, in_flight={len(self._in_flight)})",
            context="Shutdown",
            in_flight=len(self._in_flight),
            drain_timeout=drain_timeout,
        )
        # Fire ShutdownBegin early so listeners can observe the
        # transition before the drain / pre_destruct machinery runs.
        await self._signals.emit(ShutdownBegin(app=self))
        # 1. Drain in-flight requests.
        if self._in_flight:
            self._logger.log(
                f"Draining {len(self._in_flight)} in-flight request(s)",
                context="Shutdown",
                in_flight=len(self._in_flight),
            )
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._in_flight, return_exceptions=True),
                    timeout=drain_timeout,
                )
                self._logger.log("All in-flight requests drained", context="Shutdown")
            except asyncio.TimeoutError:
                self._logger.warn(
                    f"Drain timeout after {drain_timeout}s; "
                    f"{len(self._in_flight)} request(s) still in flight",
                    context="Shutdown",
                    in_flight=len(self._in_flight),
                )
        # 2. User callbacks (reverse registration order).
        if self._on_shutdown_callbacks:
            self._logger.log(
                f"Running {len(self._on_shutdown_callbacks)} on_shutdown callback(s)",
                context="Shutdown",
            )
            for cb in reversed(self._on_shutdown_callbacks):
                try:
                    result = cb()
                    if inspect.isawaitable(result):
                        await result
                except Exception as exc:
                    self._logger.error(
                        f"on_shutdown callback {getattr(cb, '__name__', repr(cb))} raised: {exc}",
                        context="Shutdown",
                        error=type(exc).__name__,
                    )
        # 3. DI lifecycle hooks.
        self._logger.log("Running @pre_destruct hooks", context="Shutdown")
        errors = await self._lifecycle.run_pre_destruct(timeout=drain_timeout)
        for err in errors:
            self._logger.error(
                f"Lifecycle shutdown error: {err}",
                context="Shutdown",
                error=type(err).__name__,
            )
        self._logger.log("Shutdown complete. Goodbye.", context="Shutdown")
        self._shutdown_complete.set()

    # -- Exception handler dispatch ---------------------------------------

    async def _dispatch_exception_handlers(
        self,
        exc: BaseException,
        *,
        request: Request,
        handlers: list[Any],
        request_cache: dict[type, Any],
        framework_values: dict[type, Any],
        owning_module: type | None,
        compiled: "CompiledHandler",
    ) -> Response | None:
        """Find and invoke the first matching ``@exception_handler``.

        ``handlers`` is the effective chain in priority order — route
        handlers first, then controller, then global. Each entry was
        decorated with :func:`exception_handler` and is therefore a
        DI provider with ``ExceptionHandlerMeta`` attached.

        Returns ``None`` when no handler claims the exception, in which
        case the caller falls back to the framework's built-in error
        response. Returns a :class:`Response` when a handler matched
        (its return value is coerced through :func:`_coerce_to_response`
        so handlers may also return dicts / Pydantic models / tuples /
        ``None`` like normal route functions).
        """
        if not handlers:
            return None
        for handler in handlers:
            meta = getattr(handler, EXCEPTION_HANDLER_META, None)
            if not isinstance(meta, ExceptionHandlerMeta):
                # Defensive: a non-decorated entry slipped in. Treat as
                # "does not match" rather than raising — the user
                # already saw a startup-time error if this path was
                # reachable.
                continue
            if not isinstance(exc, meta.exceptions):
                continue
            try:
                if isinstance(handler, type):
                    # Class form: resolve through DI so ``__init__``
                    # dependencies populate, then call ``catch(exc,
                    # request)``. Per-route handlers resolve in the
                    # controller's module; globals have no module
                    # restriction.
                    is_global = handler in self._global_exception_handlers
                    handler_owning = None if is_global else owning_module
                    instance = await self._container.resolve(
                        handler,
                        request_cache=request_cache,
                        framework_values=framework_values,
                        owning_module=handler_owning,
                    )
                    result = instance.catch(exc, request)
                else:
                    # Function-form: call directly with ``(exc, request)``.
                    # Function-form handlers are intentionally NOT
                    # registered as DI providers — their parameters
                    # describe the dispatcher's call site, not a graph
                    # of dependencies. Users that want DI should use
                    # the class form.
                    result = handler(exc, request)
                if inspect.isawaitable(result):
                    result = await result
            except Exception:
                # A failing handler is a bug; let it propagate so the
                # caller's outer ``except Exception`` safety net turns
                # it into a 500. Logging is handled there too.
                raise
            return _coerce_to_response(result, encoder=compiled.encoder or self._json_encoder)
        return None

    # -- Request logging --------------------------------------------------

    def _log_request(
        self,
        request: Request,
        response: Response | None,
        started_at: float,
        *,
        handler: str | None,
    ) -> None:
        """Emit a DEBUG-level request/response trace.

        Applications that don't want per-request logs simply keep the logger
        at INFO or above — records below the threshold are dropped cheaply.
        """
        status = response.status if response is not None else 0
        duration = time.perf_counter() - started_at
        # Pick an appropriate level based on status class.
        if status >= 500:
            level = LogLevel.ERROR
        elif status >= 400:
            level = LogLevel.WARN
        else:
            level = LogLevel.DEBUG
        # Avoid building extras when the record would be filtered anyway.
        if int(level) < int(self._logger.level):
            return
        msg_handler = f" → {handler}" if handler else ""
        message = f"{request.method} {request.path} {status} {format_duration_ms(duration)}{msg_handler}"
        extra: dict[str, Any] = {
            "method": request.method,
            "path": request.path,
            "status": status,
            "duration_ms": round(duration * 1000, 3),
        }
        if handler:
            extra["handler"] = handler
        self._logger._emit(level, message, "Request", extra)  # type: ignore[attr-defined]

    # -- Request dispatch -------------------------------------------------

    async def handle(self, request: Request) -> Response:
        """Dispatch a :class:`Request` through middleware, guards and handler.

        Global middlewares run **before** routing so they can intercept every
        request — including OPTIONS preflight — regardless of whether a
        matching route exists.  Per-route and controller middlewares run after
        routing (they need access to the compiled handler).

        Every dispatch acquires a :class:`RequestAllocation` bundle from
        the app's :class:`RequestArena`. The bundle's ``request_cache``
        holds request-scoped DI instances; ``framework_values`` is
        re-populated per call with the live ``Request`` so the DI
        container can short-circuit ``Request`` lookups; ``kwargs``
        holds handler arguments assembled from the extractor plan.

        Every container returned from the lease is cleared on exit, so
        pooled allocations never leak user data across requests.
        """
        t0 = time.perf_counter()
        # These track the outcome across every exit path so the
        # RequestComplete emission in the outer ``finally`` sees a
        # consistent view regardless of whether routing succeeded or
        # the handler raised. ``final_response`` is populated on every
        # return path; ``captured_error`` is populated only when an
        # error was surfaced (HTTPError or an unhandled exception).
        final_response: Response | None = None
        captured_error: BaseException | None = None
        # Tracks the handler qualname for request logging; set inside
        # _route_and_run once a route is matched.
        handler_qualname: str | None = None
        # Fire RequestReceived as the very first thing so listeners
        # see the request before any routing decisions have been made.
        # The fast-path check on ``_listeners`` inside SignalBus.emit
        # makes the no-listener case effectively free.
        await self._signals.emit(RequestReceived(request=request))
        try:
            # Acquire the arena lease before building the global middleware
            # chain so that ``framework_values`` is available for DI
            # resolution of global middleware instances.
            async with self._arena.lease() as alloc:
                # ``request_cache`` holds request-scoped DI instances; the
                # extractor/guard/controller paths all receive the same
                # pooled dict so a single cache entry is visible to every
                # stage. ``framework_values`` maps runtime-supplied
                # dependencies (``Request``, subclasses of Request) so the
                # DI container can short-circuit before walking its
                # provider graph.
                request_cache = alloc.request_cache
                framework_values = alloc.framework_values
                framework_values[Request] = request
                framework_values[type(request)] = request
                kwargs_dict = alloc.kwargs

                async def _route_and_run(req: Request) -> Response:
                    """Routing → per-route middleware → guards → interceptors → handler."""
                    nonlocal captured_error, handler_qualname

                    # --- Routing ---
                    try:
                        entry, params = self._router.find(req.method, req.path)
                    except RouteNotFoundError as e:
                        captured_error = e
                        return _error_response(e, error_format=self._error_format, encoder=self._json_encoder)
                    except MethodNotAllowedError as e:
                        captured_error = e
                        resp = _error_response(e, error_format=self._error_format, encoder=self._json_encoder)
                        if e.allow:
                            resp = resp.with_header("allow", ", ".join(e.allow))
                        return resp

                    compiled = self._handlers[(entry.method, entry.path_template)]
                    # Per-route encoder wins over the app-level encoder.
                    effective_encoder = compiled.encoder or self._json_encoder
                    # Reuse the request's existing ``_path_params`` dict rather
                    # than replacing it: for pooled ``Request`` objects the dict
                    # was already cleared by ``Request.reset``, and for
                    # user-constructed requests it's an empty dict. This saves
                    # one allocation per request on the hot path.
                    req._path_params.clear()
                    req._path_params.update(params)
                    req._matched_route = entry
                    req._handler_class = compiled.controller_cls
                    req._handler_func = compiled.handler_fn
                    req._route_template = entry.path_template
                    handler_qualname = getattr(compiled.handler_fn, "__qualname__", None)

                    # Build effective per-route middleware list: controller → route.
                    # Global middlewares already wrap _route_and_run so they are
                    # intentionally excluded here.
                    mw_classes = list(compiled.middleware_chain)

                    # Effective guard chain: global guards run FIRST, then the
                    # route's compiled chain (which itself is class-then-method).
                    # Global guards see no controller-module restriction so they
                    # behave like cross-cutting middleware.
                    effective_guards = list(self._global_guards) + list(compiled.guards)

                    # Effective interceptor chain: global interceptors are outermost,
                    # then controller-level, then method-level.
                    effective_interceptors = list(self._global_interceptors) + list(compiled.interceptors)

                    # Effective exception-handler chain. Route handlers come
                    # first (they are most specific), then controller, then
                    # global. The compiled tuple already encodes route-then-
                    # controller; appending globals here keeps the per-app
                    # configuration mutable without having to recompile every
                    # CompiledHandler when globals change in tests.
                    effective_exception_handlers = list(compiled.exception_handlers) + list(
                        self._global_exception_handlers
                    )

                    owning_module = compiled.owning_module

                    async def run_handler(req2: Request) -> Response:
                        # Guards
                        ctx = ExecutionContext(
                            request=req2,
                            handler_class=compiled.controller_cls,
                            handler_func=compiled.handler_fn,
                            route_template=compiled.path_template,
                            metadata=dict(compiled.metadata),
                        )
                        for guard_cls in effective_guards:
                            # Global guards have no controller-module
                            # restriction; per-route guards resolve within
                            # the controller's owning module just like
                            # before.
                            guard_owning = owning_module if guard_cls in compiled.guards else None
                            guard = await self._container.resolve(
                                guard_cls,
                                request_cache=request_cache,
                                framework_values=framework_values,
                                owning_module=guard_owning,
                            )
                            ok = await guard.can_activate(ctx)
                            if not ok:
                                raise ForbiddenError(
                                    f"guard {guard_cls.__name__} denied the request",
                                    detail={"guard": guard_cls.__name__},
                                )

                        # ------------ inner invocation (wrapped by interceptors) ---
                        async def _invoke_handler() -> Any:
                            # Instantiate controller + extract args
                            controller = await self._container.resolve(
                                compiled.controller_cls,
                                request_cache=request_cache,
                                framework_values=framework_values,
                                owning_module=owning_module,
                            )
                            # Build kwargs directly into the pooled dict.
                            # ``kwargs_dict`` starts empty (``alloc.kwargs`` was
                            # cleared on lease acquisition); we clear any prior
                            # content defensively in case a middleware populated
                            # the scratch earlier in the chain.
                            kwargs_dict.clear()
                            for ext in compiled.extractions:
                                if ext.source == "request":
                                    kwargs_dict[ext.name] = req2
                                elif ext.source == "execution_context":
                                    kwargs_dict[ext.name] = ctx
                                elif ext.source == "background_tasks":
                                    # Lazy-create once per request; same instance
                                    # for all bg params in the same handler.
                                    _bg: _BackgroundTasks | None = req2.state.get(_BG_TASKS_ATTR)
                                    if _bg is None:
                                        _bg = _BackgroundTasks()
                                        req2.state._lauren_bg_tasks = _bg
                                    kwargs_dict[ext.name] = _bg
                                else:
                                    kwargs_dict[ext.name] = await extract_parameter(
                                        req2,
                                        ext,
                                        container=self._container,
                                        request_cache=request_cache,
                                        owning_module=owning_module,
                                        execution_context=ctx,
                                    )
                            # ``kwargs`` below re-aliases the same pooled dict so
                            # the existing dispatch branches stay identical.
                            kwargs = kwargs_dict
                            # Invoke the handler via the descriptor protocol.
                            #
                            # ``raw_descriptor.__get__(instance, cls)`` produces
                            # the correctly bound callable for every descriptor
                            # kind without requiring an explicit binding branch:
                            #   staticmethod   → raw fn (no receiver)
                            #   classmethod    → bound method with cls prepended
                            #   plain function → bound method with self prepended
                            #   custom __get__ → whatever the descriptor decides
                            #
                            # The controller instance is always resolved first so
                            # that ``@post_construct`` hooks and field injection
                            # fire regardless of binding style.
                            #
                            # Sync handlers are offloaded to a thread pool via
                            # ``anyio.to_thread.run_sync`` so blocking I/O does
                            # not stall the event loop.
                            _descriptor = compiled.raw_descriptor
                            _bound = (
                                _descriptor.__get__(controller, compiled.controller_cls)
                                if _descriptor is not None
                                else compiled.handler_fn.__get__(controller, compiled.controller_cls)
                            )
                            if compiled.is_coroutine:
                                return await _bound(**kwargs)
                            _bnd, _kw = _bound, kwargs
                            return await anyio.to_thread.run_sync(lambda: _bnd(**_kw))

                        # Build interceptor chain (innermost first, then wrap
                        # outward so the first declared interceptor is outermost).
                        call_handler: CallHandler = CallHandler(_invoke_handler)
                        for inter_cls in reversed(effective_interceptors):
                            inter_owning = owning_module if inter_cls in compiled.interceptors else None
                            inter_inst = await self._container.resolve(
                                inter_cls,
                                request_cache=request_cache,
                                framework_values=framework_values,
                                owning_module=inter_owning,
                            )
                            call_handler = _wrap_interceptor(inter_inst, ctx, call_handler)

                        result = await call_handler.handle()

                        # StreamingResponse[T] path — the handler returns an async
                        # iterable of ``T`` which we must frame according to the
                        # request's Accept header. Falls back to the generic coercer
                        # for every other return shape (feature 7).
                        if compiled.streaming_item_type is not None and not isinstance(result, Response):
                            return await _coerce_streaming_response(
                                result,
                                item_type=compiled.streaming_item_type,
                                request=req2,
                                encoder=effective_encoder,
                            )
                        return _coerce_to_response(result, encoder=effective_encoder)

                    # Per-route onion chain (global mw already wraps _route_and_run)
                    chain: CallNext = run_handler
                    for mw_cls in reversed(mw_classes):
                        # Middleware declared on the controller resolves within that
                        # controller's module; global middleware resolves with no
                        # module restriction (it is application-wide).
                        mw_owning = owning_module if mw_cls in compiled.middleware_chain else None
                        mw_instance = await self._container.resolve(
                            mw_cls,
                            framework_values=framework_values,
                            owning_module=mw_owning,
                        )
                        chain = _wrap_middleware(mw_instance, chain)

                    try:
                        try:
                            return await chain(req)
                        except HTTPError as e:
                            # Give user-declared handlers a chance to claim
                            # the error before falling back to lauren's
                            # built-in error envelope. This lets a custom
                            # filter override (for example) the status code
                            # of a ForbiddenError, while leaving the default
                            # behaviour identical when no filter matches.
                            handled = await self._dispatch_exception_handlers(
                                e,
                                request=req,
                                handlers=effective_exception_handlers,
                                request_cache=request_cache,
                                framework_values=framework_values,
                                owning_module=owning_module,
                                compiled=compiled,
                            )
                            captured_error = e
                            if handled is not None:
                                return handled
                            return _error_response(
                                e, error_format=self._error_format, encoder=effective_encoder
                            )
                        except Exception as e:  # pragma: no cover - final safety net
                            # Try user handlers first — a non-HTTP exception
                            # like ValueError can be turned into a clean
                            # response by a registered filter. Only when no
                            # filter matches do we emit the generic 500.
                            handled = await self._dispatch_exception_handlers(
                                e,
                                request=req,
                                handlers=effective_exception_handlers,
                                request_cache=request_cache,
                                framework_values=framework_values,
                                owning_module=owning_module,
                                compiled=compiled,
                            )
                            captured_error = e
                            if handled is not None:
                                return handled
                            logger.exception("Unhandled handler error")
                            self._logger.error(
                                f"Unhandled exception: {type(e).__name__}: {e}",
                                context="Request",
                                method=req.method,
                                path=req.path,
                                error=type(e).__name__,
                            )
                            from ..exceptions import LaurenError

                            return _error_response(
                                LaurenError(
                                    "internal server error",
                                    detail={"type": type(e).__name__},
                                ),
                                status=500,
                                code="internal_error",
                                error_format=self._error_format,
                                encoder=effective_encoder,
                            )
                    finally:
                        # Finalize any request-scoped instances: run their
                        # @pre_destruct hook (if declared) and then any
                        # aclose() coroutine. Hooks run in reverse insertion
                        # order so the most recently constructed instance
                        # tears down first — analogous to the LIFO order used
                        # by the application-level scheduler at shutdown.
                        providers_by_cls = {p.cls: p for p in self._container.all_providers()}
                        # Snapshot the cache keys *before* the arena clears
                        # the pooled dict; otherwise we'd iterate an empty
                        # mapping as soon as the lease exits.
                        request_scoped_instances = [
                            (cls, request_cache[cls]) for cls in reversed(list(request_cache.keys()))
                        ]
                        for cls, instance in request_scoped_instances:
                            provider = providers_by_cls.get(cls)
                            if provider is not None and provider.pre_destruct is not None:
                                try:
                                    bound = getattr(instance, provider.pre_destruct.__name__)
                                    res = bound()
                                    if inspect.isawaitable(res):
                                        await res
                                except Exception:
                                    logger.exception("Error in @pre_destruct on %s", cls.__name__)
                            aclose = getattr(instance, "aclose", None)
                            if callable(aclose):
                                try:
                                    aclose_result = aclose()
                                    if inspect.isawaitable(aclose_result):
                                        await aclose_result
                                except Exception:
                                    logger.exception(
                                        "Error closing request-scoped instance %r",
                                        instance,
                                    )

                # Build global middleware onion around _route_and_run.
                # Global middlewares see every request before routing, so they
                # can intercept OPTIONS preflight, add CORS headers to 404
                # responses, and so on.
                global_chain: CallNext = _route_and_run
                for mw_cls in reversed(self._global_middlewares):
                    mw_instance = await self._container.resolve(
                        mw_cls,
                        framework_values=framework_values,
                        owning_module=None,
                    )
                    global_chain = _wrap_middleware(mw_instance, global_chain)

                response = await global_chain(request)
                self._log_request(request, response, t0, handler=handler_qualname)
                final_response = response
                return response
        finally:
            # Fire RequestComplete on every exit path (success, handled
            # HTTPError, internal error, routing miss). Skipped only
            # when ``final_response`` stayed None, which happens if
            # this coroutine was cancelled before producing anything —
            # in that case an observer would see a phantom event.
            if final_response is not None:
                try:
                    await self._signals.emit(
                        RequestComplete(
                            request=request,
                            response=final_response,
                            status=final_response.status,
                            duration_s=time.perf_counter() - t0,
                            error=captured_error,
                        )
                    )
                except Exception as _sig_exc:
                    # A signal listener error must never suppress the response
                    # that is already computed.  Log and continue so __call__
                    # can send the response normally.
                    self._logger.error(
                        f"RequestComplete signal error: {_sig_exc}",
                        context="Request",
                        error=type(_sig_exc).__name__,
                    )

    # -- ASGI entry point --------------------------------------------------

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(scope, receive, send)
            return
        # Check mounted sub-applications before lauren's own routing.  Only
        # HTTP and WebSocket scopes can match a path prefix; other ASGI types
        # (e.g. custom extensions) are left to fall through to the normal path.
        if scope["type"] in ("http", "websocket") and self._mounts:
            raw = scope.get("path", "/")
            for prefix, sub in self._mounts:
                if raw == prefix or raw.startswith(prefix + "/"):
                    stripped = raw[len(prefix) :] or "/"
                    child_scope = dict(scope)
                    child_scope["path"] = stripped
                    child_scope["root_path"] = scope.get("root_path", "") + prefix
                    await sub(child_scope, receive, send)
                    return
        if scope["type"] == "websocket":
            await self._websocket(scope, receive, send)
            return
        if scope["type"] != "http":
            return
        headers = Headers([(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope.get("headers", [])])
        client = scope.get("client") or (None, None)
        server = scope.get("server") or (None, None)
        # Resolve the effective request path, stripping a configured
        # ``root_path`` prefix when the upstream ASGI server hasn't
        # already done so.  Uvicorn honours ``--root-path`` by setting
        # ``scope["root_path"]`` *and* pre-stripping ``scope["path"]``;
        # a plain nginx proxy typically leaves ``scope["root_path"]``
        # empty and the full prefixed path in ``scope["path"]``.
        _own_root = getattr(self, "_root_path", "")
        raw_path = scope["path"]
        if _own_root and not scope.get("root_path") and raw_path.startswith(_own_root):
            effective_path = raw_path[len(_own_root) :] or "/"
        else:
            effective_path = raw_path
        # Acquire a Request through the arena so pooled instances are
        # reused where possible. ``acquire_request`` dispatches to
        # :meth:`Request.reset` for pooled instances and to the
        # ``factory`` for fresh ones — the caller need not know which
        # path was taken.
        request = self._arena.acquire_request(
            Request,
            method=scope["method"],
            path=effective_path,
            raw_query_string=scope.get("query_string", b"") or b"",
            headers=headers,
            client=ClientInfo(client[0] if client else None, client[1] if client else None),
            server=ServerInfo(server[0] if server else None, server[1] if server else None),
            receive=receive,
            app_state=self._app_state,
            max_body_size=self._max_body_size,
        )
        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)
        _response_sent = False
        try:
            response = await self.handle(request)
            _bg: _BackgroundTasks | None = request.state.get(_BG_TASKS_ATTR)
            await _send_response(response, send)
            _response_sent = True
            if _bg is not None and _bg._has_tasks():
                await _bg._run(signals=self._signals, logger=self._logger)
        except asyncio.CancelledError:
            # Task was cancelled (client disconnect or server shutdown).
            # No response can be sent — re-raise so the ASGI server can
            # close the connection cleanly.
            raise
        except BaseException:
            # An unexpected exception escaped handle() (e.g. a BaseException
            # from a misbehaving signal listener that was not caught by the
            # try/except inside handle(), or any other exotic error).
            # Attempt a best-effort 500 response with Connection: close so
            # the client is not left hanging and the keep-alive pool is not
            # poisoned with a connection that received no bytes.
            if not _response_sent:
                try:
                    _fallback = _error_response(
                        LaurenError("internal server error"),
                        status=500,
                        code="internal_error",
                        error_format=self._error_format,
                        encoder=self._json_encoder,
                    ).with_header("connection", "close")
                    await _send_response(_fallback, send)
                except Exception:
                    pass  # Cannot send — ASGI server will close the connection
        finally:
            if task is not None:
                self._in_flight.discard(task)
            # Return the Request instance to the pool for reuse. Done
            # *after* ``_send_response`` (and background tasks) so any
            # awaitable body finishes first — if the body is a streaming
            # iterable it may still reference the request's ``_receive``
            # callable.
            self._arena.release_request(request)

    async def _websocket(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """Dispatch a WebSocket connection.

        When the app has no gateways compiled (pure HTTP apps), the
        connection is rejected with close code 1008 so the ASGI server
        gets a clean signal rather than a hang. Apps with gateways
        delegate to :func:`lauren._ws_runtime.handle_websocket` which
        walks the gateway's connect / dispatch / disconnect lifecycle.
        """
        if not self._ws_router or not self._ws_gateways:
            # Wait for the opening ``websocket.connect`` and reject it.
            try:
                await receive()
            except Exception:
                return
            await send({"type": "websocket.close", "code": 1008})
            return
        from .._ws_runtime import handle_websocket

        # Apply the same root_path stripping as HTTP requests.
        _own_root = getattr(self, "_root_path", "")
        if _own_root and not scope.get("root_path"):
            raw_ws_path = scope.get("path", "/")
            if raw_ws_path.startswith(_own_root):
                scope = {**scope, "path": raw_ws_path[len(_own_root) :] or "/"}
        task = asyncio.current_task()
        if task is not None:
            self._in_flight.add(task)
        try:
            await handle_websocket(
                self,
                self._ws_gateways,
                self._ws_router,
                scope,
                receive,
                send,
            )
        except asyncio.CancelledError:
            raise  # server shutdown — let Uvicorn handle it
        except Exception:
            # An unexpected exception escaped handle_websocket (e.g. a broken
            # transport raising when we tried to send a close frame on an
            # already-rejected connection).  Log it but do NOT re-raise: Uvicorn
            # would otherwise log "ASGI callable returned without completing
            # response" for every other in-flight request on the same worker.
            import logging as _logging

            _logging.getLogger("lauren").exception(
                "Unhandled error in WebSocket handler (path=%s)", scope.get("path", "?")
            )
        finally:
            if task is not None:
                self._in_flight.discard(task)

    async def _lifespan(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        while True:
            msg = await receive()
            if msg["type"] == "lifespan.startup":
                try:
                    if not self._started:
                        await self.startup()
                    await send({"type": "lifespan.startup.complete"})
                except Exception as e:
                    self._logger.error(
                        f"Startup failed: {type(e).__name__}: {e}",
                        context="LaurenApp",
                        error=type(e).__name__,
                    )
                    await send({"type": "lifespan.startup.failed", "message": str(e)})
                    return
            elif msg["type"] == "lifespan.shutdown":
                try:
                    await self.shutdown()
                    await send({"type": "lifespan.shutdown.complete"})
                except Exception as e:  # pragma: no cover
                    self._logger.error(
                        f"Shutdown failed: {type(e).__name__}: {e}",
                        context="Shutdown",
                        error=type(e).__name__,
                    )
                    await send({"type": "lifespan.shutdown.failed", "message": str(e)})
                return


def _wrap_middleware(instance: Any, next_call: CallNext) -> CallNext:
    async def wrapped(request: Request) -> Response:
        return await instance.dispatch(request, next_call)

    return wrapped


def _wrap_interceptor(instance: Any, ctx: ExecutionContext, next_handler: CallHandler) -> CallHandler:
    """Wrap *next_handler* with *instance*'s ``intercept`` method."""

    async def fn() -> Any:
        return await instance.intercept(ctx, next_handler)

    return CallHandler(fn)


def _coerce_to_response(value: Any, *, encoder: JSONEncoder | None = None) -> Response:
    """Convert a handler return value into a :class:`Response`.

    ``encoder`` lets the dispatcher pipe its per-app encoder through
    every recursive call so JSON emission uses the configured
    backend; when omitted, :meth:`Response.json` falls back to the
    process-wide default.

    Accepted shapes (in priority order):

    1. An existing :class:`Response` is passed through.
    2. A ``(body, status)`` tuple, e.g. ``return user, 201``.
    3. A ``(body, status, headers)`` tuple.
    4. ``None`` → 204 No Content.
    5. ``bytes`` / ``bytearray`` → octet-stream response.
    6. ``str`` → plain-text response.
    7. Pydantic v2 model (has ``model_dump``) → JSON serialization via
       ``model_dump(mode="json")``.
    8. ``list`` / ``tuple`` of Pydantic models → JSON array of dumped models.
    9. Dataclass instance → JSON via :func:`dataclasses.asdict`.
    10. Plain ``dict`` / ``list`` / ``int`` / ``float`` / ``bool`` → JSON.
    11. Anything else → JSON with a permissive default handler.
    """
    # Status-override tuple form: (body, status) or (body, status, headers)
    if isinstance(value, tuple) and len(value) in (2, 3):
        body_val = value[0]
        status = value[1]
        headers = value[2] if len(value) == 3 else None
        if isinstance(status, int):
            resp = _coerce_to_response(body_val, encoder=encoder).with_status(status)
            if headers:
                resp = resp.with_headers(headers)
            return resp

    if isinstance(value, Response):
        if encoder is not None and isinstance(value, EventStream):
            value._reframe(encoder)
        return value
    if value is None:
        return Response.no_content()
    if isinstance(value, (bytes, bytearray)):
        return Response.bytes(bytes(value))
    if isinstance(value, str):
        return Response.text(value)
    # Pydantic v2 model — use JSON-mode dump so datetimes/UUIDs/Enums are
    # serialized as strings automatically.
    if hasattr(value, "model_dump") and callable(getattr(value, "model_dump")):
        try:
            return Response.json(value.model_dump(mode="json"), encoder=encoder)
        except TypeError:
            pass  # fall through to generic JSON path
    # Sequence of Pydantic models (common: return list of DTOs).
    if isinstance(value, list) and value and all(hasattr(v, "model_dump") for v in value):
        return Response.json([v.model_dump(mode="json") for v in value], encoder=encoder)
    # Dataclass instance?
    try:
        import dataclasses

        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return Response.json(dataclasses.asdict(value), encoder=encoder)
    except Exception:  # pragma: no cover - defensive
        pass
    if isinstance(value, (dict, list, int, float, bool)):
        return Response.json(value, encoder=encoder)
    # Fallback — try generic JSON with the permissive default handler.
    return Response.json(value, encoder=encoder)


async def _coerce_streaming_response(
    iterable: Any,
    *,
    item_type: Any,
    request: Request,
    encoder: JSONEncoder | None = None,
) -> Response:
    """Serialize an async iterable of ``item_type`` as a typed stream.

    Wire format is negotiated from the request's ``Accept`` header (feature
    7 content negotiation). Every yielded item is validated and serialized
    through a cached :class:`pydantic.TypeAdapter` so discriminated-union
    items (feature 6) flow through the same code path as plain Pydantic
    models and primitives.

    Frame rules:

    * ``sse`` — emit ``data: <json>\n\n`` (optionally ``event: <kind>\n``
      when the item carries a string ``kind`` attribute, which matches the
      discriminator-union convention).
    * ``ndjson`` / ``jsonlines`` — emit ``<json>\n`` per item.

    To give clients a fighting chance of receiving a proper 4xx for
    early-stream validation failures, the first item is **primed** inside
    this function (i.e. before response headers are sent). If producing
    the first item raises an :class:`HTTPError` (such as
    :class:`ExtractorError` from a malformed inbound Stream[T] payload),
    the error escapes up to ``handle()`` which maps it to the correct
    status code. Errors raised after the first item are logged and the
    stream is truncated — by that point the response start line has
    already shipped and the HTTP status is immutable.
    """
    if not hasattr(iterable, "__aiter__") and not inspect.isasyncgen(iterable):
        raise TypeError(
            f"StreamingResponse handler must return an async iterable, got {type(iterable).__name__}"
        )
    accept = request.headers.get("accept") or ""
    fmt = negotiate_stream_format(accept, default="jsonlines")
    media_type = FORMAT_TO_MEDIA_TYPE[fmt]
    adapter = _build_adapter(item_type)
    # Capture the encoder once so every framed item hits the same
    # reference — matters for the ``msgspec`` backend which bakes
    # its options into a reusable Encoder instance.
    json_encoder = encoder if encoder is not None else get_active_encoder()

    def _dump(item: Any) -> bytes:
        if adapter is not None:
            # Pydantic type: convert to a JSON-safe dict first, then encode.
            # This preserves discriminated-union serialisation and custom
            # Pydantic field serialisers while still routing the final bytes
            # through the application's configured encoder.
            payload = adapter.dump_python(item, mode="json")
            return json_encoder.encode_compact(payload)
        # Non-Pydantic type (msgspec.Struct, dataclass, plain dict, …):
        # pass directly to the encoder so native backends (msgspec, orjson)
        # can serialise their own types without a Pydantic intermediary.
        return json_encoder.encode_compact(item)

    def _frame(item: Any) -> list[bytes]:
        body = _dump(item)
        if fmt == "sse":
            out: list[bytes] = []
            kind = getattr(item, "kind", None)
            if isinstance(kind, str) and kind:
                out.append(f"event: {kind}\n".encode("utf-8"))
            out.append(b"data: " + body + b"\n\n")
            return out
        return [body + b"\n"]

    iterator = iterable.__aiter__()

    # --- Priming step: pull the first item eagerly so any validation
    # error surfaces *before* response headers are committed. If the
    # stream is empty we still return a 200 with an empty body.
    try:
        first = await iterator.__anext__()
        first_frames = _frame(first)
        has_first = True
    except StopAsyncIteration:
        has_first = False
        first_frames = []

    async def _produce() -> "Any":
        if has_first:
            for frame in first_frames:
                yield frame
        try:
            async for item in iterator:
                for frame in _frame(item):
                    yield frame
        except HTTPError as exc:  # pragma: no cover - post-header failure
            # Headers have already been sent — we can't change the status.
            # Emit a trailing error frame that clients can surface, then
            # terminate the stream. Using the canonical error envelope
            # means clients already parsing structured JSON can spot it.
            payload = json_encoder.encode_compact(exc.to_payload())
            if fmt == "sse":
                yield b"event: error\n"
                yield b"data: " + payload + b"\n\n"
            else:
                yield payload + b"\n"

    headers = Headers([("cache-control", "no-cache")]) if fmt == "sse" else None
    return Response.stream(_produce(), media_type=media_type, headers=headers)


#: Registry mapping HTTP status codes to RFC 7807 type URIs.
#: Following the convention used by HTTP Problem Details, the type
#: is a URI that identifies the problem. When the caller hasn't
#: declared a custom type, we emit the IANA HTTP-status URN which is
#: parseable and stable. Applications with richer problem catalogues
#: can override via ``HTTPError.problem_type`` on subclasses.
_STATUS_TYPE_URIS: dict[int, str] = {}


def _problem_type_for(status: int) -> str:
    """Return a stable URI for ``status`` suitable for the RFC 7807 ``type`` field.

    The URN format used here is ``urn:ietf:rfc:7231:<status>`` which
    is deterministic and does not require a live HTTP dereferenceable
    URL. Real-world apps typically override this via a custom error
    subclass carrying a more descriptive type URI.
    """
    cached = _STATUS_TYPE_URIS.get(status)
    if cached is not None:
        return cached
    uri = f"urn:ietf:rfc:7231:{status}"
    _STATUS_TYPE_URIS[status] = uri
    return uri


def _http_status_title(status: int) -> str:
    """Human-readable title for ``status`` — the IANA reason phrase.

    Falls back to ``"HTTP Error"`` for non-standard codes so the
    RFC 7807 ``title`` field is always present.
    """
    try:
        from http import HTTPStatus

        return HTTPStatus(status).phrase
    except (ValueError, ImportError):
        return "HTTP Error"


def _error_response(
    err: LaurenError,
    *,
    status: int | None = None,
    code: str | None = None,
    error_format: str = "default",
    encoder: JSONEncoder | None = None,
) -> Response:
    """Build an error :class:`Response` from a :class:`LaurenError`.

    ``error_format`` selects the wire shape:

    * ``"default"`` — the classic lauren envelope
      ``{"error": {"code", "message", "detail"}}`` with
      ``application/json`` content type. Preserves backwards
      compatibility for every existing lauren client.

    * ``"rfc7807"`` — the RFC 7807 Problem Details envelope
      ``{"type", "title", "status", "detail", "instance", "code"}``
      with the correct ``application/problem+json`` content type.
      Adopts industry-standard error reporting for clients that
      understand Problem Details (the OpenAPI ecosystem, many
      service meshes, etc.).

    The ``"code"`` field is lauren-specific; it rides alongside the
    RFC 7807 structure as an opaque machine-readable identifier.
    This is explicitly permitted by RFC 7807 §3.2 which allows
    extensions.
    """
    final_status = status or getattr(err, "status_code", None) or 500
    final_code = code or getattr(err, "code", "lauren_error")

    if error_format == "rfc7807":
        # Allow subclasses to override the ``type`` URI by declaring a
        # class-level ``problem_type`` attribute. Defaults to the
        # status-derived URN so the field is always stable.
        problem_type = getattr(err, "problem_type", None) or _problem_type_for(final_status)
        title = getattr(err, "problem_title", None) or _http_status_title(final_status)
        payload: dict[str, Any] = {
            "type": problem_type,
            "title": title,
            "status": final_status,
            "detail": err.message or title,
            "code": final_code,
        }
        # Include the extra ``detail`` dict as a typed extension so
        # machine clients can still read the structured context
        # (field names, allowed values, etc.) that lauren's default
        # envelope exposes. RFC 7807 §3.2 expressly permits this.
        if err.detail:
            payload["errors"] = err.detail
        return Response.json(
            payload,
            status=final_status,
            headers=Headers([("content-type", "application/problem+json")]),
            encoder=encoder,
        )

    # Default envelope — unchanged.
    payload = err.to_payload()
    if code:
        payload["error"]["code"] = code
    return Response.json(payload, status=final_status, encoder=encoder)


async def _send_response(
    response: Response,
    send: Callable[[dict[str, Any]], Awaitable[None]],
) -> None:
    raw_headers: list[tuple[bytes, bytes]] = [
        (k.encode("latin-1"), v.encode("latin-1")) for k, v in response.headers.raw()
    ]
    stream = response.stream_body
    if stream is None:
        body = response.body
        raw_headers = [(k, v) for k, v in raw_headers if k != b"content-length"]
        raw_headers.append((b"content-length", str(len(body)).encode("latin-1")))
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": raw_headers,
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})
    else:
        await send(
            {
                "type": "http.response.start",
                "status": response.status,
                "headers": raw_headers,
            }
        )
        # Once the response headers are sent we cannot send a fallback error
        # response if streaming fails.  Wrap the body loop so that:
        # - asyncio.CancelledError  → re-raised (server shutdown / client gone)
        # - Any other send() failure → absorbed; the ASGI server closes the
        #   connection when this coroutine returns without a more_body=False
        #   chunk, which is the correct signal for an interrupted stream.
        try:
            async for chunk in stream:
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except asyncio.CancelledError:
            raise
        except Exception:
            pass  # client disconnected mid-stream; connection will be closed


# ---------------------------------------------------------------------------
# LaurenFactory — 7-phase startup pipeline
# ---------------------------------------------------------------------------


class LaurenFactory:
    """Produces :class:`LaurenApp` instances via the 7-phase pipeline."""

    @staticmethod
    def create(
        root_module: type,
        *,
        strict_lifecycle: bool = True,
        global_middlewares: Iterable[type] | None = None,
        global_guards: Iterable[type] | None = None,
        global_interceptors: Iterable[type] | None = None,
        global_exception_handlers: Iterable[Any] | None = None,
        global_providers: Iterable[Any] | None = None,
        max_body_size: int = 1_048_576,
        app_state: AppState | None = None,
        logger: Logger | None = None,
        openapi_info: dict[str, Any] | None = None,
        openapi_servers: list[dict[str, Any]] | None = None,
        openapi_security_schemes: dict[str, Any] | None = None,
        openapi_url: str | None = None,
        docs_url: str | None = None,
        redoc_url: str | None = None,
        arena: RequestArena | None = None,
        arena_capacity: int | None = None,
        json_encoder: JSONEncoder | None = None,
        signals: SignalBus | None = None,
        error_format: str = "default",
        root_path: str = "",
        mounts: dict[str, Any] | None = None,
    ) -> LaurenApp:
        """Build a :class:`LaurenApp` from a root ``@module`` class.

        The seven-phase pipeline is logged through ``logger`` (defaults to a
        no-op :class:`~lauren.logging.NullLogger` when not provided, so
        existing test suites remain silent). Pass
        ``logger=lauren.logging.default_logger()`` for a production-ready
        configuration that auto-detects TTY vs JSON output.

        Passing any of ``openapi_url``, ``docs_url``, or ``redoc_url``
        exposes the corresponding documentation endpoint. The common
        pattern is ``openapi_url='/openapi.json'``, ``docs_url='/docs'``,
        ``redoc_url='/redoc'`` — matching FastAPI conventions.

        ``global_guards`` and ``global_exception_handlers`` mirror NestJS's
        app-level guards and exception handlers: every request runs through
        them after the route is resolved (guards) or whenever the handler
        raises (handlers). Per-route ``@use_guards`` and
        ``@use_exception_handlers`` declarations compose with these globals —
        route handlers are tried first, then globals.
        """
        _log: Logger = logger or NullLogger()

        effective_global_middlewares = list(global_middlewares or [])
        effective_global_guards = list(global_guards or [])
        effective_global_interceptors = list(global_interceptors or [])
        effective_global_exception_handlers = list(global_exception_handlers or [])
        effective_global_providers: list[Any] = list(global_providers or [])
        overall_t0 = time.perf_counter()
        _log.log(
            f"Starting application (root={root_module.__name__})",
            context="LaurenFactory",
            root=root_module.__name__,
        )

        # ---- Phase 1: Module graph construction ----
        t0 = time.perf_counter()
        graph = ModuleGraph()
        graph.compile(root_module)
        _log.verbose(
            f"Phase 1/7 module graph built: {len(graph.modules)} module(s), "
            f"{len(graph.all_controllers)} controller(s), "
            f"{len(graph.all_providers)} provider(s) "
            f"({format_duration_ms(time.perf_counter() - t0)})",
            context="ModuleGraph",
            modules=len(graph.modules),
            controllers=len(graph.all_controllers),
            providers=len(graph.all_providers),
        )

        # ---- Phase 2: Provider collection ----
        # Each provider is registered together with the module that declared
        # it so the container can enforce NestJS-style encapsulation at
        # resolution time.
        t0 = time.perf_counter()
        container = DIContainer()
        for p in graph.iter_providers():
            owning_module = graph.module_for_provider(p)
            # Custom-provider records (use_value / use_class /
            # use_factory / use_existing) take a different code path:
            # the graph stores the record alongside the token, and the
            # container's ``register_custom`` lowers each kind to the
            # right shape. Standard class / function providers fall
            # through to the regular ``register`` call.
            # A list is returned so that multi-binding scenarios where
            # multiple providers share the same ``provide=`` token are
            # all registered (previously only the last would survive).
            customs = graph.custom_providers_for(p)
            if customs:
                for custom in customs:
                    container.register_custom(custom, owning_module=owning_module)
            else:
                container.register(p, owning_module=owning_module)
        for ctrl_class in graph.iter_controllers():
            # Rebinding the loop variable to ``ctrl_class`` rather than
            # ``c`` avoids frame-walk collisions inside
            # :func:`_safe_class_hints` — when a user's class body
            # references a function named ``c`` via PEP 563
            # stringified annotations, our frame fallback would
            # otherwise pick up *this* loop variable and resolve it
            # to whichever controller class was last iterated.
            if ctrl_class not in {pp.cls for pp in container.all_providers()}:
                container.register(
                    ctrl_class,
                    owning_module=graph.module_for_controller(ctrl_class),
                )
        # Global middleware lives outside any module — it is registered
        # without an owning module so it is universally visible.
        for mw in effective_global_middlewares:
            if mw not in {pp.cls for pp in container.all_providers()}:
                _ensure_injectable(mw)
                container.register(mw)
        # Global guards — same treatment as global middleware.
        for g in effective_global_guards:
            if not hasattr(g, "can_activate"):
                from ..exceptions import GuardConfigError

                raise GuardConfigError(
                    f"global guard {getattr(g, '__name__', repr(g))} must define 'can_activate(context)'",
                )
            if g not in {pp.cls for pp in container.all_providers()}:
                _ensure_injectable(g)
                container.register(g)
        # Global interceptors — same treatment as global middleware/guards.
        for inter in effective_global_interceptors:
            if not hasattr(inter, "intercept"):
                from ..exceptions import InterceptorConfigError

                raise InterceptorConfigError(
                    f"global interceptor {getattr(inter, '__name__', repr(inter))} "
                    "must define 'intercept(context, call_handler)'",
                )
            if inter not in {pp.cls for pp in container.all_providers()}:
                _ensure_injectable(inter)
                container.register(inter)
        # Global exception handlers — must already be decorated with
        # ``@exception_handler``. We surface configuration mistakes
        # loudly at startup rather than at the first error response.
        for ef in effective_global_exception_handlers:
            if not hasattr(ef, EXCEPTION_HANDLER_META):
                raise ExceptionHandlerConfigError(
                    f"global exception filter {getattr(ef, '__name__', repr(ef))} "
                    "is not decorated with @exception_handler.",
                )
            if not isinstance(ef, type):
                # Function-form globals are invoked directly; no DI.
                continue
            if ef not in {pp.cls for pp in container.all_providers()}:
                _ensure_injectable(ef)
                container.register(ef)

        # Global providers — same treatment as global middleware/guards but
        # for the DI container. owning_module=None makes every provider
        # universally visible without requiring module imports or exports.
        for gp in effective_global_providers:
            if isinstance(gp, CustomProvider):
                container.register_custom(gp, owning_module=None)
            else:
                if gp not in {pp.cls for pp in container.all_providers()}:
                    _ensure_injectable(gp)
                    container.register(gp, owning_module=None)

        # Install per-module visible-token sets. A module sees its own
        # providers, its own controllers, and anything re-exported by a
        # module it imports (transitively).
        for mod_cls in graph.modules:
            container.set_visible(mod_cls, graph.visible_in(mod_cls))
        _log.verbose(
            f"Phase 2/7 providers collected: {len(container.all_providers())} "
            f"({format_duration_ms(time.perf_counter() - t0)})",
            context="DIContainer",
            providers=len(container.all_providers()),
        )

        # ---- Phase 3: Protocol binding ----
        _log.verbose(
            "Phase 3/7 protocols bound",
            context="DIContainer",
        )

        # ---- Phase 4: DI graph compilation ----
        t0 = time.perf_counter()
        container.compile()
        _log.verbose(
            f"Phase 4/7 DI graph compiled ({format_duration_ms(time.perf_counter() - t0)})",
            context="DIContainer",
        )

        # ---- Phase 5: Router compilation ----
        phase5_t0 = time.perf_counter()
        router = Router()
        compiled_handlers: dict[tuple[str, str], CompiledHandler] = {}
        from ..websockets import WS_CONTROLLER_META

        for ctrl_cls in graph.iter_controllers():
            # Skip classes that are ONLY WebSocket gateways — they're
            # handled by the WS compiler in Phase 5b below. A class can
            # carry both ``@controller`` and ``@ws_controller`` markers
            # (unusual but legal), in which case the HTTP side still
            # compiles its HTTP routes here.
            if WS_CONTROLLER_META in ctrl_cls.__dict__ and CONTROLLER_META not in ctrl_cls.__dict__:
                continue
            ctrl_meta = _own_controller_meta(ctrl_cls)
            # Read own-class middleware/guards/interceptors/metadata so a
            # subclass doesn't silently inherit attributes it hasn't opted into.
            ctrl_mw = list(ctrl_cls.__dict__.get(USE_MIDDLEWARES, []))
            ctrl_guards = list(ctrl_cls.__dict__.get(USE_GUARDS, []))
            ctrl_interceptors = list(ctrl_cls.__dict__.get(USE_INTERCEPTORS, []))
            ctrl_exc_handlers = list(ctrl_cls.__dict__.get(USE_EXCEPTION_HANDLERS, []))
            ctrl_extra_meta: dict[str, Any] = dict(ctrl_cls.__dict__.get(SET_METADATA, {}))

            # Register controller's guards/middleware/interceptors as providers
            for cls in ctrl_mw + ctrl_guards + ctrl_interceptors:
                if cls not in {pp.cls for pp in container.all_providers()}:
                    _ensure_injectable(cls)
                    container.register(cls)
            # Exception handlers — only class-form handlers go through
            # the DI container (their ``__init__`` parameters resolve
            # like middleware/guards). Function-form handlers are
            # invoked directly with ``(exc, request)`` at dispatch time
            # so they do not need a provider entry.
            for h in ctrl_exc_handlers:
                if not isinstance(h, type):
                    continue
                if h not in {pp.cls for pp in container.all_providers()}:
                    _ensure_injectable(h)
                    container.register(h)

            # Walk the full MRO so inherited methods still register —
            # mirroring the legacy ``dir(ctrl_cls)`` behaviour — but
            # read each candidate out of *its own* ``__dict__`` so we
            # see the descriptor objects (``staticmethod`` /
            # ``classmethod``) with their markers intact rather than
            # the bound methods that attribute access would produce.
            #
            # First pass: collect ``name -> (function, binding)`` using
            # the *first* definition encountered as we walk the MRO from
            # the subclass down, so overrides correctly shadow base-class
            # methods. Then iterate the resolved map to register routes.
            # Values are ``(unwrapped_fn, binding_tag, raw_descriptor)``.
            # The raw descriptor is preserved so the dispatcher can call
            # ``raw.__get__(instance, cls)`` rather than branching on the
            # binding tag, which makes custom ``__get__`` descriptors work.
            resolved_methods: dict[str, tuple[Callable[..., Any], str, Any]] = {}
            for klass in ctrl_cls.__mro__:
                for attr_name, raw in klass.__dict__.items():
                    if attr_name in resolved_methods:
                        continue
                    fn, binding = _unwrap_handler_descriptor(raw)
                    if fn is None:
                        continue
                    resolved_methods[attr_name] = (fn, binding, raw)

            for attr_name, (fn, binding, raw) in resolved_methods.items():
                route_metas: list[RouteMeta] = getattr(fn, ROUTE_META, [])
                if not route_metas:
                    continue
                fn_mw = list(getattr(fn, USE_MIDDLEWARES, []))
                fn_guards = list(getattr(fn, USE_GUARDS, []))
                fn_interceptors = list(getattr(fn, USE_INTERCEPTORS, []))
                fn_exc_handlers = list(getattr(fn, USE_EXCEPTION_HANDLERS, []))
                fn_meta: dict[str, Any] = dict(getattr(fn, SET_METADATA, {}))
                # Per-route encoder: method-level wins over controller-level.
                fn_encoder = getattr(fn, USE_ENCODER, None)
                ctrl_encoder = ctrl_cls.__dict__.get(USE_ENCODER)
                for cls in fn_mw + fn_guards + fn_interceptors:
                    if cls not in {pp.cls for pp in container.all_providers()}:
                        _ensure_injectable(cls)
                        container.register(cls)
                for h in fn_exc_handlers:
                    if not isinstance(h, type):
                        # Function-form: skip DI registration (see
                        # rationale at the controller-level loop above).
                        continue
                    if h not in {pp.cls for pp in container.all_providers()}:
                        _ensure_injectable(h)
                        container.register(h)
                ctrl_owning_module = graph.module_for_controller(ctrl_cls)
                for rmeta in route_metas:
                    full_path = _normalize_path(ctrl_meta.prefix, rmeta.path)
                    entry = router.add_route(
                        rmeta.method,
                        full_path,
                        fn,
                        handler_class=ctrl_cls,
                        metadata={
                            "controller_meta": ctrl_meta,
                            "route_meta": rmeta,
                            "extra": {**ctrl_extra_meta, **fn_meta},
                            "binding": binding,
                        },
                    )
                    # Compile handler extractions now that we know the path.
                    # The controller's owning module bounds which DI
                    # providers an endpoint parameter may resolve to.
                    extractions, _ = _compile_handler_signature(
                        ctrl_cls,
                        fn,
                        container,
                        path_param_names=set(entry.param_names),
                        owning_module=ctrl_owning_module,
                    )
                    # Resolve the handler's return annotation once at
                    # startup so the request path stays reflection-free.
                    return_hint = _safe_type_hints(fn).get("return", inspect.Parameter.empty)
                    streaming_item = extract_streaming_item_type(return_hint)
                    compiled = CompiledHandler(
                        controller_cls=ctrl_cls,
                        handler_fn=fn,
                        route_meta=rmeta,
                        path_template=entry.path_template,
                        extractions=extractions,
                        middleware_chain=tuple(ctrl_mw + fn_mw),
                        guards=tuple(ctrl_guards + fn_guards),
                        # Controller-level interceptors first, then method-level;
                        # global interceptors are prepended at dispatch time.
                        interceptors=tuple(ctrl_interceptors + fn_interceptors),
                        # Route handlers are most specific so they sit
                        # at the front of the chain; controller-level
                        # handlers fall through next; globals are
                        # appended at dispatch time.
                        exception_handlers=tuple(fn_exc_handlers + ctrl_exc_handlers),
                        metadata={**ctrl_extra_meta, **fn_meta},
                        owning_module=ctrl_owning_module,
                        streaming_item_type=streaming_item,
                        binding=binding,
                        is_coroutine=inspect.iscoroutinefunction(fn),
                        raw_descriptor=raw,
                        # Method-level encoder wins over controller-level.
                        encoder=fn_encoder or ctrl_encoder or None,
                    )
                    compiled_handlers[(entry.method, entry.path_template)] = compiled
                    _log.log(
                        f"Mapped {{{rmeta.method} {entry.path_template}}} "
                        f"→ {ctrl_cls.__name__}.{fn.__name__}",
                        context="RouterExplorer",
                        method=rmeta.method,
                        path=entry.path_template,
                        handler=f"{ctrl_cls.__name__}.{fn.__name__}",
                    )

        # Optional built-in documentation endpoints. They are registered
        # *before* freezing the router so they show up in ``app.routes()``
        # like any user-defined route, but they are marked
        # ``include_in_schema=False`` so they do not pollute the OpenAPI
        # document itself.
        if openapi_url or docs_url or redoc_url:
            _register_docs_routes(
                router=router,
                compiled_handlers=compiled_handlers,
                openapi_url=openapi_url,
                docs_url=docs_url,
                redoc_url=redoc_url,
            )
            # Ensure _DocsController is resolvable as a global singleton.
            if _DocsController not in {p.cls for p in container.all_providers()}:
                container.register(_DocsController)

        # ---- Phase 5b: WebSocket gateway compilation ----
        # The WS compiler walks the same module graph, picks every
        # class carrying its OWN ``@ws_controller`` marker, and builds
        # an immutable dispatch plan per gateway. Done here so late
        # provider registrations from the gateway compiler (e.g. a
        # gateway class that wasn't already in ``container``) still
        # make it into the DI graph before it's re-compiled below.
        from .._ws_runtime import compile_gateways

        ws_router = Router()
        ws_gateways = compile_gateways(graph, container, ws_router=ws_router, logger=_log)
        ws_router.freeze()

        # Re-compile after late registrations.
        container._compiled = False
        container.compile()
        router.freeze()
        _log.verbose(
            f"Phase 5/7 router compiled: {len(compiled_handlers)} route(s), "
            f"{len(ws_gateways)} ws gateway(s) "
            f"({format_duration_ms(time.perf_counter() - phase5_t0)})",
            context="RouterExplorer",
            routes=len(compiled_handlers),
            ws_gateways=len(ws_gateways),
        )

        # ---- Phase 6: Lifecycle execution ----
        lifecycle = LifecycleScheduler(container)
        lifecycle.compute_order()
        _log.verbose(
            "Phase 6/7 lifecycle order computed",
            context="Lifecycle",
            steps=len(lifecycle._order),
        )

        # ---- Phase 7: Readiness ----
        final_state = app_state or AppState()
        # Arena policy: an explicit ``arena=`` wins; otherwise build one
        # using ``arena_capacity`` if provided, or fall back to the
        # default 256-slot arena. Passing both is rejected loudly so
        # misuse surfaces at startup, not in production.
        if arena is not None and arena_capacity is not None:
            raise StartupError(
                "pass either `arena` or `arena_capacity`, not both",
                detail={"arena": type(arena).__name__, "capacity": arena_capacity},
            )
        final_arena = arena or (RequestArena(capacity=arena_capacity) if arena_capacity is not None else None)
        app = LaurenApp(
            router=router,
            container=container,
            module_graph=graph,
            lifecycle=lifecycle,
            compiled_handlers=compiled_handlers,
            global_middlewares=effective_global_middlewares,
            global_guards=effective_global_guards,
            global_interceptors=effective_global_interceptors,
            global_exception_handlers=effective_global_exception_handlers,
            global_providers=effective_global_providers,
            app_state=final_state,
            strict_lifecycle=strict_lifecycle,
            max_body_size=max_body_size,
            logger=_log,
            ws_router=ws_router,
            ws_gateways=ws_gateways,
            arena=final_arena,
            signals=signals,
            error_format=error_format,
        )
        # Install the JSON encoder — user-supplied wins, otherwise the
        # process-wide default (stdlib out of the box, swappable via
        # ``lauren.serialization.set_active_encoder``). Pinning the
        # reference on the app means the dispatcher reads it once from
        # ``self._json_encoder`` rather than re-resolving it per call.
        if json_encoder is not None:
            app._json_encoder = json_encoder
        else:
            app._json_encoder = get_active_encoder()
        # Stash OpenAPI customisation on the app so the generator picks it
        # up when the user (or the docs endpoint) calls ``app.openapi()``.
        app._openapi_info = openapi_info  # type: ignore[attr-defined]
        app._openapi_servers = openapi_servers  # type: ignore[attr-defined]
        app._openapi_security_schemes = openapi_security_schemes  # type: ignore[attr-defined]
        app._openapi_url = openapi_url  # type: ignore[attr-defined]
        app._docs_url = docs_url  # type: ignore[attr-defined]
        app._redoc_url = redoc_url  # type: ignore[attr-defined]
        app._root_path = root_path  # type: ignore[attr-defined]
        # Bind app into the stub docs handlers so they can call app.openapi().
        for compiled in compiled_handlers.values():
            if getattr(compiled.handler_fn, "__lauren_docs_stub__", False):
                compiled.handler_fn.__lauren_app__ = app  # type: ignore[attr-defined]
        _log.verbose(
            f"Phase 7/7 app built: {len(compiled_handlers)} route(s) — "
            "awaiting startup() to run lifecycle hooks",
            context="LaurenApp",
            routes=len(compiled_handlers),
        )
        # Apply user-supplied mounts (path → sub-app).  This is a convenience
        # alternative to calling ``app.mount(...)`` after the factory returns;
        # both are fully equivalent.
        for mount_path, mount_app in (mounts or {}).items():
            app.mount(mount_path, mount_app)
        _log.log(
            f"LaurenFactory.create completed ({format_duration_ms(time.perf_counter() - overall_t0)} total)",
            context="LaurenFactory",
            total_ms=round((time.perf_counter() - overall_t0) * 1000, 3),
            routes=len(compiled_handlers),
            providers=len(container.all_providers()),
        )
        return app


def _ensure_injectable(cls: type) -> None:
    """Auto-mark a class as injectable if it isn't already.

    Used for guard/middleware classes that users may forget to decorate.
    Only applies when the class itself has no decoration in its own ``__dict__``;
    it does NOT bless subclasses based on inherited markers.
    """
    if INJECTABLE_META not in cls.__dict__:
        # Detect inheritance: if a base class has the marker but this subclass
        # doesn't, force the user to decorate explicitly.
        for base in cls.__mro__[1:]:
            if INJECTABLE_META in base.__dict__:
                from ..exceptions import MetadataInheritanceError

                raise MetadataInheritanceError(
                    f"{cls.__name__} inherits injectable metadata from "
                    f"{base.__name__} but is not itself decorated. "
                    f"Re-decorate {cls.__name__} with @injectable / @middleware / "
                    "the appropriate decorator to opt in explicitly."
                )
        from ..types import Scope as _Scope

        setattr(cls, INJECTABLE_META, InjectableMeta(scope=_Scope.SINGLETON))


def _own_controller_meta(cls: type) -> ControllerMeta:
    """Return the class's OWN :class:`ControllerMeta` or raise.

    Inherited metadata is rejected: subclasses must be re-decorated with
    ``@controller`` to opt in, making the contract explicit.
    """
    own = cls.__dict__.get(CONTROLLER_META)
    if own is not None:
        assert isinstance(own, ControllerMeta)
        return own
    # Inherited?
    for base in cls.__mro__[1:]:
        if CONTROLLER_META in base.__dict__:
            from ..exceptions import MetadataInheritanceError

            raise MetadataInheritanceError(
                f"{cls.__name__} inherits @controller metadata from "
                f"{base.__name__} but is not itself decorated with @controller. "
                "Decorate the subclass explicitly to opt in.",
                detail={"class": cls.__name__, "inherits_from": base.__name__},
            )
    raise StartupError(
        f"{cls.__name__} is not a controller (missing @controller)",
        detail={"class": cls.__name__},
    )


# ---------------------------------------------------------------------------
# Built-in documentation endpoints
# ---------------------------------------------------------------------------


class _DocsController:
    """Synthetic controller that serves OpenAPI JSON, Swagger UI, and ReDoc.

    Instances are constructed by :func:`_register_docs_routes` with a lazy
    reference to the owning :class:`LaurenApp` (injected after the app is
    built) so ``openapi_json`` always reflects the live schema.
    """

    # The factory fills these in after the app is fully assembled.
    __lauren_app__: "LaurenApp | None" = None

    def __init__(self) -> None:
        pass


def _register_docs_routes(
    *,
    router: Router,
    compiled_handlers: dict[tuple[str, str], CompiledHandler],
    openapi_url: str | None,
    docs_url: str | None,
    redoc_url: str | None,
) -> None:
    """Register the docs endpoints directly on ``router`` / ``compiled_handlers``.

    The handlers close over the *app* via a thunk that's resolved when
    ``LaurenFactory.create`` is about to return — see the call site that
    sets ``__lauren_app__`` on each handler function.
    """
    from ._docs import redoc_html, swagger_ui_html
    from ..decorators import ControllerMeta, RouteMeta

    ctrl_meta = ControllerMeta(prefix="", tags=[])
    setattr(_DocsController, CONTROLLER_META, ctrl_meta)

    # The JSON endpoint must exist whenever docs or redoc are requested;
    # both UIs fetch it. If the user only asked for one UI, we still expose
    # the JSON at a sensible default so the UI has somewhere to fetch from.
    effective_openapi_url = openapi_url or ("/openapi.json" if (docs_url or redoc_url) else None)

    routes: list[tuple[str, str, Any]] = []

    if effective_openapi_url:

        async def openapi_json(self_unused) -> Any:
            app = openapi_json.__lauren_app__  # type: ignore[attr-defined]
            return app.openapi()

        openapi_json.__lauren_docs_stub__ = True  # type: ignore[attr-defined]
        openapi_json.__lauren_app__ = None  # type: ignore[attr-defined]
        openapi_json.__qualname__ = "_DocsController.openapi_json"
        routes.append(("GET", effective_openapi_url, openapi_json))

    if docs_url:

        async def swagger_ui(self_unused) -> Response:
            body = swagger_ui_html(openapi_url=effective_openapi_url or "/openapi.json")
            return Response.text(body).with_header("content-type", "text/html; charset=utf-8")

        swagger_ui.__lauren_docs_stub__ = True  # type: ignore[attr-defined]
        swagger_ui.__lauren_app__ = None  # type: ignore[attr-defined]
        swagger_ui.__qualname__ = "_DocsController.swagger_ui"
        routes.append(("GET", docs_url, swagger_ui))

    if redoc_url:

        async def redoc(self_unused) -> Response:
            body = redoc_html(openapi_url=effective_openapi_url or "/openapi.json")
            return Response.text(body).with_header("content-type", "text/html; charset=utf-8")

        redoc.__lauren_docs_stub__ = True  # type: ignore[attr-defined]
        redoc.__lauren_app__ = None  # type: ignore[attr-defined]
        redoc.__qualname__ = "_DocsController.redoc"
        routes.append(("GET", redoc_url, redoc))

    # Register _DocsController in the container as a trivial singleton so
    # it can be resolved during request dispatch without needing DI deps.
    if routes:
        if INJECTABLE_META not in _DocsController.__dict__:
            setattr(_DocsController, INJECTABLE_META, InjectableMeta(scope=DScope.SINGLETON))

    for method, path, fn in routes:
        rmeta = RouteMeta(
            method=method,
            path=path,
            include_in_schema=False,
            summary="Built-in documentation endpoint",
        )
        entry = router.add_route(
            method,
            path,
            fn,
            handler_class=_DocsController,
            metadata={
                "controller_meta": ctrl_meta,
                "route_meta": rmeta,
                "extra": {},
            },
        )
        compiled = CompiledHandler(
            controller_cls=_DocsController,
            handler_fn=fn,
            route_meta=rmeta,
            path_template=entry.path_template,
            extractions=(),
            middleware_chain=(),
            guards=(),
            metadata={},
            owning_module=None,
            is_coroutine=inspect.iscoroutinefunction(fn),
        )
        compiled_handlers[(method, entry.path_template)] = compiled


__all__ = ["LaurenApp", "LaurenFactory", "CompiledHandler"]
