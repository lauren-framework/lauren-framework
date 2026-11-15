# Extractors

Typed extractors for decomposing HTTP requests into strongly-typed Python values.

## Extractor table

| Extractor | Source | Description |
|---|---|---|
| `Path[T]` | URL path segment | Coerces path params to `T` |
| `HostParam[T]` | `host=` pattern label | Reads a case-insensitive host label and validates it as `T` |
| `Query[T]` | Query string | Coerces query params to `T` |
| `Header[T]` | Request header | Coerces header values to `T` |
| `Cookie[T]` | Cookie header | Coerces cookie values to `T` |
| `Json[T]` | Request body (JSON) | Parses JSON body into Pydantic model, struct, dataclass, TypedDict, or `Discriminated` union |
| `Form[T]` | URL-encoded or multipart body | Scalar/list/optional or dataclass, `TypedDict`, Pydantic, msgspec model; always validated; file fields require multipart |
| `Form[FormData]` | URL-encoded text or multipart text/files | Immutable ordered multi-mapping; another file binding makes the route multipart-only |
| `Bytes` | Request body | Buffers entire body as `bytes` |
| `ByteStream` | Request body | Zero-copy async iterator over body chunks |
| `UploadFile` | Multipart body | Parses `multipart/form-data` file upload |
| `Depends[T]` | DI container | Resolves `T` via dependency injection |
| `State` | Request state | Reads from `request.state` |
| `ExtractionMarker` | Custom | Base class for user-defined extractors |

## Path, Query, Header, Cookie

::: lauren.Path

::: lauren.HostParam

::: lauren.Query

::: lauren.Header

::: lauren.Cookie

## Body extractors

::: lauren.Json

::: lauren.Form

Typed forms always validate, including under stdlib `"warn"` / `"legacy"`.
Text-only bindings and `Form[FormData]` accept URL-encoded or multipart input;
any file binding requires multipart. Raw `Form[dict]` and bare `Form` retain
`dict[str, list[str]]` text results and legacy content-type leniency.
`Request.form()` remains URL-encoded-only; `Request.multipart()` itself
requires multipart and shares the multipart extraction cache.

### Form fields and resource budgets

::: lauren.FormField

::: lauren.UploadField

::: lauren.MultipartConfig

::: lauren.FormData

`FormField` adds aliases, defaults and text constraints; `UploadField` adds
file-size, MIME, prefix-sniffing and count constraints. Use defaults or
model-field `Annotated` metadata. `ContentConfig(multipart=MultipartConfig(...))`
sets app-owned count, field/header-size and temporary-spooling budgets.
`FormData` preserves ordered repeated pairs with `getall()` and `multi_items()`.
See [Forms and File Uploads](../guides/file-uploads.md) for the runnable example,
full APIs, security guidance and limits.

### Raw bytes

::: lauren.Bytes

### ByteStream

::: lauren.ByteStream

Zero-copy streaming body extractor. Yields each ASGI body chunk as a `bytes` object directly from the ASGI `receive` callable, without buffering the entire body. For large uploads, use `ByteStream` instead of `Bytes` to avoid ~2x memory overhead.

```python
@post("/upload")
async def upload(self, body: ByteStream) -> dict:
    sha = hashlib.sha256()
    total = 0
    async for chunk in body:
        sha.update(chunk)
        total += len(chunk)
    return {"bytes": total, "sha256": sha.hexdigest()}
```

The body may only be consumed once — iterating twice raises `ExtractorError`. The app's `max_body_size` is enforced across the stream.

### UploadFile

::: lauren.UploadFile

Multipart file upload extractor backed by a seekable `SpooledTemporaryFile`;
large files spill to disk and disk operations are offloaded. Client metadata
(`filename`, `content_type`, headers) is untrusted. `read(size=-1)` advances a
cursor: call `await file.seek(0)` to reread. `read_sync()` remains a full-content
snapshot without moving the cursor; `data=` construction remains supported.
Files close after response streaming and `BackgroundTasks`, including partial
cleanup on parse failures/cancellation. `save()` persists a file before it
outlives the request; prefer generated storage names.

<!-- docs-contract: fragment -->
```python
@post("/avatar")
async def upload(self, file: UploadFile) -> dict:
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "bytes": len(await file.read()),
    }
```

For multiple files: `files: list[UploadFile]` collects every part with the
matching field name. Ordinary form/file-only routes parse incrementally;
combining them with another explicit body-reading extractor (`Bytes`,
`ByteStream`, etc.) buffers raw bytes once for replay, like buffering middleware,
and opts out of upload-streaming memory bounds.

## Dependency injection extractor

::: lauren.Depends

## Pipes

::: lauren.pipe

::: lauren.Pipe

::: lauren.PipeContext

Pipes provide Axum/NestJS-style layered validation and transformation. They run after extraction, in declaration order, and may replace, re-type, or side-effect the value.

```python
@pipe()
def path_is_string(value, ctx):
    return str(value)

# Composition via | operator:
@get("/{id}")
async def get_item(self, id: Path[int] | PathField(ge=1) | path_is_string):
    ...
```

## Discriminated unions

::: lauren.Discriminated

`Discriminated[A | B, "key"]` creates a pydantic-free tagged-union annotation. The
framework reads the discriminator field from the JSON body, dispatches to the correct
variant class, and validates the remaining fields. Missing or unknown discriminator
values return `422`.

```python
from dataclasses import dataclass
from typing import Literal
from lauren import Discriminated, Json, post

@dataclass
class Cat:
    kind: Literal["cat"] = "cat"
    name: str = ""

@dataclass
class Dog:
    kind: Literal["dog"] = "dog"
    name: str = ""

Animal = Discriminated[Cat | Dog, "kind"]

@post("/animals")
async def create(body: Json[Animal]) -> dict:
    return {"type": type(body).__name__, "name": body.name}
```

Works with `@dataclass`, `TypedDict`, `msgspec.Struct`, and `pydantic.BaseModel`.
OpenAPI emits `oneOf` + `discriminator.mapping` automatically.

## Lower-level API

::: lauren.ExtractionMarker

::: lauren.StateExtractor

::: lauren.FieldDescriptor
