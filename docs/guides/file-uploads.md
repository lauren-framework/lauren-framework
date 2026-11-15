# Forms and File Uploads

`Form[T]` binds URL-encoded and multipart fields to scalars, lists, dataclasses,
`TypedDict`, Pydantic models and msgspec structs. File fields use `UploadFile`.
All form/file parameters share one parse; annotations and aliases are compiled
at startup, not inspected on each request.

## A typed mixed form

<!-- docs-contract: run -->
```python
from dataclasses import dataclass, field
from typing import Annotated

from lauren import (
    ContentConfig, Form, LaurenFactory, MultipartConfig, UploadField,
    UploadFile, controller, module, post,
)
from lauren.testing import TestClient

@dataclass
class Profile:
    name: str
    tags: list[str] = field(default_factory=list)
    avatar: Annotated[
        UploadFile | None,
        UploadField(max_size=2 * 1024 * 1024, content_types=["image/png"], sniff=True),
    ] = None

@controller("/profiles")
class Profiles:
    @post("/", max_body_size=3 * 1024 * 1024)
    async def create(self, form: Form[Profile]) -> dict:
        return {
            "name": form.name,
            "tags": form.tags,
            "size": form.avatar.size if form.avatar else None,
        }

@module(controllers=[Profiles])
class AppModule:
    pass

app = LaurenFactory.create(
    AppModule,
    content=ContentConfig(multipart=MultipartConfig(spool_threshold=64 * 1024)),
)
with TestClient(app) as client:
    response = client.post(
        "/profiles",
        data=[("name", "Ada"), ("tags", "python"), ("tags", "async")],
        files={"avatar": ("photo.png", b"\x89PNG\r\n\x1a\nexample", "image/png")},
    )
    assert response.status_code == 200
    assert response.json()["tags"] == ["python", "async"]
```

A model containing any file field requires `multipart/form-data`, even when
that field has a default. Text-only models also accept
`application/x-www-form-urlencoded`. Typed forms always validate, including
when `ValidationPolicy.stdlib_models` is `"warn"` or `"legacy"`.

## Individual fields, aliases and defaults

```python
from lauren import Form, FormField, UploadFile

@post("/profile")
async def update_profile(
    self,
    name: Form[str] = FormField(alias="display-name"),
    avatar: UploadFile | None = None,
) -> dict:
    return {"name": name, "has_avatar": avatar is not None}
```

Use `list[UploadFile]` for repeated file parts and `Form[list[str]]` for repeated
text fields. Scalars take the first occurrence. Missing optional fields use
explicit defaults; `T | None` without a default is still required. A blank
non-string field is treated as omitted, not as `None`. Checkbox tokens
`true/false`, `1/0`, `on/off` and `yes/no` are accepted case-insensitively;
other boolean strings fail validation.

On model fields, put `FormField(alias=...)` or `UploadField(...)` in
`Annotated`. Backend aliases and validation still apply; Pydantic form aliases
must be flat strings, not `AliasPath` or `AliasChoices`. Unknown text fields
follow the model's extra-field policy. Files inside `Json[Model]`, including
nested file fields, are rejected at startup: files belong in forms.

## Upload constraints and resource budgets

`UploadField(max_size=..., content_types=[...], sniff=..., max_files=...,
min_files=..., alias=..., description=...)` supports a parameter default or
model-field `Annotated` metadata. Count constraints belong on file lists.
Declared part types are checked before creating a spool; size/count limits
are enforced while reading. MIME ranges such as `image/*` are allowed.

`sniff=True` checks a bounded prefix for PNG, JPEG, GIF, WebP, PDF, ZIP or GZIP.
List explicit supported MIME types when using the built-in sniffer. A
synchronous `sniff(prefix: bytes) -> str | None` callable supports other types.
The detected type must match the declared type and allow-list. Sniffing is
not a complete file validator, antivirus scan or archive-bomb defense.

`ContentConfig(multipart=MultipartConfig(...))` configures app-owned limits:

