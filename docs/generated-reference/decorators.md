# Decorators

All user-facing class and method decorators exported by the framework.

## Module system

### `module`

```python
def module(args: Any = (), controllers: list[type] | None = None, providers: list[type] | None = None, imports: list[type | ForwardRef | str] | None = None, exports: list[type] | None = None) -> Callable[[C], C]
```

Declare a module boundary.

Must be invoked with parentheses: ``@module()`` at minimum. The bare
form ``@module`` is rejected because it is ambiguous (Python would
pass the decorated class where configuration is expected).

### `controller`

```python
def controller(prefix: str = '', tags: list[str] | None = None, summary: str | None = None, description: str | None = None, deprecated: bool = False, security: list[dict[str, Any]] | None = None) -> Callable[[C], C]
```

Declare a controller class.

Subclassing does **not** make a class a controller. Every class that
should be routed must be decorated explicitly::

    @controller("/base")
    class Base: ...

    class NotAController(Base):
        pass  # subclass is *not* a controller unless re-decorated

    @controller("/derived")  # required: explicit opt-in
    class Derived(Base): ...

A :class:`MetadataInheritanceError` is raised at startup if a class
appears in a module's ``controllers`` list without its own ``@controller``
decoration.

``@controller`` must always be invoked with parentheses. Writing bare
``@controller`` on a class is rejected with :class:`DecoratorUsageError`
because it would otherwise silently bind the class as the URL prefix.

### `injectable`

```python
def injectable(args: Any = (), scope: Scope = Scope.SINGLETON, provides: list[type] | None = None, multi: bool = False) -> Callable[[_T], _T]
```

Mark a class or function as a DI provider.

**Class form**::

    @injectable()
    class UserRepo:
        sess: Depends[make_session]   # class-body field injection
        def __init__(self, cfg: ConfigService): ...

**Function form** — the decorated function is the factory; its return
value is the dependency. The function's own parameters are resolved
through DI exactly like a class constructor's::

    @injectable()
    def make_session(cfg: ConfigService) -> AsyncSessionmaker:
        return AsyncSessionmaker(cfg.db_url)

Other consumers depend on the function by referencing it directly
(``Depends[make_session]`` or an annotated ``sess: Depends[make_session]``
field).

Must be invoked with parentheses: ``@injectable()`` or
``@injectable(scope=Scope.REQUEST)``. The bare form ``@injectable``
is rejected because it hides intent and would quietly bind the
decorated object as a positional configuration argument.

## HTTP route decorators

### `get`

### `post`

### `put`

### `patch`

### `delete`

### `head`

### `options`

## Middleware & Guards

### `middleware`

```python
def middleware(args: Any = ()) -> Callable[[C], C]
```

Mark a class as a middleware provider.

Must be invoked with parentheses: ``@middleware()``.

Usage::

    @middleware()
    class TraceMiddleware:
        async def dispatch(self, request: Request, call_next: CallNext) -> Response:
            request.state.trace = "on"
            return await call_next(request)

The decorated class must define::

    async def dispatch(self, request: Request, call_next: CallNext) -> Response: ...

Middleware is automatically registered as a singleton in the DI container.
To use a narrower scope or inject dependencies, combine with ``@injectable``::

    @middleware()
    @injectable(scope=Scope.REQUEST)
    class AuthMiddleware:
        def __init__(self, repo: UserRepository) -> None: ...

        async def dispatch(self, request, call_next): ...

### `use_middlewares`

```python
def use_middlewares(classes: type | None = ()) -> Callable[[_T], _T]
```

Attach middleware(s) to a controller class or route function.

Works on both:

* **controller classes** — the middleware runs for every handler on the
  class
* **handler methods** — the middleware runs only for that route

Composes cleanly across decoration orders: applying ``@use_middlewares``
multiple times (or on both a class and a method) appends to the chain.

``None`` entries are silently dropped so callers can build the
middleware list inline using conditionals::

    @use_middlewares(
        RequestIdMiddleware,
        TracingMiddleware if settings.tracing_enabled else None,
        AuthMiddleware,
    )
    class MyController: ...

### `use_guards`

```python
def use_guards(classes: type | None = ()) -> Callable[[_T], _T]
```

Attach guards to a controller class or route function.

Works on both:

* **controller classes** — the guards run for every handler on the class
* **handler methods** — the guards run only for that route

Guards from the class and the method are concatenated at dispatch; class
guards always run first. ``@use_guards`` is safe to apply above or below
``@controller`` — both decoration orders work::

    @use_guards(AuthenticatedGuard)
    @controller("/users")
    class A: ...

    @controller("/users")
    @use_guards(AuthenticatedGuard)
    class B: ...

``None`` entries are silently dropped, so conditional guard selection
can be expressed inline::

    @use_guards(
        AuthenticatedGuard,
        AdminGuard if route_is_admin_only else None,
        RateLimitGuard,
    )
    def handler(): ...

### `interceptor`

```python
def interceptor(args: Any = ()) -> Callable[[C], C]
```

Mark a class as an interceptor.

An interceptor runs **after guards** and **before** (and after) the
route handler.  Unlike :func:`middleware`, interceptors receive a full
:class:`~lauren.types.ExecutionContext` (matched route, controller
class, metadata) instead of a bare :class:`~lauren.types.Request`.

The decorated class **must** define::

    async def intercept(
        self,
        context: ExecutionContext,
        call_handler: CallHandler,
    ) -> Any: ...

