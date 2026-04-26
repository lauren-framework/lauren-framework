# Core Concepts

Lauren's mental model is small but explicit. Master these six concepts and the rest of the framework is just composition:

<div class="grid cards" markdown>

-   :material-package-variant-closed: [__Modules__](modules.md) — The unit of dependency visibility. Imports, exports, and the boundary that keeps large codebases honest.

-   :material-router-network: [__Controllers__](controllers.md) — Class-based HTTP route groups. Constructor injection, decorator metadata, automatic request scoping.

-   :material-needle: [__Injectables & Providers__](injectables.md) — Three scopes, Protocol binding, multi-bindings, and `list[T]` injection that stays type-correct everywhere.

-   :material-sitemap: [__Class Inheritance Rules__](inheritance.md) — Lauren's strict opt-in inheritance model and why subclassing never silently turns a class into a controller.

-   :material-clock-outline: [__Lifecycle Hooks__](lifecycle.md) — `@post_construct` and `@pre_destruct` in topological order, with timeouts and best-effort teardown.

-   :material-swap-horizontal: [__Request & Response__](request-response.md) — Immutable response builders, typed `State` and `AppState`, and the auto-serialization rules.

</div>

## The mental model in one diagram

```mermaid
flowchart TB
    subgraph Startup [LaurenFactory.create — runs ONCE]
        M[Module graph]
        D[DI container]
        R[Radix-tree router]
        L[Lifecycle scheduler]
        M -->|providers + imports + exports| D
        M -->|controllers + handlers| R
        D -->|@post_construct topological| L
    end

    subgraph Runtime [Each HTTP request — pure traversal]
        REQ[Request] --> ROUTE[Router lookup O depth]
        ROUTE --> MW[Middleware onion]
        MW --> G[Guards]
        G --> EX[Extractors run]
        EX --> H[Handler]
        H --> AS[Auto-serialize]
        AS --> RESP[Response]
    end

    Startup ==> Runtime
```

Everything in the **Startup** column is validated and frozen. Everything in the **Runtime** column is allowed to be hot-path-fast because it never has to ask "is this configured correctly?" — that question was answered at boot.
