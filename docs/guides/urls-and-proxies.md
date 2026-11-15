# URLs, Proxies & Signed Links

> An API produces URLs to itself all the time: the `Location` of a `201 Created`, a redirect target, pagination and HATEOAS links, links in emails and webhooks, expiring download links. Lauren names every route, builds URLs from a name or a **handler reference**, knows the request's real scheme, host and path prefix — behind mounts and trusted reverse proxies — and signs links that must not be forged.

## Quick start

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, module, post
from lauren.responses import Created
from lauren.testing import TestClient
from lauren.types import Request


@controller("/users", name="users")
class UserController:
    @get("/{id}", name="show")
    async def show(self, id: int, request: Request) -> dict:
        return {"id": id, "self": request.url_for("users.show", id=id)}

    @post("/")
    async def create(self, request: Request) -> Created[dict]:
        user = {"id": 7}
        # A handler reference: renaming the method breaks the import, not the link.
        return Created(user, location=request.url_path_for(UserController.show, id=user["id"]))


@module(controllers=[UserController])
class AppModule:
    pass


app = LaurenFactory.create(AppModule)
client = TestClient(app, base_url="https://api.example.com")

assert client.post("/users/").header("location") == "/users/7"
assert client.get("/users/7").json()["self"] == "https://api.example.com/users/7"
assert app.url_path_for("users.show", id=3) == "/users/3"
```

The `Location` header is right wherever the app runs: under a mount
(`app.mount("/v1", app)` → `/v1/users/7`), behind `root_path="/api"`, or
behind a proxy that forwards `X-Forwarded-Prefix` — never an f-string that
silently breaks when the controller moves.

## Naming routes

| Declaration | Route name |
|---|---|
| `@get("/{id}", name="show")` in `@controller("/users", name="users")` | `users.show` |
| the same, in `@module(..., name="accounts")` | `accounts.users.show` |
| `@get("/", name="health")` in an unnamed controller | `health` |
| `@post("/")` without `name=` on `UserController.create` | `UserController.create` (derived) |
| `@app.get("/items/{item_id}")` on `async def read_item` (`Lauren` apps) | `read_item` (derived) |
| `@ws_controller("/ws/chat/{room}", name="chat")` | `chat` (WebSocket namespace) |
| `@ws_controller("/ws/feed")` on `class FeedGateway` | `FeedGateway` (derived) |

- Names are dot-separated identifiers (`[A-Za-z_][A-Za-z0-9_-]*`); a
  malformed `name=` raises `DecoratorUsageError` when the decorator runs
  (`detail["reason"] == "invalid_route_name"`).
- Explicit names are namespaced by the controller's and the module's
  `name=`; derived names never are, so they stay greppable.
- Two routes with the same explicit name fail `LaurenFactory.create` with
  `RouterConflictError` (`detail["reason"] == "duplicate_route_name"`, plus
  `name` and `handlers`). HTTP routes and WebSocket gateways are separate
  namespaces.
- A derived name shared by several routes (two `Controller` classes in
  different modules) is left **ambiguous** instead of failing apps that never
  build URLs; looking it up raises `LookupError` asking for explicit names.
  An explicit name always wins over an equal derived one.
- A handler with several routes (`@get("/ping")` + `@get("/health")`) gets no
  derived name; give each route a `name=`, and pick one through a handler
  reference with `route=`: `request.url_path_for(Ops.ping, route="health")`.

`get_all_routes(app)` / `get_all_ws_gateways(app)` report every name
(`ReflectedRoute.name`, `ReflectedWsGateway.name`); `app.routes()` and
`app.router.find(...)` return `RouteEntry` values with `.name`.

## Building URLs

| API | Returns | Root path / mount prefix | Absolute |
|---|---|---|---|
| `app.url_path_for(target, **params)` | `URL` path | no (app-relative) | no |
| `request.url_path_for(target, **params)` | `URL` path | **yes** | no |
| `request.url_for(target, **params)` | `URL` | yes | **yes** (`request.base_url`) |
| `request.ws_url_for(gateway, **params)` | `URL` (`ws://` / `wss://`) | yes | yes |
| `urls.path_for(target, **params)` (`UrlBuilder`) | `URL` path | the public root path | no |
| `urls.url_for(target, **params)` | `URL` | yes | yes (`public_base_url`) |
| `urls.ws_url_for(gateway, **params)` | `URL` (`ws://` / `wss://`) | yes | yes (`public_base_url`) |

