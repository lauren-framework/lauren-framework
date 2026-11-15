# HTTP Routing

> Routes are values compiled at startup. Lauren's radix router matches a request's path, method and optionally host; the request path only traverses the prebuilt tree. Automatic `HEAD`, optional `OPTIONS`, typed converters and path policies are configured once per app with `RoutingConfig`.

## Quick start

<!-- docs-contract: run -->
```python
from lauren import HostParam, LaurenFactory, Request, RoutingConfig, controller, get, module
from lauren.testing import TestClient


@controller("/items")
class Items:
    @get("/{id:int}")
    async def item(self, id: int) -> dict:
        return {"id": id}

    @get("/{slug:slug}")
    async def by_slug(self, slug: str) -> dict:
        return {"slug": slug}


@controller("/", host="{tenant:slug}.example.com")
class TenantSite:
    @get("/whoami")
    async def whoami(self, tenant: HostParam[str]) -> dict:
        return {"tenant": tenant}


@module(controllers=[Items, TenantSite])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, routing=RoutingConfig(auto_options=True))
with TestClient(app) as client:
    assert client.get("/items/42").json() == {"id": 42}
    assert client.get("/items/blue-mug").json() == {"slug": "blue-mug"}
    assert client.get("/items/BAD_SLUG").status_code == 404
    head_response = client.head("/items/42")
    assert head_response.status_code == 200 and head_response.content == b""
    assert client.options("/items/42").header("allow") == "GET, HEAD, OPTIONS"
    assert client.get("/whoami", headers={"Host": "Acme.Example.com"}).json() == {"tenant": "acme"}
```

## Methods: HEAD, OPTIONS, any method

Every `GET` route answers `HEAD` **by default**. It runs the same guard, middleware, interceptor, response-contract, cache and handler pipeline; `request.method` remains `"HEAD"`, so the handler can avoid expensive work. The sender preserves headers and omits the body, including on errors and explicit `@head` routes. For buffered responses `Content-Length` is the size the `GET` body would have had. A stream is **not iterated** on `HEAD` (its `aclose()` is called if provided), and no length is invented for it. Background tasks enqueued by the handler still run. Use `RoutingConfig(auto_head=False)` to disable the GET fallback; an explicit `@head` always wins. `Allow` on 405 includes `HEAD` whenever GET is allowed.

`RoutingConfig(auto_options=True)` answers `OPTIONS` on an existing path with `204 No Content` and `Allow` (including `OPTIONS` and synthesized `HEAD`). The default is `False` (405 with `Allow`). Explicit `@options` wins. **CORS preflights** are the job of a global CORS middleware from `lauren-middlewares`, which runs before routing and may answer without reaching auto-OPTIONS. No guard or interceptor runs for synthesized OPTIONS.

For a fixed set of methods, use `@route("/hooks", methods=["POST", "PUT"])`; it is equivalent to stacking those verb decorators. For catch-all proxies use `@any_method("/{*rest}")`, which accepts every supported method and never yields a 405; an explicit method handler at the same node takes priority. Any-method routes have no single OpenAPI operation, so they are not documented; `@route(methods=[...])` is the documented alternative. `Lauren` offers `@app.route(...)` and `@app.any_method(...)` too.

## Path syntax, converters and precedence

| Template | What it matches | Unannotated value | OpenAPI schema |
|---|---|---|---|
| `{name}` or `{name:str}` | Any non-empty segment | `str` | `string` |
| `{id:int}` or `{id:int(min=1,max=99)}` | Non-negative decimal digits, optional inclusive bounds | `int` | `integer` with bounds |
| `{id:sint}` | Signed decimal integer | `int` | `integer` |
| `{price:float}` | Decimal literal (no exponent) | `float` | `number` |
| `{key:uuid}` | Canonical UUID | `uuid.UUID` | `string`, `format: uuid` |
| `{slug:slug}` | Lowercase ASCII slug (`blue-mug`) | `str` | `string` with pattern |
| `{code:re([A-Z]{3})}` | Fullmatch of a **single** segment | `str` | `string` with pattern |
| `{*rest}` or `{rest:path}` | Remainder of the path, possibly empty | `str` | `string` |

The router tries **static → typed parameters → untyped string → wildcard**, backtracking if a subtree fails. Typed siblings are ordered `re`/custom → `uuid` → `int`/`sint` → `float` → `slug` → `str`; equal ranks use declaration order. A converter mismatch is a **404** (or falls through to the next candidate); it never becomes a 422. An untyped `{id}` still matches `abc`, so an `id: int` annotation rejects it with **422** as before. Set `infer_converters=True` to opt in to implicit `sint`/`uuid` converters from untyped `int`/`uuid.UUID` handler parameters, changing invalid values to 404. By default, the handler annotation continues to coerce the *string* capture and drives validation; `request.path_params` remains `dict[str, str]` even with converters. An incompatible converter/annotation fails at startup (`UnresolvableParameterError`).

