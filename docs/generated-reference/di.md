# Dependency Injection

Custom provider recipes and DI container utilities.

## Custom providers

### `use_value`

```python
def use_value(provide: Any, value: Any, multi: bool = False) -> CustomProvider
```

Bind ``provide`` to a pre-built value.

The value is treated as a singleton (no factory ever runs) and is
returned by the container on every ``resolve()`` call. Common
uses:

* inject a mock service in tests::

      use_value(provide=CatsService, value=mock_cats_service)

* register an externally-constructed object as a DI citizen::

      use_value(provide="REDIS", value=redis.from_url(...))

* expose a literal config dict to handlers::

      use_value(provide="CONFIG", value={"debug": True})

``multi=True`` is supported for completeness — multiple
:func:`use_value` registrations sharing a token can be assembled
into a list with ``list[T]`` consumers, although the more common
multi-binding pattern is on services rather than values.

### `use_class`

```python
def use_class(provide: Any, use: type, scope: Scope = Scope.SINGLETON, multi: bool = False) -> CustomProvider
```

Bind ``provide`` to a class — typically *different* from the token.

The classic example is environment-conditional configuration::

    configServiceProvider = use_class(
        provide=ConfigService,
        use=DevelopmentConfigService if dev else ProductionConfigService,
    )

The chosen class is constructed through the standard DI pipeline,
so its own ``__init__`` parameters resolve as if it had been
registered via ``@injectable``. It does not need to itself carry
the ``@injectable`` decoration: lauren auto-marks classes used in
``use_class`` with the matching scope so the factory machinery
works end-to-end.

### `use_factory`

```python
def use_factory(provide: Any, factory: Callable[..., Any], injects: Iterable[Any] = (), scope: Scope = Scope.SINGLETON, multi: bool = False) -> CustomProvider
```

Bind ``provide`` to the result of calling ``factory``.

``injects`` lists the tokens lauren resolves and passes *positionally*
to the factory in declaration order. This positional contract is
deliberately the only one supported — it keeps the call site small
and unambiguous, and matches NestJS's `inject:` semantics::

    use_factory(
        provide="CONNECTION",
        factory=lambda opts, log: DatabaseConnection(opts.get(), log),
        injects=[OptionsProvider, "LOGGER"],
        #           ^^^               ^^^^^^^
        #           class token       string token
    )

Wrap any entry in :class:`OptionalDep` to soften the resolution::

    injects=[OptionsProvider, OptionalDep("LOGGER")]

Async factories work transparently — lauren awaits the return value
when it's a coroutine, so an ``async def`` factory is just a
factory.

### `use_existing`

```python
def use_existing(provide: Any, existing: Any) -> CustomProvider
```

Alias ``provide`` to an already-registered ``existing`` token.

Both tokens resolve to the same instance under singleton scope.
Multiple aliases can chain through several ``use_existing`` rows
— lauren walks the chain at resolve time and rejects cycles loudly.
Aliases inherit the existing provider's scope.

Typical use case: expose the same logger under two names so
legacy code injecting ``"AliasedLoggerService"`` keeps working
while new code injects the class directly::

    use_existing(provide="AliasedLoggerService", existing=LoggerService)

## Injection helpers

### `Token`

```python
class Token(name: str, unique: bool = True)
```

A typed, branded identifier for non-class providers.

Plain strings already work as DI tokens, but ``Token("DB_URL")``
gives three benefits over a bare string:

* **Identity vs equality.** Two ``Token("X")`` instances are
  *different* tokens by default, mirroring Python's ``object``
  identity semantics. This is what NestJS's :class:`Symbol`-based
  tokens give you and what most Python users expect when they
  "create a new token". (Pass ``unique=False`` to opt into
  string-style equality if you need to share the same name across
  processes / modules.)
* **Repr.** Errors mentioning ``Token("DB_URL")`` are far easier
  to grep for than errors mentioning a bare ``"DB_URL"``, which
  could be any string anywhere in the codebase.
* **IDE friendliness.** A module-level ``DB_URL = Token("DB_URL")``
  gives autocomplete and "find usages" without making the token's
  name part of the public API surface.

Tokens are hashable and compare by identity by default, so they
are safe to use as dict keys and set members. They are *not*
classes — passing a Token to a place that expects a Python type
(like a function annotation) is fine because lauren's DI machinery
treats tokens as opaque hashable keys, not as type objects.

### `Inject`

```python
def Inject(token: Any) -> _InjectMarker
```

Override the resolution token for a parameter or class field.

By default lauren resolves a parameter by its type annotation —
``def __init__(self, repo: UserRepo)`` looks up the ``UserRepo``
provider. When the provider is registered against a non-class
token (a string or a :class:`Token`), the type hint cannot itself
*be* the token — Python wouldn't accept a string in that position.
:func:`Inject` is the escape hatch::

    @injectable()
    class CatsRepository:
        def __init__(
            self,
            connection: Annotated[Connection, Inject("CONNECTION")],
        ) -> None:
            self.connection = connection

The annotation type is still used by static checkers (mypy /
pyright will still verify ``connection.execute(...)`` against the
``Connection`` interface), but the runtime resolution uses
``"CONNECTION"`` as the lookup key.

Class fields work the same way::

    @injectable()
    class CatsRepository:
        connection: Annotated[Connection, Inject("CONNECTION")]

For convenience inside ``inject=[...]`` factory lists you may also
pass a bare token (string, :class:`Token`, class) directly without
wrapping it in :func:`Inject` — the wrapper is only needed when
you're writing a Python annotation.

