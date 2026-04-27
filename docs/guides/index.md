# Guides

Step-by-step recipes for the things you'll do most often as you build a real Lauren application.

<div class="grid cards" markdown>

-   :material-needle: [__Declaring an Injectable__](declaring-injectables.md)

    ---
    The full lifecycle of an injectable — `@injectable`, scopes, lifecycle hooks, Protocols, multi-bindings.

-   :material-cog-outline: [__Custom Providers__](custom-providers.md)

    ---
    `use_value`, `use_class`, `use_factory`, `use_existing`, `Token`, and `Inject` — when `@injectable` isn't enough.

-   :material-magnify-plus: [__Custom Extractors__](custom-extractors.md)

    ---
    Build domain extractors like `CurrentUser`, `TenantId`, or `RequestSpan` and use them as parameter annotations.

-   :material-shield-key: [__Custom Guards__](custom-guards.md)

    ---
    Authentication and authorization classes, route metadata, composition with class-level guards.

-   :material-layers-triple: [__Custom Middleware__](custom-middleware.md)

    ---
    The onion model, request-id propagation, response post-processing, error handling.

-   :material-alert-circle-outline: [__Custom Exception Handlers__](custom-exception-handlers.md)

    ---
    Catch domain errors at the right scope. Class-form (DI-injected) vs function-form. Global vs per-controller vs per-route.

-   :material-lan-connect: [__WebSockets__](websockets.md)

    ---
    First-class WebSocket gateways: `@ws_controller`, `@on_message`, typed Pydantic frames, `BroadcastGroup` rooms, in-process testing.

-   :material-broadcast: [__Server-Sent Events__](server-sent-events.md)

    ---
    One-way streaming with `EventStream` and `ServerSentEvent`: keep-alive heartbeats, `Last-Event-ID` resumability, AI text-streaming patterns.

-   :material-sync-circle: [__Circular Module Imports__](circular-module-imports.md)

    ---
    Break circular import cycles between feature modules using `ForwardRef("ClassName")` in `@module(imports=[...])` — resolved lazily at startup.

-   :material-shield-lock: [__OpenAPI Security from Guards__](openapi-security.md)

    ---
    Annotate guard classes with `@openapi_security({"SchemeName": []})` and Lauren populates the `security` field on every protected operation automatically — OR / AND semantics, OAuth2 scopes, explicit overrides.

-   :material-server-network: [__Proxy & Static Files__](proxy-and-static-files.md)

    ---
    Run behind a reverse proxy with `root_path`, and serve static assets with `StaticFilesModule.for_root("/static", directory="./public")` — ETag caching, path traversal protection, multiple mounts.

</div>
