"""FastAPI-style imperative application class.

The :class:`Lauren` class wraps the NestJS-style :class:`LaurenFactory` /
:class:`LaurenApp` pipeline behind a FastAPI-like developer surface::

    from lauren import Lauren, Path

    app = Lauren(title="My API", version="1.0.0")

    @app.get("/items/{item_id}")
    async def read_item(item_id: Path[int]) -> dict:
        return {"id": item_id}

    # Include NestJS-style modules for structured apps:
    app.include_module(UserModule)

Key design points
-----------------

1. All constructor parameters have sensible defaults \u2014 no keyword is
   required. ``app = Lauren()`` is enough to boot a fully functional app.
2. Routes registered via ``@app.get`` / ``@app.post`` / etc. live on a
   synthetic controller that is compiled through the normal pipeline, so
   they benefit from extractors, pipes, DI, OpenAPI and guards uniformly.
3. The app compiles lazily: the first time it is called as an ASGI app (or
   ``await app.startup()`` is invoked explicitly) the synthetic module is
   built, every included module is merged in, and ``LaurenFactory.create``
   runs the seven-phase pipeline. After compilation any further route /
   module / middleware additions raise :class:`LifecycleViolationError`.
4. ``/docs``, ``/redoc`` and ``/openapi.json`` are **enabled by default**
   (matching FastAPI). Pass ``None`` for any URL to disable that endpoint.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any, Awaitable, Callable

from .decorators import controller, module
from .exceptions import LifecycleViolationError, StartupError
from .types import AppState


class Lauren:
    """A FastAPI-inspired ASGI application over lauren's module pipeline.

    All constructor arguments have defaults; the canonical call is simply
    ``Lauren()``. Routes are added with the verb decorators
    (``get`` / ``post`` / ``put`` / ``delete`` / ``patch`` / ``head`` /
    ``options``), modules are merged with :meth:`include_module`, and
    middleware is appended with :meth:`add_middleware`.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        title: str = "lauren application",
        version: str = "1.0.0",
        description: str | None = None,
        openapi_url: str | None = "/openapi.json",
        docs_url: str | None = "/docs",
        redoc_url: str | None = "/redoc",
        servers: list[dict[str, Any]] | None = None,
        security_schemes: dict[str, Any] | None = None,
        debug: bool = False,
        max_body_size: int = 1_048_576,
        strict_lifecycle: bool = True,
        app_state: AppState | None = None,
        logger: Any | None = None,
    ) -> None:
        self._title = title
        self._version = version
        self._description = description
        self._openapi_url = openapi_url
        self._docs_url = docs_url
        self._redoc_url = redoc_url
        self._servers = servers
        self._security_schemes = security_schemes
        self._debug = debug
        self._max_body_size = max_body_size
        self._strict_lifecycle = strict_lifecycle
        self._app_state: AppState = app_state or AppState()
        self._logger = logger

        # Pending registrations \u2014 flushed on first compile().
        self._route_buffer: list[_PendingRoute] = []
        self._modules: list[type] = []
        self._middleware: list[type] = []
        self._guards: list[type] = []
        self._exception_filters: list[Any] = []
        self._startup_handlers: list[Callable[[], Any]] = []
        self._shutdown_handlers: list[Callable[[], Any]] = []

        # State after compile().
        self._compiled: Any | None = None  # LaurenApp
        self._compile_lock: asyncio.Lock | None = None

    # ------------------------------------------------------------------
    # Public property accessors (FastAPI parity)
    # ------------------------------------------------------------------

    @property
    def state(self) -> AppState:
        """Mutable application state, shared with every request."""
        return self._app_state

    @property
    def title(self) -> str:
        return self._title

    @property
    def version(self) -> str:
        return self._version

    @property
    def debug(self) -> bool:
        return self._debug

    # ------------------------------------------------------------------
    # Route registration \u2014 FastAPI-style method decorators
    # ------------------------------------------------------------------

    def _make_route(
        self, method: str, path: str, **kwargs: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        if not isinstance(path, str):
            from .exceptions import DecoratorUsageError

            raise DecoratorUsageError(
                f"@app.{method.lower()} must be called with a path string: "
                f'write @app.{method.lower()}("/foo") not @app.{method.lower()}.',
                detail={"decorator": f"app.{method.lower()}"},
            )

        def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._assert_not_compiled("register a route")
            self._route_buffer.append(
                _PendingRoute(method=method, path=path, fn=fn, kwargs=kwargs)
            )
            return fn

        return decorator

    def get(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a ``GET`` route. Mirrors FastAPI's ``@app.get``."""
        return self._make_route("GET", path, **kw)

    def post(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._make_route("POST", path, **kw)

    def put(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._make_route("PUT", path, **kw)

    def patch(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._make_route("PATCH", path, **kw)

    def delete(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._make_route("DELETE", path, **kw)

    def head(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._make_route("HEAD", path, **kw)

    def options(
        self, path: str = "/", **kw: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        return self._make_route("OPTIONS", path, **kw)

    # ------------------------------------------------------------------
    # Structured registration \u2014 modules & middleware
    # ------------------------------------------------------------------

    def include_module(self, module_cls: type) -> None:
        """Merge a NestJS-style ``@module`` class into this application.

        Every provider declared inside the included module (and anything it
        re-exports) becomes visible to the synthetic app-level module. Call
        as many times as needed before :meth:`startup`.
        """
        self._assert_not_compiled("include a module")
        if module_cls in self._modules:
            return
        self._modules.append(module_cls)

    def include_router(self, other: "Lauren", *, prefix: str = "") -> None:
        """Merge another :class:`Lauren` instance's routes and modules.

        Every buffered route on ``other`` is copied into ``self`` with
        ``prefix`` prepended, and ``other``'s included modules are pulled
        in too. Neither instance compiles until :meth:`startup` runs on
        ``self``. ``other`` must itself be uncompiled.
        """
        self._assert_not_compiled("include a router")
        if not isinstance(other, Lauren):
            raise TypeError("include_router expects a Lauren instance")
        if other._compiled is not None:
            raise LifecycleViolationError(
                "Cannot include an already-compiled Lauren instance; "
                "combine routers before calling startup on either."
            )
        normalized = "/" + prefix.strip("/") if prefix else ""
        for r in other._route_buffer:
            merged_path = _join_paths(normalized, r.path)
            self._route_buffer.append(
                _PendingRoute(
                    method=r.method, path=merged_path, fn=r.fn, kwargs=dict(r.kwargs)
                )
            )
        for m in other._modules:
            if m not in self._modules:
                self._modules.append(m)
        for mw in other._middleware:
            if mw not in self._middleware:
                self._middleware.append(mw)

    def add_middleware(self, cls: type) -> None:
        """Register a middleware class globally.

        The class must be decorated with ``@middleware`` (defining a
        ``dispatch(request, call_next)`` coroutine). Middleware runs in
        insertion order around every request.
        """
        self._assert_not_compiled("add middleware")
        if cls not in self._middleware:
            self._middleware.append(cls)

    def add_guard(self, cls: type) -> None:
        """Register a guard class globally.

        The class must define ``can_activate(context)``. Global guards
        run before any per-route ``@use_guards`` chain on every request.
        Equivalent to passing ``global_guards=[...]`` to
        :meth:`LaurenFactory.create`.
        """
        self._assert_not_compiled("add a global guard")
        if cls not in self._guards:
            self._guards.append(cls)

    def add_exception_handler(self, handler: Any) -> None:
        """Register an exception handler globally.

        ``handler`` must already be decorated with ``@exception_handler``.
        It runs whenever a request handler raises an exception that
        matches the handler's declared exception tuple, after any
        per-route or controller-level handlers have been tried.
        """
        self._assert_not_compiled("add a global exception handler")
        from .decorators import EXCEPTION_HANDLER_META

        if not hasattr(handler, EXCEPTION_HANDLER_META):
            from .exceptions import ExceptionHandlerConfigError

            raise ExceptionHandlerConfigError(
                f"{getattr(handler, '__name__', repr(handler))} is not "
                "decorated with @exception_handler.",
            )
        if handler not in self._exception_filters:
            self._exception_filters.append(handler)

    # ------------------------------------------------------------------
    # Lifecycle hooks
    # ------------------------------------------------------------------

    def on_startup(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Register a callable to run during application startup.

        Accepts both sync and async callables. Hooks run after the DI
        graph is built and after all ``@post_construct`` hooks have fired,
        but before any request is dispatched.
        """
        self._assert_not_compiled("register an on_startup handler")
        self._startup_handlers.append(fn)
        return fn

    def on_shutdown(self, fn: Callable[[], Any]) -> Callable[[], Any]:
        """Register a callable to run during graceful shutdown.

        Executes after in-flight requests drain and before
        ``@pre_destruct`` hooks on singleton providers.
        """
        if self._compiled is not None:
            # After compile we can delegate to the underlying LaurenApp.
            self._compiled.on_shutdown(fn)
            return fn
        self._shutdown_handlers.append(fn)
        return fn

    # ------------------------------------------------------------------
    # Compilation & ASGI dispatch
    # ------------------------------------------------------------------

    def _assert_not_compiled(self, action: str) -> None:
        if self._compiled is not None:
            raise LifecycleViolationError(
                f"Cannot {action} after the application has compiled. "
                "All routes, modules and middleware must be registered "
                "before the first request or explicit startup() call.",
            )

    async def startup(self) -> Any:
        """Compile the application and run all startup hooks.

        Safe to call multiple times \u2014 subsequent invocations are
        no-ops that return the already-compiled :class:`LaurenApp`.
        Returns the underlying :class:`LaurenApp` so tests can introspect it.
        """
        return await self._compile()

    async def shutdown(self, *, drain_timeout: float = 10.0) -> None:
        """Gracefully stop the underlying :class:`LaurenApp`.

        A no-op if the app never compiled (nothing to shut down).
        """
        if self._compiled is None:
            return
        await self._compiled.shutdown(drain_timeout=drain_timeout)

    async def _compile(self) -> Any:
        if self._compiled is not None:
            return self._compiled
        if self._compile_lock is None:
            self._compile_lock = asyncio.Lock()
        async with self._compile_lock:
            if self._compiled is not None:
                return self._compiled
            self._compiled = await self._build_compiled()
        return self._compiled

    async def _build_compiled(self) -> Any:
        # Import locally to avoid circular imports at package load time.
        from ._asgi import LaurenFactory

        # --- Build the synthetic controller -------------------------------
        AppController = _build_app_controller(self._route_buffer)

        # --- Build the synthetic root module ------------------------------
        synth_module = type("_LaurenAppModule", (), {})
        module(controllers=[AppController], imports=list(self._modules))(synth_module)

        # --- Compose OpenAPI info -----------------------------------------
        openapi_info: dict[str, Any] = {
            "title": self._title,
            "version": self._version,
        }
        if self._description is not None:
            openapi_info["description"] = self._description

        # --- Build via the canonical 7-phase pipeline ---------------------
        app = LaurenFactory.create(
            synth_module,
            strict_lifecycle=self._strict_lifecycle,
            global_middlewares=list(self._middleware),
            global_guards=list(self._guards),
            global_exception_filters=list(self._exception_filters),
            max_body_size=self._max_body_size,
            app_state=self._app_state,
            logger=self._logger,
            openapi_info=openapi_info,
            openapi_servers=self._servers,
            openapi_security_schemes=self._security_schemes,
            openapi_url=self._openapi_url,
            docs_url=self._docs_url,
            redoc_url=self._redoc_url,
        )
        # --- Run user-provided startup hooks now that DI is live ---------
        for hook in self._startup_handlers:
            try:
                res = hook()
                if inspect.isawaitable(res):
                    await res
            except Exception as exc:
                raise StartupError(
                    f"on_startup handler {getattr(hook, '__name__', repr(hook))} failed: {exc}",
                    detail={"handler": getattr(hook, "__name__", repr(hook))},
                ) from exc
        # --- Attach user shutdown hooks to the underlying LaurenApp ------
        for hook in self._shutdown_handlers:
            app.on_shutdown(hook)
        return app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        """ASGI entry point \u2014 compiles lazily on first invocation."""
        if self._compiled is None:
            await self._compile()
        await self._compiled(scope, receive, send)

    # ------------------------------------------------------------------
    # Introspection helpers \u2014 forwarded to the compiled LaurenApp
    # ------------------------------------------------------------------

    def openapi(self) -> dict[str, Any]:
        """Return the OpenAPI 3.1 document.

        Requires the app to have been compiled (either via :meth:`startup`
        or an ASGI request). Raises :class:`LifecycleViolationError` if
        called before compilation.
        """
        if self._compiled is None:
            raise LifecycleViolationError(
                "openapi() is available only after startup(). "
                "Await app.startup() first, or make at least one request."
            )
        return self._compiled.openapi()

    def routes(self) -> list[Any]:
        """Return the compiled route list.

        Before compilation this reflects buffered routes as a list of
        ``(method, path, handler)`` tuples; afterwards it returns the
        underlying :class:`LaurenApp`'s :class:`RouteEntry` objects for
        parity with the classic API.
        """
        if self._compiled is not None:
            return self._compiled.routes()
        return [(r.method, r.path, r.fn) for r in self._route_buffer]

    @property
    def container(self) -> Any:
        if self._compiled is None:
            raise LifecycleViolationError("container is only available after startup()")
        return self._compiled.container


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class _PendingRoute:
    """Lightweight record of an un-compiled ``@app.get``-style registration."""

    __slots__ = ("method", "path", "fn", "kwargs")

    def __init__(
        self,
        *,
        method: str,
        path: str,
        fn: Callable[..., Any],
        kwargs: dict[str, Any],
    ) -> None:
        self.method = method
        self.path = path
        self.fn = fn
        self.kwargs = kwargs


def _build_app_controller(routes: list[_PendingRoute]) -> type:
    """Synthesize a controller class carrying the buffered app-level routes.

    Each pending route's handler is re-decorated with the matching HTTP verb
    decorator so that the normal Phase 5 router compilation picks it up,
    extractors run, pipes run, DI resolves, OpenAPI documents it \u2014
    everything flows through the canonical pipeline.

    Handlers originally registered as plain functions (not methods) are
    adapted so the controller instance is accepted as the first positional
    argument during dispatch. From the user's perspective the function
    keeps its original signature.
    """
    from .decorators import _route_decorator

    # Use a dict of attributes because we'll build the class type() style
    # and need to avoid re-decoration on the *original* function object
    # (callers may reuse it).
    attrs: dict[str, Any] = {}
    used_names: set[str] = set()
    for i, r in enumerate(routes):
        base = r.fn.__name__ if r.fn.__name__ != "<lambda>" else f"lambda_{i}"
        name = base
        n = 0
        while name in used_names:
            n += 1
            name = f"{base}_{n}"
        used_names.add(name)
        wrapped = _wrap_function_as_method(r.fn)
        decorated = _route_decorator(r.method)(r.path, **r.kwargs)(wrapped)
        # Preserve the original qualname so OpenAPI operationId generation
        # and request-log lines remain readable.
        decorated.__name__ = name
        decorated.__qualname__ = f"_LaurenAppController.{name}"
        attrs[name] = decorated

    cls = type("_LaurenAppController", (), attrs)
    # Decorate as a controller at the app root; empty prefix keeps paths
    # exactly as the user wrote them.
    controller("")(cls)
    return cls


def _wrap_function_as_method(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Return a bound-method-compatible wrapper around ``fn``.

    Lauren's Phase 5 introspection skips the leading ``self`` parameter when
    compiling extractors. Plain functions registered via ``@app.get`` have
    no ``self`` \u2014 so we wrap them to insert one while preserving the
    original signature for type-hint discovery.
    """
    sig = inspect.signature(fn)
    # Only wrap if the function does NOT already accept ``self`` as its
    # first parameter (allows the user to declare methods on a class they
    # then pass to @app.get if they prefer).
    params = list(sig.parameters.values())
    already_has_self = bool(params) and params[0].name == "self"
    if already_has_self:
        return fn

    if inspect.iscoroutinefunction(fn):

        async def method(self, *args: Any, **kwargs: Any):  # type: ignore[no-redef]
            return await fn(*args, **kwargs)
    else:

        def method(self, *args: Any, **kwargs: Any):  # type: ignore[no-redef]
            return fn(*args, **kwargs)

    # Forward annotations so ``_compile_handler_signature`` sees the real
    # parameter hints. ``__signature__`` is what ``inspect.signature`` uses.
    new_params = [
        inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ] + params
    method.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters=new_params, return_annotation=sig.return_annotation
    )
    method.__annotations__ = dict(getattr(fn, "__annotations__", {}))
    method.__wrapped__ = fn  # type: ignore[attr-defined]
    method.__name__ = fn.__name__
    method.__doc__ = fn.__doc__
    # Forward lauren's marker attributes so decorators applied to the user
    # function before @app.get (e.g. @use_guards, @use_middleware,
    # @use_exception_handlers, @set_metadata, @post_construct) survive the
    # wrap and reach Phase 5.
    for marker in (
        "__lauren_use_guards__",
        "__lauren_use_middleware__",
        "__lauren_use_exception_handlers__",
        "__lauren_metadata__",
    ):
        if hasattr(fn, marker):
            setattr(method, marker, getattr(fn, marker))
    return method


def _join_paths(prefix: str, path: str) -> str:
    """Join two URL fragments, normalising slashes."""
    p = prefix.rstrip("/")
    q = "/" + path.lstrip("/") if path else ""
    joined = f"{p}{q}" or "/"
    return joined


__all__ = ["Lauren"]
