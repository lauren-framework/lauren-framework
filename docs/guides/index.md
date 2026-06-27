# Guides

Step-by-step recipes for the things you'll do most often as you build a real Lauren application.

<div class="grid cards" markdown>

-   :material-needle: [__Declaring an Injectable__](declaring-injectables.md)

    ---
    The full lifecycle of an injectable — `@injectable`, scopes, function-based providers, lifecycle hooks, Protocols, multi-bindings.

-   :material-cog-outline: [__Custom Providers__](custom-providers.md)

    ---
    `use_value`, `use_class`, `use_factory`, `use_existing`, `Token`, and `Inject` — when `@injectable` isn't enough.

-   :material-auto-fix: [__Implicit Parameter Extraction__](implicit-params.md)

    ---
    Path params, query params, and JSON bodies auto-detected from type annotations — no `Path[…]`/`Query[…]`/`Json[…]` boilerplate unless you need it.

-   :material-magnify-plus: [__Custom Extractors__](custom-extractors.md)

    ---
    Build domain extractors like `CurrentUser`, `TenantId`, or `RequestSpan` and use them as parameter annotations.

-   :material-pipe: [__Pipes__](pipes.md)

    ---
    Post-extraction transforms: validate, coerce, enrich, or replace extracted values before they reach your handler. Function-based, class-based, chainable, and DI-aware.

-   :material-upload: [__File Uploads__](file-uploads.md)

    ---
    `UploadFile` for single and multi-file multipart uploads, mixed forms with `Form[T]`, optional files, Unicode filenames.

-   :material-file-download-outline: [__File Responses & XML__](file-responses.md)

    ---
    `await Response.file(...)` for streamed downloads, browser-inline previews, and XML responses with `Response.xml(...)`.

-   :material-shape-plus: [__Custom Responses__](custom-responses.md)

    ---
    Subclass `Response`, preserve the concrete type through dispatch, and expose domain-specific response builders or streaming bodies.

-   :material-shield-key: [__Custom Guards__](custom-guards.md)

    ---
    Authentication and authorization classes, route metadata, composition with class-level guards.

-   :material-layers-triple: [__Custom Middleware__](custom-middleware.md)

    ---
    The onion model, request-id propagation, response post-processing, error handling.

-   :material-repeat: [__Interceptors__](interceptors.md)

    ---
    AOP-style wrappers that run after routing and guards. Full `ExecutionContext` access: transform results, add headers, implement caching, catch errors, and read route metadata.

-   :material-alert-circle-outline: [__Custom Exception Handlers__](custom-exception-handlers.md)

    ---
    Catch domain errors at the right scope. Class-form (DI-injected) vs function-form. Global vs per-controller vs per-route.

-   :material-lan-connect: [__WebSockets__](websockets.md)

    ---
    First-class WebSocket gateways: `@ws_controller`, `@on_message`, typed Pydantic frames, `BroadcastGroup` rooms, in-process testing.

-   :material-broadcast: [__Server-Sent Events__](server-sent-events.md)

    ---
    One-way streaming with `EventStream` and `ServerSentEvent`: keep-alive heartbeats, `Last-Event-ID` resumability, AI text-streaming patterns.

-   :material-swap-vertical: [__Typed Streaming__](typed-streaming.md)

    ---
    `StreamingResponse[T]` for outbound content-negotiated streams and `Stream[T]` for inbound typed iterators. SSE / NDJSON / JSON Lines, discriminated unions, bidirectional pipelines.

-   :material-signal: [__Signals & Lifecycle Events__](signals.md)

    ---
    `SignalBus` for typed pub/sub: `RequestComplete`, `StartupComplete`, background-task signals. MRO dispatch, error isolation, metrics/tracing patterns.

-   :material-puzzle: [__Socket.IO__](socketio.md)

    ---
    Engine.IO v4 / Socket.IO v5 adapter — `@socketio_controller`, `@on_socketio_event`, `SocketIOConnection`, automatic ACKs, DI injection.

-   :material-sync-circle: [__Circular Module Imports__](circular-module-imports.md)

    ---
    Break circular import cycles between feature modules using `ForwardRef("ClassName")` in `@module(imports=[...])` — resolved lazily at startup.

-   :material-shield-lock: [__OpenAPI Security from Guards__](openapi-security.md)

    ---
    Annotate guard classes with `@openapi_security({"SchemeName": []})` and Lauren populates the `security` field on every protected operation automatically — OR / AND semantics, OAuth2 scopes, explicit overrides.

-   :material-server-network: [__Proxy & Static Files__](proxy-and-static-files.md)

    ---
    Run behind a reverse proxy with `root_path`, and serve static assets with `StaticFilesModule.for_root("/static", directory="./public")` — ETag caching, path traversal protection, multiple mounts.

-   :material-lightning-bolt: [__Background Tasks__](background-tasks.md)

    ---
    Fire-and-forget work after the response is sent. `BackgroundTasks` extractor, `TaskHandle`, sync/async callables, `BackgroundTaskFailed` signals, graceful-shutdown drain participation.

-   :material-cookie: [__Sessions__](sessions.md)

    ---
    Signed-cookie sessions: `sessions=SessionConfig(...)`, `session: Session` injection, pluggable `SessionStore` (in-memory, stateless cookie, Redis), secure-by-default cookies, `regenerate_id()` / `invalidate()`, and startup-validated config.

-   :material-sync: [__Sync vs Async Handlers__](sync-handlers.md)

    ---
    Every handler may be `def` or `async def`. Sync handlers are automatically offloaded to a thread pool — no blocking the event loop.

-   :material-function-variant: [__Custom Route Handlers__](custom-route-handlers.md)

    ---
    `@staticmethod` and `@classmethod` bindings, writing decorators with `@functools.wraps`, environment-conditional handler selection at class-body time, and the descriptor protocol for advanced use cases.

-   :material-database-import: [__DI with Data Model Types__](di-model-types.md)

    ---
    Using `@dataclass`, Pydantic `BaseModel`, SQLAlchemy `DeclarativeBase`, and `SQLModel` with the DI container.

-   :material-text-box-outline: [__Logging in Production__](logging.md)

    ---
    `ConsoleLogger` for development, `JsonLogger` for production, `InMemoryLogger` for tests. Log levels, env-var config, DI injection patterns, and coexistence with Python's `logging` module.

</div>
