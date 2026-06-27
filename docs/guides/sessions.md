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
| `secret` | `str \| bytes \| Sequence[...]` | `None` | HMAC signing key(s). **Required.** A list rotates keys (first signs, all verify). |
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

Process-local dict guarded by an `asyncio.Lock`, with lazy TTL expiry. The
default. Fine for development and single-worker deployments — **not** for
multi-worker production (each worker has its own dict).

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

Implement the same Protocol as an injectable singleton and pass it in. Connection
setup/teardown rides Lauren's lifecycle hooks:

```python
from typing import Any, ClassVar
from lauren import Scope, injectable, post_construct, pre_destruct, SessionConfig, use_value, SessionStore
import redis.asyncio as redis

@injectable(scope=Scope.SINGLETON)
class RedisSessionStore:
    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False

    @post_construct
    async def connect(self) -> None:
        self._r = redis.from_url("redis://localhost:6379")

    @pre_destruct
    async def close(self) -> None:
        await self._r.aclose()

    def new_id(self) -> str:
        import secrets
        return secrets.token_urlsafe(32)

    async def load(self, session_id: str) -> dict[str, Any] | None:
        raw = await self._r.get(f"sess:{session_id}")
        import json
        return json.loads(raw) if raw else None

    async def save(self, session_id: str, data: dict[str, Any], *, max_age: int | None) -> None:
        import json
        await self._r.set(f"sess:{session_id}", json.dumps(data), ex=max_age)

    async def delete(self, session_id: str) -> None:
        await self._r.delete(f"sess:{session_id}")
```

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
    A `SignedCookieSessionStore` session cannot be revoked server-side — logout
    relies on the browser honouring the `Max-Age=0` instruction. Use a
    server-side store when you need hard revocation.

---

## Security

| Concern | Default |
|---|---|
| Forgery | HMAC-SHA256 signature, constant-time verify |
| XSS theft | `HttpOnly` on |
| Plaintext transport | `Secure` on |
| CSRF | `SameSite=Lax` (pair with `lauren-guards.csrf` for state-changing flows) |
| Fixation | `regenerate_id()` at login |
| Stale sessions | absolute `max_age` + optional `idle_timeout` / `rolling` |
| Secret compromise | rotate by passing `secret=[new, old]` |

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

Lauren's `TestClient` keeps **no cookie jar**, so thread the cookie by hand:
read it from `Set-Cookie` on one response, pass it back via `cookies={...}` on
the next.

```python
from lauren import LaurenFactory, SessionConfig
from lauren.testing import TestClient

def _cookie(resp, name="lauren_session"):
    sc = resp.header("set-cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return None

client = TestClient(LaurenFactory.create(AppModule, sessions=SessionConfig(secret="x" * 32)))

r1 = client.get("/account/visits")
assert r1.json() == {"visits": 1}
cookie = _cookie(r1)

r2 = client.get("/account/visits", cookies={"lauren_session": cookie})
assert r2.json() == {"visits": 2}
```

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
