---
name: building-lauren-sessions
description: Adds signed-cookie sessions to a Lauren app. Covers sessions=SessionConfig(...), session: Session injection, request.state.session, SessionStore backends (InMemorySessionStore, SignedCookieSessionStore, Redis), secure cookie defaults, regenerate_id()/invalidate(), rolling/idle expiry, secret rotation, and the SessionConfigError startup checks. Use when persisting per-client state across requests (carts, flash messages, the authenticated user id).
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Sessions

## Enable sessions

Pass a `SessionConfig` to the factory. The cookie is HMAC-signed; `secret` is
required. Unsafe config fails at startup.

```python
from lauren import LaurenFactory, SessionConfig, InMemorySessionStore

app = LaurenFactory.create(
    AppModule,
    sessions=SessionConfig(
        secret="a-long-random-secret",     # str | bytes | list[...] for rotation
        store=InMemorySessionStore(),       # default if omitted
        max_age=1_209_600,                  # 14 days
        same_site="lax",                    # "lax" | "strict" | "none"
        secure=True, http_only=True,        # secure-by-default
        rolling=False,                      # slide the window on each request
    ),
)
```

The `Lauren` imperative app takes the same `sessions=` kwarg.

## Use the session in a handler

Declare `session: Session`. Lauren injects it at compile time (like
`request: Request`), at zero per-request cost. `request.state.session` is the
non-injected equivalent for middleware/guards.

```python
from lauren import Session, controller, get, post
from lauren.extractors import Json

@controller("/account")
class AccountController:
    @get("/visits")
    async def visits(self, session: Session) -> dict:
        session["visits"] = session.get("visits", 0) + 1   # marks modified → persisted
        return {"visits": session["visits"]}

    @post("/login")
    async def login(self, session: Session, body: Json[Credentials]) -> dict:
        session.regenerate_id()        # session-fixation defence at login
        session["user_id"] = "u-42"
        return {"ok": True}

    @post("/logout")
    async def logout(self, session: Session) -> dict:
        session.invalidate()           # drop server row + expire cookie
        return {"ok": True}
```

`Session` is a `MutableMapping`: `session[k]`, `get`, `setdefault`, `pop`,
`update`, `clear`, `in`, `len`, `as_dict()`. Identity: `id`, `is_new`,
`is_modified`, `is_invalidated`.

## Persistence is dirty-tracked

The session is written back and the cookie re-issued **only** when it is
new-with-content, modified, regenerated, invalidated, or (under `rolling=True`)
refreshed. A pure read costs no store write and no `Set-Cookie`.

## Choose a store

| Store | Use |
|---|---|
| `InMemorySessionStore` | Dev / single-worker. Process-local dict, lazy TTL. |
| `SignedCookieSessionStore(max_bytes=4096)` | Stateless — payload lives in the (signed, not encrypted) cookie. No confidential data; small payloads only. |
| Custom `SessionStore` (Redis/DB) | Multi-worker production. |

A Redis store implements the Protocol and uses lifecycle hooks for the pool:

```python
from typing import Any, ClassVar
from lauren import Scope, injectable, post_construct, pre_destruct

@injectable(scope=Scope.SINGLETON)
class RedisSessionStore:
    requires_secret: ClassVar[bool] = True
    client_side: ClassVar[bool] = False
    @post_construct
    async def connect(self) -> None: self._r = make_redis()
    @pre_destruct
    async def close(self) -> None: await self._r.aclose()
    def new_id(self) -> str:
        import secrets; return secrets.token_urlsafe(32)
    async def load(self, sid): ...
    async def save(self, sid, data, *, max_age): ...
    async def delete(self, sid): ...

app = LaurenFactory.create(AppModule, sessions=SessionConfig(secret="…", store=RedisSessionStore()))
```

The store is registered as a global provider, so a service can inject
`store: SessionStore` directly (e.g. to revoke a session).

## Configuration (startup-validated)

`SessionConfigError` (a `StartupError`) is raised by `LaurenFactory.create` when:

- `same_site="none"` without `secure=True`;
- a `__Host-` cookie without `secure=True` / `path="/"` / `domain=None`, or a
  `__Secure-` cookie without `secure=True`;
- `secret` is empty (the cookie is always signed);
- `max_age` / `idle_timeout` is non-positive;
- a handler injects `Session` but `sessions=` was never passed.

Rotate secrets without logging everyone out: `secret=[new_key, old_key]` — the
first signs, all verify.

## Test sessions

`TestClient` has **no cookie jar** — thread the cookie by hand:

```python
def _cookie(resp, name="lauren_session"):
    sc = resp.header("set-cookie") or ""
    for part in sc.split(";"):
        part = part.strip()
        if part.startswith(f"{name}="):
            return part.split("=", 1)[1]
    return None

r1 = client.get("/account/visits")
sid = _cookie(r1)
r2 = client.get("/account/visits", cookies={"lauren_session": sid})
assert r2.json()["visits"] == 2
```

## Key points

- Always call `session.regenerate_id()` at login / privilege change (fixation).
- `invalidate()` truly revokes a server-side session; a `SignedCookieSessionStore`
  cannot be revoked server-side (logout relies on the browser dropping the cookie).
- The cookie store is **signed, not encrypted** — the client can read it.
- Don't confuse core `lauren.Session` (mutable request handle) with
  `lauren_guards.Session` (a frozen auth record). The `lauren-guards`
  `session_cookie` guard is authentication; core sessions are state. They compose.
- Never hand a live `Session` to `BackgroundTasks.add_task` — capture
  `session.as_dict()` or specific values (it is request-scoped).
