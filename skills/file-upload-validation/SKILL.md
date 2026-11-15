---
name: file-upload-validation
description: Validates Lauren multipart uploads with typed Form models, UploadField size/MIME/sniff/count constraints and bounded temporary spooling. Use when accepting files, mixed text/file forms, or saving uploads safely; includes cursor migration and cleanup rules.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Forms and Upload Validation

## Prefer compiled constraints, not whole-body byte checks

Use `Form[T]` for text fields and mixed text/file models. Supported model
backends are dataclass, `TypedDict`, Pydantic and `msgspec.Struct`; scalar,
list and optional text bindings also work. Text-only forms accept URL-encoded
and multipart bodies. Any file field requires `multipart/form-data`, even if
it has a default. Typed forms always validate under stdlib `"warn"` / `"legacy"`.
Files inside a JSON body model, including nested files, fail at startup.

```python fragment
from dataclasses import dataclass, field
from typing import Annotated

from lauren import Form, FormField, UploadField, UploadFile, controller, post

@dataclass
class ProfileForm:
    name: Annotated[str, FormField(alias="display-name")]
    tags: list[str] = field(default_factory=list)
    avatar: Annotated[
        UploadFile | None,
        UploadField(max_size=2 * 1024 * 1024, content_types=["image/png", "image/jpeg"], sniff=True),
    ] = None

@controller("/profiles")
class Profiles:
    @post("/", max_body_size=3 * 1024 * 1024)
    async def create(self, form: Form[ProfileForm]) -> dict:
        return {"name": form.name, "tags": form.tags,
                "size": form.avatar.size if form.avatar else None}
```

- `FormField` supplies aliases, defaults, numeric/text constraints and OpenAPI
  hints. Put it in a parameter default or model-field `Annotated` metadata.
- Scalars take the first occurrence; lists collect repetitions. `T | None`
  without a default is required. Blank non-string fields count as omitted.
  Boolean tokens are `true/false`, `1/0`, `on/off`, `yes/no` (case-insensitive).
- Backend aliases and validators apply. Pydantic form aliases must be flat
  strings, not `AliasPath` / `AliasChoices`; unknown text follows model extras.
- `UploadField(max_size=..., content_types=[...], sniff=..., max_files=...,
  min_files=..., alias=..., description=...)` works on a standalone upload
  parameter or model field. Count constraints belong on `list[UploadFile]`.
- Declared part MIME types are checked before creating a spool; byte/count
  limits apply while parsing. `content_types=["image/*"]` supports a MIME range.
- `sniff=True` recognizes only PNG, JPEG, GIF, WebP, PDF, ZIP and GZIP from a
  bounded prefix. Use explicit supported types with the built-in sniffer;
  a synchronous `sniff(prefix: bytes) -> str | None` callable supports others.
  Detection must agree with the declared type and allow-list. It is not a
  complete format validator, antivirus scan or archive-bomb defense.

## Bound resources at the app and route

```python fragment
from lauren import ContentConfig, LaurenFactory, MultipartConfig

app = LaurenFactory.create(
    AppModule,
    content=ContentConfig(multipart=MultipartConfig(
        max_parts=1000, max_fields=1000, max_files=100,
        max_field_size=1024 * 1024, max_part_header_bytes=16 * 1024,
        spool_threshold=64 * 1024, temp_dir=None, undeclared_parts="ignore",
    )),
)
```

Defaults are shown except the spool threshold (default 1 MiB per file).
All numeric budgets are positive integers; custom temporary directories must
already exist and be writable. The request's total `max_body_size` remains in
force. Raise it on the upload route, not app-wide. URL-encoded extraction also
bounds field count and size.

Parsing is incremental across ASGI chunks. Files use `SpooledTemporaryFile`
and spill to disk after the threshold; disk operations run in workers.
Unknown file parts drain without spooling by default; use
`undeclared_parts="reject"` to reject unknown names. Avoid middleware that
buffers `request.body()` when streaming matters. Another explicit body-reading
extractor (`Bytes`, `ByteStream`, etc.) alongside forms/uploads also buffers
the raw body once for replay and opts out of upload-streaming memory bounds;
ordinary form/file-only routes stay incremental. Parsing before routing uses
app budgets first; route constraints are then checked against the cache.
Low-level `Request.body()` after an unbuffered multipart parse raises
`RuntimeError` instead of rereading the exhausted body.

## File API, storage and lifetime

`UploadFile` exposes `filename`, `content_type`, `headers`, `name`, `size`,
`file` (underlying seekable binary file) and `safe_filename`. Prefer async
`read(size=-1)`, `seek(offset, whence=0)`, `write(data)`, `stream(chunk_size=65536)`,
`save(path, overwrite=False)` and `close()` over direct synchronous `.file` I/O.
An unbounded `read()` still allocates the entire remaining file.

**Migration:** `read()` now advances a cursor; call `await file.seek(0)` to
reread. `read_sync()` remains a full-content snapshot preserving the cursor
(and can block on disk). `UploadFile(data=..., filename=..., content_type=...)`
remains supported for fixtures; manually constructed uploads need explicit
cleanup (`async with file:` works).

```python fragment
from pathlib import Path
from uuid import uuid4

from lauren import UploadField, UploadFile, post

UPLOAD_DIR = Path("/srv/uploads")  # provision an application-owned directory

@post("/documents", max_body_size=12 * 1024 * 1024)
async def upload(self, file: UploadFile = UploadField(max_size=10 * 1024 * 1024)) -> dict:
    storage_key = uuid4().hex
    size = await file.save(UPLOAD_DIR / storage_key)
    return {"key": storage_key, "size": size}
```

`save()` copies the whole file without moving its cursor, does not create
directories, and refuses overwrite unless explicitly enabled. Never use a
client filename as a destination path. `safe_filename` is an NFC sanitized
basename, not authorization; use generated storage names. Client filenames
and content types are untrusted. `filename*` is decoded and preferred over
`filename`.

Files stay open through response streaming and `BackgroundTasks`, then close
automatically before request-pool reuse. Partial files close on parse errors
or cancellation. Save files before detached tasks; never retain live uploads
beyond the request. Integrate antivirus/content inspection on the spool or a
saved quarantine file without reading the whole body into memory; offload
blocking scanners and apply a timeout. Do not treat prefix sniffing as a scan.

## Raw forms, failures and security

- `Form[FormData]` captures all multipart parts or text-only URL-encoded
  fields in an immutable ordered multi-mapping. `get`, indexing and `items()`
  select the first value; `getall` returns a fresh list and `multi_items()`
  preserves every pair. `await request.multipart()` shares the multipart
  cache but itself requires multipart; a coexisting file binding also makes
  the route multipart-only.
- Raw `Form[dict]` and bare `Form` return `dict[str, list[str]]` text fields
  from URL-encoded or multipart bodies and retain legacy content-type leniency.
  `Request.form()` stays URL-encoded-only. Use text bindings, not `UploadFile`,
  for text parts.
- Budgets and per-file bytes answer 413. Invalid values, missing/wrong-kind
  fields, MIME and file-count violations answer 422 with `detail.issues`.
  Malformed framing remains 422 by default;
  `ContentConfig(malformed_body_status=400)` opts into 400.
- Cookie-authenticated form routes still need CSRF protection. CORS and form
  content types do not stop cross-site requests.

The runnable canonical example and full defaults are in
[`docs/guides/file-uploads.md`](../../docs/guides/file-uploads.md).
