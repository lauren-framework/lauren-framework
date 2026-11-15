# Decorators

All user-facing class and method decorators exported by the framework.

## Module system

### `module`

```python
def module(args: Any = (), controllers: list[type] | None = None, providers: list[type] | None = None, imports: list[type | ForwardRef | str] | None = None, exports: list[type] | None = None, host: str | None = None, name: str | None = None) -> Callable[[C], C]
```

Declare a module boundary.

Must be invoked with parentheses: ``@module()`` at minimum. The bare
form ``@module`` is rejected because it is ambiguous (Python would
pass the decorated class where configuration is expected).

``host`` (e.g. ``"{tenant}.example.com"``) routes the module's own
controllers and WebSocket gateways by the request's ``Host`` header,
unless a controller declares its own ``host=``; it does not apply to
imported modules.
``name`` (dot-separated identifiers) namespaces the explicit route names
of the module's controllers: ``@module(name="accounts")`` +
``@controller(name="users")`` + ``@get(name="show")`` is
``"accounts.users.show"``.

### `controller`

```python
def controller(prefix: str = '', tags: list[str] | None = None, summary: str | None = None, description: str | None = None, deprecated: bool = False, security: list[dict[str, Any]] | None = None, consumes: Sequence[str] | str | None = None, produces: Sequence[str] | str | None = None, max_body_size: int | None = None, host: str | None = None, name: str | None = None) -> Callable[[C], C]
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

``consumes`` / ``produces`` / ``max_body_size`` are defaults for the
controller's routes (a route's own value wins); ``consumes`` applies to the
routes that read a body. They are validated like the route options.

``host`` (``"admin.example.com"``, ``"{tenant}.example.com"``) restricts
the controller's routes to requests whose ``Host`` matches; ``{name}``
labels are read with :class:`~lauren.extractors.HostParam`.
``name`` (dot-separated identifiers) namespaces the explicit route names
of the controller: ``@controller("/users", name="users")`` with
``@get("/{id}", name="show")`` is the route ``"users.show"``.

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

### `route`

```python
def route(path: str = '', methods: Iterable[str], kwargs: Any = {}) -> Callable[[F], F]
```

Register one handler for several HTTP methods.

Sugar for stacking verb decorators: ``@route("/x", methods=["POST",
"PUT"])`` appends one route per method (stacking accumulates), and every
other keyword is a verb-decorator option (``summary=``,
``status_code=``, …). Method names are validated here
(:class:`DecoratorUsageError`, ``reason="invalid_method"``).

### `any_method`

```python
def any_method(path: str = '', kwargs: Any = {}) -> Callable[[F], F]
```

Register a handler for every HTTP method at ``path`` (proxies, webhook sinks, fallbacks).

An explicit verb handler at the same path wins over it, and a path with
an any-method handler never answers 405. Takes the verb decorators'
options. Method-agnostic routes are left out of the OpenAPI document;
use :func:`route` to document a fixed method list.

## Metadata & composition

### `MetadataKey`

A typed scalar route-metadata key: a decorator and a typed lookup handle.

``IsPublic = MetadataKey[bool]("app.is_public", default=False)`` declares
the key; ``@IsPublic(True)`` attaches a value to a controller, route or
WebSocket gateway; ``ctx.get_metadata(IsPublic)`` reads it back as ``bool``
(the ``default`` when no level set it). Stacking the key twice on one
target with different values raises :class:`DecoratorUsageError` at
decoration time. Across levels (controller ⟵ route) ``merge`` decides:
``"override"`` (most specific wins), ``"outer"`` (outermost wins — pin
security settings), ``"error"`` (conflicting levels fail
``LaurenFactory.create``) or a callable ``(outer, inner) -> value``.
``validate=True`` checks every decorated value against ``T`` at
decoration time; ``public=True`` records future manifest visibility.

### `MetadataSetKey`

A typed collection route-metadata key whose items accumulate.

``Roles = MetadataSetKey[str]("app.roles")`` declares the key;
``@Roles("admin", "auditor")`` adds items; ``ctx.get_metadata(Roles)``
reads ``tuple[str, ...]`` (``()`` when no level set it). Stacked
decorators on one target accumulate (de-duplicated, application order,
like ``@use_guards``). Across levels ``merge="union"`` (default) gives the
ordered union, outer level first; ``"override"`` lets the most specific
level replace the outer items; a callable ``(outer, inner) -> tuple`` is
also accepted.

### `StateKey`

A typed slot in ``request.state`` (and ``app_state`` / ``ws.state``).

``CurrentUser = StateKey[AuthUser]("app.user")`` names the slot. Guards
and middleware write ``ctx.request.state[CurrentUser] = user``; handlers
read ``request.state[CurrentUser]`` or declare a parameter
``user: Annotated[AuthUser, CurrentUser]``, compiled at startup into a
single dict read. Values are stored under ``name``, so string readers
(``request.state.get("app.user")``) interoperate.

A missing value raises ``on_missing`` — :class:`MissingStateError` (500)
by default, or an :class:`HTTPError` subclass / zero-argument factory
(``UnauthorizedError`` for authentication keys). ``require_provider=True``
makes ``LaurenFactory.create`` check that every route injecting the key
has a guard, middleware or interceptor declaring
``@provides_state(key)`` in its chain.

### `set_metadata`

```python
def set_metadata(key: Any, value: Any) -> Callable[[_T], _T]
```

Attach metadata observable from ``ExecutionContext.get_metadata``.

*key* is a string (legacy: the last write on a target wins; a more
specific level replaces a controller's value) or a typed key —
``@set_metadata(Roles, ("admin",))`` is ``@Roles("admin")`` and
``@set_metadata(IsPublic, True)`` is ``@IsPublic(True)``, with the key's
stacking and merge rules. On classes only the class's **own** metadata is
read and extended (strict inheritance); use ``@propagate_metadata(Parent)``
to copy a parent's values deliberately.

### `compose`

Bundle decorators into one reusable decorator.

``compose(a, b)(f)`` is exactly ``a(b(f))`` — the decorators apply as if
stacked in the written order, so accumulation semantics are unchanged.
Every step must return the object it received (Lauren decorators only
attach metadata): a wrapping decorator raises
:class:`DecoratorUsageError` naming it. Each application is recorded on
the target (``lauren.reflect.reflect_composed``)::

    def auth(*roles: str):
        reject_bare_usage("auth", roles[0] if roles else None)
        return compose(use_guards(JwtGuard, RolesGuard), Roles(*roles), name="auth")

### `propagate_metadata`

```python
def propagate_metadata(source: Any, guards: bool = True, interceptors: bool = True, middlewares: bool = True, exception_handlers: bool = True, encoder: bool = True, user_metadata: bool = True) -> Callable[[_T], _T]
```

Copy Lauren decorator metadata from *source* to the decorated target.

Like :func:`functools.wraps` but for Lauren's ``@use_guards``,
``@use_interceptors``, ``@use_middlewares``, ``@use_exception_handlers``,
``@use_encoder``, and ``@set_metadata`` annotations rather than standard
Python dunder attributes.

For **list-based** metadata (guards, interceptors, middlewares, exception
handlers), the source entries are **prepended** before the target's own
existing entries so that propagated behaviour runs as the outermost layer:

.. code-block:: python

    # source declares GuardA; target already has GuardB from @use_guards.
    # After propagation the effective order is [GuardA, GuardB].

    @propagate_metadata(source)     # applied last — GuardA prepended
    @use_guards(GuardB)             # applied first — GuardB in dict
    @controller("/x")
    class Target: ...

For **encoder** metadata, the source encoder is copied only when the target
has no encoder of its own (target's explicit ``@use_encoder`` wins).

For **user metadata** (``@set_metadata`` key/value pairs and typed keys),
the source values are merged in and the target's existing values take
precedence — except :class:`~lauren.MetadataSetKey` items, which are
unioned (source items first).

Use ``guards=False``, ``interceptors=False``, etc. to suppress individual
categories::

    @propagate_metadata(BaseController, interceptors=False)
    @controller("/derived")
    class DerivedController: ...

*source* may be any class or callable decorated with Lauren's ``@use_*``
family.  It does not need to be a ``@controller`` — plain classes and
functions work as sources.

:param source: The class or callable whose metadata is copied.
:param guards: Propagate ``@use_guards`` entries.
:param interceptors: Propagate ``@use_interceptors`` entries.
:param middlewares: Propagate ``@use_middlewares`` entries.
:param exception_handlers: Propagate ``@use_exception_handlers`` entries.
:param encoder: Propagate ``@use_encoder`` (only when target has none).
:param user_metadata: Propagate ``@set_metadata`` key/value pairs (target
    keys win on conflict).

### `provides_state`

Declare that a guard, middleware or interceptor class sets these state keys.

Pure metadata (own ``__dict__``; a subclass must re-declare it). Stacking
accumulates. ``LaurenFactory.create`` uses it to check routes that inject
a ``StateKey(..., require_provider=True)``::

    @provides_state(CurrentUser)
    class JwtGuard:
        async def can_activate(self, ctx) -> bool:
            ctx.request.state[CurrentUser] = AuthUser(...)
            return True

### `reject_bare_usage`

Raise if a configurable decorator was used without parentheses.

When a user writes ``@controller`` instead of ``@controller()`` Python
passes the decorated class or function straight to the decorator's
factory as its first positional argument, which almost always produces
a confusing downstream error (or, worse, silently "succeeds" and
creates a broken registration). Call this with the factory's first
argument to surface the mistake at decoration time::

    def auth(*roles: str):
        reject_bare_usage("auth", roles[0] if roles else None)
        return compose(use_guards(JwtGuard), Roles(*roles), name="auth")

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
