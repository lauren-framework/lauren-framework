# `@propagate_metadata`

> `@propagate_metadata(source)` is Lauren's equivalent of `functools.wraps` — it
> copies `@use_guards`, `@use_interceptors`, `@use_middlewares`,
> `@use_exception_handlers`, `@use_encoder`, and `@set_metadata` annotations from
> a *source* object to the decorated target.

## When to use it

The decorator is useful in two main patterns:

1. **Decorator wrappers** — when you write a decorator that wraps an existing
   class or function, propagating its Lauren metadata ensures the wrapper behaves
   identically at the framework level.
2. **Mixin-like composition** — share a common guard/interceptor set between
   multiple controllers without Python inheritance (which Lauren's strict
   own-class rule would ignore anyway).

## Basic usage

```python
from lauren import propagate_metadata, use_guards, controller, injectable, Scope

@injectable(scope=Scope.SINGLETON)
class ApiKeyGuard:
    async def can_activate(self, ctx) -> bool:
        return ctx.request.headers.get("x-api-key") == "secret"

# A plain class that acts as a metadata "mixin"
@use_guards(ApiKeyGuard)
class _AuthMixin:
    pass

# Propagate its guards onto multiple controllers
@propagate_metadata(_AuthMixin)
@controller("/users")
class UserController:
    ...

@propagate_metadata(_AuthMixin)
@controller("/orders")
class OrderController:
    ...
```

Both controllers now behave as if they had `@use_guards(ApiKeyGuard)` applied
directly.

## Decorator wrapper pattern

```python
import functools
from lauren import propagate_metadata, get, controller

def cached_route(fn):
    """Decorator that wraps a route handler and copies its Lauren metadata."""
    @propagate_metadata(fn)
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        # ... caching logic ...
        return await fn(*args, **kwargs)
    return wrapper

@controller("/items")
class ItemController:
    @cached_route
    @get("/{id}")
    @use_guards(AuthGuard)
    async def get_item(self, id: Path[int]):
        ...

# The wrapper keeps @use_guards(AuthGuard) from the original handler.
```

## Ordering: source entries run first

For list-based metadata (guards, interceptors, middlewares, exception handlers),
source entries are **prepended** before the target's own entries:

```python
@use_guards(GuardA)
class Source:
    pass

@propagate_metadata(Source)   # applied last → GuardA prepended
@use_guards(GuardB)           # applied earlier → GuardB already in dict
@controller("/x")
class Target:
    pass

# Effective guard order: [GuardA, GuardB]
# GuardA (from source) runs first as the outermost check.
```

## Encoder: target wins

When both source and target declare a `@use_encoder`, the target's explicit
encoder is kept unchanged:

```python
@use_encoder(OrjsonEncoder())
class Source:
    pass

@propagate_metadata(Source)     # source encoder is ignored here
@use_encoder(PydanticEncoder()) # ← this wins
@controller("/x")
class Target:
    pass
```

## User metadata: target keys win

`@set_metadata` key/value pairs are merged; if the same key exists in both
source and target, the target's value takes precedence:

```python
@set_metadata("cache_ttl", 60)
@set_metadata("auth", "bearer")
class Source:
    pass

@propagate_metadata(Source)
@set_metadata("cache_ttl", 300)  # overrides source's 60
@controller("/fast")
class FastController:
    pass

# Effective metadata: {"cache_ttl": 300, "auth": "bearer"}
```

## Selective propagation

Disable individual categories with keyword arguments:

```python
@propagate_metadata(
    Source,
    interceptors=False,
    encoder=False,
    user_metadata=False,
)
@controller("/partial")
class PartialController:
    pass
# Only guards, middlewares, and exception_handlers are propagated.
```

## Works on functions too

`source` and `target` can both be plain functions or methods:

```python
@use_guards(ApiKeyGuard)
@get("/original")
async def original_handler():
    ...

@propagate_metadata(original_handler)
@get("/alias")
async def alias_handler():
    return await original_handler()
```

## See also

- [Custom Guards](custom-guards.md) — attaching guards to controllers and routes
- [Interceptors](interceptors.md) — `@use_interceptors` and `CallHandler`
- [Reference → Reflect](../reference/reflect.md) — reading propagated metadata back via `reflect_guards` etc.
