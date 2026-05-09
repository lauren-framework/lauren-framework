# 04 — Testing

## Directory layout

Mirror the `tests/unit/` + `tests/integration/` split from `lauren-framework`
and `lauren-ai`:

```
tests/
├── __init__.py
├── unit/
│   ├── __init__.py
│   ├── test_config.py
│   └── test_service.py
└── integration/
    ├── __init__.py
    ├── test_module_wiring.py
    └── test_end_to_end.py
```

## `pyproject.toml` markers

```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
addopts = "-m 'not benchmark and not eval'"
markers = [
    "benchmark: performance benchmarks, excluded from default run",
    "eval: evaluation tests requiring API keys, excluded from default run",
    "integration: tests requiring external services",
]
testpaths = ["tests"]
pythonpath = ["src", "../lauren-framework"]
```

## Unit test pattern — test service logic in isolation

Unit tests bypass the DI container and instantiate classes directly:

```python
import pytest
from lauren_cache._service import CacheService
from lauren_cache._config import CacheConfig

class TestCacheService:
    def test_get_returns_none_on_miss(self):
        svc = CacheService(CacheConfig())
        result = asyncio.run(svc.get("missing"))
        assert result is None

    async def test_set_then_get_round_trip(self):
        svc = CacheService(CacheConfig(default_ttl_seconds=60))
        await svc.set("key", "value")
        assert await svc.get("key") == "value"

    async def test_expired_entry_returns_none(self):
        import time
        svc = CacheService(CacheConfig(default_ttl_seconds=0))
        await svc.set("key", "value", ttl=0)
        # With ttl=0 the entry expires immediately on next access
        assert await svc.get("key") is None
```

## Integration test pattern — test DI wiring

Integration tests use `LaurenFactory.create()` + `TestClient` to verify that
the module factory correctly wires everything:

```python
from typing import Annotated
from lauren import LaurenFactory, controller, get, module, Inject
from lauren.testing import TestClient
from lauren_cache import CacheModule
from lauren_cache._service import CacheService

@controller("/cache")
class CacheController:
    def __init__(self, cache: CacheService) -> None:
        self.cache = cache

    @get("/{key}")
    async def read(self, key: str) -> dict:
        value = await self.cache.get(key)
        return {"value": value}

    @get("/set/{key}/{value}")
    async def write(self, key: str, value: str) -> dict:
        await self.cache.set(key, value)
        return {"ok": True}


class TestCacheModuleWiring:
    def test_cache_service_is_injected(self):
        @module(
            imports=[CacheModule.for_root()],
            controllers=[CacheController],
        )
        class AppModule:
            pass

        client = TestClient(LaurenFactory.create(AppModule))
        client.get("/cache/set/greeting/hello")
        r = client.get("/cache/greeting")
        assert r.status_code == 200
        assert r.json() == {"value": "hello"}

    def test_for_testing_wires_correctly(self):
        @module(
            imports=[CacheModule.for_testing()],
            controllers=[CacheController],
        )
        class TestAppModule:
            pass

        client = TestClient(LaurenFactory.create(TestAppModule))
        # for_testing() uses ttl=0 so entries expire immediately
        client.get("/cache/set/key/val")
        r = client.get("/cache/key")
        assert r.json() == {"value": None}
```

## Mock pattern for transport/external dependencies

When a companion wraps an external service (HTTP, database, message queue),
provide a `Mock*` class:

```python
# lauren_cache/testing.py
from ._service import CacheService
from ._config import CacheConfig
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class MockCacheService(CacheService):
    """In-memory cache with no TTL expiry — deterministic for tests."""

    def __init__(self) -> None:
        super().__init__(CacheConfig(default_ttl_seconds=86_400))
        self.calls: list[tuple[str, str, object]] = []  # audit trail

    async def set(self, key: str, value: object, ttl: int | None = None) -> None:
        self.calls.append(("set", key, value))
        await super().set(key, value, ttl=86_400)  # never expire in tests
```

Usage:

```python
from lauren_cache.testing import MockCacheService

mock = MockCacheService()
@module(
    providers=[use_value(provide=CacheService, value=mock)],
    controllers=[MyController],
)
class TestModule: pass
```

## Coverage configuration

Exclude transport implementations that require live API keys:

```toml
[tool.coverage.run]
source = ["src/lauren_cache"]
omit = [
    "tests/*",
    # Requires live Redis connection
    "src/lauren_cache/_redis_backend.py",
]

[tool.coverage.report]
fail_under = 80
show_missing = true
```

## `conftest.py` patterns

```python
# tests/conftest.py
import pytest
from lauren import LaurenFactory, module
from lauren_cache import CacheModule

@pytest.fixture
def cache_module():
    """Return a wired CacheModule using for_testing() defaults."""
    return CacheModule.for_testing()

@pytest.fixture
def app(cache_module):
    @module(imports=[cache_module], controllers=[])
    class TestApp: pass
    return LaurenFactory.create(TestApp)
```