Names belong to **routes**, not tree positions: `/users/{id}` and `/users/{user_id}/posts` coexist; duplicate routes that differ only by parameter names still conflict. OpenAPI uses `{id}` (no converter spec) and the converter's schema. Static routes retain the flat-table lookup fast path.

### Custom converters and URL generation

<!-- docs-contract: fragment -->
```python
from lauren import PathConverter, RoutingConfig

class IsbnConverter(PathConverter):
    name = "isbn"
    python_type = str

    def matches(self, segment: str) -> bool:
        return len(segment) == 13 and segment.isascii() and segment.isdigit()

    def to_python(self, segment: str) -> str:
        return segment

    def to_url(self, value: str) -> str:
        return str(value)

    def openapi_schema(self) -> dict:
        return {"type": "string", "pattern": "^[0-9]{13}$"}

routing = RoutingConfig(converters={"isbn": IsbnConverter()})
# @get("/books/{isbn:isbn}") accepts only valid ISBN-shaped segments.
```

Register **instances** in `RoutingConfig.converters`; built-in names cannot be overridden. Implement `matches(segment)`, `to_python(segment)`, `to_url(value)` and `openapi_schema()`. The router keeps each converter on its `RouteEntry` (`entry.converters["isbn"]`); `entry.build_path(isbn="9780306406157")` formats via `to_url`, validates with `matches`, then percent-encodes the segment. `entry.build_host(...)` does the same for host labels. These methods are the seam for named-route URL generation; always have `to_url` produce a value that `matches` accepts. Custom converter arguments are not supported; constructor configuration belongs in the registered instance. Invalid templates, converter arguments and host patterns fail at decoration time; unknown custom names fail when the app compiles.

## Slash and raw-path policies

`RoutingConfig(trailing_slash=...)` offers:

| Mode | Behaviour |
|---|---|
| `"ignore"` (default) | `/x` and `/x/` are the same template, preserving existing apps. |
| `"strict"` | Different templates; the other spelling is a 404 unless declared. |
| `"redirect"` | Declared form is canonical; the other spelling gets **308** with query string and `root_path` / mount prefix in `Location`. Preserves POST/PUT method and body. |

`merge_slashes=True` collapses runs of `/` before matching (`/items//1` → `/items/1`); combine with `"redirect"` to send clients to a merged canonical URL. The defaults are `merge_slashes=False`, `trailing_slash="ignore"`.

ASGI defines `scope["path"]` as already percent-decoded. The default (`match_raw_path=False` in 1.x) routes on it: a server sees `/files/a%2Fb` as `/files/a/b`. Set `match_raw_path=True` to split `scope["raw_path"]` **first**, then percent-decode each segment strictly as UTF-8, so `{name}` receives `"a/b"` as *one* value. A malformed escape or UTF-8 sequence raises `BadRequestError` (400); if ASGI omitted `raw_path`, Lauren falls back to the decoded `path`. `TestClient` builds both fields as a real ASGI server does. The raw-path mode remains opt-in until a major release; do not rely on encoded slashes in the default mode.

## Hosts and tenant routing

`@controller(host="{tenant:slug}.example.com")` restricts a controller to matching hosts; `@module(host=...)` sets a default for its **own** controllers and WebSocket gateways (not imported modules), and a controller's own `host=` overrides it. `@ws_controller(host=...)` has the same pattern support. Literal labels and `{name}` / `{name:converter}` labels are accepted; no host wildcard. DNS matches case-insensitively and the port is ignored; `HostParam[T]` reads the lowercased captured label and validates it like `Path[T]`. A missing label, a host-param/path-param name collision, or two routes with the same host + method + template fail at startup.

Lookup tries exact host, then parameterized host (fewer labels with params first, then declaration order), then host-agnostic routes. Host routing is **not an authorization boundary**: a client controls the `Host` header. Use `lauren-middlewares.trusted_hosts` for host validation. Forwarded host headers must be accepted **only from trusted proxies**; this router does not trust `X-Forwarded-Host` or `Forwarded` on its own. Host-routed operations expose OpenAPI `servers` variables; synthesized HEAD and OPTIONS are not OpenAPI operations.

## Custom 404 and 405

Routing misses are offered to **global** `@exception_handler(RouteNotFoundError)` and `@exception_handler(MethodNotAllowedError)` handlers before the built-in envelope. A 405 handler returning status 405 without `Allow` gets the router's `Allow` header added. There is no matched route to supply per-route or controller exception handlers. Errors raised inside a matched route still use its usual route → controller → global chain. See [Custom Exception Handlers](custom-exception-handlers.md).

### Host-aware inspection

Compiled manifests keep `host` separate from `path` for both HTTP and WebSocket
routes; no internal host-prefixed lookup key is exposed as a URL path.
`get_all_routes(app)` and `get_all_ws_gateways(app)` return the host pattern
after startup. To query one exact host route, use
`get_route_metadata(app, "GET", "/items/{id:int}", host="admin.example.com")`.

HTTP requests and WebSocket upgrade requests expose the same read-only
`host_params` mapping, so a tenant guard can read
`ctx.request.host_params.get("tenant")` on either transport. Retaining a
matched mapping does not expose later pooled requests.
