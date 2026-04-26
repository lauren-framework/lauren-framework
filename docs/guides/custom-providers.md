# Custom Providers

> When `@injectable` isn't expressive enough — environment-conditional swaps, externally-built objects, alias tokens, factory functions — Lauren ships the four NestJS-style custom-provider recipes: `use_value`, `use_class`, `use_factory`, `use_existing`.

## When to reach for a custom provider

| Situation | Recipe |
|---|---|
| You want to bind a token to a *literal value* (mock, constant, externally built object) | `use_value` |
| You want to bind a token to a *different class* than the token itself (env-conditional swap) | `use_class` |
| You want to *compute* the bound value from a function whose own params are DI-resolved | `use_factory` |
| You want to *alias* one token to another (two names, same instance) | `use_existing` |

All four return a `CustomProvider` record you list in a module's `providers=[...]`. The records are immutable — same `providers=` list can be reused across multiple `LaurenFactory.create` calls (handy for tests that boot variations of the same graph).

## `use_value` — bind a token to a pre-built value

```python
from lauren import use_value, module

@module(providers=[
    use_value(provide="REDIS", value=redis.from_url("redis://localhost")),
    use_value(provide="CONFIG", value={"debug": True, "feature_x": False}),
    use_value(provide=CatsService, value=mock_cats_service),  # test override
])
class AppModule: ...
```

The value is treated as a **singleton** — no factory ever runs, the same object is returned on every resolve.

Common uses:

* **Inject a mock service in tests** — `use_value(provide=CatsService, value=mock)`.
* **Register an externally-constructed object** — pre-built Redis client, S3 boto3 client.
* **Expose a literal config dict** — feature flags, environment data.

Pass `multi=True` to allow multiple `use_value` rows under the same token (collected into a list by `list[T]` consumers).

## `use_class` — bind a token to a different class

The classic case is environment-conditional configuration:

```python
from lauren import use_class, Scope

config_provider = use_class(
    provide=ConfigService,
    use=DevelopmentConfigService if os.environ.get("ENV") == "dev"
        else ProductionConfigService,
    scope=Scope.SINGLETON,
)

@module(providers=[config_provider])
class AppModule: ...
```

What happens:

* The chosen class is constructed through the standard DI pipeline — its own `__init__` parameters resolve as if it had been registered via `@injectable`.
* It does **not** need to itself carry the `@injectable` decoration. Lauren auto-marks classes used in `use_class` with the matching scope so the factory machinery works end-to-end.
* `provide` is the public token; `use` is the implementation.

A second use case: **generic interfaces with a default**. Bind `Logger` to `JsonLogger` in production, `ConsoleLogger` in dev:

```python
use_class(
    provide=Logger,
    use=JsonLogger if PROD else ConsoleLogger,
)
```

Consumers ask for `Logger`. They never know which implementation arrived.

## `use_factory` — compute the value from a DI-resolved function

When the bound value needs to be computed and the computation itself has dependencies, `use_factory` is the right tool:

```python
from lauren import use_factory, OptionalDep

def make_connection(opts: OptionsProvider, log: Logger) -> Connection:
    return Connection(opts.dsn, logger=log)

@module(providers=[
    use_factory(
        provide="CONNECTION",
        factory=make_connection,
        inject=[OptionsProvider, "LOGGER"],     # tokens — resolved positionally
    ),
])
class AppModule: ...
```

The `inject=[...]` list specifies **what tokens to resolve** and the factory receives the resolved instances **positionally** in declaration order.

`inject` entries can be:

* a **class** (`OptionsProvider`)
* a **string token** (`"LOGGER"`)
* a **`Token`** (`DB_URL = Token("DB_URL")`)
* an **`OptionalDep`** wrapper for soft dependencies — uses `None` if unresolved

```python
from lauren import OptionalDep

inject=[OptionsProvider, OptionalDep("METRICS_CLIENT")]
# factory receives (opts, metrics_or_None)
```

### Async factories

Async factories work transparently. Lauren awaits the return value when it's a coroutine:

```python
async def make_pool(dsn: str) -> asyncpg.Pool:
    return await asyncpg.create_pool(dsn)

use_factory(
    provide="POOL",
    factory=make_pool,
    inject=[DB_URL],
    scope=Scope.SINGLETON,
)
```

