# Custom Route Handlers

Route handlers are methods on a `@controller` class.  By default they are
**instance methods**, so Lauren resolves a fresh (or cached) controller
instance via DI and passes it as the implicit `self`.  But three other
binding styles are available, and you can wrap handlers in your own
decorators to implement cross-cutting behaviour — feature flags,
environment-conditional implementations, caching, audit logging — as long
as a single rule is respected: **always use `@functools.wraps(f)`** so
that Lauren can find the route marker and the handler's real parameter
list.

---

## Binding styles

### Instance method (default)

The most common style.  `self` is the DI-resolved controller, so you can
declare constructor dependencies as usual.

```python
@injectable()
class UserRepository:
    async def find(self, uid: int) -> dict: ...

@controller("/users")
class UserController:
    def __init__(self, repo: UserRepository) -> None:
        self.repo = repo

    @get("/{id}")
    async def get_user(self, id: int) -> dict:
        return await self.repo.find(id)
```

### `@staticmethod` — no receiver

Use `@staticmethod` when the route does not need access to the controller
instance or class.  No receiver is injected; the handler must accept only
request-level parameters (path, query, body, `Depends`, etc.).

```python
@controller("/health")
class HealthController:
    @get("/")
    @staticmethod
    async def ping() -> dict:
        return {"status": "ok"}

    @get("/version")
    @staticmethod
    def version(app_version: Annotated[str, Inject("APP_VERSION")]) -> dict:
        # Inject is still resolved from DI — only `self` is absent.
        return {"version": app_version}
```

!!! tip "Decorator order"
    Both orderings work:

    ```python
    # @staticmethod outer — preferred for readability
    @staticmethod
    @get("/")
    def handler(): ...

    # @get outer — also fine; Lauren's _merge_markers copies the route
    # marker from the staticmethod descriptor onto the underlying function
    @get("/")
    @staticmethod
    def handler(): ...
    ```

### `@classmethod` — class reference, no instance

Use `@classmethod` when you want access to the controller class itself —
for example, to read class-level constants without resolving an instance,
or to dynamically switch on a class attribute.

```python
@controller("/config")
class ConfigController:
    _env: str = "production"

    @get("/env")
    @classmethod
    async def get_env(cls) -> dict:
        return {"env": cls._env}
```

The DI container still resolves the controller instance (so
`@post_construct` hooks and field injection fire), but `cls` rather than
`self` is passed as the first argument.

---

## Writing your own decorators

Any Python decorator can wrap a route handler.  The only requirement is
that the wrapper must preserve two things:

| What | Why |
|---|---|
| `__dict__` (all `__lauren_*` markers) | Lauren reads `__lauren_route__`, `__lauren_use_guards__`, etc. from the callable at startup.  Without them the handler is invisible. |
| `__wrapped__` chain | `inspect.signature` and `inspect.iscoroutinefunction` follow `__wrapped__` to find the real parameter list and async flag.  Without it, DI injection and sync/async dispatch break. |

`@functools.wraps(f)` sets both with one line.

### Minimal decorator skeleton

```python
import functools

def my_decorator(fn):
    @functools.wraps(fn)          # ← copies __dict__ and sets __wrapped__
    async def wrapper(*args, **kwargs):
        # do something before
        result = await fn(*args, **kwargs)
        # do something after
        return result
    return wrapper
```

Decorate the route handler:

```python
@controller("/orders")
class OrderController:
    @get("/{id}")
    @my_decorator           # outermost decorator runs last
    async def get_order(self, id: int) -> dict: ...
```

!!! warning "What breaks without `@wraps`"
    A decorator that returns a bare function without `@functools.wraps`:

    ```python
    def bad_decorator(fn):
        def wrapper(*args, **kwargs):   # no @wraps!
            return fn(*args, **kwargs)
        return wrapper
    ```

    - **Outer decorator** (`@bad_decorator @get("/")`): the `__lauren_route__`
      marker is lost in the new `wrapper.__dict__`.  The handler is never
      registered — **silent 404**.
    - **Inner decorator** (`@get("/") @bad_decorator`): the marker lands on
      `wrapper`, but `inspect.signature(wrapper) == (*args, **kwargs)`.
      Lauren sees no typed parameters and injects nothing — **runtime 500
      when the handler expects DI arguments**.

### Decorator order rules

```
┌──────────────────────────────────────────────────────────────┐
│  @controller                  (always outermost on the class) │
│  @use_guards(...)             (class-level)                   │
│  ——————————————————————————————————————————————              │
│  @get("/path")                (route marker)                  │
│  @use_guards(...)             (route-level guard)             │
│  @my_decorator(...)           (your decorator, any order)     │
│  def handler(self, ...): ...  (innermost)                     │
└──────────────────────────────────────────────────────────────┘
```

