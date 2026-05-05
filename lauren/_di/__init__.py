"""Dependency injection container.

Providers are discovered by the ``@injectable`` decorator and compiled into an
immutable graph at startup. Three scopes are supported:

* ``SINGLETON`` — one instance per application
* ``REQUEST``   — one instance per request
* ``TRANSIENT`` — new instance on every resolution

Dependency declaration styles
-----------------------------

A class marked ``@injectable()`` may declare its dependencies in one (or
both!) of two places:

* **Constructor parameters** — ``def __init__(self, repo: UserRepo): ...``
  The classic style. Parameters are introspected at registration time
  and resolved via the container during instantiation.

* **Class-body annotations** — ::

      @injectable()
      class Service:
          repo: UserRepo
          cache: Depends[cache_factory]

  Field annotations that don't have a class-level default are treated
  as dependencies. The container resolves them and sets them as plain
  attributes on the instance *before* invoking ``__init__``. This means
  a user-supplied ``__init__`` (or ``__new__``) sees the injected
  attributes already in place, so it can freely read them.

Parameters in ``__init__`` / ``__new__`` that name the same attribute
as a class-body annotation are injected by keyword as well — so existing
code that spells the dependencies in ``__init__`` keeps working, and
authors can mix the two forms to taste.

Function injectables
--------------------

``@injectable()`` also accepts a function. The function becomes a factory
provider whose return value is the dependency::

    @injectable()
    def async_sessionmaker(cfg: ConfigService) -> AsyncSessionmaker:
        return AsyncSessionmaker(cfg.db_url)

    @injectable()
    class UserRepo:
        sessmkr: Depends[async_sessionmaker]

The function's own parameters are resolved via DI, exactly like a class
constructor's. When resolving ``Depends[async_sessionmaker]`` the
container calls the function (once per scope) and returns the value.

Module-scoped resolution
------------------------

The container enforces NestJS-style module encapsulation. Each provider is
registered together with the *module class* that declared it. A caller (a
controller, an endpoint ``Depends[X]``, or another provider) resolves a
dependency **through** its owning module, and the container only considers
providers that are *visible* to that module — its own providers plus anything
re-exported by a module it imports.

When ``owning_module`` is ``None`` (legacy callers, unit tests, the framework
itself) the container skips visibility checks and behaves like a flat
registry; this preserves backward compatibility and keeps test setup concise.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from .._typing import resolve_type_hints

from ..exceptions import (
    CircularDependencyError,
    DIScopeViolationError,
    DuplicateBindingError,
    MissingProviderError,
    ProtocolAmbiguityError,
    UnresolvableParameterError,
)
from ..types import Scope
from .custom import (
    CustomProvider,
    OptionalDep,
    _InjectMarker,
)

INJECTABLE_META = "__lauren_injectable__"


# ---------------------------------------------------------------------------
# Multi-binding collection tokens (``list[T]``)
# ---------------------------------------------------------------------------


def _multi_binding_element_type(token: Any) -> Any | None:
    """Return ``T`` when ``token`` is ``list[T]`` (or ``typing.List[T]``).

    This is the *only* collection shape the container treats as a
    multi-binding request. A handler parameter, constructor argument, or
    class-body annotation typed as ``list[SomeProtocol]`` tells the
    container: "give me every provider registered with ``multi=True``
    for ``SomeProtocol``, wrapped in a list."

    The narrow scope is intentional — widening to ``Sequence[T]`` /
    ``Iterable[T]`` / ``Collection[T]`` would quietly capture
    ``BaseModel`` fields, third-party generics and a host of unrelated
    type hints. ``list[T]`` is the canonical, unambiguous spelling and
    mirrors the shape that ``DIContainer.resolve(T)`` already returns
    for multi-bound tokens.

    Returns ``None`` for anything else (bare types, ``dict[K, V]``,
    ``Optional[X]``, …) so the caller can fall through to its normal
    single-provider resolution path.
    """
    import typing as _typing

    origin = _typing.get_origin(token)
    # ``list[X]`` has ``list`` as its origin (the builtin); ``typing.List[X]``
    # normalises to the same thing in 3.9+. Anything else — ``dict``,
    # ``tuple``, ``Annotated``, ``Union`` — is not a multi-binding request.
    if origin is not list:
        return None
    args = _typing.get_args(token)
    # ``list[T]`` has exactly one type argument; reject ``list`` (bare),
    # ``list[T, U]`` (illegal), and similar oddities defensively.
    if len(args) != 1:
        return None
    return args[0]


@dataclass
class InjectableMeta:
    scope: Scope = Scope.SINGLETON
    provides: tuple[type, ...] = ()
    multi: bool = False


@dataclass
class Provider:
    """A compiled provider entry.

    ``cls`` is the DI token the provider binds to. Despite the legacy
    name, it is **not** required to be a class — string tokens and
    :class:`Token` instances live here too, registered through the
    custom-provider helpers (:func:`use_value`, :func:`use_class`,
    :func:`use_factory`, :func:`use_existing`). The annotation stays
    typed as ``type`` for backward compatibility with the wide surface
    of code that already imports ``Provider``; callers that need to
    reason about non-class tokens should use :attr:`provider_kind`.

    ``factory`` is the callable actually invoked to produce an instance.
    For classes it's the class itself (``Cls(**kwargs)``); for function
    providers it's the decorated function. Keeping the split explicit
    means the runtime never needs to ask "is this a class or a function?"
    — it always calls ``factory(**kwargs)``.

    ``deps`` are constructor / function arguments to resolve via DI.
    ``field_deps`` are class-body-annotated attributes resolved to plain
    attributes set on the instance before ``__init__`` runs.
    """

    cls: type
    scope: Scope
    provides: tuple[type, ...]
    multi: bool
    deps: tuple[tuple[str, Any], ...]
    post_construct: Callable[..., Any] | None = None
    pre_destruct: Callable[..., Any] | None = None
    #: Module class that declared this provider. ``None`` means the provider
    #: was registered without a module context (test fixtures, late-registered
    #: helpers like auto-discovered middleware/guards) and is therefore visible
    #: to every module.
    owning_module: type | None = None
    #: The callable invoked to produce an instance. Usually ``cls`` itself
    #: for class providers; for function providers (``@injectable()`` on a
    #: ``def``) it's the decorated function. Kept nullable so existing
    #: construction sites that set ``cls`` continue to work — see
    #: :meth:`_call_factory` for the fallback.
    factory: Callable[..., Any] | None = None
    #: Class-body annotated dependencies: ``(attribute_name, type)`` pairs.
    #: Resolved before ``__init__`` runs and set as plain attributes.
    field_deps: tuple[tuple[str, type], ...] = ()
    #: True when this provider was declared via ``@injectable()`` on a
    #: function rather than a class. Affects lifecycle semantics (no
    #: ``@post_construct`` / ``@pre_destruct`` on a plain value).
    is_function_provider: bool = False
    #: Custom-provider kind: ``"value"`` / ``"class"`` / ``"factory"`` /
    #: ``"existing"`` for the four NestJS-style recipes, or ``None`` for
    #: the legacy ``@injectable``-decorated registration. Used by the
    #: dispatcher to skip lifecycle hooks on value providers, redirect
    #: alias resolutions, and forbid duplicate registration when one
    #: side is custom.
    provider_kind: str | None = None
    #: For ``provider_kind == "existing"`` only: the token this alias
    #: redirects to. The dispatcher sees this and forwards the resolve
    #: call rather than running its own factory.
    alias_target: Any = None
    #: Indices into ``deps`` whose values may resolve to ``None``
    #: instead of raising :class:`MissingProviderError`. Populated by
    #: :meth:`DIContainer.register_factory` when an entry of
    #: ``injects=[...]`` was wrapped in :class:`OptionalDep`.
    optional_dep_names: frozenset[str] = field(default_factory=frozenset)
    #: Positional-argument names (in order) for factory-style providers.
    #: When present the resolver passes the resolved deps positionally
    #: rather than by keyword — supporting raw lambdas where parameter
    #: names are arbitrary placeholders.
    positional_arg_names: tuple[str, ...] = ()


class DIContainer:
    """Compiled DI container; immutable after :meth:`compile`."""

    def __init__(self) -> None:
        self._providers: dict[type, Provider] = {}
        # Token -> list of providers (for protocols / multi-bindings)
        self._token_bindings: dict[type, list[Provider]] = {}
        self._singletons: dict[type, Any] = {}
        self._singletons_initialized: set[type] = set()
        self._compiled = False
        #: module_cls -> frozenset of provider tokens visible inside that module.
        #: An absent entry means "no visibility restrictions for this module" —
        #: callers that specify a module not present here fall back to global
        #: resolution (used by unit tests that bypass ModuleGraph).
        self._visible: dict[type, frozenset[type]] = {}

    # -- Registration ------------------------------------------------------

    def register(
        self, target: type | Callable[..., Any], *, owning_module: type | None = None
    ) -> Provider:
        """Register a class or function provider.

        Both flavours are accepted. The decorator
        (:func:`lauren.injectable`) already attaches
        :class:`InjectableMeta`; this method reads that marker, inspects
        the target's callable signature (and, for classes, class-body
        field annotations), and installs an immutable :class:`Provider`
        entry. Classes and functions are routed by the same registry
        so module visibility, scope validation, and multi-binding all
        work uniformly.
        """
        meta = _get_injectable_meta(target)
        if meta is None:
            raise MissingProviderError(f"{_describe(target)} is not marked @injectable")

        # Function providers: the token the container indexes by is the
        # function itself (functions are hashable and unique), so
        # ``Depends[my_factory]`` resolves correctly.
        if not inspect.isclass(target):
            token: Any = target
            if token in self._providers:
                raise DuplicateBindingError(f"{_describe(target)} already registered")
            deps = _inspect_callable_deps(target, is_class_init=False)
            provider = Provider(
                cls=token,  # type: ignore[arg-type]
                scope=meta.scope,
                provides=tuple(meta.provides),
                multi=meta.multi,
                deps=tuple(deps),
                post_construct=None,
                pre_destruct=None,
                owning_module=owning_module,
                factory=target,
                field_deps=(),
                is_function_provider=True,
            )
            self._providers[token] = provider
            self._bind_token(token, provider)
            for tok in meta.provides:
                self._bind_token(tok, provider)
            return provider

        cls = target
        if cls in self._providers:
            raise DuplicateBindingError(f"{cls.__name__} already registered")

        # Class-body field annotations: any name annotated on the class
        # body that (a) has no class-level default value AND (b) whose
        # type resolves to a real class (not a ``ClassVar`` / primitive
        # with an explicit default) is a candidate for injection. The
        # compiler cross-references these against the DI graph during
        # :meth:`compile` so missing providers surface at startup.
        field_deps = _collect_field_deps(cls)

        # Use ``inspect.signature(cls)`` as the single source of truth
        # for the class's callable surface. The stdlib walks any
        # ``__signature__`` attribute, the metaclass's ``__call__``,
        # and only then falls back to ``__init__`` / ``__new__`` — so
        # this one call handles every shape Python supports, including
        # Pydantic models, attrs classes, and metaclass-driven
        # factories. The legacy two-callable merge would have ignored
        # all three.
        deps = _inspect_class_deps(cls)

        post = _find_lifecycle_hook(cls, "__lauren_post_construct__")
        pre = _find_lifecycle_hook(cls, "__lauren_pre_destruct__")
        provider = Provider(
            cls=cls,
            scope=meta.scope,
            provides=tuple(meta.provides),
            multi=meta.multi,
            deps=tuple(deps),
            post_construct=post,
            pre_destruct=pre,
            owning_module=owning_module,
            factory=cls,
            field_deps=tuple(field_deps),
            is_function_provider=False,
        )
        self._providers[cls] = provider
        self._bind_token(cls, provider)
        for tok in meta.provides:
            self._bind_token(tok, provider)
        return provider

    def _bind_token(self, token: Any, provider: Provider) -> None:
        self._token_bindings.setdefault(token, []).append(provider)

    # ------------------------------------------------------------------
    # Custom-provider registration
    # ------------------------------------------------------------------
    #
    # The four methods below mirror NestJS's useValue / useClass /
    # useFactory / useExisting recipes. Each lowers a high-level
    # description into a :class:`Provider` row that the existing
    # resolver path already knows how to handle. Keeping the lowering
    # here (rather than in the factory pipeline) means tests can drive
    # the container directly without booting an entire LaurenApp.

    def register_value(
        self,
        token: Any,
        value: Any,
        *,
        owning_module: type | None = None,
        multi: bool = False,
    ) -> Provider:
        """Register a literal ``value`` under ``token`` (NestJS ``useValue``).

        The container caches the value as if it were a singleton and
        returns it untouched on every resolve. The factory the
        :class:`Provider` carries is a no-op closure so the resolver's
        "call factory" path still runs without special-casing.

        Multi-binding registrations don't pre-populate the singleton
        cache because that cache is keyed by token — two providers for
        the same token would race each other. The factory still runs
        on resolve, so the value still bypasses any user-construction.
        """
        if token in self._providers and not multi:
            raise DuplicateBindingError(
                f"{_describe(token)} already registered",
            )

        # Closure over ``value`` — the resolver invokes the factory
        # exactly once on first resolve, after which the singleton
        # cache short-circuits subsequent calls.
        def _value_factory() -> Any:
            return value

        # Multi-binding providers do NOT use the per-token singleton
        # cache: every binding gets its own instance during multi
        # resolution, so caching by token would conflate them. Use a
        # synthetic per-provider key for the singleton-style "build
        # once" behaviour without colliding with sibling bindings.
        cache_key: Any = token
        if multi and token in self._providers:
            # Use the factory's identity so each value provider has a
            # distinct cache row.
            cache_key = _value_factory
        provider = Provider(
            cls=cache_key,  # type: ignore[arg-type]
            scope=Scope.SINGLETON,
            provides=(),
            multi=multi,
            deps=(),
            owning_module=owning_module,
            factory=_value_factory,
            is_function_provider=True,
            provider_kind="value",
        )
        self._providers[cache_key] = provider
        self._bind_token(token, provider)
        # Pre-populate the singleton cache so even introspection
        # (``container.singletons()``) sees the value immediately,
        # without forcing a resolve.
        self._singletons[cache_key] = value
        self._singletons_initialized.add(cache_key)
        return provider

    def register_class(
        self,
        token: Any,
        cls: type,
        *,
        owning_module: type | None = None,
        scope: Scope = Scope.SINGLETON,
        multi: bool = False,
    ) -> Provider:
        """Register ``cls`` under ``token`` (NestJS ``useClass``).

        Unlike a plain ``@injectable`` registration, the bound class
        does not have to be the same as the token — that's the whole
        point of ``useClass``. The class's ``__init__`` and class-body
        deps are introspected as usual so its own collaborators
        resolve through DI.

        If the class isn't already marked ``@injectable`` we synthesise
        the metadata so the user doesn't have to remember to decorate
        a target they're only re-binding under another token.
        """
        if not isinstance(cls, type):
            raise UnresolvableParameterError(
                f"register_class expected a class; got {cls!r}",
            )
        if token in self._providers and not multi:
            raise DuplicateBindingError(
                f"{_describe(token)} already registered",
            )
        # Synthesise injectable meta on the class if missing so the
        # standard inspection path does not balk — the user explicitly
        # opted in by passing it to use_class.
        if INJECTABLE_META not in cls.__dict__:
            setattr(cls, INJECTABLE_META, InjectableMeta(scope=scope))
        field_deps = _collect_field_deps(cls)
        # Same rationale as :meth:`register` — inspect the class as a
        # callable to honour custom ``__signature__`` / metaclass
        # ``__call__`` overrides instead of demanding ``__init__``
        # parameters that may never run.
        deps = _inspect_class_deps(cls)
        provider = Provider(
            cls=token,  # type: ignore[arg-type]
            scope=scope,
            provides=(),
            multi=multi,
            deps=tuple(deps),
            owning_module=owning_module,
            factory=cls,
            field_deps=tuple(field_deps),
            is_function_provider=False,
            provider_kind="class",
            post_construct=_find_lifecycle_hook(cls, "__lauren_post_construct__"),
            pre_destruct=_find_lifecycle_hook(cls, "__lauren_pre_destruct__"),
        )
        self._providers[token] = provider
        self._bind_token(token, provider)
        return provider

    def register_factory(
        self,
        token: Any,
        factory: Callable[..., Any],
        *,
        inject: tuple[Any, ...] = (),
        scope: Scope = Scope.SINGLETON,
        owning_module: type | None = None,
        multi: bool = False,
    ) -> Provider:
        """Register a factory callable under ``token`` (NestJS ``useFactory``).

        ``inject`` lists the tokens lauren resolves and passes
        positionally to ``factory``. Optional dependencies are wrapped
        in :class:`OptionalDep` and lower to ``None`` when no provider
        is visible — mirroring NestJS's ``{ token, optional: true }``.

        The factory itself does **not** need to be ``@injectable``
        decorated. We treat its parameters as pure positional slots
        keyed by ``inject``, so a bare lambda works just as well as a
        named function. This contrasts with ``register()`` for a
        function provider, where lauren introspects the function's
        own signature and resolves by *parameter type annotation*.
        """
        if token in self._providers and not multi:
            raise DuplicateBindingError(
                f"{_describe(token)} already registered",
            )
        # Build synthetic param names so the existing kwargs-based
        # resolver code path keeps working. Optional positions are
        # tracked separately so the resolver can fall back to ``None``.
        optional_names: set[str] = set()
        deps: list[tuple[str, Any]] = []
        positional_names: list[str] = []
        for i, raw in enumerate(inject):
            name = f"__factory_arg_{i}"
            if isinstance(raw, OptionalDep):
                optional_names.add(name)
                tok = raw.token
            else:
                tok = raw
            deps.append((name, tok))
            positional_names.append(name)
        provider = Provider(
            cls=token,  # type: ignore[arg-type]
            scope=scope,
            provides=(),
            multi=multi,
            deps=tuple(deps),
            owning_module=owning_module,
            factory=factory,
            is_function_provider=True,
            provider_kind="factory",
            optional_dep_names=frozenset(optional_names),
            positional_arg_names=tuple(positional_names),
        )
        self._providers[token] = provider
        self._bind_token(token, provider)
        return provider

    def register_alias(
        self,
        token: Any,
        existing: Any,
        *,
        owning_module: type | None = None,
    ) -> Provider:
        """Alias ``token`` to ``existing`` (NestJS ``useExisting``).

        Alias rows carry no factory of their own; the resolver detects
        ``provider_kind == 'existing'`` and forwards the lookup to
        the target. Cycle detection runs at compile time.
        """
        if token in self._providers:
            raise DuplicateBindingError(
                f"{_describe(token)} already registered",
            )
        provider = Provider(
            cls=token,  # type: ignore[arg-type]
            scope=Scope.SINGLETON,  # placeholder; the real scope is the alias target's
            provides=(),
            multi=False,
            deps=(),
            owning_module=owning_module,
            factory=None,
            is_function_provider=True,
            provider_kind="existing",
            alias_target=existing,
        )
        self._providers[token] = provider
        self._bind_token(token, provider)
        return provider

    def register_custom(
        self, custom: CustomProvider, *, owning_module: type | None = None
    ) -> Provider:
        """Single entry point that dispatches to the right helper.

        Used by :class:`LaurenFactory` so the module-graph compiler
        doesn't need to know the four-way branch.
        """
        if custom.kind == "value":
            return self.register_value(
                custom.provide,
                custom.value,
                owning_module=owning_module,
                multi=custom.multi,
            )
        if custom.kind == "class":
            assert custom.use_class is not None
            return self.register_class(
                custom.provide,
                custom.use_class,
                owning_module=owning_module,
                scope=custom.scope,
                multi=custom.multi,
            )
        if custom.kind == "factory":
            assert custom.factory is not None
            return self.register_factory(
                custom.provide,
                custom.factory,
                inject=custom.inject,
                scope=custom.scope,
                owning_module=owning_module,
                multi=custom.multi,
            )
        if custom.kind == "existing":
            return self.register_alias(
                custom.provide,
                custom.existing,
                owning_module=owning_module,
            )
        raise ValueError(f"unknown custom provider kind: {custom.kind!r}")

    # -- Visibility --------------------------------------------------------

    def set_visible(self, module_cls: type, tokens: frozenset[type]) -> None:
        """Install the visible-token set for ``module_cls``.

        Called once per module during Phase 2 of startup with the module's
        own providers plus anything re-exported by a transitively imported
        module. Subsequent ``resolve(..., owning_module=module_cls)`` calls
        are restricted to bindings whose provider class is in that set.
        """
        self._visible[module_cls] = frozenset(tokens)

    def _is_visible(self, provider: Provider, owning_module: type | None) -> bool:
        """Return True if ``provider`` is visible from ``owning_module``."""
        if owning_module is None:
            return True
        visible = self._visible.get(owning_module)
        if visible is None:
            # No explicit visibility installed — legacy / unit-test mode.
            return True
        if provider.cls in visible:
            return True
        # Providers without an owning module are "globally visible" helpers
        # (auto-registered middleware, guards, test fixtures).
        return provider.owning_module is None

    # -- Compilation -------------------------------------------------------

    def compile(self) -> None:
        """Validate graph: detect cycles, missing deps, scope violations."""
        if self._compiled:
            return
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[Any, int] = {c: WHITE for c in self._providers}
        stack: list[Any] = []

        def _check_scope(chosen: Provider, *, for_scope: Scope) -> None:
            """Enforce the scope-narrowing rule on a single edge.

            A wider-scoped consumer must never depend on a narrower-scoped
            provider — that would either keep a short-lived instance alive
            past its sensible lifetime (``SINGLETON`` consumers) or silently
            cache a per-resolution value (``REQUEST`` consumers of a
            ``TRANSIENT`` provider).

            :class:`~lauren.types.Scope` is an ``IntEnum`` whose values are
            explicitly ordered from narrowest (``TRANSIENT = 0``) to widest
            (``SINGLETON = 2``). That lets us replace the previous lookup
            table with a single comparison — a dependent outlives its
            dependency exactly when ``consumer_scope > dependency_scope``.
            """
            if for_scope > chosen.scope:
                raise DIScopeViolationError(
                    f"{for_scope.label.capitalize()} depends on "
                    f"{chosen.scope.label}-scoped {_describe(chosen.cls)}",
                    detail={
                        "dependent_scope": for_scope.label,
                        "dependency_scope": chosen.scope.label,
                        "dependency": _describe(chosen.cls),
                    },
                )

        def resolve_one(
            token: Any, *, for_scope: Scope, owning_module: type | None
        ) -> list[Provider]:
            """Resolve ``token`` to the list of dep-graph providers it expands to.

            Normal (scalar) tokens resolve to a single-element list. A
            ``list[T]`` token resolves to every multi-bound provider for
            ``T``; the caller treats each element as an independent edge
            so cycle detection and scope checks apply per-binding.

            This function raises the same typed errors as before —
            ``MissingProviderError``, ``ProtocolAmbiguityError``,
            ``DIScopeViolationError`` — so callers that were relying on
            the original exception semantics keep working.
            """
            element_type = _multi_binding_element_type(token)
            if element_type is not None:
                # ``list[T]``: every registered provider for ``T`` becomes an
                # edge. Non-multi registrations are a programming error in
                # this context — the user either forgot ``multi=True`` or
                # should depend on ``T`` directly.
                inner_bindings = self._token_bindings.get(element_type)
                if not inner_bindings:
                    raise MissingProviderError(
                        f"No provider for {_describe(element_type)} "
                        f"(requested as {_describe(token)}). "
                        "Register at least one provider with multi=True.",
                        detail={"token": _describe(token)},
                    )
                visible = [
                    b for b in inner_bindings if self._is_visible(b, owning_module)
                ]
                if not visible:
                    owner_name = (
                        owning_module.__name__
                        if owning_module is not None
                        else "<global>"
                    )
                    raise MissingProviderError(
                        f"No provider for {_describe(element_type)} "
                        f"(requested as {_describe(token)}) "
                        f"visible from module {owner_name}.",
                        detail={
                            "token": _describe(token),
                            "module": owner_name,
                        },
                    )
                if not all(b.multi for b in visible):
                    # A mix of multi / non-multi (or a lone non-multi) is a
                    # registration error. Surface it with the same exception
                    # class users already handle for "multiple bindings".
                    raise ProtocolAmbiguityError(
                        f"{_describe(token)} requires every provider for "
                        f"{_describe(element_type)} to be registered with "
                        "multi=True",
                        detail={
                            "token": _describe(token),
                            "element": _describe(element_type),
                            "count": len(visible),
                        },
                    )
                # Scope narrowing applies to each provider independently:
                # a singleton consumer of ``list[T]`` must not receive a
                # request-scoped member.
                for b in visible:
                    _check_scope(b, for_scope=for_scope)
                return list(visible)

            bindings = self._token_bindings.get(token)
            if not bindings and getattr(token, "_is_runtime_protocol", False):
                # Structural Protocol fallback — when no provider is explicitly
                # registered under a Protocol token, scan for providers whose
                # class is a structural subtype of the Protocol. The scan is
                # scoped to the *owning module* of the consumer so that each
                # module sees only its own runner/service, not those imported
                # from sibling modules.
                bindings = _structural_protocol_providers(
                    token,
                    owning_module,
                    self._providers,
                    is_visible=self._is_visible if self._visible else None,
                )
            if not bindings:
                raise MissingProviderError(
                    f"No provider for {_describe(token)}",
                    detail={"token": _describe(token)},
                )
            # Filter by visibility first — a caller can only see bindings
            # within its module's reach.
            visible_bindings = [
                b for b in bindings if self._is_visible(b, owning_module)
            ]
            if not visible_bindings:
                owner_name = (
                    owning_module.__name__ if owning_module is not None else "<global>"
                )
                raise MissingProviderError(
                    f"No provider for {_describe(token)} "
                    f"visible from module {owner_name}. "
                    "Either declare it in this module or import a module that exports it.",
                    detail={
                        "token": _describe(token),
                        "module": owner_name,
                    },
                )
            if len(visible_bindings) > 1 and not all(b.multi for b in visible_bindings):
                raise ProtocolAmbiguityError(
                    f"Multiple bindings for {_describe(token)}",
                    detail={"count": len(visible_bindings)},
                )
            chosen = visible_bindings[0]
            _check_scope(chosen, for_scope=for_scope)
            return [chosen]

        # Framework-provided types that are resolved by the runtime rather
        # than by DI (Request, etc.). These are skipped during dep resolution.
        from ..types import Request as _Request

        framework_tokens = {_Request}

        def visit(provider: Provider) -> None:
            tok = provider.cls
            c = color.get(tok, WHITE)
            if c == GRAY:
                cycle = [_describe(t) for t in stack[stack.index(tok) :] + [tok]]
                raise CircularDependencyError(
                    "Circular dependency: " + " -> ".join(cycle),
                    detail={"cycle": cycle},
                )
            if c == BLACK:
                return
            color[tok] = GRAY
            stack.append(tok)
            # ``existing`` aliases have no deps of their own — their
            # only edge is the alias_target. Validate that the target
            # exists and recurse so a chain of aliases is checked end
            # to end (and cycles are caught by the GRAY path above).
            if provider.provider_kind == "existing":
                target = provider.alias_target
                target_provider = self._providers.get(target)
                if target_provider is None:
                    raise MissingProviderError(
                        f"Alias {_describe(tok)} -> {_describe(target)} "
                        "points at an unknown token. Did you forget to "
                        "register the underlying provider?",
                        detail={
                            "alias": _describe(tok),
                            "target": _describe(target),
                        },
                    )
                visit(target_provider)
                stack.pop()
                color[tok] = BLACK
                return
            # Map param name -> has_default for this provider's factory.
            defaults = _callable_default_map(provider)
            # Field-injected attributes are always required (they have no
            # parameter-level default concept); add them to the dep graph.
            dep_iter = list(provider.deps) + list(provider.field_deps)
            for name, dep_type in dep_iter:
                if dep_type in framework_tokens:
                    continue
                try:
                    dep_providers = resolve_one(
                        dep_type,
                        for_scope=provider.scope,
                        owning_module=provider.owning_module,
                    )
                except ProtocolAmbiguityError:
                    raise
                except MissingProviderError:
                    # Three escape hatches: parameter defaults, an
                    # explicit OptionalDep wrap on a use_factory inject
                    # entry, and (legacy) a field-side default. Only
                    # the first two are intentional, but they share the
                    # same handler.
                    if defaults.get(name, False):
                        continue
                    if name in provider.optional_dep_names:
                        continue
                    raise
                # ``list[T]`` deps expand to every bound provider; each edge
                # participates in cycle detection on its own terms.
                for dep_provider in dep_providers:
                    visit(dep_provider)
            stack.pop()
            color[tok] = BLACK

        for p in list(self._providers.values()):
            visit(p)

        # Validate all protocol bindings eagerly: any token with multiple
        # non-multi providers is an error, regardless of whether it is
        # referenced elsewhere.
        for token, bindings in self._token_bindings.items():
            if len(bindings) > 1 and not all(b.multi for b in bindings):
                raise ProtocolAmbiguityError(
                    f"Multiple bindings for {_describe(token)}",
                    detail={
                        "token": _describe(token),
                        "count": len(bindings),
                    },
                )

        self._compiled = True

    # -- Resolution --------------------------------------------------------

    def has_provider(self, token: Any, *, owning_module: type | None = None) -> bool:
        """Return True when ``token`` could be resolved by this container.

        Recognises two shapes:

        * **Scalar tokens** — returns True iff at least one provider for
          ``token`` is visible from ``owning_module``.
        * **``list[T]`` tokens** — returns True iff at least one provider
          for ``T`` is visible from ``owning_module`` (the resolver
          enforces the ``multi=True`` requirement; visibility alone is
          enough to say "yes, this route can go through DI").

        Keeping the visibility check here (rather than deferring it to
        ``resolve``) lets the ASGI handler compiler distinguish a
        parameter that happens to be typed ``list[T]`` but has no
        providers at all from one whose DI path should be taken.
        """
        effective = _multi_binding_element_type(token) or token
        bindings = self._token_bindings.get(effective)
        if not bindings:
            return False
        if owning_module is None:
            return True
        return any(self._is_visible(b, owning_module) for b in bindings)

    def get_provider(
        self, token: Any, *, owning_module: type | None = None
    ) -> Provider:
        bindings = self._token_bindings.get(token)
        if not bindings:
            raise MissingProviderError(f"No provider for {_describe(token)}")
        if owning_module is not None:
            bindings = [b for b in bindings if self._is_visible(b, owning_module)]
            if not bindings:
                raise MissingProviderError(
                    f"No provider for {_describe(token)} "
                    f"visible from module {owning_module.__name__}"
                )
        if len(bindings) > 1 and not all(b.multi for b in bindings):
            raise ProtocolAmbiguityError(f"Multiple bindings for {_describe(token)}")
        return bindings[0]

    def all_providers(self) -> list[Provider]:
        return list(self._providers.values())

    async def resolve(
        self,
        token: Any,
        *,
        request_cache: dict[type, Any] | None = None,
        framework_values: dict[type, Any] | None = None,
        owning_module: type | None = None,
    ) -> Any:
        """Resolve an instance of ``token``.

        ``request_cache`` stores request-scoped instances for the current
        request. ``framework_values`` is a type-keyed map for objects supplied
        by the runtime (e.g. ``Request``). ``owning_module`` restricts which
        bindings may be returned: only providers visible to that module are
        considered — when omitted, all bindings are eligible.
        """
        framework_values = framework_values or {}
        if token in framework_values:
            return framework_values[token]
        # ``list[T]`` tokens expand to every visible multi-binding for
        # ``T``. The inner resolution reuses the same request cache and
        # framework values so request-scoped collaborators still see a
        # single instance per request.
        element_type = _multi_binding_element_type(token)
        if element_type is not None:
            return await self._resolve_multi_list(
                token,
                element_type,
                request_cache=request_cache,
                framework_values=framework_values,
                owning_module=owning_module,
            )
        bindings = self._token_bindings.get(token)
        if not bindings and getattr(token, "_is_runtime_protocol", False):
            bindings = _structural_protocol_providers(
                token,
                owning_module,
                self._providers,
                is_visible=self._is_visible if self._visible else None,
            )
        if not bindings:
            raise MissingProviderError(f"No provider for {_describe(token)}")
        # Narrow to visible bindings.
        visible_bindings = [b for b in bindings if self._is_visible(b, owning_module)]
        if not visible_bindings:
            owner_name = (
                owning_module.__name__ if owning_module is not None else "<global>"
            )
            raise MissingProviderError(
                f"No provider for {_describe(token)} visible from module {owner_name}",
                detail={
                    "token": _describe(token),
                    "module": owner_name,
                },
            )
        # Return all multi-bindings as a list when requested explicitly:
        if all(b.multi for b in visible_bindings) and len(visible_bindings) > 1:
            results = []
            for b in visible_bindings:
                results.append(
                    await self._instantiate(b, request_cache, framework_values)
                )
            return results
        if len(visible_bindings) > 1:
            raise ProtocolAmbiguityError(f"Multiple bindings for {_describe(token)}")
        return await self._instantiate(
            visible_bindings[0], request_cache, framework_values
        )

    async def _resolve_multi_list(
        self,
        token: Any,
        element_type: Any,
        *,
        request_cache: dict[type, Any] | None,
        framework_values: dict[type, Any],
        owning_module: type | None,
    ) -> list[Any]:
        """Resolve ``list[T]`` to a list of every visible multi-bound instance.

        Every provider for ``element_type`` that is both visible to
        ``owning_module`` and marked ``multi=True`` produces one element
        of the returned list, in registration order. The result type is
        a real ``list`` (not a tuple or a generator) so handler code can
        iterate it multiple times.

        Error modes, in priority order:

        * No providers for ``element_type`` at all →
          :class:`MissingProviderError`.
        * No *visible* providers for ``element_type`` from
          ``owning_module`` → :class:`MissingProviderError`.
        * At least one matching provider was **not** registered with
          ``multi=True`` → :class:`ProtocolAmbiguityError`, since
          returning a single-element list from a lone non-multi binding
          would silently diverge from how ``resolve(T)`` behaves on the
          same registration.
        """
        bindings = self._token_bindings.get(element_type)
        if not bindings:
            raise MissingProviderError(
                f"No provider for {_describe(element_type)} "
                f"(requested as {_describe(token)})",
                detail={"token": _describe(token)},
            )
        visible = [b for b in bindings if self._is_visible(b, owning_module)]
        if not visible:
            owner_name = (
                owning_module.__name__ if owning_module is not None else "<global>"
            )
            raise MissingProviderError(
                f"No provider for {_describe(element_type)} "
                f"(requested as {_describe(token)}) "
                f"visible from module {owner_name}",
                detail={
                    "token": _describe(token),
                    "module": owner_name,
                },
            )
        if not all(b.multi for b in visible):
            raise ProtocolAmbiguityError(
                f"{_describe(token)} requires every provider for "
                f"{_describe(element_type)} to be registered with multi=True",
                detail={
                    "token": _describe(token),
                    "element": _describe(element_type),
                    "count": len(visible),
                },
            )
        results: list[Any] = []
        for provider in visible:
            results.append(
                await self._instantiate(provider, request_cache, framework_values)
            )
        return results

    async def _instantiate(
        self,
        provider: Provider,
        request_cache: dict[type, Any] | None,
        framework_values: dict[type, Any],
    ) -> Any:
        # Alias rows carry no factory — redirect to the underlying token
        # before doing any caching work. ``existing`` aliases inherit the
        # target's scope automatically because we delegate the resolve
        # back through :meth:`resolve`, which re-enters ``_instantiate``
        # on the real provider.
        if provider.provider_kind == "existing":
            return await self.resolve(
                provider.alias_target,
                request_cache=request_cache,
                framework_values=framework_values,
                owning_module=provider.owning_module,
            )

        if provider.scope == Scope.SINGLETON:
            if provider.cls in self._singletons:
                return self._singletons[provider.cls]
        elif provider.scope == Scope.REQUEST:
            if request_cache is None:
                # Falls back to transient if no request context is active.
                pass
            elif provider.cls in request_cache:
                return request_cache[provider.cls]

        # Build ``kwargs`` for the factory: function-provider parameters
        # and class-provider __init__ / __new__ parameters are resolved
        # identically.
        defaults = _callable_default_map(provider)
        kwargs: dict[str, Any] = {}
        for name, dep_type in provider.deps:
            try:
                kwargs[name] = await self.resolve(
                    dep_type,
                    request_cache=request_cache,
                    framework_values=framework_values,
                    owning_module=provider.owning_module,
                )
            except MissingProviderError:
                if dep_type in framework_values:
                    kwargs[name] = framework_values[dep_type]
                elif name in provider.optional_dep_names:
                    # ``OptionalDep("...")`` from a use_factory inject
                    # list — missing provider lowers to ``None`` so the
                    # factory's positional argument receives a sentinel
                    # instead of an exception.
                    kwargs[name] = None
                elif defaults.get(name, False):
                    # Optional param — let the factory's default apply.
                    continue
                else:
                    raise

        # Class providers: resolve class-body annotated deps FIRST, then
        # construct through ``__new__`` / ``__init__`` with the same
        # kwargs. Setting the attributes *before* ``__init__`` runs is
        # deliberate — it mirrors how ``dataclasses`` populate fields
        # and lets user-written initializers read the injected values.
        if not provider.is_function_provider:
            field_values: dict[str, Any] = {}
            for attr, dep_type in provider.field_deps:
                field_values[attr] = await self.resolve(
                    dep_type,
                    request_cache=request_cache,
                    framework_values=framework_values,
                    owning_module=provider.owning_module,
                )
            # For ``use_class`` the token (``cls`` field) and the
            # *concrete class to instantiate* (``factory`` field) are
            # different objects — the whole point of useClass is that
            # the consumer-facing token may be a string while the
            # implementation is a real Python class. Pick the right
            # one to pass to the constructor.
            target = (
                provider.factory if provider.provider_kind == "class" else provider.cls
            )
            instance = await _construct_class(target, kwargs, field_values)
        else:
            # Function provider: call the factory; the return value IS
            # the dependency. ``await`` if it returns a coroutine.
            factory = provider.factory
            if provider.positional_arg_names:
                # ``use_factory`` records: pass deps positionally so the
                # factory's parameter names stay irrelevant to the
                # contract (lambdas, lambdas with renamed params, and
                # named functions all work identically).
                positional = [kwargs[n] for n in provider.positional_arg_names]
                result = factory(*positional)  # type: ignore[misc]
            else:
                result = factory(**kwargs)  # type: ignore[misc]
            if inspect.isawaitable(result):
                result = await result
            instance = result

        if provider.scope == Scope.SINGLETON:
            self._singletons[provider.cls] = instance
        elif provider.scope == Scope.REQUEST and request_cache is not None:
            request_cache[provider.cls] = instance

        # Run @post_construct for REQUEST and TRANSIENT scopes here — those
        # scopes construct per request (or per resolution) so there is no
        # other sensible place to invoke the hook. Singleton hooks are fired
        # exactly once by :class:`LifecycleScheduler` in topological order
        # during Phase 6; firing them here would cause a double-invoke.
        if provider.post_construct is not None and provider.scope in (
            Scope.REQUEST,
            Scope.TRANSIENT,
        ):
            await _invoke_hook(instance, provider.post_construct)

        return instance

    # -- Lifecycle access --------------------------------------------------

    def singletons(self) -> dict[type, Any]:
        return dict(self._singletons)

    def set_singleton(self, cls: type, instance: Any) -> None:
        self._singletons[cls] = instance
        self._singletons_initialized.add(cls)

    def mark_singleton_initialized(self, cls: type) -> None:
        """Record that a singleton's ``@post_construct`` has already run.

        Called by :class:`LifecycleScheduler` after it invokes the hook in
        topological order during startup so that subsequent ``resolve()``
        calls don't re-fire the hook.
        """
        self._singletons_initialized.add(cls)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_injectable_meta(target: Any) -> InjectableMeta | None:
    """Return the ``@injectable`` metadata, or ``None`` if absent.

    Classes follow the usual no-inheritance rule: reading the marker off
    a base class without the subclass re-decorating raises
    :class:`MetadataInheritanceError`. Functions simply look at the
    attribute; there's no inheritance surface for plain functions.
    """
    if not isinstance(target, type):
        return getattr(target, INJECTABLE_META, None)
    own_meta = target.__dict__.get(INJECTABLE_META)
    if own_meta is not None:
        return own_meta
    # Attribute not in own __dict__ but reachable via MRO -> inherited.
    for base in target.__mro__[1:]:
        if INJECTABLE_META in base.__dict__:
            from ..exceptions import MetadataInheritanceError

            raise MetadataInheritanceError(
                f"{target.__name__} inherited injectable metadata from "
                f"{base.__name__}; re-decorate explicitly."
            )
    return None


def _find_lifecycle_hook(cls: type, marker: str) -> Callable[..., Any] | None:
    for attr_name in dir(cls):
        try:
            attr = getattr(cls, attr_name)
        except AttributeError:
            continue
        if callable(attr) and getattr(attr, marker, False):
            return attr
    return None


async def _invoke_hook(instance: Any, hook: Callable[..., Any]) -> None:
    """Invoke a lifecycle hook bound to ``instance``, awaiting it if needed."""
    bound = getattr(instance, hook.__name__)
    result = bound()
    if inspect.isawaitable(result):
        await result


def _has_unresolved(hints: dict[str, Any]) -> bool:
    """Return True when any resolved annotation still contains a
    :class:`typing.ForwardRef` — i.e. the lenient resolver had to fall
    back for at least one name. The caller then retries with a wider
    namespace (typically the calling-frame stack) before giving up.

    Only the outermost layer is inspected, which covers the common cases
    (a bare name like ``"Config"`` or a one-level generic like
    ``"list[Config]"``). Deeper sweeps are unnecessary because any
    successful evaluation at the top level means the needed name was
    visible in the namespace.
    """
    import typing as _typing

    def _walk(ann: Any) -> bool:
        if isinstance(ann, _typing.ForwardRef):
            return True
        for arg in _typing.get_args(ann):
            if _walk(arg):
                return True
        return False

    return any(_walk(v) for v in hints.values())


def _safe_type_hints(
    fn: Callable[..., Any], *, include_extras: bool = True
) -> dict[str, Any]:
    """Resolve a callable's annotations, tolerating unresolved refs.

    Delegates to :func:`lauren._typing.resolve_type_hints`, which first
    tries the stdlib ``typing.get_type_hints`` (fast path) and then
    falls back to a lenient walker that returns a
    :class:`typing.ForwardRef` for any name it cannot resolve. As a last
    resort we scan the calling frame stack for local names so that a
    ``Depends[factory]`` pointing at a function-scoped type still
    resolves when the test or plugin defined it inline.

    ``include_extras`` defaults to ``True`` so callers see the full
    ``Annotated[T, ...]`` wrapper and can react to ``Inject``,
    :class:`Depends`, and other markers. Callers that just want the
    bare type pass ``include_extras=False``; the helper still strips
    the metadata before returning.
    """
    try:
        hints = resolve_type_hints(fn, include_extras=include_extras)
        if not _has_unresolved(hints):
            return hints
    except Exception:
        hints = {}

    import sys

    frame = sys._getframe(1)
    merged_locals: dict[str, Any] = {}
    while frame is not None:
        for k, v in frame.f_locals.items():
            merged_locals.setdefault(k, v)
        frame = frame.f_back  # type: ignore[assignment]
    try:
        return resolve_type_hints(
            fn,
            globalns=dict(getattr(fn, "__globals__", {}) or {}),
            localns=merged_locals,
            include_extras=include_extras,
        )
    except Exception:
        return hints or {}


def _safe_class_hints(cls: type, *, include_extras: bool = False) -> dict[str, Any]:
    """Resolve class-body annotations, tolerating unresolved strings.

    Tries progressively broader namespaces so PEP 563 / ``from __future__
    import annotations`` stringified references to function-scoped names
    (the common shape when ``Depends[factory]`` is used inside a test
    method or a nested class) still resolve. The order is:

    1. Plain ``get_type_hints(cls)`` — the fast path that succeeds
       whenever the annotation references module-level names.
    2. ``get_type_hints`` with the caller's frame stack merged into a
       local namespace dict. Class-scoped and method-scoped names get
       picked up this way; real-dict ``globalns`` is required because
       the stdlib's ``_eval_type`` rejects mapping-likes.
    3. Raw ``__annotations__`` — unresolved string entries simply don't
       participate in DI, which is the safe fallback.

    ``include_extras`` preserves ``Annotated[...]`` wrappers so callers
    that care about attached metadata (``Depends``, ``Path``, ...) can
    see them. It defaults to ``False`` for backwards compatibility with
    the callable-deps path, which wants the bare inner type.
    """
    try:
        hints = resolve_type_hints(cls, include_extras=include_extras)
        if not _has_unresolved(hints):
            return hints
    except Exception:
        hints = {}

    import sys as _sys

    frame = _sys._getframe(1)
    merged_locals: dict[str, Any] = {}
    while frame is not None:
        for k, v in frame.f_locals.items():
            merged_locals.setdefault(k, v)
        frame = frame.f_back  # type: ignore[assignment]

    # Module globals for the class — look the module up on sys.modules
    # rather than trusting ``cls.__globals__`` (plain classes don't have
    # that attribute; only functions do).
    module = _sys.modules.get(getattr(cls, "__module__", ""))
    globalns: dict[str, Any] = (
        dict(getattr(module, "__dict__", {})) if module is not None else {}
    )
    try:
        return resolve_type_hints(
            cls,
            globalns=globalns,
            localns=merged_locals,
            include_extras=include_extras,
        )
    except Exception:
        return hints or dict(getattr(cls, "__annotations__", {}) or {})


def _describe(target: Any) -> str:
    """Pretty-name a class or function for error messages."""
    name = getattr(target, "__name__", None)
    if name is not None:
        return str(name)
    return str(target)


def _inspect_class_deps(cls: type) -> list[tuple[str, Any]]:
    """Collect ``(param_name, token)`` pairs for a class's DI deps.

    Uses :func:`inspect.signature` on the **class itself** — the only
    reliable way to discover what arguments a class accepts. The
    stdlib walks several alternative sources, in order:

    1. An explicit ``__signature__`` attribute on the class (Pydantic
       v2, attrs, and many DSLs publish their constructor surface
       this way).
    2. The metaclass's ``__call__`` if it is not ``type.__call__``
       (factory metaclasses, plug-in registries).
    3. ``__init__`` and/or ``__new__`` (the usual case).
    4. ``object.__init__`` — yielding an empty signature.

    Inspecting ``cls.__init__`` and ``cls.__new__`` directly — as the
    legacy ``__init__``/``__new__`` merge did — is wrong whenever
    Python's own callable-resolution lands somewhere else: a class
    with a metaclass that bypasses ``__init__`` would be told it
    needs the never-invoked initializer's parameters; a Pydantic
    model with ``__init__(*args, **kwargs)`` would expose no deps at
    all instead of its declared field surface.

    The behaviour is the same as :func:`_inspect_callable_deps` for
    the inner walking of parameters: ``self`` / ``cls`` skipped,
    ``*args``/``**kwargs`` skipped, missing-annotation-without-default
    rejected, ``Annotated[T, Inject("X")]`` honoured, plain
    ``Annotated[T, ...]`` unwrapped to ``T``.

    Forward references (PEP 563 / ``from __future__ import annotations``
    stringified hints) are resolved via :func:`_resolve_class_signature_hints`
    so a class defined in a function-local scope, or a model whose
    annotations were captured as strings, still produces real type
    tokens.
    """
    try:
        sig = inspect.signature(cls)
    except (TypeError, ValueError):
        # Some C-implemented classes refuse signature introspection
        # entirely (PyType_Type, raw bound methods on slot descriptors).
        # Treat them as zero-arg constructors — the user can still
        # write a function provider if they need to feed arguments.
        return []
    sig = _resolve_class_signature_hints(cls, sig)
    # The signature returned by ``inspect.signature(cls)`` does NOT
    # include ``self`` (the stdlib already drops it) but DOES include
    # ``cls`` for ``__new__``-derived signatures. We still skip both
    # defensively in case a custom ``__signature__`` carries them.
    return _params_to_deps(
        sig,
        callable_name=cls.__name__,
        owner_label=cls.__name__,
        skip_first_self_or_cls=False,
    )


def _resolve_class_signature_hints(
    cls: type, sig: inspect.Signature
) -> inspect.Signature:
    """Replace string annotations on ``sig`` with resolved types.

    With ``from __future__ import annotations`` (or any other
    PEP-563-style deferral), :func:`inspect.signature` returns a
    :class:`Signature` whose ``annotation`` fields are still strings.
    The container needs real types to look up providers, so we walk
    every parameter and re-resolve those strings against the most
    informative namespace we can build:

    * the underlying initializer's globals (so module-level types
      always resolve);
    * the calling stack's locals (so class definitions inside a test
      method or factory function still resolve, mirroring what the
      legacy ``_safe_type_hints`` path did);
    * a fallback to the original string annotation if all else fails
      — the caller will then surface a clean ``MissingProviderError``
      pointing at the unresolved name.
    """
    import typing as _typing

    # Fast path: if no parameter has a string-or-ForwardRef annotation,
    # there's nothing to resolve. ``typing.ForwardRef`` shows up when a
    # class machinery (NamedTuple, TypedDict, etc.) captures annotations
    # at class-creation time under ``from __future__ import annotations``.
    def _needs_resolving(ann: Any) -> bool:
        return isinstance(ann, (str, _typing.ForwardRef))

    if not any(_needs_resolving(p.annotation) for p in sig.parameters.values()):
        return sig

    # Build the resolution namespace. Prefer the class's own module
    # globals; layer the most relevant initializer's globals on top so
    # type-only imports done in that file are visible.
    globalns: dict[str, Any] = {}
    init_fn = cls.__dict__.get("__init__")
    new_fn = cls.__dict__.get("__new__")
    for fn in (init_fn, new_fn):
        if fn is not None:
            globalns.update(getattr(fn, "__globals__", {}) or {})
    if not globalns:
        # No user-defined initializer; pull the class module's globals.
        import sys as _sys

        mod = _sys.modules.get(cls.__module__)
        if mod is not None:
            globalns.update(vars(mod))

    # Walk the calling stack so locally-defined types (class declared
    # inside a function) still resolve. Mirrors the fallback in the
    # legacy ``_safe_type_hints`` path so behaviour stays consistent.
    import sys as _sys

    localns: dict[str, Any] = {}
    frame = _sys._getframe(1)
    while frame is not None:
        for k, v in frame.f_locals.items():
            localns.setdefault(k, v)
        frame = frame.f_back  # type: ignore[assignment]

    # Stringified annotations under PEP 563 may carry an extra layer
    # of quoting when the source already wrote the annotation as a
    # literal string (``foo: "UpstreamService"`` becomes the Python
    # string ``"'UpstreamService'"`` after ``__future__`` defers it).
    # Plain :func:`eval` peels both layers cleanly and avoids
    # depending on the rapidly-evolving private API of
    # :class:`typing.ForwardRef` (Python 3.12 added ``type_params``;
    # Python 3.13 added ``recursive_guard`` as keyword-only). The
    # call is restricted to the dependency-resolution namespaces we
    # built above, so it cannot reach arbitrary expressions — the
    # same trust boundary applied by :func:`typing.get_type_hints`.

    def _resolve_string_annotation(ann: str) -> Any:
        try:
            resolved = eval(ann, globalns, localns)  # noqa: S307
        except Exception:
            return ann
        # If the resolved value is itself a string the source had
        # nested quoting; peel one more layer.
        if isinstance(resolved, str) and resolved != ann:
            return _resolve_string_annotation(resolved)
        return resolved

    new_params: list[inspect.Parameter] = []
    for param in sig.parameters.values():
        ann = param.annotation
        if isinstance(ann, str):
            param = param.replace(annotation=_resolve_string_annotation(ann))
        elif isinstance(ann, _typing.ForwardRef):
            # NamedTuple / TypedDict-style — the class machinery
            # already wrapped the string in a ForwardRef. Use the same
            # eval-based resolution by extracting the inner string.
            inner = getattr(ann, "__forward_arg__", None)
            if isinstance(inner, str):
                param = param.replace(annotation=_resolve_string_annotation(inner))
        new_params.append(param)
    return sig.replace(parameters=new_params)


def _params_to_deps(
    sig: inspect.Signature,
    *,
    callable_name: str,
    owner_label: str,
    skip_first_self_or_cls: bool,
) -> list[tuple[str, Any]]:
    """Walk a :class:`inspect.Signature` and produce DI dep pairs.

    Shared backend for :func:`_inspect_class_deps` (which already
    receives the class-level signature with ``self``/``cls`` removed)
    and :func:`_inspect_callable_deps` (which receives a raw
    ``__init__`` / ``__new__`` / function and should drop the leading
    receiver). Keeping the parameter walk in one place means the
    Inject-vs-Annotated handling and the missing-annotation rule are
    not duplicated across two paths.
    """
    deps: list[tuple[str, Any]] = []
    items = list(sig.parameters.items())
    if skip_first_self_or_cls and items and items[0][0] in ("self", "cls"):
        items = items[1:]
    for name, param in items:
        if name in ("self", "cls"):
            continue
        if param.kind in (
            inspect.Parameter.VAR_POSITIONAL,
            inspect.Parameter.VAR_KEYWORD,
        ):
            continue
        ann = param.annotation
        has_default = param.default is not inspect.Parameter.empty
        if ann is inspect.Parameter.empty:
            if not has_default:
                raise UnresolvableParameterError(
                    f"{owner_label} parameter {name!r} missing annotation",
                    detail={"target": owner_label, "param": name},
                )
            continue
        token = _extract_inject_token(ann)
        if token is None:
            token = _unwrap_annotated(ann)
        deps.append((name, token))
    return deps


def _inspect_callable_deps(
    fn: Callable[..., Any],
    *,
    is_class_init: bool,
    owning_class: type | None = None,
) -> list[tuple[str, Any]]:
    """Collect ``(param_name, token)`` pairs for a callable's DI deps.

    Shared by class ``__init__`` / ``__new__`` inspection and function-
    provider inspection. Rules:

    * ``self`` and ``cls`` are skipped.
    * ``*args`` / ``**kwargs`` are skipped.
    * Parameters without an annotation are rejected unless they carry a
      default; when they carry a default they are simply omitted from
      the DI plan.
    * Parameters annotated with ``Annotated[T, Inject("X")]`` resolve
      against ``"X"`` instead of ``T``. The annotation type is still
      kept on the parameter for static analysis but plays no role in
      runtime resolution. This is how non-class tokens (strings,
      :class:`Token` instances) reach into a class ``__init__``.
    * Plain ``Annotated[T, ...]`` whose metadata does NOT contain an
      :class:`_InjectMarker` is unwrapped to ``T`` so the existing
      class-token resolution path keeps working.
    """
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return []
    hints = _safe_type_hints(fn)
    # Replace each parameter's annotation with the resolved type-hint
    # so forward refs work, then dispatch to the shared walker.
    new_params = [
        param.replace(annotation=hints.get(name, param.annotation))
        for name, param in sig.parameters.items()
    ]
    sig = sig.replace(parameters=new_params)
    target_name = (
        owning_class.__name__
        if owning_class is not None
        else getattr(fn, "__name__", "?")
    )
    return _params_to_deps(
        sig,
        callable_name=getattr(fn, "__name__", "<callable>"),
        owner_label=target_name,
        skip_first_self_or_cls=False,
    )


def _extract_inject_token(ann: Any) -> Any | None:
    """Return the token an ``Annotated[T, Inject(...)]`` carries, else ``None``.

    Walks the metadata tuple looking for the first :class:`_InjectMarker`.
    Multiple markers on the same parameter is a programming error; we
    take the first one and trust the user to spot the duplicate —
    raising a startup-time error here would force users to special-case
    re-decorations they have a legitimate reason to write.
    """

    metadata = getattr(ann, "__metadata__", None)
    if not metadata:
        return None
    for entry in metadata:
        if isinstance(entry, _InjectMarker):
            return entry.token
    return None


def _collect_field_deps(cls: type) -> list[tuple[str, type]]:
    """Collect class-body-annotated DI fields.

    Only annotations that **clearly name an injectable collaborator**
    are harvested as DI fields. This deliberately narrow rule keeps
    lauren compatible with ``@dataclass`` and attrs-style classes out
    of the box: plain-data fields like ``name: str``,
    ``count: int = 0`` or ``tags: list[str] = field(default_factory=list)``
    are NOT mistaken for DI dependencies, so the container doesn't try
    to resolve a ``str`` / ``int`` / ``list`` provider it was never
    asked to create.

    A class-body annotation is treated as a DI field when **all** of
    the following hold:

    * The attribute name lives in the class's own ``__annotations__``
      dict (inherited annotations stay on their owner class).
    * The class body does NOT set a value for the attribute — neither a
      literal (``x: int = 5``) nor a dataclass descriptor
      (``x: int = field(default=...)``).
    * The dataclass machinery does NOT list the attribute in
      ``__dataclass_fields__`` (any field registered there has either
      a default, a default-factory, or is required by ``__init__`` and
      should be set by the caller rather than the container).
    * The resolved type is a concrete class that is either decorated
      with ``@injectable`` or a runtime-checkable ``Protocol``.

    Everything else is treated as plain data and left alone. Users who
    want true DI-injected class fields can still opt in: annotate the
    field with a type that carries an ``@injectable`` decorator (or is
    itself a ``Protocol`` bound by a provider) and omit any class-body
    default.
    """
    # Python 3.14+ (PEP 649/annotationlib) no longer stores ``__annotations__``
    # directly in ``cls.__dict__``; the key is absent and the annotations are
    # produced on demand via ``__annotate_func__``.  ``inspect.get_annotations``
    # handles this transparently on all supported Python versions (3.10+).
    own_annotations = inspect.get_annotations(cls, eval_str=False) or {}
    if not own_annotations:
        return []
    # Resolve hints with include_extras=True so ``Annotated[T, Depends]``
    # survives the ``get_type_hints`` pass. The DI collector needs the
    # metadata (``Depends``, ``Path``, ...) to decide whether a field
    # with a non-class annotation (e.g. ``Depends[function_factory]``)
    # should still be registered.
    hints = _safe_class_hints(cls, include_extras=True)
    # Dataclasses register every field in ``__dataclass_fields__`` —
    # we use that as a second defence against treating plain-data
    # fields (even those without an in-body default) as DI fields.
    dataclass_fields: dict[str, Any] = getattr(cls, "__dataclass_fields__", {}) or {}
    deps: list[tuple[str, type]] = []
    for name in own_annotations:
        if name in cls.__dict__:
            # Class-level default present → not a DI field.
            continue
        if name in dataclass_fields:
            # Dataclass-declared field → initialised by the generated
            # ``__init__`` (or a default_factory); never DI-injected.
            continue
        ann = hints.get(name, own_annotations[name])
        # Stringified annotations we couldn't resolve stay as strings;
        # skip them so the class still loads (the user will see the
        # attribute absent and get a regular AttributeError if they try
        # to read it, which is the right signal).
        if isinstance(ann, str):
            continue
        # ``Annotated[T, Inject("TOKEN")]`` overrides the default
        # type-hint-as-token convention. The injected token may be a
        # string or a Token instance, neither of which would pass
        # ``_looks_injectable`` — so we short-circuit BEFORE that gate.
        inject_token = _extract_inject_token(ann)
        if inject_token is not None:
            deps.append((name, inject_token))
            continue
        if not _looks_injectable(ann):
            # The annotation names something the container obviously
            # cannot produce (``str``, ``int``, ``list[str]``, ...). We
            # leave the attribute alone — the user's ``__init__`` (or a
            # future assignment) is responsible for populating it.
            continue
        # Unwrap ``Annotated[T, Depends]`` / ``Annotated[T, Marker]`` so
        # the downstream resolver sees the bare token it was registered
        # under (the function factory, the injectable class, etc.)
        # rather than the typing wrapper.
        deps.append((name, _unwrap_annotated(ann)))
    return deps


def _unwrap_annotated(ann: Any) -> Any:
    """Strip a top-level ``Annotated[T, ...]`` wrapper, returning ``T``.

    Leaves non-annotated types untouched. Used by the field-deps
    collector so the DI container's token lookup sees the registered
    class / function rather than the typing wrapper.
    """
    import typing as _typing

    if _typing.get_origin(ann) is None:
        return ann
    # Annotated[...] always has a non-empty ``__metadata__`` tuple; any
    # other typing generic (``list[int]``, ``dict[str, X]``) reaches
    # here with an empty metadata and we should leave it alone.
    if getattr(ann, "__metadata__", None):
        args = _typing.get_args(ann)
        if args:
            return args[0]
    return ann


# Primitive / stdlib types the container will never resolve as DI
# dependencies. Kept as a frozenset of tokens so the membership check
# is a single pointer comparison.
_NON_INJECTABLE_PRIMITIVES: frozenset[type] = frozenset(
    {
        str,
        bytes,
        bytearray,
        int,
        float,
        complex,
        bool,
        type(None),
        list,
        tuple,
        set,
        frozenset,
        dict,
    }
)


def _structural_protocol_providers(
    protocol: Any,
    owning_module: type | None,
    providers: dict[Any, "Provider"],
    is_visible: "Callable[[Provider, type | None], bool] | None" = None,
) -> list["Provider"]:
    """Return providers whose class is a structural subtype of *protocol*.

    Filtered by *is_visible*: only providers that are visible from
    *owning_module* are returned.  This ensures ``runner: AgentRunner``
    resolves to a runner that is actually reachable from the consumer's module
    (either owned by it or exported from an imported module), not an
    unrelated sibling runner.

    When *owning_module* is ``None`` or *is_visible* is ``None`` all providers
    are scanned (global / test context).
    """
    matches: list[Provider] = []
    for p in providers.values():
        if not isinstance(p.cls, type):
            continue
        if owning_module is not None and is_visible is not None:
            if not is_visible(p, owning_module):
                continue
        try:
            if issubclass(p.cls, protocol) and p.cls is not protocol:
                matches.append(p)
        except TypeError:
            pass
    return matches


def _looks_injectable(ann: Any) -> bool:
    """Return True when ``ann`` plausibly names an injectable collaborator.

    The check is deliberately structural so lauren stays compatible with
    the full range of idioms users reach for:

    * Bare ``@injectable`` classes — always accepted.
    * Runtime-checkable ``Protocol`` tokens — may be bound at
      registration time via ``provides=[...]``.
    * ``Depends[factory_fn]`` / ``Depends[SomeClass]`` — an explicit
      field-level opt-in to function-provider or class-provider
      injection. These arrive here wrapped in
      ``Annotated[T, Depends]``; we unwrap and recurse so the inner
      type is inspected on its own terms.
    * Other ``ExtractionMarker`` subclasses attached via ``Annotated``
      (``Path[int]``, ``Header[str]``, ...) — accepted on field level
      for symmetry with handler-parameter extractors.

    Stdlib primitives (``str``, ``int``, ``list[...]``, ``dict[...]``)
    are rejected so plain-data fields on ``@dataclass``-decorated
    providers aren't mistaken for DI dependencies.
    """
    import typing as _typing

    origin = _typing.get_origin(ann)
    # ``Annotated[T, Marker, ...]`` — inspect the metadata for a lauren
    # extractor marker (``Depends``, ``Path``, ...) and treat any such
    # form as an injectable field. Falling through to the inner type
    # check gives callers who wrote ``Annotated[int, SomeUnrelatedMeta]``
    # the right answer too.
    if origin is not None and _typing.get_origin(ann) is not None:
        # ``typing.Annotated`` identity test varies a little across Python
        # versions; ``__metadata__`` is the stable marker.
        metadata = getattr(ann, "__metadata__", ())
        if metadata:
            for meta in metadata:
                # A lauren extractor marker class or instance is enough
                # to justify collecting the field.
                if _is_extractor_marker(meta):
                    return True
            # Annotated with something unrelated — check the inner type.
            inner = _typing.get_args(ann)
            if inner:
                return _looks_injectable(inner[0])
            return False
        # ``list[T]`` is the one parameterised generic the container
        # knows how to materialise — it stands in for "every multi-bound
        # provider of T". Recurse into ``T`` so a field typed
        # ``list[SomeProtocol]`` is collected as a DI field when
        # ``SomeProtocol`` itself is injectable.
        element_type = _multi_binding_element_type(ann)
        if element_type is not None:
            return _looks_injectable(element_type)
        # Other parameterised generics (``dict[K, V]``, ``tuple[X, ...]``,
        # …) never name an injectable provider directly.
        return False
    if not isinstance(ann, type):
        return False
    if ann in _NON_INJECTABLE_PRIMITIVES:
        return False
    # Explicit opt-in: the class carries an ``@injectable`` marker.
    if hasattr(ann, "__lauren_injectable__"):
        return True
    # Protocol — may be bound by a provider at registration time.
    if getattr(ann, "_is_runtime_protocol", False):
        return True
    # Last resort: a user-defined class in the application itself.
    # The compiler will reject this as ``MissingProviderError`` if no
    # provider is actually registered, surfacing the mistake early
    # rather than silently skipping the field.
    return True


def _is_extractor_marker(obj: Any) -> bool:
    """Return True when ``obj`` is (or is a subclass of) a lauren extractor
    marker — ``Depends``, ``Path``, ``Header``, etc.

    Kept as a local helper to avoid importing the extractor module at
    the top of this file (which would drag pydantic into the DI core's
    import path).
    """
    try:
        from ..extractors import ExtractionMarker
    except Exception:  # pragma: no cover - defensive
        return False
    if isinstance(obj, type) and issubclass(obj, ExtractionMarker):
        return True
    if isinstance(obj, ExtractionMarker):
        return True
    return False


def _callable_default_map(provider: Provider) -> dict[str, bool]:
    """Map ``param_name -> has_default`` for the provider's factory.

    Used during compile and instantiation to detect optional params —
    those may silently skip DI resolution if no provider exists.

    For ``provider_kind == 'factory'`` (use_factory) we don't ask the
    user's lambda about its defaults: the dependency contract there is
    declared positionally via ``injects=[...]`` and optional entries
    are tagged by :class:`OptionalDep`. Returning ``{}`` here lets the
    OptionalDep path own that decision rather than the lambda's
    parameter defaults.
    """
    if provider.provider_kind in ("value", "factory", "existing"):
        return {}
    target: Any
    if provider.is_function_provider:
        target = provider.factory
    elif provider.provider_kind == "class":
        # use_class: the bound class lives on ``factory``; ``cls`` is
        # the token (which may be a string). Use the bound class for
        # signature introspection.
        target = provider.factory
        if target is None:
            return {}
    else:
        # Standard class provider — inspect the class as a callable
        # so the same ``__signature__`` / metaclass-``__call__``
        # priority applies as in :func:`_inspect_class_deps`.
        target = provider.cls
    try:
        sig = inspect.signature(target)
    except (TypeError, ValueError):
        return {}
    return {
        n: (p.default is not inspect.Parameter.empty) for n, p in sig.parameters.items()
    }


async def _construct_class(
    cls: type,
    init_kwargs: dict[str, Any],
    field_values: dict[str, Any],
) -> Any:
    """Construct an instance of ``cls`` via Python's normal call protocol.

    Strategy:

    1. Filter ``init_kwargs`` down to the parameters
       :func:`inspect.signature` advertises for ``cls`` — that's the
       authoritative shape, regardless of whether the eventual call
       reaches ``__init__`` / ``__new__`` / a metaclass ``__call__``
       / a ``__signature__``-published surface.
    2. Invoke ``cls(**filtered_kwargs)``. Whatever construction path
       Python chooses (metaclass ``__call__`` first, then
       ``__new__`` + ``__init__`` by default) runs verbatim.
    3. Inject any class-body-annotated DI fields as plain attributes
       on the resulting instance.

    This is a deliberate departure from the original implementation,
    which emulated :meth:`type.__call__` directly by walking
    ``cls.__new__`` and ``cls.__init__`` itself. That approach broke
    every class whose construction is actually mediated by a custom
    metaclass or a published ``__signature__`` (Pydantic models,
    attrs classes, NestJS-style factory metaclasses, ORM identity
    caches — anything that wires its own call protocol). Letting
    Python do the call is simpler and correct for every shape.

    .. note::
        Class-body-annotated DI fields are now set **after** the
        instance returns from ``cls(**kwargs)``. If a user-written
        ``__init__`` needs the value, they should declare it as an
        ``__init__`` parameter (the standard, framework-agnostic
        contract) rather than as a class-body annotation.

    Any kwarg that doesn't match the class's signature is silently
    dropped — useful when the same dep dict is shared with sibling
    callables (function providers, etc.) that consume different
    subsets.
    """
    try:
        sig = inspect.signature(cls)
        sig = _resolve_class_signature_hints(cls, sig)
    except (TypeError, ValueError):
        sig = None

    if sig is not None:
        kwargs = _filter_kwargs_for_signature(init_kwargs, sig, skip=())
    else:
        # No usable signature — best effort: pass everything and let
        # the class itself complain. This branch is reached only for
        # the rare C-extension class that refuses introspection.
        kwargs = init_kwargs

    result = cls(**kwargs)
    if inspect.isawaitable(result):
        # ``cls(**kwargs)`` returning a coroutine is unusual but
        # possible if a metaclass ``__call__`` is async. Await it
        # consistently with the rest of the resolver path.
        result = await result  # pragma: no cover

    # Field injection happens after construction. The class — and any
    # metaclass mediating its construction — has finished its work,
    # so we can safely layer DI-resolved fields on top. Frozen /
    # immutable instances reject ``__setattr__``; we skip those gracefully
    # because a user opting into immutability should be expressing
    # their dependencies through constructor parameters anyway.
    for attr, value in field_values.items():
        try:
            object.__setattr__(result, attr, value)
        except (AttributeError, TypeError):
            pass
    return result


def _filter_kwargs_for_signature(
    kwargs: dict[str, Any],
    sig: inspect.Signature,
    *,
    skip: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Return the subset of ``kwargs`` accepted by ``sig``.

    Respects ``**kwargs`` (everything goes through) and filters out any
    names the signature doesn't declare explicitly when no var-keyword
    parameter is present.
    """
    has_var_kw = any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
    )
    if has_var_kw:
        return {k: v for k, v in kwargs.items() if k not in skip}
    accepted = {
        n
        for n, p in sig.parameters.items()
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        )
        and n not in skip
    }
    return {k: v for k, v in kwargs.items() if k in accepted}


__all__ = [
    "DIContainer",
    "Provider",
    "InjectableMeta",
    "INJECTABLE_META",
]