`WebSocket` connections and the `WsUpgradeRequest` that guards receive
offer the same `url_for` / `url_path_for` / `ws_url_for` / `base_url`.

**Targets.** A route name (`"users.show"`) or a handler reference
(`UserController.show`). Prefer references inside the codebase: Python
checks them at import time and type checkers flag a rename. Names are for
templates, configuration and cross-service contracts.

**Parameters** are keyword arguments, rendered with `str()` (booleans as
`true` / `false`, enums by value) and percent-encoded **per segment**:
`name="a/b c"` becomes `a%2Fb%20c`, `.` and `..` are escaped so they are never
dot-segments. A wildcard (`{*path}`) keeps its `/` separators.

**Mistakes are programming errors**, raised as built-in exceptions that
surface as a logged 500 — never a silently wrong URL:

- unknown name → `LookupError` (with the closest names: *did you mean
  'users.show'?*);
- missing, unexpected, `None` or empty parameter → `ValueError` naming the
  route and the expected parameters;
- a handler reference that is not a route of this app → `LookupError`.

Query strings and fragments go on the result. `URL` is an immutable `str`
subclass — use it anywhere a string goes:

```python fragment
url = request.url_for("orders.list").with_query(user=7, status=["open", "held"])
# https://api.example.com/orders?user=7&status=open&status=held
url.with_fragment("top")          # ...#top
url.replace(scheme="http", port=8080)
url.host, url.port, url.path, url.query, url.is_absolute
```

`with_query` replaces the keys it is given (a list repeats a key, `None`
removes it). `url.replace("a", "b")` with positional arguments is still
`str.replace`.

## Links outside a request: `UrlBuilder`

Services, background tasks, scheduled jobs and commands have no request.
Inject the app's `UrlBuilder` and give the app its public URL:

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, injectable, module
from lauren.urls import UrlBuilder


@controller("/invoices", name="invoices")
class InvoiceController:
    @get("/{id}", name="show")
    async def show(self, id: int) -> dict:
        return {"id": id}


@injectable()
class InvoiceMailer:
    def __init__(self, urls: UrlBuilder) -> None:
        self.urls = urls

    def link(self, invoice_id: int) -> str:
        return self.urls.url_for(InvoiceController.show, id=invoice_id)


@module(controllers=[InvoiceController], providers=[InvoiceMailer])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, public_base_url="https://billing.example.com/api")
assert app.urls.url_for("invoices.show", id=12) == "https://billing.example.com/api/invoices/12"
```

`public_base_url` is the app's public URL **including** any path prefix (when
it has no path, `root_path=` is used). `url_for` / `ws_url_for` without it
raise `RuntimeError` — a request's `Host` header is never consulted here.
Pagination `Link` headers use the same origin when
`PaginationConfig(base_url=...)` is not set.

## The request URL model

| Property | Example | Source |
|---|---|---|
| `request.scheme` | `"https"` | ASGI `scope["scheme"]`, or a trusted proxy |
| `request.host` | `"api.example.com"` | trusted forwarded host, else the `Host` header, else `scope["server"]` |
| `request.port` | `443` | the host's port, a trusted `X-Forwarded-Port`, else the scheme's default |
| `request.root_path` | `"/backend"` | mounts, `root_path=` / ASGI `root_path`, a trusted `X-Forwarded-Prefix` |
| `request.base_url` | `URL("https://api.example.com/backend/")` | composed |
| `request.full_url` | `URL("https://api.example.com/backend/users/7?x=1")` | composed |
| `request.client` | `ClientInfo("203.0.113.9", None)` | `scope["client"]`, or the client a trusted proxy reported |

`request.url` keeps returning `path?query` in 1.x (`request.path_with_query`
is the forward-compatible spelling); `full_url` is the absolute URL.
`WebSocket` has `client`, `scheme` (`"ws"` / `"wss"`), `host`, `port`,
`root_path` and `base_url` too.

!!! warning "The `Host` header is client-chosen"
    Without a trusted proxy (or `lauren-middlewares`' `trusted_hosts`), an
    absolute URL built from the request echoes whatever `Host` the client
    sent. That is fine for links in the response to that client, but links
    stored or sent elsewhere (password-reset emails, webhooks, cached
    responses) must come from `UrlBuilder` and `public_base_url`. An invalid
    `Host` value is ignored.

## Trusted reverse proxies

Behind a load balancer the immediate peer is the proxy: `request.client`
would be the balancer's address for every user, and the scheme `http` even
though the client used HTTPS. Tell Lauren which peers are proxies:

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient
from lauren.types import Request
from lauren.urls import ProxyConfig


@controller("/whoami")
class WhoAmI:
    @get("/")
    async def whoami(self, request: Request) -> dict:
        return {"client": request.client.host, "url": request.full_url}


@module(controllers=[WhoAmI])
class AppModule:
    pass


app = LaurenFactory.create(
    AppModule,
    proxy=ProxyConfig(trusted=["10.0.0.0/8"], trust_prefix=True),
)
headers = {
    "x-forwarded-for": "198.51.100.66, 203.0.113.9",   # leftmost entry: client-supplied
    "x-forwarded-proto": "https",
    "x-forwarded-host": "api.example.com",
    "x-forwarded-prefix": "/backend",
}

with TestClient(app, client=("10.0.0.5", 4000)) as proxied:      # the balancer's address
    body = proxied.get("/whoami/", headers=headers).json()
assert body == {"client": "203.0.113.9", "url": "https://api.example.com/backend/whoami/"}

with TestClient(LaurenFactory.create(AppModule, proxy=ProxyConfig(trusted=["10.0.0.0/8"])),
                client=("198.51.100.1", 4000)) as direct:         # not a proxy: headers ignored
    assert direct.get("/whoami/", headers=headers).json()["client"] == "198.51.100.1"
```

