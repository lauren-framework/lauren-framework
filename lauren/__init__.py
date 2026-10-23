"""lauren — a metadata-first Python web framework.

Inspired by Rust's Axum and NestJS. Every route, DI binding, module boundary,
and lifecycle hook is declared with decorators and resolved into an
immutable execution graph at startup. The request path is pure traversal
— no reflection, no registration during a request.

Core capabilities
-----------------

* Radix-tree router with O(depth) lookup (static > param > wildcard).
* Dependency injection with SINGLETON / REQUEST / TRANSIENT scopes,
  Protocol binding, multi-bindings, cycle detection.
* Typed extractors: ``Path[T]``, ``Query[T]``, ``Header[T]``, ``Cookie[T]``,
  ``Json[T]``, ``Form[T]``, ``Bytes``, ``ByteStream``, ``UploadFile``,
  ``State``, ``Depends[T]``, plus user-defined extractors — an
  ``ExtractionMarker`` subclass whose instance method
  ``extract(self, ctx, extraction)`` is resolved through DI.
* Validation without a hard dependency: Pydantic models, msgspec structs,
  dataclasses, ``TypedDict`` and ``Discriminated[A | B, "key"]`` unions
  (Pydantic is optional).
* Modules with imports / exports and circular-import detection.
* Lifecycle hooks (``@post_construct``, ``@pre_destruct``) in topological
  order with timeouts.
* Seven-phase startup (``LaurenFactory.create``) that fails fast on any
  invalid graph.
* Middleware (onion model), guards, interceptors and exception handlers,
  attachable globally, to controllers, or to routes.
* WebSocket gateways, Server-Sent Events, typed streaming and sessions.
* Typed application events: ``@on_event`` handlers on providers, validated at
  startup, published through ``EventBus`` / the request-scoped ``EventPublisher``.
* Standalone contexts (``LaurenFactory.create_context``) and DI-aware
  ``@command`` methods run with ``python -m lauren exec`` — the app's graph
  and lifecycle without HTTP.
* OpenAPI 3.1 generation from return annotations and ``response_model``
  (any of the supported model kinds).
* Auto-serialization: return dicts, models, dataclasses, or
  ``(body, status)`` tuples; lauren builds the Response.
* Strict inheritance: subclasses are controllers / injectables only when
  re-decorated explicitly.

Minimal example
---------------

.. code-block:: python

    from lauren import LaurenFactory, controller, module, get, Path
    from pydantic import BaseModel

    class Greeting(BaseModel):
        message: str

    @controller("/hello")
    class HelloController:
        @get("/{name}")
        async def greet(self, name: Path[str]) -> Greeting:
            # Return a Pydantic model directly — lauren serializes it.
            return Greeting(message=f"hello {name}")

    @module(controllers=[HelloController])
    class AppModule:
        pass

    app = LaurenFactory.create(AppModule)
    # `app` is an ASGI callable — serve with uvicorn.

For a complete reference intended for LLM ingestion, see the top-level
``llms-full.txt`` file shipped with the package.
"""

from __future__ import annotations