### Scope choice

`use_factory(scope=...)` picks how often the factory runs:

* `Scope.SINGLETON` (default) — factory runs once at first resolve, result is cached forever.
* `Scope.REQUEST` — factory runs once per request, cached for that request.
* `Scope.TRANSIENT` — factory runs every resolve.

## `use_existing` — alias one token to another

Sometimes two names should map to the same instance. The classic case is supporting a legacy token name while migrating consumers:

```python
from lauren import use_existing

@module(providers=[
    LoggerService,                                                    # the real provider
    use_existing(provide="AliasedLoggerService", existing=LoggerService),
])
class AppModule: ...
```

Now consumers asking for `"AliasedLoggerService"` get the same instance as those asking for `LoggerService`.

* Both tokens resolve to the same object under singleton scope.
* Aliases can chain through several `use_existing` rows — Lauren walks the chain at resolve time and rejects cycles loudly.
* Aliases inherit the existing provider's scope.

## Putting it all together

A realistic module that mixes all four recipes:

```python
import os
from typing import Annotated
from lauren import (
    Inject, Token, module, use_value, use_class, use_factory, use_existing,
)

DB_URL = Token("DB_URL")
REDIS = Token("REDIS")

def make_db_engine(url: str) -> Engine:
    return create_engine(url, pool_size=20)

@module(providers=[
    # 1. Literal config value:
    use_value(provide=DB_URL, value=os.environ["DATABASE_URL"]),

    # 2. Environment-conditional impl:
    use_class(
        provide=Logger,
        use=JsonLogger if os.environ.get("ENV") == "prod" else ConsoleLogger,
    ),

    # 3. Factory with DI-resolved inputs:
    use_factory(
        provide=Engine,
        factory=make_db_engine,
        inject=[DB_URL],
        scope=Scope.SINGLETON,
    ),

    # 4. Alias for legacy code paths:
    use_existing(provide="DBEngine", existing=Engine),

    # Plus a regular @injectable class:
    UserRepository,
])
class AppModule: ...
```

Inside this module, an injectable can ask for any of:

```python
@injectable()
class Repo:
    def __init__(
        self,
        engine: Engine,                              # from use_factory
        log: Logger,                                 # from use_class
        url: Annotated[str, Inject(DB_URL)],         # from use_value
    ) -> None: ...
```

## Tokens — when to use them

`Token("NAME")` is preferred over bare strings because:

* **Identity vs equality.** Two `Token("X")` instances are *different* tokens by default (mirrors NestJS's Symbol-based tokens). Pass `unique=False` to opt into string-style equality if you genuinely need cross-module sharing by name.
* **Repr.** Errors mentioning `Token("DB_URL")` are far easier to grep than errors mentioning a bare string.
* **IDE friendliness.** A module-level `DB_URL = Token("DB_URL")` gives autocomplete and "find usages" without making the name part of the public API.

```python
from lauren import Token

DB_URL = Token("DB_URL")               # unique by default
SHARED_NAME = Token("X", unique=False) # shared by name across modules
```

## Errors raised at startup

| Error | Meaning |
|---|---|
| `DecoratorUsageError` | A custom-provider helper was called incorrectly (e.g. `use_class.use` is not a class). |
| `MissingProviderError` | A factory's `inject` token has no provider. |
| `CircularDependencyError` | A `use_existing` chain forms a cycle. |
| `DuplicateBindingError` | Same token registered twice without `multi=True`. |

## Best practices

* **Prefer `@injectable` for owned classes.** Custom providers are for things you *don't* own (clients, configs) or *can't* declare as a class (literal values, factory results).
* **Keep factories small.** A factory that's more than a few lines usually wants to become an `@injectable` class with a `@post_construct`.
* **Use `Token` for non-class tokens.** Bare strings work, but `Token` is dramatically more debuggable.
* **Wrap optional deps in `OptionalDep`.** Don't try to encode "maybe missing" into the factory's logic — let the container do it.

## See also

* [Declaring an Injectable](declaring-injectables.md) — the basic case.
* [Core Concepts → Injectables & Providers](../core-concepts/injectables.md) — scopes, Protocols, multi-bindings.
* [Modules](../core-concepts/modules.md) — how `providers=` and `exports=` interact.
