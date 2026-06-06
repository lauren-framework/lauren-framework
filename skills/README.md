# Lauren Skills Index

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> the exact file + line range and is faster than grep across the whole repo.

Each skill directory contains a `SKILL.md` entry point and optional topic-specific
reference files (e.g. `extractors.md`, `custom-providers.md`).

| Skill | Purpose |
|---|---|
| [building-lauren-apps](building-lauren-apps/) | Project layout, `LaurenFactory.create()`, root module, `StaticFilesModule`, mount system, app bootstrap |
| [building-lauren-controllers](building-lauren-controllers/) | Route handlers, HTTP method decorators, typed extractors, pipes, serialization, `@use_encoder` |
| [building-lauren-services](building-lauren-services/) | DI scopes (`SINGLETON`/`REQUEST`/`TRANSIENT`), custom providers (`Token`, `use_value`, `use_class`, `use_factory`, `use_existing`), lifecycle hooks |
| [building-lauren-guards](building-lauren-guards/) | Guards (`can_activate`), interceptors (`CallHandler`), middleware (`dispatch`/`call_next`), `@exception_handler`, `@use_guards`/`@use_interceptors`/`@use_middlewares`/`@use_exception_handlers` |
| [building-lauren-streaming](building-lauren-streaming/) | SSE (`EventStream`, `ServerSentEvent`), WebSocket gateways (`@ws_controller`), `StreamingResponse[T]`, Socket.IO adapter |
| [building-lauren-background-tasks](building-lauren-background-tasks/) | `BackgroundTasks`, `TaskHandle`, fire-and-forget patterns |
| [testing-lauren-apps](testing-lauren-apps/) | `TestClient`, `WsTestClient`, async tests, mock providers, startup-failure assertions |
| [migrating-from-fastapi](migrating-from-fastapi/) | Side-by-side FastAPI → lauren equivalents (routing, DI, middleware, errors) |
| [using-companion-packages](using-companion-packages/) | CORS, auth guards (`jwt_bearer`, `api_key`), structured logging with `lauren-logging` |
| [building-companion-packages](building-companion-packages/) | Package structure, DI/module integration, CI/CD, `llms*.txt`, and publishing for Lauren ecosystem packages |
| [common-patterns](common-patterns/) | Copy-paste complete patterns: auth CRUD, health check, background job, typed SSE stream, module composition from examples |

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

## Updating Docs After Code Changes

When a framework change lands, derive the documentation refresh from the git
diff since the latest release tag instead of guessing from memory:

```bash
git --no-pager log -p $(git describe --tags --abbrev=0)..HEAD
```

Use that range to update:

- `CHANGELOG.md` under `[Unreleased]`
- the relevant pages under `docs/`
- `llms.txt`, `llms-full.txt`, `AGENTS.md`, and `CLAUDE.md` when public or agent-facing behaviour changed

Validate with:

```bash
uv run nox -s docs
uv run nox -s llms_check
```