`ProxyConfig(*, trusted, headers="x-forwarded", max_hops=1, trust_prefix=False, server_applied=False)`:

- `trusted` — addresses and CIDR networks, `"unix"` for Unix-socket peers,
  or `"*"` (every peer; logged as a warning — the headers are then
  client-controlled).
- `headers` — `"x-forwarded"` (`X-Forwarded-For` / `-Proto` / `-Host` /
  `-Port` / `-Prefix`), `"forwarded"` (RFC 7239) or `"both"` (`Forwarded`
  when present).
- `max_hops` — how many forwarded-for entries may be walked: one per proxy
  in front of the app (CDN → load balancer → app is `max_hops=2`).
- `trust_prefix` — honour `X-Forwarded-Prefix` as the public path prefix.
- `server_applied` — the ASGI server already rewrote `client` and `scheme`;
  Lauren derives only host and prefix.

**How a request is resolved.** Forwarding headers are read **only** when the
immediate peer is trusted. The forwarded-for list is walked right to left —
each proxy appends the address it received the request from — skipping
trusted addresses for at most `max_hops` entries; the first untrusted entry
is the client (`unknown` and obfuscated `_ids` count as untrusted). Scheme,
host, port and prefix come from the same hop, so a value the client wrote
into the header itself is never used. Invalid values (a scheme other than
`http`/`https`, a malformed host, a prefix that does not start with a single
`/`) are ignored and logged at debug level; a request never fails because of
them. A bad `trusted` entry fails `create()` (`StartupError`,
`reason="invalid_proxy_config"`).

The answer is computed once per request, before any middleware, so guards,
handlers, sessions, rate limiters and the companion packages all see the same
client, scheme and host. Mounted sub-applications receive it in their scope
(`client`, `scheme`, `root_path`, `Host`), so a mounted Starlette or Lauren
app agrees too. WebSocket connections are resolved the same way (`wss` behind
a TLS-terminating proxy).

### nginx

```nginx
location /backend/ {
    proxy_pass http://app:8000/;                 # strips /backend
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Prefix /backend;
}
```

```python fragment
app = LaurenFactory.create(AppModule, proxy=ProxyConfig(trusted=["172.16.0.0/12"], trust_prefix=True))
```

### Choosing one layer (uvicorn `--proxy-headers`)

| Setup | Lauren config |
|---|---|
| uvicorn without `--proxy-headers` (or granian / hypercorn / `TestClient`) | `ProxyConfig(trusted=[...])` — identical behaviour under every server and in tests |
| uvicorn `--proxy-headers --forwarded-allow-ips=...` already rewrites client and scheme | `ProxyConfig(trusted=[...], server_applied=True)` for host / prefix only — or no `ProxyConfig` |
| Directly exposed app (no proxy) | no `ProxyConfig` |

