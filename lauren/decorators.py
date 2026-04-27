"""Decorators — the declarative surface of lauren."""

from __future__ import annotations

from typing import Any, Callable, ForwardRef, TypeVar

from ._di import INJECTABLE_META, InjectableMeta
from .exceptions import (
    DecoratorUsageError,
    ExceptionHandlerConfigError,
    GuardConfigError,
    LifecycleConfigError,
    MiddlewareConfigError,
)
from .types import Scope


def _describe(obj: Any) -> str:
    """Return a short human-readable description of an arbitrary value.

    Used in error messages where the value is expected to be a class but
    might be a stray scalar; falling back through ``__name__`` / ``repr``
    keeps the message readable for every input type.
    """
    name = getattr(obj, "__name__", None)
    if name is not None:
        return str(name)
    return repr(obj)


def _reject_bare_usage(decorator_name: str, arg: Any) -> None:
    """Raise if a configurable decorator was used without parentheses.

    When a user writes ``@controller`` instead of ``@controller()`` Python
    passes the decorated class or function straight to the decorator's
    factory as its first positional argument, which almost always produces
    a confusing downstream error (or, worse, silently "succeeds" and
    creates a broken registration). By checking at decoration time we
    surface the mistake immediately with actionable guidance.
    """
    if isinstance(arg, type) or callable(arg):
        raise DecoratorUsageError(
            f"@{decorator_name} must be used with parentheses: write "
            f"'@{decorator_name}()' even when all arguments are default. "
            "The bare form is rejected because it silently binds the "
            "decorated object as the first positional configuration argument.",
            detail={
                "decorator": decorator_name,
                "target": getattr(arg, "__qualname__", repr(arg)),
            },
        )


F = TypeVar("F", bound=Callable[..., Any])
C = TypeVar("C", bound=type)

# Marker attributes (placed on classes / functions).
CONTROLLER_META = "__lauren_controller__"
MODULE_META = "__lauren_module__"
ROUTE_META = "__lauren_route__"
MIDDLEWARE_META = "__lauren_middleware__"
USE_MIDDLEWARE = "__lauren_use_middleware__"
USE_GUARDS = "__lauren_use_guards__"
USE_EXCEPTION_HANDLERS = "__lauren_use_exception_handlers__"
EXCEPTION_HANDLER_META = "__lauren_exception_handler__"
SET_METADATA = "__lauren_metadata__"
POST_CONSTRUCT = "__lauren_post_construct__"
PRE_DESTRUCT = "__lauren_pre_destruct__"
OPENAPI_SECURITY_META = "__lauren_openapi_security__"


# ---------------------------------------------------------------------------
# @injectable
# ---------------------------------------------------------------------------


def injectable(
    *args: Any,
    scope: Scope = Scope.SINGLETON,
    provides: list[type] | None = None,
    multi: bool = False,
) -> Callable[..., Any]:
    """Mark a class or function as a DI provider.

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
    """
    if args:
        _reject_bare_usage("injectable", args[0])

    def decorator(target: Any) -> Any:
        meta = InjectableMeta(
            scope=scope,
            provides=tuple(provides or []),
            multi=multi,
        )
        # Functions don't have a ``__dict__`` the same way classes do
        # (Python allows attribute assignment on plain functions), so
        # setattr works uniformly for both paths.
        try:
            setattr(target, INJECTABLE_META, meta)
        except (AttributeError, TypeError) as exc:  # pragma: no cover
            raise DecoratorUsageError(
                f"@injectable cannot attach metadata to {target!r}"
            ) from exc
        return target

    return decorator


# ---------------------------------------------------------------------------
# @module
# ---------------------------------------------------------------------------


class ModuleMeta:
    def __init__(
        self,
        *,
        controllers: list[type] | None = None,
        providers: list[type] | None = None,
        imports: list[type | ForwardRef | str] | None = None,
        exports: list[type] | None = None,
    ) -> None:
        self.controllers = tuple(controllers or [])
        self.providers = tuple(providers or [])
        # imports may contain ForwardRef/str entries that are resolved lazily
        # during ModuleGraph.compile() once all modules are loaded.
        self.imports: tuple[type | ForwardRef | str, ...] = tuple(imports or [])
        self.exports = tuple(exports or [])


