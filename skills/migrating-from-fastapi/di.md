# Dependency Injection — FastAPI vs Lauren

## Core difference

FastAPI DI: call-time resolution via `Depends(factory_fn)` — a new instance per call unless you cache manually.

Lauren DI: compile-time graph resolution via constructor injection — lifetime controlled by `scope=` on the provider class.

## Scope equivalents

| FastAPI pattern | Lauren equivalent |
|---|---|
| `Depends(fn)` — new instance every call | `@injectable(scope=Scope.TRANSIENT)` |
| `Depends(fn)` with manual caching | `@injectable(scope=Scope.SINGLETON)` |
| `Depends` with `yield` (request-scoped) | `@injectable(scope=Scope.REQUEST)` + `@pre_destruct` |
| No equivalent | `@injectable(scope=Scope.SINGLETON, provides=(Interface,))` — Protocol binding |

## Constructor injection

**FastAPI:**
```python
def get_repo(db: Session = Depends(get_db)) -> UserRepo:
    return UserRepo(db)

@router.get("/users")
async def list_users(repo: UserRepo = Depends(get_repo)): ...
```

**Lauren:**
```python
@injectable(scope=Scope.SINGLETON)
class UserRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

@controller("/users")
class UserController:
    def __init__(self, repo: UserRepo) -> None:
        self._repo = repo

    @get("/")
    async def list_users(self) -> list: ...
```

No `Depends()` call sites — the DI container resolves the graph at startup.

## Lifecycle hooks

**FastAPI:**
```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect()          # startup
    yield
    await db.disconnect()       # shutdown

app = FastAPI(lifespan=lifespan)
```

**Lauren:**
```python
from lauren import injectable, post_construct, pre_destruct, Scope

@injectable(scope=Scope.SINGLETON)
class Database:
    @post_construct
    async def connect(self) -> None:
        await self._pool.open()

    @pre_destruct
    async def disconnect(self) -> None:
        await self._pool.close()
```

Lifecycle hooks are per-provider and run in topological order — no need for a global lifespan context manager.

## Custom providers (use_value / use_factory)

**FastAPI:**
```python
settings = Settings()
app = FastAPI()
app.dependency_overrides[get_settings] = lambda: settings
```

**Lauren:**
```python
from lauren import use_value

settings = Settings()

@module(providers=[use_value(provide=Settings, value=settings)])
class AppModule: ...
```

Other forms: `use_class(provide=Interface, use_class=Impl)`, `use_factory(provide=T, factory=fn)`, `use_existing(provide=A, existing=B)`.

## Testing overrides

**FastAPI:**
```python
app.dependency_overrides[get_db] = lambda: FakeDb()
```

**Lauren:**
```python
from lauren import LaurenFactory, use_value

app = LaurenFactory.create(
    AppModule,
    global_providers=[use_value(provide=Database, value=FakeDb())]
)
```
