# Proxy & Static Files

## Running Behind a Reverse Proxy

When your app is served at a sub-path (e.g. `/api`) behind nginx or another
reverse proxy, pass `root_path` to `LaurenFactory.create`.

### nginx scenario

nginx strips nothing — `scope["path"]` arrives with the full prefix:

```python
app = LaurenFactory.create(AppModule, root_path="/api")
```

Lauren automatically strips `/api` from every incoming path before routing,
so a request to `/api/users` reaches your `GET /users` handler unchanged.

### uvicorn `--root-path`

When uvicorn is started with `--root-path /api`, it sets `scope["root_path"]`
and pre-strips the prefix from `scope["path"]`.  Lauren detects this and does
**not** double-strip — the right thing happens automatically.

```bash
uvicorn myapp:app --root-path /api
```

### OpenAPI `servers`

If you specify `root_path` but no explicit `openapi_servers`, the generated
OpenAPI document automatically includes a `servers` entry:

```json
{ "servers": [{ "url": "/api" }] }
```

Override this by supplying your own servers:

```python
app = LaurenFactory.create(
    AppModule,
    root_path="/api",
    openapi_servers=[{"url": "https://example.com/api"}],
)
```

---

## Mounting ASGI Sub-Applications

`app.mount(path, sub_app)` attaches any ASGI application at a path prefix.
Requests whose path starts with that prefix are forwarded to the sub-app;
Lauren strips the prefix from `scope["path"]` and appends it to
`scope["root_path"]` before forwarding.

```python
from starlette.staticfiles import StaticFiles

app = LaurenFactory.create(AppModule)
app.mount("/static", StaticFiles(directory="static"))
app.mount("/admin", admin_app)   # any ASGI app
```

Alternatively, pass a `mounts` dict to the factory:

```python
app = LaurenFactory.create(
    AppModule,
    mounts={
        "/static": StaticFiles(directory="static"),
        "/admin": admin_app,
    },
)
```

Prefix matching uses **longest-prefix-first** ordering regardless of the
registration order, so `/api/v2` always wins over `/api` when both are mounted.

### How the scope is modified

| Field | Before | After |
|---|---|---|
| `scope["path"]` | `/static/logo.png` | `/logo.png` |
| `scope["root_path"]` | `""` (or upstream value) | `"" + "/static"` |

The sub-app therefore sees itself as serving from the root, while browsers and
CDNs see the full `root_path` for relative-URL generation.

### lifespan

Lifespan events (`startup` / `shutdown`) of the sub-app are **not** forwarded.
If your sub-app needs lifespan, wire it up separately or wrap it:

```python
from contextlib import asynccontextmanager
from starlette.applications import Starlette

@asynccontextmanager
async def sub_lifespan(app):
    await db.connect()
    yield
    await db.disconnect()

sub = Starlette(routes=[...], lifespan=sub_lifespan)
app.mount("/sub", sub)
```

---

## Serving Static Files

`StaticFilesModule` is a NestJS-inspired feature module that registers a
controller serving files from a local directory.

### Basic usage

```python
from lauren import LaurenFactory, module
from lauren.static_files import StaticFilesModule

@module(
    controllers=[...],
    imports=[
        StaticFilesModule.for_root("/static", directory="./public"),
    ],
)
class AppModule:
    pass

app = LaurenFactory.create(AppModule)
```

- `GET /static` → serves `public/index.html`
- `GET /static/css/app.css` → serves `public/css/app.css`
- `GET /static/missing.png` → **404**

### Multiple mounts

Each `for_root()` call produces an independent module — import as many as you
need:

```python
@module(
    imports=[
        StaticFilesModule.for_root("/static", directory="./public"),
        StaticFilesModule.for_root("/assets", directory="./dist/assets"),
    ],
)
class AppModule:
    pass
```

### Cache control

A `Cache-Control: public, max-age=3600` header is attached to every 200
response by default.  Change the TTL or disable it:

```python
# 24-hour cache
StaticFilesModule.for_root("/s", directory="./dist", max_age=86400)

# No caching headers
StaticFilesModule.for_root("/s", directory="./dist", max_age=0)
```

### Conditional GET (ETag)

Every 200 response includes an `ETag` derived from the file content.  Browsers
and CDNs that send `If-None-Match` back receive a **304 Not Modified** without
the body, saving bandwidth.

### Security

Path traversal is blocked at the controller level: any path that resolves
outside the configured directory returns **403 Forbidden**.  The router also
normalises `..` segments before they reach the controller, so most traversal
attempts never reach the handler at all.
