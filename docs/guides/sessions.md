# Sessions

Persist per-client state across requests — a shopping cart, a CSRF token, a
flash message, or the authenticated user id — behind a signed cookie. Lauren's
sessions are **first-class and secure by default**: one factory kwarg enables
them, handlers receive a mutable `session: Session`, and any unsafe
configuration fails inside `LaurenFactory.create(...)`, never at runtime.

---

## Minimal example

```python
from lauren import LaurenFactory, SessionConfig, Session, controller, get, post, module

@controller("/account")
class AccountController:
    @get("/visits")
    async def visits(self, session: Session) -> dict:
        session["visits"] = session.get("visits", 0) + 1   # marks the session modified
        return {"visits": session["visits"]}

@module(controllers=[AccountController])
class AppModule:
    pass

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(secret="a-long-random-secret"),
)
```

Declare `session: Session` as a handler parameter. Lauren detects this at
compile time — exactly like `request: Request` or `tasks: BackgroundTasks` — and
injects a per-request session at **zero per-request reflection cost**. The
session engine loads it from the cookie before your handler runs and persists it
(setting the `Set-Cookie` header) after the response is built.

The same value is available as `request.state.session` for middleware, guards,
and interceptors that hold only a `Request`.

---

## SessionConfig

`SessionConfig` is the single declaration point, passed as the `sessions=` kwarg
to `LaurenFactory.create` (or the `Lauren(sessions=...)` constructor).

