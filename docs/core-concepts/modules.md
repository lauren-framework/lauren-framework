# Modules

> A module is the unit of **dependency visibility**. It's where you say *what providers and controllers exist together*, and *what other modules can see from yours*.

Lauren's module system is borrowed directly from NestJS and serves the same purpose: in a codebase larger than one file, you need a way to scope dependencies so a `UserRepository` from the `users` module can't accidentally be reached from the `billing` module without an explicit import.

## What a module does

A module groups four things:

| Field | What it declares |
|---|---|
| `controllers` | Classes the HTTP router exposes. |
| `providers` | Classes (or custom-provider records) added to the DI container. |
| `imports` | Other `@module`-decorated classes whose `exports` become visible here. |
| `exports` | Subset of `providers` and `imports` re-exposed to modules that import this one. |

```python
from lauren import module

@module(
    controllers=[UserController, ProjectController],
    providers=[Clock, DbSession, UserRepo],
    imports=[SharedModule],
    exports=[DbSession],
)
class AppModule:
    pass
```

## What a module does **not** do

* **It does not run code.** A module is metadata. The decorated class body never executes any logic on import. Decorations attach a `ModuleMeta` payload; that's it.
* **It is not a singleton.** Modules are not instantiated. `LaurenFactory.create(AppModule)` walks the module graph by reading `ModuleMeta` from each class — `AppModule` itself is never constructed.
* **It does not implicitly inherit.** Subclassing a `@module` class does not produce a new module. Like every other Lauren decorator, `@module` enforces strict opt-in (see [Class Inheritance Rules](inheritance.md)).

## Visibility, in one rule

> A token is visible inside a module if and only if (a) it is declared as a provider here, **or** (b) it is exported by a module imported here (transitively, but only along `exports` edges).

Concretely:

```python
@module(providers=[Clock], exports=[Clock])
class SharedModule: ...

@module(providers=[Db], imports=[SharedModule])      # Db can use Clock
class DataModule: ...

@module(controllers=[UserController], imports=[DataModule])
class AppModule: ...
```

Inside `AppModule`, `Db` is **not** visible — `DataModule` did not export it. `Clock` is **not** visible either — visibility doesn't transit through a module that didn't re-export it. If `UserController.__init__` asks for `Db` here, startup raises `MissingProviderError`.

To make `Db` visible at the top:

```python
@module(providers=[Db], imports=[SharedModule], exports=[Db])
class DataModule: ...
```

This explicit-by-default rule is what makes Lauren module graphs readable in large codebases. There are no "spooky" providers reachable just because they exist somewhere in the import tree.

## Errors raised at startup

| Error | Meaning |
|---|---|
| `CircularModuleError` | An import cycle exists in the module graph. |
| `ModuleExportViolation` | A module exports something it neither declares as a provider nor imports. |
| `MissingProviderError` | A provider tries to inject something not visible from its module. |
| `DuplicateBindingError` | The same class is registered as a provider twice. |
| `MetadataInheritanceError` | A subclass of an `@module` class is registered without re-decoration. |

All of these are caught in `LaurenFactory.create(...)` — no broken graphs make it to runtime.

## A larger example

```python
from lauren import module, injectable, controller, get
from lauren.exceptions import HTTPError

# --- shared infrastructure ---
@injectable()
class Clock:
    def now(self) -> float: ...

@module(providers=[Clock], exports=[Clock])
class SharedModule: ...


# --- users feature ---
@injectable()
class UserRepo:
    def __init__(self, clock: Clock) -> None: ...

@controller("/users")
class UserController:
    def __init__(self, repo: UserRepo) -> None: ...
    @get("/")
    async def list(self): ...

@module(
    controllers=[UserController],
    providers=[UserRepo],
    imports=[SharedModule],          # for Clock
    exports=[UserRepo],               # so other modules can compose us
)
class UsersModule: ...


# --- billing feature ---
@injectable()
class Invoicer:
    def __init__(self, repo: UserRepo, clock: Clock) -> None: ...

@module(
    providers=[Invoicer],
    imports=[UsersModule, SharedModule],
)
class BillingModule: ...


# --- root ---
@module(imports=[UsersModule, BillingModule])
class AppModule:
    pass
```

A graph this size is already enough to feel the benefits: when someone reading `BillingModule` asks "where does `Invoicer` get `UserRepo` from?", the answer is one line up — `imports=[UsersModule, ...]` — not buried in some implicit auto-discovery convention.

## Best practices

* **Export the smallest surface.** Modules are easier to refactor when they re-export only the providers they intend others to depend on. Internal helpers stay internal.
* **One module per feature, not per layer.** A `UsersModule` containing `UserController`, `UserRepo`, and `UserService` is easier to evolve than three layered modules (`ControllersModule`, `ServicesModule`, `ReposModule`).
* **Shared infra goes in a `SharedModule`.** Cross-cutting providers (clocks, telemetry, config) belong in a module everything else imports.
* **Don't fight the explicitness.** If you find yourself wanting to "just make everything available everywhere", reach for a `SharedModule` and explicit imports rather than a global container — your future self will thank you when the graph stops fitting in your head.

Continue to [Controllers →](controllers.md).
