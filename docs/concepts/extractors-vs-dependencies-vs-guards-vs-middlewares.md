# Extractors vs Dependencies vs Guards vs Middlewares

Lauren gives you four tools for acting on a request before (or alongside) a handler. They look similar on the surface but solve distinct problems. This page explains the differences and helps you choose the right tool.

## Quick summary

| | Extractor | Dependency | Guard | Middleware |
|---|---|---|---|---|
| **Primary job** | Parse a value from the request | Provide a service | Allow or deny | Wrap the request/response cycle |
| **Result** | A typed value injected into the handler | A service instance injected into the handler | `True` (proceed) or `False`/exception (reject) | Calls `call_next` or returns a response |
| **Where declared** | On a handler parameter's type annotation | On a handler parameter's type annotation | On the controller or handler via `@use_guards` | On the module, controller, or handler |
| **Runs** | At handler parameter resolution | At handler parameter resolution | Before the handler runs (after routing) | Around the whole request/response cycle |
| **Has access to `ExecutionContext`** | Yes — first arg to `extract()` | No | Yes — first arg to `can_activate()` | No (sees raw `Request`) |
| **Stops the request?** | Yes — raise an `HTTPError` | No (raises a DI error) | Yes — return `False` or raise an `HTTPError` | Yes — return a `Response` instead of calling `call_next` |

---

## Extractors

An **extractor** pulls a typed value out of the request and hands it directly to the handler as a parameter. The handler never sees the raw request: it just receives a `User`, a `Tenant`, a `Cursor`, or whatever domain object the extractor produced.

```python
class CurrentUser(ExtractionMarker):
    source = "app.current_user"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> User:
        uid = execution_context.request.state.get("user_id")
        if uid is None:
            raise UnauthorizedError("not authenticated")
        ...

@controller("/profile")
class ProfileController:
    @get("/")
    async def get(self, user: CurrentUser) -> dict:
        return {"id": user.id}  # handler only sees a User, not a Request
```

**Use an extractor when:**

- You want to convert a request value (header, path param, session data) into a domain object.
- The same parsing/validation logic is repeated across multiple handlers.
- You want the handler's signature to document what it needs in domain terms, not HTTP terms.

**Extractors run** at parameter resolution time, alongside the built-in extractors (`Path`, `Query`, etc.). They are co-equal with the DI system at that stage.

---

## Dependencies (`Depends`)

A **dependency** provides a *service* rather than a *value*. It does not read the request at all — it wires up an object (a repository, a cache client, a config object) that the handler needs to do its job.

```python
from lauren.extractors import Depends

@controller("/posts")
class PostController:
    @get("/{id}")
    async def get(self, id: Path[int], repo: Depends[PostRepository]) -> dict:
        post = await repo.find(id)
        ...
```

Under the hood, `Depends[X]` is sugar for asking the DI container to resolve `X`. Services declared with `@injectable` in the module's `providers` list are eligible.

**Use `Depends` when:**

- You need a service (repository, mailer, cache) inside a single handler that you don't want to put in the controller's constructor.
- You want to vary the service per-request (e.g. a transient connection or a per-user cache scope).

**Contrast with extractors:** both inject something into the handler, but an extractor reads the *request* to produce a *domain value*. `Depends` asks the *DI container* to produce a *service*. If what you need is "resolve the current user from the bearer token in the header", that's an extractor. If what you need is "give me a database session", that's `Depends`.

---

## Guards

A **guard** decides whether the request is allowed to proceed. It does not return a value to the handler — it either permits or rejects the request.

```python
from lauren import CanActivate, ExecutionContext

class AuthGuard(CanActivate):
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        token = ctx.request.headers.get("authorization")
        if not token:
            raise UnauthorizedError("missing token")
        return True   # permit

@controller("/admin")
@use_guards(AuthGuard)
class AdminController:
    @get("/")
    async def dashboard(self) -> dict: ...
```

Guards run **before handler parameter resolution** (including extractors). If a guard rejects the request, no extractor or handler code runs.

**Use a guard when:**

- You need to globally allow or deny access based on authentication, roles, IP allow-lists, rate limits, or CSRF tokens.
- The decision is binary (proceed / reject), not "produce a value for the handler".
- You want the policy attached to a controller or handler rather than wired through individual parameters.

**Contrast with extractors:** an extractor that raises `UnauthorizedError` effectively acts as a guard for the parameter it's attached to. The practical difference is that a guard runs before all parameters are resolved and is declared separately from the handler's parameter list, making it easier to apply globally or to many routes at once.

---

## Middlewares

A **middleware** wraps the entire request/response cycle. It runs before routing (unless declared at the controller or handler level), can inspect or mutate the request and response, and calls `call_next` to continue to the handler.

```python
from lauren import MiddlewareProtocol, Request, Response, CallNext

class TimingMiddleware(MiddlewareProtocol):
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        duration = time.monotonic() - start
        response.headers["x-duration-ms"] = str(int(duration * 1000))
        return response
```

**Use middleware when:**

- You need to act on *every* request regardless of which handler is matched (logging, tracing, CORS, compression, rate limiting at the transport level).
- You need to mutate the **response** — add headers, rewrite the body, catch specific exceptions.
- You need to run *before routing*, e.g. to modify the path before it is matched.
- You need to conditionally short-circuit with a response without even hitting a handler.

**Contrast with guards:** a guard has access to `ExecutionContext` (it knows which handler was matched) and produces a boolean decision. Middleware sees a raw `Request` and calls `call_next` — it has more power (can rewrite the response) but less information (no handler context at entry time).

---

## Decision guide

```
"I need to…"

├── …produce a typed value for a specific handler parameter
│   from request data (header, cookie, session, …)?
│       → Extractor

├── …wire a service into a handler without polluting
│   the controller constructor?
│       → Depends[X]

├── …allow or reject the request based on auth, roles,
│   CSRF, IP, or any other policy?
│       → Guard

└── …wrap every request/response for logging, CORS,
│   compression, tracing, or response mutation?
│       → Middleware
```

### Can they be combined?

Yes. A common pattern is:

1. **Middleware** adds a trace ID to `request.state` on every request.
2. **Guard** checks authentication and rejects unauthenticated requests.
3. **Extractor** reads the authenticated user from the session store and hands it to the handler.
4. **`Depends`** provides a repository that the handler uses to fetch data.

Each layer does one thing, all four work together.
