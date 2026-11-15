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
from lauren import Path, Query, Header, Cookie, Json, Form, Bytes, Depends, State
```

| Extractor | Source | Notes |
|---|---|---|
| `Path[T]` | `{name}` URL segment | 422 if segment not found |
| `Query[T]` | `?key=value` | `Query[list[str]]` collects multi-value |
| `Header[T]` | request header | key lookup is case-insensitive |
| `Cookie[T]` | cookie | by name |
| `Json[T]` | JSON request body | T may be Pydantic `BaseModel`, `msgspec.Struct`, dataclass, `TypedDict`, or `Discriminated[A\|B,"key"]`; 422 on failure |
| `Form[T]` | URL-encoded or multipart fields | Scalar/list/optional or dataclass, `TypedDict`, Pydantic, msgspec model; always validates; file fields require multipart |
| `Form[FormData]` | URL-encoded text or multipart text/file parts | Immutable ordered multi-mapping; `getall` / `multi_items` preserve repetitions; another file binding requires multipart |
| `UploadFile` / `list[UploadFile]` | multipart file parts | Seekable temporary spools; `UploadField` constraints |
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
from lauren import PathField, QueryField, HeaderField, CookieField, FormField, UploadField
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
| Annotation is a `BaseModel`, `msgspec.Struct`, dataclass, `TypedDict`, or `Discriminated[A\|B,"key"]` | `Json[T]` |
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
    body: CreateUserDto,   # → Json[CreateUserDto] (BaseModel / dataclass / TypedDict)
) -> UserDto: ...
```

---

## File uploads

```python fragment
from dataclasses import dataclass
from typing import Annotated

from lauren import Form, FormField, UploadField, UploadFile, post

@dataclass
class ProfileForm:
    title: Annotated[str, FormField(alias="display-title")]
    avatar: Annotated[
        UploadFile | None,
        UploadField(max_size=2 * 1024 * 1024, content_types=["image/png"], sniff=True),
    ] = None

@post("/profile", max_body_size=3 * 1024 * 1024)
async def profile(self, form: Form[ProfileForm]) -> dict:
    return {"title": form.title, "size": form.avatar.size if form.avatar else None}

@post("/multi-upload")
async def multi(
    self,
    files: list[UploadFile] = UploadField(min_files=1, max_files=5),
    tags: Form[list[str]] = FormField(default=[]),
) -> dict:
    return {"count": len(files), "tags": tags}
```

A file field requires `multipart/form-data` even when optional/defaulted;
text-only typed forms also accept URL-encoded input. Typed `Form[T]` always
validates under stdlib `"warn"` / `"legacy"`. All form/file parameters share
one parse with aliases and validators compiled at startup. Files inside
`Json[Model]`, including nested files, fail startup.

`FormField` adds the usual alias/default/text constraints. Use `UploadField`
for `max_size`, `content_types`, `sniff`, `min_files` / `max_files` (lists),
`alias` and `description`, as defaults or model-field `Annotated` metadata.
Scalars take the first occurrence; lists collect repetitions. `T | None`
without a default remains required; blank non-string fields count as omitted.
Boolean tokens are `true/false`, `1/0`, `on/off`, `yes/no` (case-insensitive).
Model extra-field policy and backend aliases/validators apply; Pydantic form
aliases must be flat strings, not `AliasPath` / `AliasChoices`.

`ContentConfig(multipart=MultipartConfig(...))` sets app-owned positive
part/field/file counts, text/header sizes and per-file spool thresholds
(default 1 MiB). Custom temporary directories must exist and be writable.
Incremental framing feeds `SpooledTemporaryFile`; disk operations use workers.
Unknown files drain without spooling by default (`undeclared_parts="reject"`
opts into rejection). Keep the app body limit small; raise `max_body_size`
on the upload route. Body-buffering middleware defeats streaming; so does
another explicit body-reading extractor (`Bytes`, `ByteStream`, etc.) alongside
forms/uploads: raw bytes buffer once for replay. Form/file-only routes remain
incremental. Parsing before routing uses app budgets and checks route
constraints against the cache. Low-level `Request.body()` after an unbuffered
multipart parse raises `RuntimeError` rather than rereading exhausted input.

The file value exposes `filename`, `content_type`, `headers`, `name`, `size`,
`file` and `safe_filename`; async `read(size=-1)`, `seek`, `write`, `stream`,
`save(path, overwrite=False)` and `close`. **`read()` advances the cursor**;
rewind with `await file.seek(0)` to reread. `read_sync()` remains a full-content
snapshot without moving the cursor (can block); `UploadFile(data=...)` remains
supported. `save()` copies all content, preserves the cursor, and creates no
directories or overwrites by default. Direct `.file` operations are synchronous.

Files stay open through response streams and `BackgroundTasks`, then close;
partial files close on parse failure/cancellation. Save before detached work.
Never trust client filenames or MIME metadata; use generated storage names.
`sniff=True` only recognizes PNG, JPEG, GIF, WebP, PDF, ZIP and GZIP prefixes;
use explicit supported types or a synchronous custom sniffer. It is not
antivirus or full format validation. Cookie-authenticated forms need CSRF
protection.

`Form[FormData]` captures every multipart text/file pair or text-only
URL-encoded fields; `get`, indexing and `items()` select the first, `keys()`
lists distinct names, `getall()` returns an independent list and `multi_items()`
preserves wire order. `await request.multipart()` shares the multipart cache
but itself requires multipart. A coexisting file binding makes the route
multipart-only. Bare `Form` and `Form[dict]`
return `dict[str, list[str]]` text fields for URL-encoded or multipart bodies
with legacy content-type leniency; `Request.form()` stays URL-encoded-only.
Do not use `UploadFile` for text parts.

Budgets/per-file bytes answer 413; field, MIME and count validation answer
422 with `detail.issues`. Malformed framing stays 422 by default (opt into
400 with `ContentConfig(malformed_body_status=400)`). Full defaults and a
runnable example: [Forms and File Uploads](../../docs/guides/file-uploads.md).

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