| Field | Type | Default | Purpose |
|---|---|---|---|
| `secret` | `str \| bytes \| Secret[...] \| Sequence[...]` | `None` | HMAC signing key(s). **Required.** A list rotates keys (first signs, all verify). A [`Secret`](configuration.md#secrets) from typed configuration is unwrapped internally. |
| `store` | `SessionStore` | `InMemorySessionStore()` | Backend (see [Stores](#stores)). |
| `cookie_name` | `str` | `"lauren_session"` | Cookie name. `__Host-`/`__Secure-` prefixes are validated. |
| `max_age` | `int \| None` | `1209600` | Absolute cookie lifetime + server TTL, in seconds (14 days). |
| `idle_timeout` | `int \| None` | `None` | Optional sliding server-side TTL. |
| `rolling` | `bool` | `False` | Re-issue the cookie (slide the window) on every request. |
| `path` | `str` | `"/"` | Cookie `Path`. |
| `domain` | `str \| None` | `None` | Cookie `Domain`. |
| `secure` | `bool` | `True` | `Secure` attribute (HTTPS only). |
| `http_only` | `bool` | `True` | `HttpOnly` attribute (no JS access). |
| `same_site` | `str` | `"lax"` | `"lax"`, `"strict"`, or `"none"`. |
| `serializer` | `SessionSerializer` | JSON | Payload codec for the cookie store. |
| `revocation_store` | `RevocationStore \| None` | `None` | Opt in to [revocation](#revocation) (deny-list + per-user cutoff). |
| `user_id_key` | `str` | `"user_id"` | Session key holding the user id, for per-user revocation. |

Validation runs at startup and raises [`SessionConfigError`](#what-fails-at-startup):

- `same_site="none"` requires `secure=True`.
- A `__Host-` cookie requires `secure=True`, `path="/"`, and `domain=None`; a
  `__Secure-` cookie requires `secure=True`.
- `secret` must be non-empty (the cookie is always signed).
- `max_age` / `idle_timeout` must be positive or `None`.

---

## The Session object

`Session` is a mutable `MutableMapping[str, Any]` — use it like a `dict`:

```python
session["cart"] = ["book", "pen"]
items = session.get("cart", [])
session.setdefault("seen", 0)
session.pop("flash", None)
del session["temp"]
session.update(theme="dark")
"cart" in session
len(session)
session.as_dict()          # a shallow copy
```

It also carries identity and lifecycle:

| Member | Meaning |
|---|---|
| `session.id` | Opaque server-side id (`""` for the cookie store before first save). |
| `session.is_new` | `True` when no valid session cookie was presented. |
| `session.is_modified` | `True` after any mutation, regeneration, or invalidation. |
| `session.is_invalidated` | `True` after `invalidate()`. |
| `session.regenerate_id()` | Issue a fresh id, keeping the data — see [Login / logout](#login-logout-and-session-fixation). |
| `session.invalidate()` | Clear the session, delete the server row, expire the cookie. |

### Pay-for-what-you-touch persistence

Persistence is **dirty-tracked**. The engine writes to the store and re-emits the
cookie only when the session is new-with-content, modified, regenerated,
invalidated, or (under `rolling=True`) refreshed. A handler that only *reads* the
session costs no store write and emits no `Set-Cookie`:

```python
@get("/whoami")
async def whoami(self, session: Session) -> dict:
    return {"user": session.get("user_id")}   # pure read → no Set-Cookie
```

---

## Stores

A `SessionStore` is the pluggable backend. Two ship in core; production
multi-worker deployments implement the Protocol over Redis or a database.

```python
class SessionStore(Protocol):
    requires_secret: ClassVar[bool]
    client_side: ClassVar[bool]
    async def load(self, session_id: str) -> dict | None: ...
    async def save(self, session_id: str, data: dict, *, max_age: int | None) -> None: ...
    async def delete(self, session_id: str) -> None: ...
    def new_id(self) -> str: ...
```

### `InMemorySessionStore`

The default: process-local and **bounded**. Sessions expire after `max_age`
(checked on load and swept proactively, so a session written once and never
revisited — an anonymous visitor, a bot — does not linger), and at most
`max_entries` (default 100 000) are kept, least recently used dropped first.
`InMemorySessionStore(max_entries=..., sweep_interval=60.0, clock=None)`; the
`clock` exists for tests. Fine for development and single-worker deployments —
**not** for multi-worker production (each worker has its own map).

### `SignedCookieSessionStore`

Stateless: there is no server-side row. The whole session payload is serialised,
signed, and stored **in the cookie**. Great when you have no shared store and the
payload is small.

```python
from lauren import SignedCookieSessionStore

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(secret="…", store=SignedCookieSessionStore(max_bytes=4096)),
)
```

!!! warning "Signed, not encrypted"
    The cookie is **tamper-proof** (a client cannot forge it without the secret)
    but **not encrypted** — the client can read it. Never put confidential data
    in the cookie store, and keep payloads small (`max_bytes`, default 4 KB; an
    over-size payload raises `ValueError`).

### A Redis backend (recipe)

Implement the same Protocol and pass an **instance** via
`SessionConfig(store=...)`. Lauren registers that instance as a
lifecycle-managed singleton, so its hooks run with the app: `@post_construct`
at `app.startup()` and `@pre_destruct` at `app.shutdown()` (a store without
those markers may define `async def startup(self)` / `async def aclose(self)`
instead). Open the connection there:

```python
from typing import Any, ClassVar
import json
import secrets

import redis.asyncio as redis

from lauren import post_construct, pre_destruct


class RedisSessionStore:
    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False

    def __init__(self, url: str = "redis://localhost:6379") -> None:
        self._url = url
        self._r: redis.Redis | None = None

    @post_construct
    async def connect(self) -> None:          # runs at app.startup()
        self._r = redis.from_url(self._url)
        await self._r.ping()

    @pre_destruct
    async def close(self) -> None:            # runs at app.shutdown()
        if self._r is not None:
            await self._r.aclose()

    def new_id(self) -> str:
        return secrets.token_urlsafe(32)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._r.get(f"sess:{session_id}")
        return json.loads(raw) if raw else None

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        await self._r.set(f"sess:{session_id}", json.dumps(data), ex=max_age)

    async def delete(self, session_id: str) -> None:
        await self._r.delete(f"sess:{session_id}")


app = LaurenFactory.create(AppModule, sessions=SessionConfig(secret=SECRET, store=RedisSessionStore()))
```

The same rule applies to `revocation_store=`. Each object's hooks run once, even
if you also register it elsewhere. (A plain `use_value(provide=..., value=...)`
registration still never runs hooks.)

Because the store is registered as a global provider, any service can inject
`store: SessionStore` directly — for example an admin endpoint that revokes a
user's session.

---

## Login, logout, and session fixation

At a privilege change (login), call `regenerate_id()` **before** writing the new
identity. This issues a fresh session id, deletes the old server-side row, and
re-sends the cookie — the canonical defence against session fixation:

```python
@post("/login")
async def login(self, session: Session, body: Json[Credentials]) -> dict:
    user = await self._auth.verify(body)
    session.regenerate_id()          # fixation defence
    session["user_id"] = user.id
    return {"ok": True}

@post("/logout")
async def logout(self, session: Session) -> dict:
    session.invalidate()             # drop server row + expire cookie (Max-Age=0)
    return {"ok": True}
```

!!! note "Cookie-store revocation"
    `invalidate()` truly revokes a **server-side** session (the row is deleted).
    A bare `SignedCookieSessionStore` session cannot be revoked server-side —
    logout there relies on the browser honouring `Max-Age=0`, so a replayed
    cookie still validates until it expires. To make the cookie store revocable,
    add a [`revocation_store`](#revocation).

---

## Revocation

Stateless cookie sessions have a well-known limitation: there is no server-side
record, so a logged-out (or stolen) cookie can be replayed until it expires —
exactly like a JWT. Opt in to a `RevocationStore` to fix this. It stays **off by
default**, so the cookie store remains truly stateless unless you ask for
revocation; when enabled, only *revoked* tokens/users are stored, each
self-pruning at the cookie's natural expiry.

```python
from lauren import LaurenFactory, SessionConfig, SignedCookieSessionStore, InMemoryRevocationStore

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(
        secret="…",
        store=SignedCookieSessionStore(),
        revocation_store=InMemoryRevocationStore(),   # opt in
        max_age=3600,                                 # finite lifetime required
    ),
)
```

Two mechanisms come on together:

**Per-session deny-list (automatic).** `session.invalidate()` (logout) and
`session.regenerate_id()` (login) record the prior cookie's token id; a replayed
cookie carrying it is rejected on load. This is the cookie store's equivalent of
deleting a server-side row — logout now truly revokes.

**Per-user "log out everywhere" (you call it).** Inject the store and stamp a
cutoff for the user; any session minted before it is rejected — across every
device, and for server-side stores too. Ideal for "log out all devices" and
force-logout-on-password-change:

```python
from lauren import RevocationStore

@post("/logout-everywhere")
async def logout_everywhere(self, session: Session, revocation: RevocationStore) -> dict:
    await revocation.revoke_user(session["user_id"])   # revokes all OTHER sessions
    session.regenerate_id()                            # keep THIS device logged in
    return {"ok": True}
```

The default `InMemoryRevocationStore` is dev/single-worker: TTL-pruned and
bounded by `max_entries` (default 100 000). An entry evicted to make room is no
longer enforced — size `max_entries` above the revocations you expect within
one `max_age`; a warning is logged the first time a live entry is evicted. For
multi-worker production implement the `RevocationStore` Protocol over Redis (a
shared blocklist is the whole point). Revocation requires a finite `max_age` (or
`idle_timeout`) so entries can self-prune — `SessionConfigError` at startup
otherwise.

!!! note "Cost"
    Revocation trades a little of the cookie store's statelessness for true
    logout: each authenticated request does one small deny-list / cutoff lookup.
    That is still far cheaper than a full session store — active sessions read
    their data from the cookie, and server state stays bounded to "revoked within
    the last `max_age`."

---

## Security

| Concern | Default |
|---|---|
| Forgery | HMAC-SHA256 signature, constant-time verify |
| XSS theft | `HttpOnly` on |
| Plaintext transport | `Secure` on |
| CSRF | `SameSite=Lax`; JSON routes log mismatched `Content-Type`s (`strict_content_type="warn"`) — set `True` or pair with `lauren-guards.csrf` |
| Fixation | `regenerate_id()` at login |
| Stale sessions | absolute `max_age` + optional `idle_timeout` / `rolling` |
| Secret compromise | rotate by passing `secret=[new, old]` |

### CSRF and JSON routes

A cookie session turns every state-changing route into a CSRF target. A JSON
body is parsed regardless of `Content-Type` unless you opt in, and a cross-site
HTML form or a `fetch(..., {mode: "no-cors"})` can send `text/plain` **without a
CORS preflight** — the browser attaches the session cookie (`SameSite=Lax` stops
cross-*site* subresource requests, but not same-site attackers such as a taken-over
subdomain, nor `SameSite=None` cookies). Close the gap by requiring a JSON
`Content-Type`:

```python
from lauren import ContentConfig, LaurenFactory, SessionConfig

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(secret=SECRET),
    content=ContentConfig(strict_content_type=True),   # text/plain JSON → 415
)
```

With `strict_content_type=True`, a `Json[T]` (or implicit model) body accepts
only `application/json` / `application/*+json`; anything else — including a
body announced by `Content-Length` or `Transfer-Encoding` without a
`Content-Type` — is rejected with **415**
`UnsupportedMediaTypeError` before the body is read. A browser can only send
`application/json` cross-origin after a CORS preflight, which your CORS policy
controls.

Until you choose, session-enabled apps run in `"warn"` mode: mismatched
requests are accepted and logged once per route and media type, and
`LaurenFactory.create` logs one startup warning listing the JSON `POST` routes
(PUT/PATCH/DELETE need a CORS preflight cross-site, so CORS already guards them). Setting `strict_content_type` explicitly — `True`, `"warn"`, or `False`
(for example after installing `lauren-guards.csrf`) — silences the startup
warning. Form-encoded routes still need CSRF tokens: use `lauren-guards.csrf`.

### What fails at startup

`SessionConfigError` (a `StartupError`) is raised by `LaurenFactory.create` for
any unsafe configuration — and for the inverse mistake of injecting a `Session`
into an app that never enabled sessions:

```python
@get("/")
async def h(self, session: Session) -> dict:   # no sessions=SessionConfig(...) →
    return {}                                   # SessionConfigError at startup
```

---

## Testing sessions

A lifespan-managed `TestClient` keeps a browser-like **cookie jar**: the session
cookie a response sets is replayed on the next request, `Max-Age=0` (logout)
removes it, and the default `secure=True` cookie works on the default
`http://testserver` base URL (browsers treat test hosts as secure contexts).

```python
from lauren import LaurenFactory, SessionConfig
from lauren.testing import TestClient

app = LaurenFactory.create(AppModule, sessions=SessionConfig(secret="x" * 32))

with TestClient(app) as client:
    assert client.get("/account/visits").json() == {"visits": 1}
    assert client.get("/account/visits").json() == {"visits": 2}   # cookie replayed

    other = client.new_session()              # a second browser with an empty jar
    assert other.get("/account/visits").json() == {"visits": 1}

    client.set_session({"visits": 41})        # a valid signed cookie, issued by the app's engine
    assert client.get("/account/visits").json() == {"visits": 42}
```

`client.cookies.get("lauren_session")` reads the stored value; pass
`cookies={...}` to override a cookie for a single request (tampering tests). A
bare `TestClient(app)` — not used with `with` — keeps the 1.x behaviour of not
storing `Set-Cookie` (opt in with `persist_cookies=True`). See the
[Testing guide](testing.md#cookies-and-sessions).

---

## Comparison with FastAPI / Starlette sessions

| | Lauren | Starlette `SessionMiddleware` |
|---|---|---|
| Enablement | `sessions=SessionConfig(...)` factory kwarg | `app.add_middleware(SessionMiddleware, ...)` |
| Handler access | `session: Session` (typed, zero-cost) | `request.session` (dict on `request`) |
| Storage | pluggable `SessionStore` (server-side or cookie) | cookie-only (signed) |
| Signing | HMAC, multi-key rotation | `itsdangerous` |
| Dirty tracking | ✅ (no write/cookie on pure reads) | ❌ (re-signs every request) |
| Startup validation | ✅ (unsafe config fails at `create()`) | ❌ |
| Fixation helper | ✅ `regenerate_id()` | ❌ (manual) |

---

## Relationship to `lauren-guards`

Core `lauren.Session` is the general-purpose **session-state** mechanism. The
companion `lauren-guards` package's `session_cookie` **guard** is an
*authentication* layer that verifies a session and populates
`request.state.user`. They compose. Don't confuse `lauren.Session` (a mutable
request handle) with `lauren_guards.Session` (a frozen auth record).

---

## Non-goals

- **Encryption** of cookie payloads — the cookie store *signs* but does not
  *encrypt*. Use a server-side store for confidential data.
- **A bundled Redis backend** — Lauren core has no third-party runtime deps; the
  Redis store above is a recipe.
- **WebSocket session writes** — a gateway may read the session off the upgrade
  cookie, but there is no response frame to attach a `Set-Cookie` to.