def module(
    *args: Any,
    controllers: list[type] | None = None,
    providers: list[type] | None = None,
    imports: list[type | ForwardRef | str] | None = None,
    exports: list[type] | None = None,
) -> Callable[[C], C]:
    """Declare a module boundary.

    Must be invoked with parentheses: ``@module()`` at minimum. The bare
    form ``@module`` is rejected because it is ambiguous (Python would
    pass the decorated class where configuration is expected).
    """
    if args:
        _reject_bare_usage("module", args[0])

    def decorator(cls: C) -> C:
        setattr(
            cls,
            MODULE_META,
            ModuleMeta(
                controllers=controllers,
                providers=providers,
                imports=imports,
                exports=exports,
            ),
        )
        return cls

    return decorator


# ---------------------------------------------------------------------------
# @controller + HTTP verb decorators
# ---------------------------------------------------------------------------


class ControllerMeta:
    def __init__(
        self,
        *,
        prefix: str = "",
        tags: list[str] | None = None,
        summary: str | None = None,
        description: str | None = None,
        deprecated: bool = False,
        security: list[dict[str, Any]] | None = None,
    ) -> None:
        self.prefix = prefix
        self.tags = tuple(tags or [])
        self.summary = summary
        self.description = description
        self.deprecated = deprecated
        self.security = tuple(security or [])


def controller(
    prefix: str = "",
    *,
    tags: list[str] | None = None,
    summary: str | None = None,
    description: str | None = None,
    deprecated: bool = False,
    security: list[dict[str, Any]] | None = None,
) -> Callable[[C], C]:
    """Declare a controller class.

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
    """
    # ``prefix`` defaults to an empty string, so bare ``@controller`` would
    # pass the decorated class as ``prefix``. Detect and reject.
    if isinstance(prefix, type) or (callable(prefix) and not isinstance(prefix, str)):
        _reject_bare_usage("controller", prefix)

    def decorator(cls: C) -> C:
        setattr(
            cls,
            CONTROLLER_META,
            ControllerMeta(
                prefix=prefix,
                tags=tags,
                summary=summary,
                description=description,
                deprecated=deprecated,
                security=security,
            ),
        )
        # Mark this class as injectable in its OWN __dict__ — never by
        # inheritance. Controllers default to REQUEST scope so they may depend
        # on request-scoped providers (DB sessions, CurrentUser, etc.).
        if INJECTABLE_META not in cls.__dict__:
            setattr(cls, INJECTABLE_META, InjectableMeta(scope=Scope.REQUEST))
        return cls

    return decorator


class RouteMeta:
    def __init__(
        self,
        *,
        method: str,
        path: str,
        summary: str | None = None,
        description: str | None = None,
        response_model: type | None = None,
        responses: dict[int, Any] | None = None,
        deprecated: bool = False,
        operation_id: str | None = None,
        include_in_schema: bool = True,
        tags: list[str] | None = None,
    ) -> None:
        self.method = method.upper()
        self.path = path
        self.summary = summary
        self.description = description
        self.response_model = response_model
        self.responses = dict(responses or {})
        self.deprecated = deprecated
        self.operation_id = operation_id
        self.include_in_schema = include_in_schema
        self.tags = tuple(tags or [])


def _route_decorator(method: str) -> Callable[..., Callable[[F], F]]:
    def factory(
        path: str = "",
        *,
        summary: str | None = None,
        description: str | None = None,
        response_model: type | None = None,
        responses: dict[int, Any] | None = None,
        deprecated: bool = False,
        operation_id: str | None = None,
        include_in_schema: bool = True,
        tags: list[str] | None = None,
    ) -> Callable[[F], F]:
        # Bare usage — ``@get`` on a function — passes the function as
        # ``path``. Reject loudly: the undetected case would silently
        # never register the route.
        if callable(path) and not isinstance(path, str):
            _reject_bare_usage(method.lower(), path)

        def decorator(fn: F) -> F:
            existing: list[RouteMeta] = getattr(fn, ROUTE_META, [])
            existing.append(
                RouteMeta(
                    method=method,
                    path=path,
                    summary=summary,
                    description=description,
                    response_model=response_model,
                    responses=responses,
                    deprecated=deprecated,
                    operation_id=operation_id,
                    include_in_schema=include_in_schema,
                    tags=tags,
                )
            )
            setattr(fn, ROUTE_META, existing)
            return fn

        return decorator

    return factory


get = _route_decorator("GET")
post = _route_decorator("POST")
put = _route_decorator("PUT")
delete = _route_decorator("DELETE")
patch = _route_decorator("PATCH")
head = _route_decorator("HEAD")
options = _route_decorator("OPTIONS")


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


def post_construct(fn: F) -> F:
    """Mark a method to be invoked after DI construction, in topological order."""
    if not callable(fn):
        raise LifecycleConfigError("@post_construct must wrap a method")
    setattr(fn, POST_CONSTRUCT, True)
    return fn


