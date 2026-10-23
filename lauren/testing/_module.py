"""``Test.create_testing_module(...)`` — build-time provider overrides for tests.

An override is a custom provider whose token is the thing being replaced. The
builder collects overrides and :meth:`TestingModuleBuilder.compile` runs the
real seven-phase ``LaurenFactory.create`` with them active, so every override
is validated exactly like production code: a missing target, an unresolvable
dependency or a scope violation fails ``compile()``.

The replacement row keeps the target's token, owning module, ``provides`` and
``multi`` flag, so module visibility and Protocol / ``list[T]`` bindings are
unchanged. Nothing is mutated on user classes and nothing outlives the build:
two testing modules with different overrides coexist in one process.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Callable, Iterable

from .._di._overrides import OverridePlan, ProviderOverride, building_with
from .._di.custom import CustomProvider
from ..decorators import EXCEPTION_HANDLER_META
from ..exceptions import DecoratorUsageError, MissingProviderError, StartupError
from ..types import Scope

if TYPE_CHECKING:
    from .._asgi import LaurenApp


def _name(token: Any) -> str:
    return str(getattr(token, "__qualname__", None) or getattr(token, "__name__", None) or repr(token))


class _StaticGuard:
    """Guard replacement whose ``can_activate`` always returns the same answer."""

    def __init__(self, allow: bool) -> None:
        self._allow = allow

    async def can_activate(self, context: Any) -> bool:
        return self._allow

    def __repr__(self) -> str:
        return f"<guard override: {'allow' if self._allow else 'deny'}>"


class _PassthroughMiddleware:
    """Middleware replacement that just calls the next handler."""

    async def dispatch(self, request: Any, call_next: Callable[[Any], Any]) -> Any:
        return await call_next(request)


class _PassthroughInterceptor:
    """Interceptor replacement that just runs the handler."""

    async def intercept(self, context: Any, call_handler: Any) -> Any:
        return await call_handler.handle()


class _SubstituteHandler:
    """Stands in for a *function-form* exception handler inside the compiled chains.

    Carries the original's ``@exception_handler`` metadata (so it claims the
    same exception types) and forwards to the replacement — a function
    ``(exc, request)`` or an object with ``catch(exc, request)``.
    """

    def __init__(self, original: Any, replacement: Any) -> None:
        setattr(self, EXCEPTION_HANDLER_META, getattr(original, EXCEPTION_HANDLER_META))
        self.__qualname__ = f"override of {_name(original)}"
        self._replacement = replacement

    def __call__(self, exc: BaseException, request: Any) -> Any:
        target = getattr(self._replacement, "catch", self._replacement)
        return target(exc, request)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise DecoratorUsageError(message)


class ProviderOverrideBuilder:
    """``override_provider(token)`` step: pick the replacement, get the builder back."""

    __test__ = False
    _role = "provider"

    def __init__(self, builder: "TestingModuleBuilder", token: Any) -> None:
        self._builder = builder
        self._token = token
        self._optional = False

    def optional(self) -> "ProviderOverrideBuilder":
        """Ignore this override if nothing in the app registers the token (instead of failing)."""
        self._optional = True
        return self

    def _done(self, kind: str, **payload: Any) -> "TestingModuleBuilder":
        override = ProviderOverride(
            token=self._token, kind=kind, optional=self._optional, role=self._role, **payload
        )
        self._builder._add(override)
        return self._builder

    def use_value(self, value: Any) -> "TestingModuleBuilder":
        """Replace the provider with ``value`` (a singleton; its lifecycle hooks never run)."""
        return self._done("value", value=value)

    def use_class(self, cls: type, *, scope: Scope | None = None) -> "TestingModuleBuilder":
        """Replace the provider with ``cls``, constructed through DI (target's scope unless ``scope=``)."""
        _require(isinstance(cls, type), f"use_class() needs a class, got {cls!r}")
        return self._done("class", use_class=cls, scope=scope)

    def use_factory(
        self, factory: Callable[..., Any], *, injects: Iterable[Any] = (), scope: Scope | None = None
    ) -> "TestingModuleBuilder":
        """Replace the provider with ``factory(*resolved injects)`` (target's scope unless ``scope=``)."""
        _require(callable(factory), f"use_factory() needs a callable, got {factory!r}")
        return self._done("factory", factory=factory, injects=tuple(injects), scope=scope)

    def use_existing(self, token: Any) -> "TestingModuleBuilder":
        """Alias the provider to another registered ``token``."""
        return self._done("existing", existing=token)

    def use_values(self, values: Iterable[Any]) -> "TestingModuleBuilder":
        """Replace a whole multi-binding set (``list[T]``) with ``values``."""
        return self._done("values", values=tuple(values))


class GuardOverrideBuilder(ProviderOverrideBuilder):
    """``override_guard(G)`` step: ``allow()``, ``deny()``, ``use_value()`` or ``use_class()``."""

    _role = "guard"

    def allow(self) -> "TestingModuleBuilder":
        """The guard lets every request through."""
        return self.use_value(_StaticGuard(True))

    def deny(self) -> "TestingModuleBuilder":
        """The guard rejects every request (the normal 403 path)."""
        return self.use_value(_StaticGuard(False))

    def use_value(self, value: Any) -> "TestingModuleBuilder":
        _require(
            hasattr(value, "can_activate"), f"a guard override needs can_activate(context); got {value!r}"
        )
        return super().use_value(value)

    def use_class(self, cls: type, *, scope: Scope | None = None) -> "TestingModuleBuilder":
        _require(hasattr(cls, "can_activate"), f"a guard override needs can_activate(context); got {cls!r}")
        return super().use_class(cls, scope=scope)


class InterceptorOverrideBuilder(ProviderOverrideBuilder):
    """``override_interceptor(I)`` step: ``passthrough()``, ``use_value()`` or ``use_class()``."""

    _role = "interceptor"

    def passthrough(self) -> "TestingModuleBuilder":
        """The interceptor just runs the handler."""
        return self.use_value(_PassthroughInterceptor())


class MiddlewareOverrideBuilder(ProviderOverrideBuilder):
    """``override_middleware(M)`` step: ``passthrough()``, ``use_value()`` or ``use_class()``."""

    _role = "middleware"

    def passthrough(self) -> "TestingModuleBuilder":
        """The middleware just calls the next handler."""
        return self.use_value(_PassthroughMiddleware())


class ExceptionHandlerOverrideBuilder:
    """``override_exception_handler(H)`` step: ``use_value()`` (or ``use_class()`` for class-form handlers)."""

    __test__ = False

    def __init__(self, builder: "TestingModuleBuilder", handler: Any) -> None:
        self._builder = builder
        self._handler = handler
        self._optional = False

    def optional(self) -> "ExceptionHandlerOverrideBuilder":
        self._optional = True
        return self

    def use_value(self, replacement: Any) -> "TestingModuleBuilder":
        """Replace the handler with an object exposing ``catch(exc, request)`` (or, for a
        function-form handler, a function ``(exc, request)``)."""
        if isinstance(self._handler, type):
            _require(
                hasattr(replacement, "catch"),
                f"an exception-handler override needs catch(exc, request); got {replacement!r}",
            )
            self._builder._add(
                ProviderOverride(
                    token=self._handler,
                    kind="value",
                    value=replacement,
                    optional=self._optional,
                    role="exception handler",
                )
            )
        else:
            _require(
                callable(replacement) or hasattr(replacement, "catch"),
                f"a function-form exception-handler override must be callable; got {replacement!r}",
            )
            self._builder._add_handler(self._handler, replacement, optional=self._optional)
        return self._builder

    def use_class(self, cls: type, *, scope: Scope | None = None) -> "TestingModuleBuilder":
        """Replace a class-form handler with ``cls``, constructed through DI."""
        _require(
            isinstance(self._handler, type),
            "use_class() replaces class-form exception handlers only; use use_value() for a function-form handler",
        )
        _require(
            isinstance(cls, type) and hasattr(cls, "catch"),
            f"use_class() needs a class with catch(); got {cls!r}",
        )
        self._builder._add(
            ProviderOverride(
                token=self._handler,
                kind="class",
                use_class=cls,
                scope=scope,
                optional=self._optional,
                role="exception handler",
            )
        )
        return self._builder


class ModuleOverrideBuilder:
    """``override_module(Old)`` step: ``with_module(New)``."""

    __test__ = False

    def __init__(self, builder: "TestingModuleBuilder", module: type) -> None:
        self._builder = builder
        self._module = module

    def with_module(self, replacement: type) -> "TestingModuleBuilder":
        """Use ``replacement`` wherever the original module is imported."""
        from ..decorators import MODULE_META

        _require(
            isinstance(replacement, type) and MODULE_META in replacement.__dict__,
            f"with_module() needs a @module class, got {replacement!r}",
        )
        self._builder._add_module(self._module, replacement)
        return self._builder


def _from_custom(custom: CustomProvider) -> ProviderOverride:
    if custom.kind == "value":
        return ProviderOverride(token=custom.provide, kind="value", value=custom.value)
    if custom.kind == "class":
        return ProviderOverride(
            token=custom.provide, kind="class", use_class=custom.use_class, scope=custom.scope
        )
    if custom.kind == "factory":
        return ProviderOverride(
            token=custom.provide,
            kind="factory",
            factory=custom.factory,
            injects=tuple(custom.inject),
            scope=custom.scope,
        )
    return ProviderOverride(token=custom.provide, kind="existing", existing=custom.existing)


class TestingModuleBuilder:
    """Collects overrides for one root module; :meth:`compile` builds the app.

    Returned by :meth:`Test.create_testing_module`. Every ``override_*``
    method returns a step object whose ``use_*`` / ``allow`` / ``deny`` /
    ``passthrough`` / ``with_module`` method returns this builder, so calls
    chain. Overriding the same token twice raises
    :class:`~lauren.exceptions.DecoratorUsageError` immediately.
    """

    __test__ = False

    def __init__(
        self, root_module: type, *, overrides: Iterable[CustomProvider] = (), **factory_kwargs: Any
    ) -> None:
        from .._asgi import LaurenFactory

        accepted = set(inspect.signature(LaurenFactory.create).parameters) - {"root_module"}
        unknown = sorted(set(factory_kwargs) - accepted)
        _require(
            not unknown, f"create_testing_module() got unknown LaurenFactory.create arguments: {unknown}"
        )
        self._root = root_module
        self._factory_kwargs = factory_kwargs
        self._providers: dict[Any, ProviderOverride] = {}
        self._modules: dict[type, type] = {}
        self._handlers: dict[Any, tuple[Any, bool]] = {}
        self._declarative_multi: set[Any] = set()
        for custom in overrides:
            _require(
                isinstance(custom, CustomProvider),
                f"overrides= takes use_value/use_class/... records; got {custom!r}",
            )
            if custom.kind == "value" and custom.multi:
                current = self._providers.get(custom.provide)
                if current is not None and custom.provide in self._declarative_multi:
                    self._providers[custom.provide] = ProviderOverride(
                        token=custom.provide, kind="values", values=current.values + (custom.value,)
                    )
                    continue
                self._add(ProviderOverride(token=custom.provide, kind="values", values=(custom.value,)))
                self._declarative_multi.add(custom.provide)
                continue
            self._add(_from_custom(custom))

    # -- fluent API --------------------------------------------------------------

    def override_provider(self, token: Any) -> ProviderOverrideBuilder:
        """Replace the provider registered under ``token`` (class, function, string or ``Token``)."""
        return ProviderOverrideBuilder(self, token)

    def override_guard(self, guard: type) -> GuardOverrideBuilder:
        """Replace a guard class wherever it is used (global, controller, route, WebSocket)."""
        _require(isinstance(guard, type), f"override_guard() needs a guard class, got {guard!r}")
        return GuardOverrideBuilder(self, guard)

    def override_interceptor(self, interceptor: type) -> InterceptorOverrideBuilder:
        """Replace an interceptor class wherever it is used."""
        _require(isinstance(interceptor, type), f"override_interceptor() needs a class, got {interceptor!r}")
        return InterceptorOverrideBuilder(self, interceptor)

    def override_middleware(self, middleware: type) -> MiddlewareOverrideBuilder:
        """Replace a middleware class wherever it is used."""
        _require(isinstance(middleware, type), f"override_middleware() needs a class, got {middleware!r}")
        return MiddlewareOverrideBuilder(self, middleware)

    def override_exception_handler(self, handler: Any) -> ExceptionHandlerOverrideBuilder:
        """Replace an ``@exception_handler`` (class or function form) wherever it is used."""
        _require(
            hasattr(handler, EXCEPTION_HANDLER_META),
            f"override_exception_handler() needs an @exception_handler-decorated class or function, got {handler!r}",
        )
        return ExceptionHandlerOverrideBuilder(self, handler)

    def override_module(self, module: type) -> ModuleOverrideBuilder:
        """Substitute another ``@module`` class wherever ``module`` is imported."""
        _require(isinstance(module, type), f"override_module() needs a @module class, got {module!r}")
        return ModuleOverrideBuilder(self, module)

    # -- recording ------------------------------------------------------------------

    def _add(self, override: ProviderOverride) -> None:
        token = override.token
        _require(
            token not in self._providers and token not in self._handlers,
            f"{_name(token)} is already overridden in this testing module; override each token once",
        )
        self._providers[token] = override

    def _add_handler(self, handler: Any, replacement: Any, *, optional: bool) -> None:
        _require(
            handler not in self._handlers and handler not in self._providers,
            f"{_name(handler)} is already overridden in this testing module; override each token once",
        )
        self._handlers[handler] = (replacement, optional)

    def _add_module(self, module: type, replacement: type) -> None:
        _require(module not in self._modules, f"{_name(module)} is already overridden in this testing module")
        self._modules[module] = replacement

    # -- build ------------------------------------------------------------------------

    def compile(self) -> "LaurenApp":
        """Run the full ``LaurenFactory.create`` pipeline with the overrides applied.

        Raises :class:`~lauren.exceptions.MissingProviderError` with
        ``detail["reason"] == "override_target_missing"`` for an override whose
        target nothing registers (unless marked ``.optional()``), and every
        error production startup would raise for the overridden graph.
        """
        from .._asgi import LaurenFactory

        plan = OverridePlan(providers=self._providers, modules=self._modules)
        with building_with(plan):
            app = LaurenFactory.create(self._root, **self._factory_kwargs)
        self._verify_targets(app, plan)
        self._substitute_handlers(app)
        return app

    def _verify_targets(self, app: "LaurenApp", plan: OverridePlan) -> None:
        applied = app.container.applied_overrides()
        for token, override in plan.providers.items():
            if token not in applied and not override.optional:
                raise MissingProviderError(
                    f"cannot override {override.role} {_name(token)}: nothing in the application registers it "
                    "(mark the override .optional() to allow that)",
                    detail={
                        "token": _name(token),
                        "reason": "override_target_missing",
                        "role": override.role,
                    },
                )
        replaced = app.module_graph.applied_substitutions
        for module in plan.modules:
            if module not in replaced:
                raise StartupError(
                    f"cannot override module {_name(module)}: it is not imported anywhere in the application",
                    detail={"module": _name(module), "reason": "override_target_missing"},
                )

    def _substitute_handlers(self, app: "LaurenApp") -> None:
        if not self._handlers:
            return
        subs = {h: _SubstituteHandler(h, replacement) for h, (replacement, _) in self._handlers.items()}
        found: set[Any] = set()
        for compiled in app._handlers.values():
            chain = compiled.exception_handlers
            if any(h in subs for h in chain):
                found.update(h for h in chain if h in subs)
                compiled.exception_handlers = tuple(subs.get(h, h) for h in chain)
        if any(h in subs for h in app._global_exception_handlers):
            found.update(h for h in app._global_exception_handlers if h in subs)
            app._global_exception_handlers = [subs.get(h, h) for h in app._global_exception_handlers]
        for handler, (_, optional) in self._handlers.items():
            if handler not in found and not optional:
                raise MissingProviderError(
                    f"cannot override exception handler {_name(handler)}: no route, controller or global list uses it",
                    detail={
                        "token": _name(handler),
                        "reason": "override_target_missing",
                        "role": "exception handler",
                    },
                )


class Test:
    """Entry point for testing modules (NestJS ``Test.createTestingModule``)."""

    __test__ = False  # not a pytest test class

    @staticmethod
    def create_testing_module(
        root_module: type, *, overrides: Iterable[CustomProvider] = (), **factory_kwargs: Any
    ) -> TestingModuleBuilder:
        """Start a testing module for ``root_module``.

        ``factory_kwargs`` are passed to ``LaurenFactory.create`` unchanged.
        ``overrides`` is the declarative form: ``use_value`` / ``use_class`` /
        ``use_factory`` / ``use_existing`` records whose ``provide=`` token is
        the provider being replaced (several ``multi=True`` value records for
        one token replace that multi-binding set). Records carry their own
        ``scope`` (``SINGLETON`` unless given); the fluent ``use_class`` /
        ``use_factory`` inherit the replaced provider's scope instead.
        """
        return TestingModuleBuilder(root_module, overrides=overrides, **factory_kwargs)


__all__ = [
    "ExceptionHandlerOverrideBuilder",
    "GuardOverrideBuilder",
    "InterceptorOverrideBuilder",
    "MiddlewareOverrideBuilder",
    "ModuleOverrideBuilder",
    "ProviderOverrideBuilder",
    "Test",
    "TestingModuleBuilder",
]
