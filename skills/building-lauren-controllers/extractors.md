# Lauren Extractors — Reference

## Contents
- [Built-in extractors](#built-in-extractors)
- [Field descriptors](#field-descriptors)
- [Pipes](#pipes)
- [Implicit parameter promotion](#implicit-parameter-promotion)
- [File uploads](#file-uploads)
- [Custom extractors](#custom-extractors)

---

## Built-in extractors

```python
from lauren.types import Path, Query, Header, Cookie, Json, Form, Bytes, Depends, State
```

| Extractor | Source | Notes |
|---|---|---|
| `Path[T]` | `{name}` URL segment | 422 if segment not found |
| `Query[T]` | `?key=value` | `Query[list[str]]` collects multi-value |
| `Header[T]` | request header | key lookup is case-insensitive |
| `Cookie[T]` | cookie | by name |
| `Json[T]` | JSON request body | T must be Pydantic `BaseModel`; 422 on failure |
| `Form[T]` | `application/x-www-form-urlencoded` or `multipart/form-data` | T must be `BaseModel` |
| `Bytes` | raw body `bytes` | no T argument |
| `State` | `request.state` | reads per-request mutable state set by middleware |
| `Depends[T]` | DI container | resolves `T` as if injected into a constructor |
| `BackgroundTasks` | (injected per-request) | Collect tasks to run after response is sent |

### BackgroundTasks — fire-and-forget after response

Declare `tasks: BackgroundTasks` in a handler to enqueue work that runs **after**
the HTTP response has been sent to the client:

```python
from lauren import BackgroundTasks

@post("/users")
async def create(self, body: Json[CreateUser], tasks: BackgroundTasks) -> UserOut:
    user = await self._repo.create(body)
    handle = tasks.add_task(send_welcome_email, user.email, name=user.name)
    return user, 201
```

- `add_task(fn, *args, **kwargs)` — enqueue `fn`; returns a `TaskHandle` with `.task_id` and `.status`.
- Sync functions are offloaded to `anyio.to_thread.run_sync` automatically.
- Task failures are caught and logged; subsequent tasks always run.
- Tasks run in the same `asyncio.Task` as the request — they participate in graceful-shutdown drain.
- **Do NOT pass `Scope.REQUEST` DI instances** — they are torn down before tasks run. Pass values (IDs, strings) or `Scope.SINGLETON` services.

### Multi-value query parameter

```python
@get("/search")
async def search(self, tags: Query[list[str]]) -> list[Item]:
    ...
```

### Query as a Pydantic model

Collect several query fields into a typed model:

```python
class PaginationQuery(BaseModel):
    page: int = 1
    limit: int = 20

@get("/")
async def list_items(self, q: Query[PaginationQuery]) -> list[ItemDto]:
    return await self._svc.find_all(q.page, q.limit)
```

---

## Field descriptors

Field descriptors add validation and aliasing **on top of** an extractor marker.
They have no effect on which source is read — only on what value is accepted.

```python
from lauren.extractors import PathField, QueryField, HeaderField, CookieField
```

| Descriptor | Type | Effect |
|---|---|---|
| `alias` | `str` | Read from a different key name |
| `default` | `Any` | Used when the key is absent |
| `ge` / `gt` | `float` | Greater-than constraint |
| `le` / `lt` | `float` | Less-than constraint |
| `min_length` / `max_length` | `int` | String length constraint |
| `pattern` | `str` | Regex fullmatch |
| `description` | `str` | OpenAPI documentation |
| `example` | `Any` | OpenAPI example |

### Three equivalent placement forms

```python
# 1. Subscript (most compact)
def a(self, id: Path[int, PathField(ge=1)]): ...

# 2. Annotated metadata
def b(self, id: Annotated[Path[int], PathField(ge=1)]): ...

# 3. Default value
def c(self, id: Path[int] = PathField(ge=1)): ...
```

### Example — aliased header with default

```python
@get("/")
async def index(
    self,
    x_request_id: Header[str] = HeaderField(alias="x-request-id", default="none"),
) -> dict:
    return {"req_id": x_request_id}
```

---

## Pipes

Pipes transform the extracted value **after** extraction and field validation.

```python
from lauren.extractors import pipe
```

```python
def lookup_user(user_id: int) -> User:
    ...  # query DB, raise 404 if absent

@get("/{user_id}")
async def find(self, user_id: Path[int, pipe(lookup_user)]) -> UserDto:
    # user_id is already a User object here
    return user_id
```

### Composition — field descriptor + pipe

```python
# Using | operator on the default
@get("/{id}")
async def find(
    self,
    id: Path[int] = PathField(ge=1) | pipe(lookup),
) -> ItemDto: ...

# Using subscript
@get("/{id}")
async def find(
    self,
    id: Path[int, PathField(ge=1), pipe(lookup)],
) -> ItemDto: ...
```

Subscript pipes run first; default-side pipes run after.

---

## Implicit parameter promotion

When a parameter has **no** extractor marker, Lauren auto-promotes it:

| Condition | Promoted to |
|---|---|
| Parameter name matches `{segment}` in URL | `Path[T]` |
| Annotation is a `BaseModel` | `Json[T]` |
| Annotation is `int`, `str`, `float`, `bool`, `bytes`, `complex` | `Query[T]` |
| Annotation is `list[scalar]` | `Query[list[T]]` |
| Annotation is `Optional[Model]` | `Json[Model \| None]`, body optional |
| Annotation is `Optional[scalar]` | `Query[T \| None]`, query optional |
| Anything else | `UnresolvableParameterError` at startup |

```python
# All implicit — no extractor markers needed
@get("/{user_id}")
async def find(
    self,
    user_id: int,          # → Path[int] (name matches segment)
    include_deleted: bool = False,  # → Query[bool] (scalar with default)
) -> UserDto: ...

@post("/")
async def create(
    self,
    body: CreateUserDto,   # → Json[CreateUserDto] (BaseModel)
) -> UserDto: ...
```

---

## File uploads

```python
from lauren.extractors import UploadFile
from lauren.types import UploadFile as UploadFileType
```

```python
@post("/upload")
async def upload(self, file: UploadFile) -> dict:
    # file is lauren.types.UploadFile with .filename, .content_type, .content (bytes)
    data = file.content
    return {"name": file.filename, "size": len(data)}

@post("/multi-upload")
async def multi(self, files: list[UploadFile]) -> dict:
    return {"count": len(files)}
```

The request must use `multipart/form-data`. `UploadFile` parameters are extracted from the multipart body; the framework parses the body once and caches the result for sibling `UploadFile` parameters.

---

## Custom extractors

Subclass `ExtractionMarker` and override `extract`:

```python
from lauren.extractors import Extraction, ExtractionMarker
from lauren.types import ExecutionContext

class CurrentUser(ExtractionMarker):
    source = "current_user"

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> User:
        user_id = execution_context.request.state.get("user_id")
        if user_id is None:
            raise UnauthorizedError("Not authenticated")
        return await SomeRepo.get(user_id)  # see DI form below
```

For DI-injected extractors (needs constructor deps):

```python
from lauren import injectable, Scope

@injectable(scope=Scope.REQUEST)
class CurrentUser(ExtractionMarker):
    source = "current_user"

    def __init__(self, repo: UserRepository) -> None:
        self._repo = repo

    async def extract(
        self,
        execution_context: ExecutionContext,
        extraction: Extraction,
    ) -> User:
        uid = execution_context.request.state.get("user_id")
        return await self._repo.get(uid)
```

The DI form requires `CurrentUser` to be listed in a module's `providers=`. Usage:

```python
@get("/me")
async def me(self, user: CurrentUser) -> UserDto:
    return UserDto.model_validate(user)
```