def pre_destruct(fn: F) -> F:
    """Mark a method to be invoked during shutdown, in reverse topological order."""
    if not callable(fn):
        raise LifecycleConfigError("@pre_destruct must wrap a method")
    setattr(fn, PRE_DESTRUCT, True)
    return fn


# ---------------------------------------------------------------------------
# Middleware / Guards / arbitrary metadata
# ---------------------------------------------------------------------------


def middleware(cls: C) -> C:
    """Mark a class as a middleware provider."""
    if not hasattr(cls, "dispatch"):
        raise MiddlewareConfigError(
            f"@middleware class {cls.__name__} must define 'dispatch(request, call_next)'"
        )
    setattr(cls, MIDDLEWARE_META, True)
    if not hasattr(cls, INJECTABLE_META):
        setattr(cls, INJECTABLE_META, InjectableMeta(scope=Scope.SINGLETON))
    return cls


def use_middleware(*classes: type | None) -> Callable[[Any], Any]:
    """Attach middleware(s) to a controller class or route function.

    Works on both:

    * **controller classes** — the middleware runs for every handler on the
      class
    * **handler methods** — the middleware runs only for that route

    Composes cleanly across decoration orders: applying ``@use_middleware``
    multiple times (or on both a class and a method) appends to the chain.

    ``None`` entries are silently dropped so callers can build the
    middleware list inline using conditionals::

        @use_middleware(
            RequestIdMiddleware,
            TracingMiddleware if settings.tracing_enabled else None,
            AuthMiddleware,
        )
        class MyController: ...
    """
    filtered: tuple[type, ...] = tuple(c for c in classes if c is not None)
    for c in filtered:
        if not hasattr(c, "dispatch"):
            raise MiddlewareConfigError(
                f"{_describe(c)} must define 'dispatch(request, call_next)'"
            )

    def decorator(target: Any) -> Any:
        # Read own dict when target is a class so subclasses don't silently
        # share the parent's middleware list.
        if isinstance(target, type):
            existing = list(target.__dict__.get(USE_MIDDLEWARE, []))
        else:
            existing = list(getattr(target, USE_MIDDLEWARE, []))
        existing.extend(filtered)
        try:
            setattr(target, USE_MIDDLEWARE, existing)
        except (AttributeError, TypeError):  # pragma: no cover
            raise MiddlewareConfigError(f"Cannot attach middleware to {target!r}")
        return target

    return decorator


def use_guards(*classes: type | None) -> Callable[[Any], Any]:
    """Attach guards to a controller class or route function.

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
    """
    filtered: tuple[type, ...] = tuple(c for c in classes if c is not None)
    for c in filtered:
        if not hasattr(c, "can_activate"):
            raise GuardConfigError(
                f"{_describe(c)} must define 'can_activate(context)'"
            )

    def decorator(target: Any) -> Any:
        if isinstance(target, type):
            existing = list(target.__dict__.get(USE_GUARDS, []))
        else:
            existing = list(getattr(target, USE_GUARDS, []))
        existing.extend(filtered)
        try:
            setattr(target, USE_GUARDS, existing)
        except (AttributeError, TypeError):  # pragma: no cover
            raise GuardConfigError(f"Cannot attach guards to {target!r}")
        return target

    return decorator


# ---------------------------------------------------------------------------
# @exception_handler / @use_exception_handlers
# ---------------------------------------------------------------------------


class ExceptionHandlerMeta:
    """Marker payload attached by :func:`exception_handler`.

    Stores the tuple of exception types the handler claims responsibility
    for. The dispatcher uses this tuple at request time to pick the first
    handler whose ``isinstance(exc, exceptions)`` test succeeds.
    """

    __slots__ = ("exceptions",)

    def __init__(self, exceptions: tuple[type[BaseException], ...]) -> None:
        self.exceptions = exceptions


