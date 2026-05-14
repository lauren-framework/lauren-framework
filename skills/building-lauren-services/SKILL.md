---
name: building-lauren-services
description: Writes Lauren injectable services, configures DI scopes, wires lifecycle hooks, and uses custom providers (Token, use_value, use_class, use_factory, use_existing). Use when adding a service, repository, or DI provider to a Lauren module.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.


# Lauren Services & Dependency Injection

## Injectable service

```python
from lauren import injectable, post_construct, pre_destruct
from lauren.types import Scope

@injectable()
class UsersService:
    def __init__(self, repo: UserRepository, cfg: ConfigService) -> None:
        self._repo = repo
        self._cfg = cfg

    @post_construct
    async def on_init(self) -> None:
        # Called after DI construction, before first request
        await self._repo.connect()

    @pre_destruct
    async def on_shutdown(self) -> None:
        await self._repo.disconnect()

    async def find_all(self) -> list[User]:
        return await self._repo.list()
```

Register in a module:

```python
@module(
    controllers=[UsersController],
    providers=[UsersService, UserRepository, ConfigService],
    exports=[UsersService],   # expose to other modules
)
class UsersModule:
    pass
```

## Scopes

```python
from lauren.types import Scope

@injectable(scope=Scope.SINGLETON)   # default — one instance for app lifetime
class DatabaseService: ...

@injectable(scope=Scope.REQUEST)     # new instance per HTTP request
class TransactionService: ...

@injectable(scope=Scope.TRANSIENT)   # new instance every time it's resolved
class IdGenerator: ...
```

**Scope rule**: A `SINGLETON` cannot depend on a `REQUEST`-scoped provider — raises `DIScopeViolationError` at startup. `REQUEST` can depend on `SINGLETON`.

## Lifecycle hooks

| Decorator | When | Sync or async |
|---|---|---|
| `@post_construct` | After construction, before first request | Both |
| `@pre_destruct` | On shutdown, reverse topological order | Both |

Hooks are called in topological order (dependencies first for `@post_construct`, last for `@pre_destruct`).
Plain `def` lifecycle hooks are offloaded to a worker thread, so blocking sync
cleanup no longer freezes the event loop during startup or shutdown. Keep them
bounded anyway — they still delay readiness and teardown completion.

## Injectable function (factory provider)

```python
from lauren import injectable

@injectable()
def make_http_client(cfg: ConfigService) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=cfg.api_url)

# Consumer:
class SomeService:
    def __init__(self, client: Depends[make_http_client]) -> None:
        self._client = client
```

The function itself is the DI token — reference it with `Depends[make_http_client]`.

## Class-body field injection

```python
from lauren.types import Depends

@injectable()
class OrderService:
    # Resolved by DI at construction — no __init__ needed
    repo: Depends[OrderRepository]
    cfg: Depends[ConfigService]

    async def create(self, data: CreateOrderDto) -> Order:
        ...
```

## Custom providers

See [custom-providers.md](custom-providers.md) for `Token`, `use_value`, `use_class`, `use_factory`, `use_existing`, and `Inject`.

Quick example — override a service for testing:

```python
from lauren import use_value

fake_svc = FakeUsersService()

@module(providers=[use_value(provide=UsersService, value=fake_svc)])
class TestModule:
    pass
```

## Common mistakes

- Not listing a provider in `providers=[]` → `MissingProviderError` at startup.
- `SINGLETON` depending on `REQUEST` → `DIScopeViolationError` at startup.
- Calling `await LaurenFactory.create()` — it is **synchronous**, not async.
- Reading env vars at import time (before `load_dotenv()`) — read in `__init__` or `@post_construct`.
