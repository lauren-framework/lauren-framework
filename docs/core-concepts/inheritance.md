# Class Inheritance Rules

> **Lauren has strict, opt-in inheritance for every metadata-bearing decorator.** Subclasses are *not* automatically controllers, injectables, modules, middlewares, guards, or exception handlers — even when their parent class is. This is a deliberate, hard-edged rule.

## The TL;DR

| Decorator | Inherited by subclasses? |
|---|---|
| `@injectable` | ❌ No — re-decorate explicitly |
| `@controller` | ❌ No — re-decorate explicitly |
| `@module` | ❌ No — re-decorate explicitly |
| `@middleware` | ❌ No — re-decorate explicitly |
| `@exception_handler` | ❌ No — re-decorate explicitly |
| `@get` / `@post` / ... (route methods) | ✅ Yes — plain Python MRO |
| `@post_construct` / `@pre_destruct` | ✅ Yes — plain Python MRO |
| `@use_guards` / `@use_middleware` / `@use_exception_handlers` | ❌ Attached to exact target only |

If you try to register a class that has inherited a parent's decoration without redeclaring it, Lauren raises `MetadataInheritanceError` at startup.

## Why this is enforced

When you subclass, you usually do it for one of three reasons:

1. **Implementation reuse** — share a method or property, no semantic relationship to the parent's role.
2. **Specialisation** — extend the parent and *also* register the child as the same role (controller / injectable / etc.).
3. **Polymorphism** — the parent is an interface; the child is one of many implementations.

Cases (1) and (3) are **far more common** than (2) in real codebases. If decorators inherited automatically, every helper class that subclasses a base controller for code reuse would silently become a registered controller, with its own routes, its own DI lifetime, and its own request-scoped instance.

The bug surface here is ugly:

* You add a "private utility" subclass; production now exposes a duplicate set of routes.
* You make a test fixture that subclasses your real `UserService`; the test fixture becomes a registered injectable and shadows the real one in some module graphs.
* You refactor a controller to extract shared logic into a base; the base also gets routes registered.

Lauren's stance: **subclassing is for code, not for registration**. If you want the subclass to be registered, say so.

## What it looks like in practice

### Injectables

```python
@injectable()
class Base:
    def shared(self) -> str:
        return "hi"

# Child inherits behavior but NOT injectability:
class ChildInternal(Base):
    pass

# Registering ChildInternal as a provider raises MetadataInheritanceError.
# But you can use it as a plain class (e.g. as a helper in another service).

# To register it, opt in:
@injectable()
class ChildInjectable(Base):
    pass
```

### Controllers

```python
@controller("/a")
class A:
    @get("/idx")
    async def idx(self) -> dict:
        return {"src": "A.idx"}

# This is NOT a controller. Registering it raises MetadataInheritanceError.
class B(A):
    pass

# Method-level @get *does* propagate (Python MRO), so you don't need to
# rewrite handlers. You just need to re-attach @controller:
@controller("/b")
class B2(A):
    pass
# → B2 exposes /b/idx, calling A.idx(self) (now self: B2 instance).
```

You can override methods in the subclass like normal Python — and decorate them with new `@get` calls if you want to register additional routes:

```python
@controller("/v2")
class B3(A):
    @get("/idx2")
    async def idx2(self) -> dict:
        return {"src": "B3.idx2"}
# → B3 exposes /v2/idx (inherited from A.idx) and /v2/idx2 (new).
```

### Modules

```python
@module(providers=[A])
class BaseModule: ...

class DerivedModule(BaseModule):
    pass    # not a module — registering raises MetadataInheritanceError

@module(providers=[A, B])
class ConcreteModule(BaseModule):
    pass    # OK
```

A practical pattern: a `BaseModule` class exists only to share a class body (e.g. shared `providers=[...]`), and concrete modules subclass it *and* re-decorate.

### Middleware, guards, and exception handlers

`@middleware` and `@exception_handler` follow the same rule — re-decorate the subclass. `@use_guards`, `@use_middleware`, and `@use_exception_handlers` attach to **the exact target** (class or method) only — a subclass that wants the parent's attached guards must re-declare them:

```python
@use_guards(AuthGuard)
@controller("/private")
class Parent:
    @get("/")
    async def idx(self): ...

@controller("/v2")
class Child(Parent):
    pass
# → Child has /v2/idx (inherited handler) but NO AuthGuard. Re-declare:

@use_guards(AuthGuard)
@controller("/v2")
class ChildOK(Parent):
    pass
```

This is intentional. Imagine the alternative: a child controller extending an authenticated parent silently inherits *and silently doesn't apply* a guard depending on whether the parent was decorated before or after the child. That's a security bug waiting to happen.

## What *does* inherit normally

* **Method-level route decorators** (`@get`, `@post`, `@put`, ...). These propagate via plain Python MRO. A `@controller`-decorated subclass automatically picks up parent handlers under its own prefix.
* **Lifecycle hooks** (`@post_construct`, `@pre_destruct`). They're attached to the *method*; if a subclass inherits the method, it inherits the hook. If a subclass overrides the method, only the override runs (unless it explicitly calls `super()`).
* **Plain methods, attributes, properties** — everything Python normally inherits.

The boundary is therefore clear: **class-level decorators are opt-in; method-level decorators ride the MRO like every other method attribute**.

## Migrating from "automatic inheritance" frameworks

If you're coming from a framework where subclasses inherit registration automatically, the refactor is mechanical:

1. Run your test suite with Lauren (it'll raise `MetadataInheritanceError` immediately).
2. For every error, decide: did the subclass *intend* to be a controller / injectable / module? If yes, decorate it. If no, leave it un-decorated (and Lauren won't try to register it).
3. Verify that your route table is the one you expected by reading `app.routes()` or `app.openapi()`.

Most users find the migration takes minutes and immediately surfaces 1–2 routes/services that were registered by accident in the old framework.

## When this rule bites — and when it saves you

This rule **bites** the first time you write:

```python
@controller("/admin")
class AdminController:
    ...

class TestAdminController(AdminController):       # for an integration test
    ...
```

…and the test runner blows up with `MetadataInheritanceError`. Five seconds of confusion, then you realize: the parent intended *only that exact class* to be the controller. Adding `@controller("/admin-test")` to the child is one line, and now the test setup is explicit.

The same rule **saves** you when:

* A junior dev refactors `UserController` into a base with two children, and the staging environment fails to start because both children would conflict on `/users` — caught in CI, not at 3 a.m.
* Someone subclasses `AuthService` to create a mock that lives in `tests/`. The mock is *never* accidentally registered as a real provider in production graphs.

## See also

* [Modules](modules.md) — how visibility and inheritance interact at the module level.
* [Injectables & Providers](injectables.md) — DI scopes and Protocol binding.
* [Controllers](controllers.md) — class-level vs route-level decorator composition.
