---
name: building-lauren-apps
description: Scaffolds and builds Lauren Python web framework applications. Covers LaurenFactory.create(), the @module system, project file layout, and wiring everything together. Use when creating a new Lauren project, setting up AppModule, or understanding how Lauren's startup phases work.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Building Lauren Apps

Lauren is a Python ASGI framework (NestJS-inspired). Every app is a tree of `@module`s assembled by `LaurenFactory.create()`.

## Quickstart

```python
# main.py  ← module-level `app` for uvicorn
from dotenv import load_dotenv
load_dotenv()                        # MUST be before any app imports

from app.app_module import AppModule
from lauren import LaurenFactory

app = LaurenFactory.create(AppModule)  # synchronous, returns ASGI callable
```

Serve: `uvicorn main:app --reload`

## LaurenFactory.create()

```python
app = LaurenFactory.create(
    AppModule,
    global_middlewares=[CorsMiddleware, LoggingMiddleware],  # run BEFORE routing
    global_guards=[AuthGuard],
    global_interceptors=[TimingInterceptor],
    global_exception_handlers=[ChatMessageErrorHandler],
    global_providers=[use_value(provide=API_KEY, value="...")],  # module-free providers
    max_body_size=1_048_576,   # bytes, default 1 MB
    strict_lifecycle=True,
    json_encoder=MsgspecEncoder(),          # app-wide JSON encoder
    logger=default_logger(),                # NestJS-style structured logger
    signals=signal_bus,                     # SignalBus for lifecycle events
    docs_url="/docs",                       # Swagger UI
    openapi_url="/openapi.json",            # OpenAPI 3.1 schema
    redoc_url="/redoc",                     # ReDoc
    openapi_info={"title": "My API", "version": "1.0.0"},
    openapi_security_schemes={"BearerAuth": {"type": "http", "scheme": "bearer"}},
    app_state={"env": "prod"},              # shared state accessible via request.state.app
    mounts={"/static": static_app},         # ASGI sub-applications
    root_path="",                           # proxy prefix
)
```

- **Synchronous** — call at module level; uvicorn imports `app` directly.
- **Seven startup phases** (module graph → providers → DI compile → router → lifecycle → app sealed). Any error in phases 1–5 raises `StartupError` immediately.
- `global_middlewares` run **before routing** — they intercept every request including OPTIONS preflight.
- `json_encoder` sets the app-wide encoder; override per-route with `@use_encoder(...)`.
- `signals` wires a `SignalBus` for lifecycle events (`StartupBegin`, `RequestComplete`, `BackgroundTaskFailed`, etc.).

## Module system

See [modules.md](modules.md) for the complete module API.

```python
# Root module — imports feature modules, owns nothing itself
@module(imports=[ChatModule, HealthModule, AuthModule])
class AppModule:
    pass

# Feature module — owns its own controllers + providers
@module(
    controllers=[UsersController],
    providers=[UsersService, UserRepo],
    imports=[DatabaseModule],       # re-use shared providers
    exports=[UsersService],         # expose to other modules
)
class UsersModule:
    pass
```

Rules:
- A provider is visible inside a module only if declared in `providers=` or exported by an imported module.
- Exports propagate one hop; B must re-export what A needs from C.
- `CircularModuleError` raised at startup for import cycles.

## Static files — StaticFilesModule

Serve static files from a directory using a NestJS-style module factory:

```python
from lauren import module, LaurenFactory
from lauren._staticfiles import StaticFilesModule

@module(
    imports=[
        StaticFilesModule.for_root("/static", directory="./public"),
    ],
    controllers=[...],
)
class AppModule:
    pass

app = LaurenFactory.create(AppModule)
```

`for_root(path, directory, max_age=3600, dotfiles="allow_well_known")` returns a `@module` class that generates a controller with two routes: `GET /` (serves `index.html`) and `GET /{*filepath}` (serves any file). Path traversal is blocked (403). Dotfile paths (`/.env`, `/.git/config` — any segment starting with `.`) get an empty 404 by default while `/.well-known/**` is still served; `dotfiles="deny"` hides `/.well-known/` too and `dotfiles="allow"` serves everything. ETag + `Cache-Control` headers are set automatically.

Multiple mounts are supported — each `for_root()` call returns a unique class:

```python
@module(
    imports=[
        StaticFilesModule.for_root("/static", directory="public"),
        StaticFilesModule.for_root("/assets", directory="dist/assets"),
    ],
)
class AppModule:
    pass
```

Alternatively, use `LaurenFactory.create(mounts=...)` for ASGI sub-application mounting:

```python
app = LaurenFactory.create(
    AppModule,
    mounts={"/api/v2": other_asgi_app},
)
```

## Seeding, migrations and other admin tasks

Write them as commands, never as HTTP endpoints. A `@command` method on a
provider runs with the app started and its own DI scope:

```python
from lauren import injectable
from lauren.commands import CommandContext, command


@injectable()
class SeedCommands:
    def __init__(self, db: DatabaseService) -> None:
        self._db = db

    @command("db:seed", help="Insert the demo data.")
    async def seed(self, ctx: CommandContext, *, reset: bool = False) -> int:
        if reset and not ctx.confirm("Delete existing data first?"):
            return 1
        ctx.echo(f"seeded {await self._db.seed(reset=reset)} rows")
        return 0
```

```console
python -m lauren exec my_app.main:app db:seed --reset --yes
python -m lauren commands my_app.main:app
```

`main:app` (a built `LaurenApp`), `main:create_app` (a factory) and
`app_module:AppModule` all work as the target. Scripts use
`async with LaurenFactory.create_context(AppModule) as ctx:`. Full story:
`skills/building-lauren-commands/`.

## Recommended project layout

See [project-layout.md](project-layout.md) for a full tree.

```
src/
  my_app/
    main.py                 ← LaurenFactory.create(AppModule)
    app_module.py           ← root @module(imports=[...])
    users/
      users_module.py
      users_controller.py
      users_service.py
      schemas.py
    middlewares/
      cors_middleware.py
      logging_middleware.py
    interceptors/
      timing_interceptor.py
tests/
  conftest.py               ← set env vars BEFORE app imports
  unit/
  integration/
.env.example
pyproject.toml
```

## Common mistakes

- Forgetting `load_dotenv()` before importing the app (singletons read env vars on construction).
- Using `await LaurenFactory.create()` — it is **synchronous**, not async.
- Not listing a provider in `providers=[]` — it will be missing from the DI container.
- Exporting a class that is not in `providers=` or not imported — raises `ModuleExportViolation`.
- Exposing seeding or other admin work as an HTTP route — use a `@command` and `python -m lauren exec`.
- Resolving request-scoped providers from `app.container` in a script — they are never torn down; use `ctx.scope()` (`LaurenFactory.create_context`).