from . import docs, logging, serialization, signals
from .background import BackgroundTasks, TaskHandle
from ._app import Lauren
from ._arena import RequestAllocation, RequestArena
from ._asgi import LaurenApp, LaurenFactory
from .commands import CommandContext, CommandResult, Opt, command
from .context import ApplicationContext, OperationScope
from .events import EmitResult, EventBus, EventContext, EventPublisher, on_event
from ._di import DIContainer
from ._di.custom import (
    Inject,
    OptionalDep,
    Token,
    use_class,
    use_existing,
    use_factory,
    use_value,
)
from ._routing import Router, RouteEntry
from .serialization import (
    JSONEncoder,
    MsgspecEncoder,
    OrjsonEncoder,
    PydanticEncoder,
    StdlibJSONEncoder,
    auto_encoder,
)
from .decorators import (
    controller,
    delete,
    exception_handler,
    get,
    head,
    injectable,
    interceptor,
    middleware,
    module,
    openapi_security,
    options,
    patch,
    post,
    post_construct,
    pre_destruct,
    put,
    set_metadata,
    use_encoder,
    use_exception_handlers,
    use_guards,
    use_interceptors,
    use_middlewares,
    propagate_metadata,
    OpenAPISecurityMeta,
)
from .exceptions import (
    BadRequestError,
    CircularDependencyError,
    ConflictError,
    CircularModuleError,
    ConfigValidationError,
    DecoratorUsageError,
    DestructError,
    DestructTimeoutError,
    DIScopeViolationError,
    DrainTimeoutError,
    DuplicateBindingError,
    ExceptionHandlerConfigError,
    ExtractorError,
    ExtractorFieldError,
    ForbiddenError,
    GoneError,
    GuardConfigError,
    HTTPError,
    InterceptorConfigError,
    LaurenError,
    LifecycleConfigError,
    LifecycleError,
    LifecycleViolationError,
    MetadataInheritanceError,
    MethodNotAllowedError,
    MiddlewareConfigError,
    MissingProviderError,
    MissingStateError,
    ModuleExportViolation,
    NotAcceptableError,
    NotFoundError,
    OpenAPISchemaError,
    PreconditionFailedError,
    ProtocolAmbiguityError,
    RequestBodyTooLarge,
    RequestTimeoutError,
    RouteNotFoundError,
    RouterConflictError,
    ServiceUnavailableError,
    SessionConfigError,
    StartupError,
    StateTypeError,
    TooManyRequestsError,
    UnauthorizedError,
    UnresolvableParameterError,
    UnsupportedMediaTypeError,
)
from .extractors import (
    PIPE_META,
    Bytes,
    ByteStream,
    Cookie,
    CookieField,
    Depends,
    ExtractionMarker,
    FieldDescriptor,
    Form,
    Header,
    HeaderField,
    Json,
    Path,
    PathField,
    Pipe,
    PipeContext,
    PipeMeta,
    Query,
    QueryField,
    State as StateExtractor,
    UploadFile,
    is_pipe,
    pipe,
)
from .signals import (
    BackgroundTaskComplete,
    BackgroundTaskFailed,
    BackgroundTaskStarted,
    EventHandlerFailed,
    HostedServiceFailed,
    HostedServiceStarted,
    HostedServiceStopped,
    JobCompleted,
    JobFailed,
    JobSkipped,
    JobStarted,
    LifecycleEvent,
    RequestComplete,
    RequestReceived,
    ShutdownBegin,
    SignalBus,
    StartupBegin,
    StartupComplete,
)
from .sse import (
    EventStream,
    ServerSentEvent,
    format_sse_event,
    last_event_id,
)
from .streaming import (
    Stream,
    StreamReader,
    StreamingResponse,
)
from .socketio import (
    SocketIOConnection,
    on_socketio_event,
    socketio_controller,
)
from .websockets import (
    BroadcastGroup,
    WebSocket,
    WebSocketDisconnect,
    WebSocketError,
    WebSocketRouteNotFoundError,
    WebSocketValidationError,
    on_connect,
    on_disconnect,
    on_error,
    on_message,
    ws_controller,
)
from . import reflect  # noqa: F401
from .reflect import (
    # context types
    WsConnectionContext,
    WsMessageContext,
    WsUpgradeRequest,
    # cross-cutting readers
    reflect_guards,
    reflect_interceptors,
    reflect_middlewares,
    reflect_all,
    ReflectedMeta,
    # static class readers
    reflect_controller,
    reflect_module,
    reflect_injectable,
    reflect_ws_controller,
    reflect_routes,
    reflect_ws_messages,
    reflect_exception_handlers,
    get_controller_metadata,
    get_module_metadata,
    # user metadata + encoder
    reflect_user_metadata,
    reflect_encoder,
    # app-level readers
    get_all_routes,
    get_all_ws_gateways,
    get_route_metadata,
    # result types
    ReflectedRoute,
    ReflectedWsMessage,
    ReflectedController,
    ReflectedModule,
    ReflectedWsGateway,
    ReflectedError,
    # error catalog
    error_catalog,
)
from ._staticfiles import StaticFilesModule
from .config import ConfigModule, Secret
from .scheduling import (
    CronExpression,
    JobContext,
    ScheduleModule,
    SchedulerLock,
    SchedulerRegistry,
    ServiceContext,
    cron,
    hosted_service,
    interval,
    run_once,
)
from .sessions import (
    InMemoryRevocationStore,
    InMemorySessionStore,
    RevocationStore,
    Session,
    SessionConfig,
    SessionSerializer,
    SessionStore,
    SignedCookieSessionStore,
)
from .types import (
    AppState,
    CallHandler,
    CallNext,
    ClientInfo,
    ContentConfig,
    DebugConfig,
    Discriminated,
    ExecutionContext,
    GuardProtocol,
    Headers,
    InterceptorProtocol,
    MiddlewareProtocol,
    MutableHeaders,
    Request,
    Response,
    Scope,
    ServerInfo,
    State,
)