Applying both layers fully would treat the real client as a peer. With
`server_applied=True` the trust check sees the address the server reported
(already the client's), so use it with `trusted` naming the networks allowed
to set `X-Forwarded-Host` / `-Prefix` — or `["*"]` only when the app port is
reachable solely through the proxy.

## Signed URLs

Expiring, tamper-proof links — downloads, unsubscribe and invitation links,
webhooks back into the app:

<!-- docs-contract: run -->
```python
from lauren import LaurenFactory, controller, get, module, use_guards
from lauren.testing import TestClient
from lauren.urls import UrlSigningConfig, ValidSignature


@controller("/downloads", name="downloads")
class Downloads:
    @get("/{id}", name="file")
    @use_guards(ValidSignature)
    async def file(self, id: int) -> dict:
        return {"file": id}


@module(controllers=[Downloads])
class AppModule:
    pass


app = LaurenFactory.create(AppModule, url_signing=UrlSigningConfig(secret=["k-2026-10", "k-2026-04"]))
link = app.urls.signed_url_for("downloads.file", id=7, expires_in=600)

with TestClient(app) as client:
    assert client.get(link).json() == {"file": 7}
    tampered = client.get(link.replace("/7?", "/8?"))
    assert tampered.status_code == 403
    assert tampered.json()["error"]["detail"] == {"reason": "signature_invalid"}
```

In application code inject `UrlBuilder` and call
`urls.signed_url_for(target, *, expires_in=None, query=None, route=None, **params)`
(`signed_ws_url_for` for gateways). The link carries `exp` (unix seconds) and
`sig`; `query=` adds signed query parameters. With `public_base_url` the
link is absolute.

`UrlSigningConfig(*, secret, default_ttl=3600, bind_method=False, bind_host=False, expires_param="exp", signature_param="sig", clock=None)`:

- `secret` — str / bytes / `Secret`, or a list: the **first signs, all
  verify**. Rotate by prepending a new key and dropping the oldest after
  `default_ttl`. Keep it separate from the session secret.
- `bind_method` — the method is signed too (a link for `GET` does not
  authorise `DELETE`).
- `bind_host` — scheme and host are signed too (needs `public_base_url`).
- `clock` — a callable returning unix time; tests pass a fake one to expire
  links without sleeping.

The signature covers the **app-relative** path (no root path or mount
prefix), so a link stays valid when the app is served under several
prefixes, and the query sorted by key. It uses the framework's shared HMAC
signer (`"url"` namespace, constant-time comparison). `ValidSignature`
answers a missing, tampered or expired link with `ForbiddenError` (403,
`detail["reason"]` `signature_missing` / `signature_invalid` /
`signature_expired`); on a WebSocket gateway it closes the connection with
code 4403. A route or gateway guarded by `ValidSignature` without
`url_signing=` fails `create()` (`StartupError`,
`reason="url_signing_disabled"`); `signed_url_for` without it raises
`RuntimeError`.

!!! note "Signed is not secret"
    Everyone holding the link can read its parameters. Keep TTLs short,
    serve `Referrer-Policy: no-referrer` (`lauren-middlewares` security
    headers) and do not log query strings of signed routes.

## Testing

- `TestClient(app, base_url="https://api.example.com")` sets scheme and host;
  `root_path=` sets the ASGI root path.
- `TestClient(app, client=("10.0.0.5", 4000))` sets the peer address, to test
  trusted and untrusted proxies (`client=None` is a Unix-socket peer).
- `UrlSigningConfig(clock=fake)` expires links deterministically.
- `TestClient` does not percent-decode the request path (ASGI servers do), so
  a path parameter containing encoded characters reaches the handler encoded
  in tests; keep test parameters to unreserved characters.

## Errors

| Error | When |
|---|---|
| `DecoratorUsageError` (`reason="invalid_route_name"`) | malformed `name=` on a route, controller, module or gateway |
| `RouterConflictError` (`reason="duplicate_route_name"`) | two routes share an explicit name |
| `StartupError` (`reason="invalid_proxy_config"` / `"invalid_public_base_url"` / `"invalid_url_signing_config"` / `"url_signing_disabled"`) | invalid configuration, `ValidSignature` without `url_signing=` |
| `ForbiddenError` (403, `detail["reason"]`) | missing, invalid or expired signature |
| `LookupError` / `ValueError` | unknown route / wrong parameters while building a URL (a bug: logged 500) |
