# Extractors

Typed extractors for decomposing HTTP requests into strongly-typed Python values.

## Extractor table

| Extractor | Source | Description |
|---|---|---|
| `Path[T]` | URL path segment | Coerces path params to `T` |
| `Query[T]` | Query string | Coerces query params to `T` |
| `Header[T]` | Request header | Coerces header values to `T` |
| `Cookie[T]` | Cookie header | Coerces cookie values to `T` |
| `Json[T]` | Request body (JSON) | Parses JSON body into Pydantic model, struct, dataclass, TypedDict, or `Discriminated` union |
| `Form[T]` | Request body (form) | Parses `application/x-www-form-urlencoded` |
| `Bytes` | Request body | Buffers entire body as `bytes` |
| `ByteStream` | Request body | Zero-copy async iterator over body chunks |
| `UploadFile` | Multipart body | Parses `multipart/form-data` file upload |
| `Depends[T]` | DI container | Resolves `T` via dependency injection |
| `State` | Request state | Reads from `request.state` |
| `ExtractionMarker` | Custom | Base class for user-defined extractors |

## Path, Query, Header, Cookie

::: lauren.Path

::: lauren.Query

::: lauren.Header

::: lauren.Cookie

## Body extractors

::: lauren.Json

::: lauren.Form

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

Multipart file upload extractor with FastAPI-compatible ergonomics. Parses `multipart/form-data` and hands the handler a `UploadFile` instance with file bytes, filename, content type, and headers.

```python
@post("/avatar")
async def upload(self, file: UploadFile) -> dict:
    return {
        "filename": file.filename,
        "content_type": file.content_type,
        "bytes": len(await file.read()),
    }
```

For multiple files: `files: list[UploadFile]` collects every part with the matching field name.

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