The route decorator (`@get`, `@post`, etc.) and your decorator can be in
**either** relative order as long as every decorator in the chain uses
`@functools.wraps`.  Lauren propagates markers found anywhere in the chain
at startup.

---

## Environment-conditional implementations

A practical use of custom decorators is selecting a handler implementation
at class-body definition time — before the first request arrives.  This
gives you zero per-request overhead compared to an `if` branch inside the
handler body.

### Feature-flagged handler

```python
import os
import functools

def feature(flag: str, fallback):
    """Use the decorated handler if *flag* is set; otherwise use *fallback*."""
    def decorator(fn):
        if os.environ.get(flag):
            return fn             # no wrapper needed — original fn is used
        # Replace with the fallback, but copy markers so the route is still
        # registered under the same path.  Use ``async def`` when ``fn`` is
        # a coroutine function so that ``inspect.iscoroutinefunction`` returns
        # the correct value on all supported Python versions (3.11 does not
        # follow ``__wrapped__`` in ``iscoroutinefunction``).
        import inspect
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def wrapped(*args, **kwargs):
                return await fallback(*args, **kwargs)
        else:
            @functools.wraps(fn)
            def wrapped(*args, **kwargs):
                return fallback(*args, **kwargs)
        return wrapped
    return decorator


async def _experimental_handler(self, id: int) -> dict:
    return {"id": id, "source": "experimental"}

async def _stable_handler(self, id: int) -> dict:
    return {"id": id, "source": "stable"}


@controller("/items")
class ItemController:
    @get("/{id}")
    @feature("USE_EXPERIMENTAL_ITEMS", fallback=_stable_handler)
    async def get_item(self, id: int) -> dict:
        return await _experimental_handler(self, id)
```

When `USE_EXPERIMENTAL_ITEMS` is **not** set, `@feature` returns a
`@functools.wraps`-wrapped `_stable_handler`.  Because `@functools.wraps`
copies the `__lauren_route__` marker from the original `get_item` onto the
wrapper, the route `/items/{id}` is registered correctly pointing at the
stable implementation.

### Environment-conditional implementation chosen at import time

```python
import os

_prod_mode = os.environ.get("APP_ENV", "development") == "production"

@controller("/diagnostics")
class DiagnosticsController:

    if _prod_mode:
        @get("/debug")
        @staticmethod
        async def debug_info() -> dict:
            return {"detail": "disabled in production"}
    else:
        @get("/debug")
        @staticmethod
        async def debug_info() -> dict:   # type: ignore[misc]
            import sys, platform
            return {
                "python": sys.version,
                "platform": platform.platform(),
            }
```

The class body runs once at import time.  The `if/else` picks the
definition that gets attached to the class; no runtime branching is needed.

---

## Custom descriptors (advanced)

Any class that implements `__get__` can act as a route handler descriptor.
Lauren calls `descriptor.__get__(instance, cls)` at dispatch time, so the
descriptor decides how the bound callable is produced.

To be detected by Lauren's startup scanner the descriptor must be:

1. **Callable** — implement `__call__` so that `callable(descriptor)`
   returns `True`.
2. **Properly wrapped** — carry the `__lauren_route__` marker (use
   `functools.update_wrapper(self, fn)` in `__init__`).
3. **Signature-transparent** — set `__wrapped__ = fn` so
   `inspect.signature` follows the chain to the real parameter list.

```python
import functools

class retry_on_error:
    """Descriptor that retries the handler up to *n* times on exception."""

    def __init__(self, fn, *, retries: int = 3) -> None:
        self._fn = fn
        self._retries = retries
        functools.update_wrapper(self, fn)      # sets __wrapped__, copies __dict__

    def __call__(self, *args, **kwargs):
        last_exc: Exception | None = None
        for _ in range(self._retries):
            try:
                return self._fn(*args, **kwargs)
            except Exception as exc:
                last_exc = exc
        raise last_exc  # type: ignore[misc]

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self
        return functools.partial(self, obj)     # bind the instance


def retry(retries: int = 3):
    def decorator(fn):
        return retry_on_error(fn, retries=retries)
    return decorator


@controller("/payments")
class PaymentController:
    @get("/{id}")
    @retry(retries=3)
    async def get_payment(self, id: int) -> dict:
        return await self._payments.fetch(id)
```

Because `retry_on_error` implements `__get__`, Lauren's `__get__`-based
dispatcher calls `descriptor.__get__(controller_instance, PaymentController)`
at request time, which returns a `functools.partial(descriptor, instance)`
— effectively a bound method.

---

## See also

- [Sync vs Async Handlers](sync-handlers.md) — thread-pool dispatch, asyncio safety.
- [Dependency Injection](dependency-injection.md) — scopes, constructor injection, `Depends`.
- [Custom Extractors](custom-extractors.md) — `ExtractionMarker` for adding typed parameters.
- [Interceptors](interceptors.md) — AOP wrappers that run after routing and guards, with full `ExecutionContext` access.
