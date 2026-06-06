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

`for_root(path, directory, max_age=3600)` returns a `@module` class that generates a controller with two routes: `GET /` (serves `index.html`) and `GET /{*filepath}` (serves any file). Path traversal is blocked (403). ETag + `Cache-Control` headers are set automatically.

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
