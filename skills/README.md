# Lauren Skills Index

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> the exact file + line range and is faster than grep across the whole repo.

| Skill | Purpose |
|---|---|
| [building-lauren-apps](building-lauren-apps/) | Project layout, `LaurenFactory.create()`, root module, app bootstrap |
| [building-lauren-controllers](building-lauren-controllers/) | Route handlers, HTTP method decorators, typed extractors, pipes, serialization |
| [building-lauren-services](building-lauren-services/) | DI scopes (`SINGLETON`/`REQUEST`/`TRANSIENT`), custom providers, lifecycle hooks |
| [building-lauren-guards](building-lauren-guards/) | Guards (`CanActivate`), interceptors, middleware, `@use_guards`/`@use_interceptors` |
| [building-lauren-streaming](building-lauren-streaming/) | SSE (`EventStream`), WebSocket gateways (`@ws_controller`), `StreamingResponse[T]` |
| [building-lauren-background-tasks](building-lauren-background-tasks/) | `BackgroundTasks`, `TaskHandle`, fire-and-forget patterns |
| [testing-lauren-apps](testing-lauren-apps/) | `TestClient`, `WsTestClient`, async tests, mock providers, startup-failure assertions |
| [migrating-from-fastapi](migrating-from-fastapi/) | Side-by-side FastAPI → lauren equivalents (routing, DI, middleware, errors) |
| [using-companion-packages](using-companion-packages/) | CORS, auth guards (`jwt_bearer`, `api_key`), structured logging with `lauren-logging` |
| [common-patterns](common-patterns/) | Copy-paste complete patterns: auth CRUD, health check, background job, typed SSE stream |

## Quick nav by error

| Startup error | Go to |
|---|---|
| `MetadataInheritanceError` | [building-lauren-services](building-lauren-services/) §Strict inheritance |
| `ModuleExportViolation` | [building-lauren-apps](building-lauren-apps/) §Module wiring |
| `CircularDependencyError` | [building-lauren-services](building-lauren-services/) §Custom providers |
| `UnresolvableProviderError` | [building-lauren-apps](building-lauren-apps/) §Module imports |
| `DecoratorUsageError` | [building-lauren-guards](building-lauren-guards/) §Guards |

## See also

- [`AGENTS.md`](../AGENTS.md) — by-task lookup, common errors, definition of done
- [`CLAUDE.md`](../CLAUDE.md) — golden rules, conventions, pattern selection guide
- [`lauren/llms-full.txt`](../lauren/llms-full.txt) — complete 25-section API reference