def exception_handler(
    *exceptions: type[BaseException],
) -> Callable[[Any], Any]:
    """Mark a class or function as an exception handler.

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
    ``LaurenFactory.create(global_exception_filters=[...])``.

    ``@exception_handler`` must be invoked with at least one exception
    type. Bare usage (``@exception_handler``) and empty parentheses
    (``@exception_handler()``) are both rejected — a handler with no
    exception scope is almost certainly a bug.
    """
    # Bare usage: @exception_handler on a class / function passes the
    # decorated object as the first positional argument.
    if exceptions and not all(isinstance(e, type) for e in exceptions):
        # Non-type entries can mean either bare usage (decorated callable
        # arrives in args[0]) or a genuinely bad argument.
        first = exceptions[0]
        if callable(first) and not isinstance(first, type):
            _reject_bare_usage("exception_handler", first)
        # Otherwise fall through to the type validation below which will
        # produce a clear error.

    if not exceptions:
        raise ExceptionHandlerConfigError(
            "@exception_handler requires at least one exception type. "
            "Write '@exception_handler(MyError, OtherError)' — an empty "
            "handler scope would never fire and is almost always a typo."
        )

    for exc in exceptions:
        if not (isinstance(exc, type) and issubclass(exc, BaseException)):
            raise ExceptionHandlerConfigError(
                f"@exception_handler arguments must be exception classes; "
                f"got {_describe(exc)}.",
                detail={"argument": _describe(exc)},
            )

    captured = tuple(exceptions)

    def decorator(target: Any) -> Any:
        # Class form: must define ``catch``. Function form: target is
        # itself the handler callable.
        if isinstance(target, type):
            if not hasattr(target, "catch"):
                raise ExceptionHandlerConfigError(
                    f"@exception_handler class {target.__name__} must define "
                    "'catch(self, exc, request)' returning a Response.",
                    detail={"class": target.__name__},
                )
            # Singleton-scoped by default so the handler is built once
            # and reused; ``catch(self, ...)`` resolves its dependencies
            # via the class ``__init__`` exactly like middleware and
            # guards. Users that need request-scoped state can override
            # by additionally decorating with @injectable(scope=...).
            if INJECTABLE_META not in target.__dict__:
                setattr(target, INJECTABLE_META, InjectableMeta(scope=Scope.SINGLETON))
        elif callable(target):
            # Function-form handler: it's invoked directly at dispatch
            # time with ``(exc, request)`` — NOT registered as a DI
            # function provider. If the user needs to inject services
            # they should switch to the class form (``__init__``
            # parameters resolve through DI just like guards). This
            # keeps the function-form contract unambiguous: the
            # function's parameters describe the dispatcher's call
            # site, not a DI graph.
            pass
        else:
            raise ExceptionHandlerConfigError(
                f"@exception_handler must wrap a class or function; got {target!r}",
            )
        try:
            setattr(target, EXCEPTION_HANDLER_META, ExceptionHandlerMeta(captured))
        except (AttributeError, TypeError):  # pragma: no cover
            raise ExceptionHandlerConfigError(
                f"Cannot attach exception-handler metadata to {target!r}"
            )
        return target

    return decorator


def use_exception_handlers(
    *handlers: type | Callable[..., Any] | None,
) -> Callable[[Any], Any]:
    """Attach exception handler(s) to a controller class or route function.

    Mirrors :func:`use_guards` / :func:`use_middleware`:

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
    """
    filtered: tuple[Any, ...] = tuple(h for h in handlers if h is not None)
    for h in filtered:
        if not hasattr(h, EXCEPTION_HANDLER_META):
            raise ExceptionHandlerConfigError(
                f"{_describe(h)} is not an @exception_handler. "
                "Decorate it with '@exception_handler(...)' first.",
                detail={"target": _describe(h)},
            )

    def decorator(target: Any) -> Any:
        if isinstance(target, type):
            existing = list(target.__dict__.get(USE_EXCEPTION_HANDLERS, []))
        else:
            existing = list(getattr(target, USE_EXCEPTION_HANDLERS, []))
        existing.extend(filtered)
        try:
            setattr(target, USE_EXCEPTION_HANDLERS, existing)
        except (AttributeError, TypeError):  # pragma: no cover
            raise ExceptionHandlerConfigError(
                f"Cannot attach exception handlers to {target!r}"
            )
        return target

    return decorator


# ---------------------------------------------------------------------------
# @openapi_security
# ---------------------------------------------------------------------------


class OpenAPISecurityMeta:
    """Metadata attached by :func:`openapi_security`.

    Stores the list of OpenAPI security requirement objects declared on a
    guard class.  Each element is a ``dict[str, list[str]]`` mapping a
    security scheme name (as registered in ``securitySchemes``) to its
    required OAuth2 scopes (empty list for Bearer / API-key schemes).

    When multiple requirements are stored on a *single* guard they form an
    **OR** relationship (any one scheme is sufficient).  When multiple
    *different* guards each carry security metadata, the OpenAPI generator
    applies **AND** semantics: all guards must be satisfied simultaneously,
    so their requirements are merged into a single requirement object.
    """

    __slots__ = ("requirements",)

    def __init__(self, requirements: list[dict[str, list[str]]]) -> None:
        self.requirements = requirements


