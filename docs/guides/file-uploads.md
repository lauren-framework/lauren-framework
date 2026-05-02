# File Uploads

> Lauren handles multipart form uploads through the `UploadFile` extractor. Declare a
> parameter with `UploadFile` (single file) or `list[UploadFile]` (multiple files),
> and Lauren parses the `multipart/form-data` body once, lazily, shared across all
> file parameters on the same handler.

---

## Single file upload

```python
from lauren import UploadFile, controller, module, post

@controller("/files")
class FilesController:
    @post("/avatar")
    async def upload(self, avatar: UploadFile) -> dict:
        content = await avatar.read()
        return {
            "filename": avatar.filename,
            "content_type": avatar.content_type,
            "size": len(content),
        }
```

`POST /files/avatar` with `multipart/form-data; name="avatar"` receives the file in
the `avatar` parameter. Missing the field raises `ExtractorFieldError` (422).

---

## `UploadFile` API

| Attribute / Method | Type | Description |
|---|---|---|
| `file.filename` | `str \| None` | Original filename from the client's `Content-Disposition` header. `None` when the browser didn't send one. |
| `file.content_type` | `str \| None` | MIME type from the part's `Content-Type` header, e.g. `"image/jpeg"`. |
| `await file.read()` | `bytes` | Read all bytes. May be called multiple times — seeks back to the start. |
| `await file.seek(offset)` | `None` | Seek to byte position. |
| `file.size` | `int \| None` | Content length if the part declares it; `None` otherwise. |

```python
@post("/upload")
async def upload(self, doc: UploadFile) -> dict:
    data = await doc.read()
    # process data...
    await doc.seek(0)           # rewind and re-read if needed
    data_again = await doc.read()
    return {"filename": doc.filename, "size": len(data)}
```

---

## Multiple files (same field name)

Declare `list[UploadFile]` to receive all files uploaded under the same form field:

```python
@post("/gallery")
async def upload_gallery(self, photos: list[UploadFile]) -> dict:
    names = []
    for photo in photos:
        data = await photo.read()
        names.append(photo.filename)
    return {"uploaded": names}
```

The client sends multiple parts with the same `name="photos"`:

```bash
curl -F "photos=@img1.jpg" -F "photos=@img2.jpg" localhost:8000/files/gallery
```

---

## Mixed form: files + text fields

Combine `UploadFile` with `Form[T]` for forms that carry both metadata and file
content:

```python
from pydantic import BaseModel
from lauren import Form, UploadFile, controller, post

class UploadMeta(BaseModel):
    title: str
    description: str = ""

@controller("/documents")
class DocumentsController:
    @post("/")
    async def create(
        self,
        meta: Form[UploadMeta],
        file: UploadFile,
    ) -> dict:
        content = await file.read()
        return {
            "title": meta.title,
            "filename": file.filename,
            "size": len(content),
        }
```

The multipart body is parsed **once** per request and shared across all parameters,
regardless of how many `UploadFile` or `Form[T]` parameters the handler declares.

---

## Optional file upload

Use `| None` with a default of `None` to make the file optional:

```python
@post("/profile")
async def update_profile(
    self,
    name: Form[str],
    avatar: UploadFile | None = None,
) -> dict:
    if avatar is not None:
        data = await avatar.read()
        # persist avatar...
    return {"name": name, "has_avatar": avatar is not None}
```

---

## Unicode filenames

Lauren preserves Unicode filenames exactly as sent by the client. Filenames with
non-ASCII characters (e.g. `résumé.pdf`) survive the round-trip without any
escaping or transliteration.

---

## Content-type checking

`UploadFile` doesn't enforce content type. Apply your own validation inside the
handler or with a pipe:

```python
from lauren.exceptions import UnprocessableEntityError

@post("/images")
async def upload_image(self, image: UploadFile) -> dict:
    allowed = {"image/jpeg", "image/png", "image/webp"}
    if image.content_type not in allowed:
        raise UnprocessableEntityError(
            "unsupported image type",
            detail={"content_type": image.content_type, "allowed": list(allowed)},
        )
    data = await image.read()
    return {"filename": image.filename, "size": len(data)}
```

---

## Large file handling

`UploadFile` buffers the part in memory. For uploads that may be several megabytes,
consider:

* **Streaming writes** — `await file.read()` returns the full bytes, then write to
  disk or cloud storage.
* **Body size limits** — configure `LaurenFactory.create(..., max_body_size=N)` to
  reject oversized requests with a `413 Request Body Too Large` before the handler
  even runs.

```python
app = LaurenFactory.create(
    AppModule,
    max_body_size=50 * 1024 * 1024,   # 50 MB hard cap
)
```

For truly streaming large uploads without full buffering, use the
[`ByteStream`](../reference/cheat-sheet.md) extractor instead, which gives you the
raw ASGI receive loop.

---

## Testing

Build a `multipart/form-data` body manually or with `httpx`:

```python
from lauren.testing import TestClient

def test_avatar_upload():
    client = TestClient(app)
    r = client.post(
        "/files/avatar",
        content=build_multipart([("avatar", b"fake-image", {"filename": "photo.jpg", "content_type": "image/jpeg"})]),
        headers={"Content-Type": "multipart/form-data; boundary=----Boundary"},
    )
    assert r.status_code == 200
    assert r.json()["filename"] == "photo.jpg"
```

Or use a helper like `httpx`'s `files` argument directly (Lauren's `TestClient`
accepts the same API as `httpx`):

```python
def test_upload_with_httpx_api():
    client = TestClient(app)
    r = client.post(
        "/files/avatar",
        files={"avatar": ("photo.jpg", b"fake-image", "image/jpeg")},
    )
    assert r.status_code == 200
    assert r.json()["size"] == 10
```

### Missing file → 422

```python
def test_missing_file_returns_422():
    client = TestClient(app)
    r = client.post("/files/avatar", content=b"", headers={"Content-Type": "multipart/form-data; boundary=x"})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "extractor_error"
```

---

## See also

* [Extractors → Cheat Sheet](../reference/cheat-sheet.md) — one-line `UploadFile` pattern.
* [Core Concepts → Request & Response](../core-concepts/request-response.md) — `Request.body()` for raw access.
* [Reference → Error Catalog](../reference/errors.md) — `ExtractorFieldError` (422) for validation failures.
* [Typed Streaming](typed-streaming.md) — for streaming large request bodies without buffering.