### `OptionalDep`

```python
class OptionalDep(token: Any)
```

Mark a member of an ``injects=[...]`` list as optional.

When the matching provider is missing at resolution time, lauren
passes ``None`` to the factory's positional argument instead of
raising :class:`MissingProviderError`. NestJS exposes the same
capability via ``{ token, optional: true }``; lauren keeps the
affordance type-safe with a small wrapper class so the inject
list reads top-to-bottom.

Example::

    use_factory(
        provide="CONNECTION",
        factory=lambda opts, optional_logger=None: ...,
        injects=[Options, OptionalDep("LOGGER")],
    )

## Container

### `DIContainer`

```python
class DIContainer()
```

Compiled DI container; immutable after :meth:`compile`.

#### `DIContainer.register`

```python
def register(self, target: type | Callable[..., Any], owning_module: type | None = None) -> Provider
```

Register a class or function provider.

Both flavours are accepted. The decorator
(:func:`lauren.injectable`) already attaches
:class:`InjectableMeta`; this method reads that marker, inspects
the target's callable signature (and, for classes, class-body
field annotations), and installs an immutable :class:`Provider`
entry. Classes and functions are routed by the same registry
so module visibility, scope validation, and multi-binding all
work uniformly.

#### `DIContainer.register_value`

```python
def register_value(self, token: Any, value: Any, owning_module: type | None = None, multi: bool = False) -> Provider
```

Register a literal ``value`` under ``token`` (NestJS ``useValue``).

The container caches the value as if it were a singleton and
returns it untouched on every resolve. The factory the
:class:`Provider` carries is a no-op closure so the resolver's
"call factory" path still runs without special-casing.

Multi-binding registrations don't pre-populate the singleton
cache because that cache is keyed by token — two providers for
the same token would race each other. The factory still runs
on resolve, so the value still bypasses any user-construction.

#### `DIContainer.register_class`

```python
def register_class(self, token: Any, cls: type, owning_module: type | None = None, scope: Scope = Scope.SINGLETON, multi: bool = False) -> Provider
```

Register ``cls`` under ``token`` (NestJS ``useClass``).

Unlike a plain ``@injectable`` registration, the bound class
does not have to be the same as the token — that's the whole
point of ``useClass``. The class's ``__init__`` and class-body
deps are introspected as usual so its own collaborators
resolve through DI.

If the class isn't already marked ``@injectable`` we synthesise
the metadata so the user doesn't have to remember to decorate
a target they're only re-binding under another token.

#### `DIContainer.register_factory`

```python
def register_factory(self, token: Any, factory: Callable[..., Any], inject: tuple[Any, ...] = (), scope: Scope = Scope.SINGLETON, owning_module: type | None = None, multi: bool = False) -> Provider
```

Register a factory callable under ``token`` (NestJS ``useFactory``).

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

#### `DIContainer.register_alias`

```python
def register_alias(self, token: Any, existing: Any, owning_module: type | None = None) -> Provider
```

Alias ``token`` to ``existing`` (NestJS ``useExisting``).

Alias rows carry no factory of their own; the resolver detects
``provider_kind == 'existing'`` and forwards the lookup to
the target. Cycle detection runs at compile time.

#### `DIContainer.register_custom`

```python
def register_custom(self, custom: CustomProvider, owning_module: type | None = None) -> Provider
```

Single entry point that dispatches to the right helper.

Used by :class:`LaurenFactory` so the module-graph compiler
doesn't need to know the four-way branch.

#### `DIContainer.set_visible`

```python
def set_visible(self, module_cls: type, tokens: frozenset[type]) -> None
```

Install the visible-token set for ``module_cls``.

Called once per module during Phase 2 of startup with the module's
own providers plus anything re-exported by a transitively imported
module. Subsequent ``resolve(..., owning_module=module_cls)`` calls
are restricted to bindings whose provider class is in that set.

#### `DIContainer.compile`

```python
def compile(self) -> None
```

Validate graph: detect cycles, missing deps, scope violations.

#### `DIContainer.has_provider`

```python
def has_provider(self, token: Any, owning_module: type | None = None) -> bool
```

Return True when ``token`` could be resolved by this container.

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

#### `DIContainer.get_provider`

```python
def get_provider(self, token: Any, owning_module: type | None = None) -> Provider
```

#### `DIContainer.all_providers`

```python
def all_providers(self) -> list[Provider]
```

#### `DIContainer.resolve`

```python
def resolve(self, token: Any, request_cache: dict[type, Any] | None = None, framework_values: dict[type, Any] | None = None, owning_module: type | None = None) -> Any
```

Resolve an instance of ``token``.

``request_cache`` stores request-scoped instances for the current
request. ``framework_values`` is a type-keyed map for objects supplied
by the runtime (e.g. ``Request``). ``owning_module`` restricts which
bindings may be returned: only providers visible to that module are
considered — when omitted, all bindings are eligible.

#### `DIContainer.singletons`

```python
def singletons(self) -> dict[type, Any]
```

#### `DIContainer.set_singleton`

```python
def set_singleton(self, cls: type, instance: Any) -> None
```

#### `DIContainer.mark_singleton_initialized`

```python
def mark_singleton_initialized(self, cls: type) -> None
```

Record that a singleton's ``@post_construct`` has already run.

Called by :class:`LifecycleScheduler` after it invokes the hook in
topological order during startup so that subsequent ``resolve()``
calls don't re-fire the hook.