def openapi_security(
    *requirements: dict[str, list[str]],
) -> Callable[[C], C]:
    """Attach OpenAPI 3.1 security requirements to a guard class.

    Use this decorator alongside ``@use_guards`` to tell the OpenAPI
    generator which security scheme(s) protect every route guarded by this
    class.  The guard's ``can_activate`` logic is **not** affected — the
    decorator only adds metadata used during schema generation.

    **Basic usage — single scheme** (Bearer token)::

        @openapi_security({"BearerAuth": []})
        class JwtGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                ...

        @use_guards(JwtGuard)
        @controller("/secure")
        class SecureController: ...

    **OR semantics — multiple schemes on one guard**

    Multiple requirements on the *same* guard mean *any* scheme is
    acceptable (OpenAPI OR)::

        @openapi_security({"BearerAuth": []}, {"ApiKey": []})
        class FlexibleAuthGuard:
            async def can_activate(self, ctx: ExecutionContext) -> bool:
                ...

    The generated ``security`` field for operations guarded by
    ``FlexibleAuthGuard`` will be ``[{"BearerAuth": []}, {"ApiKey": []}]``.

    **AND semantics — multiple guards**

    When multiple guards *each* carry ``@openapi_security``, the generator
    merges them into a single requirement object (OpenAPI AND — all schemes
    must be present)::

        @openapi_security({"BearerAuth": []})
        class AuthGuard: ...

        @openapi_security({"TenantHeader": []})
        class TenantGuard: ...

        @use_guards(AuthGuard, TenantGuard)
        @controller("/tenant-api")
        class TenantController: ...
        # → security: [{"BearerAuth": [], "TenantHeader": []}]

    **Explicit override**

    If the ``@controller`` decorator already declares ``security=[...]``
    explicitly, that value takes precedence and guard-derived security is
    ignored for that controller.

    **Registering the scheme**

    You must still register the scheme in the OpenAPI components.  Pass
    ``openapi_security_schemes`` to :func:`~lauren.LaurenFactory.create`::

        app = await LaurenFactory.create(
            AppModule,
            openapi_security_schemes={
                "BearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "JWT",
                },
            },
        )

    ``@openapi_security`` must be invoked with at least one requirement dict
    and must decorate a class.  Bare usage (``@openapi_security``) and empty
    parentheses (``@openapi_security()``) are both rejected.
    """
    # Bare usage: @openapi_security on a class passes the class as
    # requirements[0].
    if requirements and isinstance(requirements[0], type):
        _reject_bare_usage("openapi_security", requirements[0])

    if not requirements:
        raise GuardConfigError(
            "@openapi_security requires at least one security requirement dict. "
            "Write '@openapi_security({\"BearerAuth\": []})' — an empty "
            "requirement list would produce no security entry in the OpenAPI "
            "schema and is almost certainly a typo."
        )

    for req in requirements:
        if not isinstance(req, dict):
            raise GuardConfigError(
                f"@openapi_security arguments must be dicts mapping a scheme "
                f'name to its scopes (e.g. {{"BearerAuth": []}}); '
                f"got {_describe(req)!r}."
            )

    captured: list[dict[str, list[str]]] = list(requirements)  # type: ignore[arg-type]

    def decorator(cls: C) -> C:
        if not isinstance(cls, type):
            raise GuardConfigError(
                f"@openapi_security must decorate a class; got {cls!r}."
            )
        setattr(cls, OPENAPI_SECURITY_META, OpenAPISecurityMeta(captured))
        return cls

    return decorator


def set_metadata(key: str, value: Any) -> Callable[[Any], Any]:
    """Attach free-form metadata observable from ``ExecutionContext``."""

    def decorator(target: Any) -> Any:
        existing: dict[str, Any] = dict(getattr(target, SET_METADATA, {}))
        existing[key] = value
        setattr(target, SET_METADATA, existing)
        return target

    return decorator


__all__ = [
    "injectable",
    "module",
    "controller",
    "get",
    "post",
    "put",
    "delete",
    "patch",
    "head",
    "options",
    "post_construct",
    "pre_destruct",
    "middleware",
    "use_middleware",
    "use_guards",
    "exception_handler",
    "use_exception_handlers",
    "set_metadata",
    "openapi_security",
    "ControllerMeta",
    "ModuleMeta",
    "RouteMeta",
    "ExceptionHandlerMeta",
    "OpenAPISecurityMeta",
    "CONTROLLER_META",
    "MODULE_META",
    "ROUTE_META",
    "MIDDLEWARE_META",
    "USE_MIDDLEWARE",
    "USE_GUARDS",
    "USE_EXCEPTION_HANDLERS",
    "EXCEPTION_HANDLER_META",
    "SET_METADATA",
    "OPENAPI_SECURITY_META",
]