Interceptors are automatically registered as *singletons* in the DI
container — this mirrors the behaviour of :func:`middleware`.  To use
a narrower scope or inject dependencies, combine with
:func:`injectable`::

    @interceptor()
    @injectable(scope=Scope.REQUEST)
    class CurrentUserInterceptor:
        def __init__(self, repo: UserRepository) -> None:
            self._repo = repo

        async def intercept(self, ctx, call_handler):
            ...

Must be invoked with parentheses: ``@interceptor()``.

Usage::

    @interceptor()
    class LoggingInterceptor:
        async def intercept(
            self, ctx: ExecutionContext, call_handler: CallHandler
        ) -> Any:
            print(f"→ {ctx.route_template}")
            result = await call_handler.handle()
            print(f"← {ctx.route_template}")
            return result

### `use_interceptors`

```python
def use_interceptors(classes: type | None = ()) -> Callable[[_T], _T]
```

Attach interceptors to a controller class or route handler.

Works on both:

* **controller classes** — the interceptors run for every handler on
  the class.
* **handler methods** — the interceptors run only for that route.

Interceptors execute in **declaration order** (outermost → innermost),
which is the same onion model used by middlewares:

* Global interceptors (declared in :func:`~lauren.LaurenFactory.create`)
  are outermost.
* Controller-level interceptors run next.
* Method-level interceptors are innermost.

``None`` entries are silently dropped so callers can use inline
conditionals::

    @use_interceptors(
        LoggingInterceptor,
        CacheInterceptor if caching_enabled else None,
    )
    @controller("/users")
    class UsersController: ...

### `exception_handler`

```python
def exception_handler(exceptions: type[BaseException] = ()) -> Callable[[_T], _T]
```

Mark a class or function as an exception handler.

Like every other decorator in lauren, this *only* attaches metadata to
the decorated entity. Wiring is done at startup by
:class:`LaurenFactory` and at dispatch time by :class:`LaurenApp`.

The decorated entity:

* declares which exception types it handles via the positional tuple;
* is automatically marked **injectable** (singleton scope), so handlers
  participate in DI exactly like guards and middleware — they may take
  ``__init__`` dependencies (class form) or function-parameter
  dependencies (function form).

**Class form** — define ``catch(exc, request) -> Response``::

    @exception_handler(NotFoundError, ConflictError)
    class DomainErrors:
        def __init__(self, log: Logger) -> None:
            self.log = log

        async def catch(self, exc: Exception, request: Request) -> Response:
            self.log.warn(f"domain error: {exc}")
            return Response.json({"error": str(exc)}, status=400)

**Function form** — the function itself is the handler::

    @exception_handler(ValueError)
    async def handle_value_error(exc: ValueError, request: Request) -> Response:
        return Response.json({"detail": str(exc)}, status=422)

Compose handlers onto controllers / routes via
:func:`use_exception_handlers`, or register them globally via
``LaurenFactory.create(global_exception_handlers=[...])``.

``@exception_handler`` must be invoked with at least one exception
type. Bare usage (``@exception_handler``) and empty parentheses
(``@exception_handler()``) are both rejected — a handler with no
exception scope is almost certainly a bug.

### `use_exception_handlers`

```python
def use_exception_handlers(handlers: type | Callable[..., Any] | None = ()) -> Callable[[_T], _T]
```

Attach exception handler(s) to a controller class or route function.

Mirrors :func:`use_guards` / :func:`use_middlewares`:

* **controller classes** — every handler on the class is covered;
* **handler methods** — only that route is covered.

Resolution order at dispatch is **route → controller → global**, with
the first handler whose declared exception tuple matches
``isinstance(raised_exc, tuple)`` winning. Handlers from the same
decoration scope run in the order they were declared.

Decoration order is irrelevant — applying ``@use_exception_handlers``
above or below ``@controller`` / ``@get`` works identically::

    @use_exception_handlers(NotFoundHandler)
    @controller("/items")
    class ItemsController: ...

    @controller("/items")
    @use_exception_handlers(NotFoundHandler)
    class ItemsController: ...

``None`` entries are silently dropped so callers can build the
handler list inline using conditionals (consistent with the rest of
the ``use_*`` family)::

    @use_exception_handlers(
        DomainErrors,
        DebugErrors if settings.debug else None,
    )
    class C: ...

## Lifecycle

### `post_construct`

```python
def post_construct(fn: F) -> F
```

Mark a method to be invoked after DI construction, in topological order.

### `pre_destruct`

```python
def pre_destruct(fn: F) -> F
```

Mark a method to be invoked during shutdown, in reverse topological order.

## Scope

### `Scope`

```python
class Scope
```

DI scope values, ordered from narrowest to widest.

Scopes form a total order on *lifetime width*:

* ``TRANSIENT`` (0) — a fresh instance on every resolution. Narrowest.
* ``REQUEST``   (1) — one instance per in-flight request.
* ``SINGLETON`` (2) — one instance per application. Widest.

The numeric ordering is what the DI compiler uses to detect *scope
narrowing violations* without any bespoke lookup table. A dependent
whose scope value is **greater than** its dependency's scope value
would outlive that dependency and therefore constitutes a violation:

>>> Scope.SINGLETON > Scope.REQUEST    # singleton -> request
True
>>> Scope.REQUEST > Scope.TRANSIENT    # request -> transient
True
>>> Scope.TRANSIENT > Scope.SINGLETON  # transient -> singleton (ok)
False

Prefer :attr:`label` over ``str(scope)`` when producing human-readable
output — it yields the stable lowercase name (``"singleton"``,
``"request"``, ``"transient"``) that tests and logs rely on, and
does not depend on the ``IntEnum`` ``__str__`` formatting which
varies between Python 3.11 and 3.12.

