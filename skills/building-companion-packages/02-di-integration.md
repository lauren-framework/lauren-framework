# 02 — DI Integration

Companion packages must participate in Lauren's DI container.  Classes that the
host application can inject are decorated with `@injectable`; they are grouped
into a `@module` (the companion's public API surface) and made available via
`imports=` in the host's root module.

## Making companion classes injectable

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class CacheService:
    """Application-wide cache backed by an in-process dict."""

    def __init__(self, config: "CacheConfig") -> None:
        self._ttl = config.default_ttl_seconds
        self._store: dict[str, object] = {}

    async def get(self, key: str) -> object | None:
        return self._store.get(key)

    async def set(self, key: str, value: object) -> None:
        self._store[key] = value
```

`Scope.SINGLETON` is the right default for stateful services that manage
external connections or in-process state.  Use `Scope.REQUEST` for per-request
contexts (e.g. a tracing span), `Scope.TRANSIENT` for cheap value objects.

## Companion module with exports

```python
from lauren import module, use_value
from ._service import CacheService
from ._config import CacheConfig

@module(
    providers=[CacheService, CacheConfig],
    exports=[CacheService],   # CacheConfig is internal — not exported
)
class _InternalCacheModule:
    pass
```

`exports=` controls visibility: only exported tokens are resolvable by modules
that `import` this one.  Internal helpers stay encapsulated.

## Host application imports the companion module

```python
from lauren import module
from lauren_cache import CacheModule

@module(imports=[CacheModule.for_root(CacheConfig(default_ttl_seconds=300))])
class AppModule:
    pass
```

## Full worked example: `lauren-cache`

### File layout

```
src/lauren_cache/
├── __init__.py
├── _config.py          ← CacheConfig dataclass
├── _service.py         ← CacheService (injectable)
├── _module.py          ← CacheModule with .for_root()
└── py.typed
```

### `_config.py`

```python
from __future__ import annotations
from dataclasses import dataclass, field

@dataclass
class CacheConfig:
    default_ttl_seconds: int = 300
    max_entries: int = 10_000
    namespace: str = "cache"
```

### `_service.py`

```python
from __future__ import annotations
import time
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class CacheService:
    def __init__(self, config: "CacheConfig") -> None:
        self._ttl = config.default_ttl_seconds
        self._ns = config.namespace
        self._store: dict[str, tuple[object, float]] = {}

    async def get(self, key: str) -> object | None:
        entry = self._store.get(f"{self._ns}:{key}")
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[f"{self._ns}:{key}"]
            return None
        return value

    async def set(self, key: str, value: object, ttl: int | None = None) -> None:
        effective_ttl = ttl if ttl is not None else self._ttl
        self._store[f"{self._ns}:{key}"] = (value, time.monotonic() + effective_ttl)

    async def delete(self, key: str) -> None:
        self._store.pop(f"{self._ns}:{key}", None)
```

### `_module.py`

```python
from __future__ import annotations
from lauren import module, use_value
from ._config import CacheConfig
from ._service import CacheService

class CacheModule:
    """Companion module for in-process caching.

    Usage::

        @module(imports=[CacheModule.for_root(CacheConfig(default_ttl_seconds=60))])
        class AppModule: pass
    """

    @classmethod
    def for_root(cls, config: CacheConfig | None = None) -> type:
        cfg = config or CacheConfig()

        @module(
            providers=[
                use_value(provide=CacheConfig, value=cfg),
                CacheService,
            ],
            exports=[CacheService],
        )
        class _CacheModule:
            pass

        _CacheModule.__name__ = "CacheModule"
        return _CacheModule

    @classmethod
    def for_testing(cls) -> type:
        """Return a module wired with zero-TTL cache (all entries expire immediately)."""
        return cls.for_root(CacheConfig(default_ttl_seconds=0))
```

## Scope rules for companion packages

| Scope | Use when |
|---|---|
| `SINGLETON` | Stateful services, connection pools, shared in-memory state |
| `REQUEST` | Per-request tracing contexts, per-request DB sessions |
| `TRANSIENT` | Cheap value objects, per-injection fresh state |

A SINGLETON companion service **must not** depend on a REQUEST-scoped service —
Lauren raises `DIScopeViolationError` at startup if it detects this.