try:
    from importlib.metadata import PackageNotFoundError, version as _pkg_version

    __version__: str = _pkg_version("lauren")
except PackageNotFoundError:  # editable install without a tag
    __version__ = "0.0.0+unknown"

__all__ = [
    # background tasks
    "BackgroundTasks",
    "TaskHandle",
    "BackgroundTaskStarted",
    "BackgroundTaskComplete",
    "BackgroundTaskFailed",
    # application events (the rest lives in lauren.events)
    "EventBus",
    "EventPublisher",
    "on_event",
    "EventContext",
    "EmitResult",
    "EventHandlerFailed",
    # app
    "Lauren",
    "LaurenApp",
    "LaurenFactory",
    "DIContainer",
    "OperationScope",
    "ApplicationContext",
    # DI-aware commands (the rest lives in lauren.commands)
    "command",
    "Opt",
    "CommandContext",
    "CommandResult",
    # Custom providers (NestJS-style)
    "Token",
    "Inject",
    "OptionalDep",
    "use_value",
    "use_class",
    "use_factory",
    "use_existing",
    "RequestArena",
    "RequestAllocation",
    "JSONEncoder",
    "StdlibJSONEncoder",
    "OrjsonEncoder",
    "MsgspecEncoder",
    "PydanticEncoder",
    "auto_encoder",
    "serialization",
    "docs",
    "logging",
    "signals",
    "Router",
    "RouteEntry",
    # decorators
    "controller",
    "module",
    "injectable",
    "middleware",
    "interceptor",
    "use_encoder",
    "use_interceptors",
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "post_construct",
    "pre_destruct",
    "use_middlewares",
    "use_guards",
    "exception_handler",
    "use_exception_handlers",
    "set_metadata",
    "propagate_metadata",
    "openapi_security",
    "OpenAPISecurityMeta",
    # static files
    "StaticFilesModule",
    # typed configuration (the rest lives in lauren.config)
    "ConfigModule",
    "Secret",
    # hosted services & scheduling (the rest lives in lauren.scheduling)
    "hosted_service",
    "interval",
    "cron",
    "run_once",
    "ScheduleModule",
    "SchedulerRegistry",
    "JobContext",
    "ServiceContext",
    "CronExpression",
    "SchedulerLock",
    "HostedServiceStarted",
    "HostedServiceStopped",
    "HostedServiceFailed",
    "JobStarted",
    "JobCompleted",
    "JobFailed",
    "JobSkipped",
    # sessions
    "Session",
    "SessionConfig",
    "SessionStore",
    "InMemorySessionStore",
    "SignedCookieSessionStore",
    "SessionSerializer",
    "RevocationStore",
    "InMemoryRevocationStore",
    # types
    "Scope",
    "State",
    "AppState",
    "Request",
    "Response",
    "Headers",
    "MutableHeaders",
    "ClientInfo",
    "ServerInfo",
    "ContentConfig",
    "DebugConfig",
    "CallNext",
    "CallHandler",
    "MiddlewareProtocol",
    "GuardProtocol",
    "InterceptorProtocol",
    "ExecutionContext",
    # discriminated unions
    "Discriminated",
    # extractors
    "Path",
    "Query",
    "Header",
    "Cookie",
    "Json",
    "Form",
    "Bytes",
    "ByteStream",
    "UploadFile",
    "LifecycleEvent",
    "StartupBegin",
    "StartupComplete",
    "RequestReceived",
    "RequestComplete",
    "ShutdownBegin",
    "SignalBus",
    "Depends",
    "ExtractionMarker",
    "StateExtractor",
    "FieldDescriptor",
    "PathField",
    "QueryField",
    "HeaderField",
    "CookieField",
    "pipe",
    "Pipe",
    "PipeContext",
    "PipeMeta",
    "PIPE_META",
    "is_pipe",
    # streaming
    "Stream",
    "StreamReader",
    "StreamingResponse",
    # server-sent events
    "EventStream",
    "ServerSentEvent",
    "format_sse_event",
    "last_event_id",
    # websockets
    "ws_controller",
    "on_connect",
    "on_disconnect",
    "on_message",
    "on_error",
    "WebSocket",
    # reflect — context types
    "WsConnectionContext",
    "WsMessageContext",
    "WsUpgradeRequest",
    # reflect — cross-cutting readers
    "reflect_guards",
    "reflect_interceptors",
    "reflect_middlewares",
    "reflect_all",
    "ReflectedMeta",
    # reflect — static class readers
    "reflect_controller",
    "reflect_module",
    "reflect_injectable",
    "reflect_ws_controller",
    "reflect_routes",
    "reflect_ws_messages",
    "reflect_exception_handlers",
    "get_controller_metadata",
    "get_module_metadata",
    # reflect — user metadata + encoder
    "reflect_user_metadata",
    "reflect_encoder",
    # reflect — app-level readers
    "get_all_routes",
    "get_all_ws_gateways",
    "get_route_metadata",
    # reflect — result types
    "ReflectedRoute",
    "ReflectedWsMessage",
    "ReflectedController",
    "ReflectedModule",
    "ReflectedWsGateway",
    "ReflectedError",
    # reflect — error catalog
    "error_catalog",
    "BroadcastGroup",
    "WebSocketError",
    "WebSocketDisconnect",
    "WebSocketValidationError",
    "WebSocketRouteNotFoundError",
    # socket.io adapter
    "SocketIOConnection",
    "socketio_controller",
    "on_socketio_event",
    # errors
    "LaurenError",
    "StartupError",
    "HTTPError",
    "LifecycleError",
    "RouterConflictError",
    "CircularDependencyError",
    "CircularModuleError",
    "DecoratorUsageError",
    "MissingProviderError",
    "ProtocolAmbiguityError",
    "ModuleExportViolation",
    "LifecycleConfigError",
    "MetadataInheritanceError",
    "DuplicateBindingError",
    "UnresolvableParameterError",
    "DIScopeViolationError",
    "MiddlewareConfigError",
    "GuardConfigError",
    "InterceptorConfigError",
    "ExceptionHandlerConfigError",
    "SessionConfigError",
    "ConfigValidationError",
    "OpenAPISchemaError",
    "ExtractorError",
    "ExtractorFieldError",
    "BadRequestError",
    "NotFoundError",
    "RouteNotFoundError",
    "MethodNotAllowedError",
    "NotAcceptableError",
    "ConflictError",
    "GoneError",
    "PreconditionFailedError",
    "RequestBodyTooLarge",
    "UnsupportedMediaTypeError",
    "TooManyRequestsError",
    "ServiceUnavailableError",
    "RequestTimeoutError",
    "UnauthorizedError",
    "ForbiddenError",
    "MissingStateError",
    "StateTypeError",
    "LifecycleViolationError",
    "DestructError",
    "DestructTimeoutError",
    "DrainTimeoutError",
]