| Option | Default |
|---|---:|
| `max_parts` | `1000` |
| `max_fields` | `1000` |
| `max_files` | `100` |
| `max_field_size` | `1 MiB` |
| `max_part_header_bytes` | `16 KiB` |
| `spool_threshold` | `1 MiB` per file |
| `temp_dir` | system temporary directory |
| `undeclared_parts` | `"ignore"` |

Limits must be positive integers and custom temporary directories must exist
and be writable. The request's total `max_body_size` remains in force. Raise
it on the upload route, not every route. Unknown file parts are drained without
spooling by default; `undeclared_parts="reject"` rejects unknown names.
`Form[FormData]` explicitly captures all parts instead. URL-encoded extraction
also bounds text-field count and size.

Multipart framing is incremental, including when delimiters cross ASGI chunks.
Files spill from memory to disk; disk operations use workers. This bounds
parser memory, not calls such as `await file.read()` that explicitly allocate
the entire file. Avoid buffering middleware (`await request.body()`) when
streaming matters. Combining forms with another explicit body extractor
(`Bytes`, `ByteStream`, etc.) also buffers once for replay. After an unbuffered
multipart parse, `request.body()` raises rather than rereading an exhausted
stream. If middleware parses before routing, app budgets apply
first and matched-route constraints are checked against the cached result.

## File API and lifetime

| Member | Behavior |
|---|---|
| `filename`, `content_type`, `headers`, `name` | Untrusted client metadata |
| `size` | Actual size in bytes; remains available after close |
| `file` | Underlying seekable binary file; direct operations are synchronous |
| `safe_filename` | NFC basename without path separators, controls or device names |
| `await read(size=-1)` | Read from the current position |
| `await seek(offset, whence=0)` | Move the cursor; use `seek(0)` to reread |
| `await write(data)` | Write and update size |
| `stream(chunk_size=65536)` | Async iterator from the current position |
| `await save(path, overwrite=False)` | Copy the entire file, preserve cursor; return bytes written |
| `await close()` | Idempotent cleanup |
| `read_sync()` | Legacy full-content snapshot without moving the cursor; can block |

**Migration:** `read()` now follows a file cursor. Rewind before reading again;
`read_sync()` keeps its legacy snapshot behavior. `UploadFile(data=bytes, ...)`
remains available for fixtures and already-buffered values.

Files remain open through response streaming and `BackgroundTasks`, then close
automatically before the request returns to its pool. Partial files close on
parse failure or cancellation. Do not retain live uploads for detached tasks;
save them or copy the necessary values first. Manually constructed uploads
should be closed explicitly (they support `async with`).

Never use a client filename as a destination path. Prefer generated storage
names. `save()` does not create directories or overwrite existing files unless
explicitly asked. `safe_filename` is a convenience, not an authorization policy.
RFC 5987/2231 `filename*` values are decoded and preferred over `filename`.

## Raw forms and errors

`Form[FormData]` returns an immutable multi-mapping of `str | UploadFile`.
`get`, indexing and `items()` select the first value; `getall(name)` returns an
independent list; `multi_items()` preserves every pair in wire order.
`await request.multipart()` shares this cache. `request.form()` retains its
URL-encoded `dict[str, list[str]]` contract. Raw `Form[dict]` and bare `Form`
return the same dictionary shape for URL-encoded or multipart text fields.
Legacy raw-dict routes retain lenient content-type behavior unless strict
checking is enabled; typed forms reject unsupported media with 415.

Budgets and per-file byte limits answer 413. Missing/wrong-kind fields,
invalid values, MIME mismatches and file-count constraints answer 422 with
structured `detail.issues`. Malformed framing keeps the 1.x default 422;
`ContentConfig(malformed_body_status=400)` opts into 400. OpenAPI documents
form object properties, binary files, arrays of files and accepted part types.

Cookie-authenticated form routes still need CSRF protection; accepting an HTML
form media type is not an authentication boundary.

See [Content Negotiation & Body Limits](content-negotiation.md),
[Validation](validation.md) and [Typed Streaming](typed-streaming.md).
