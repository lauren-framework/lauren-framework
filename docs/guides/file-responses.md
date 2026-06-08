# File Responses & XML

Lauren provides two built-in factories for sending file content and XML from handlers: `Response.file()` and `Response.xml()`.

## `Response.file()` — streaming file download

`Response.file()` is an **async** factory that opens the file with `anyio.open_file` and streams it in chunks. The event loop is never blocked, even for gigabyte-scale files.

```python title="app/documents.py"
from pathlib import Path
from lauren import Response, controller, get

@controller("/documents")
class DocumentController:
    @get("/{name}")
    async def download(self, name: str) -> Response:
        path = Path("/var/reports") / name
        return await Response.file(path, filename=name)
```

The handler returns a streaming response with:

- `Content-Type` auto-detected from the file extension (`application/pdf`, `image/png`, …)
- `Content-Disposition: attachment; filename="<name>"` so the browser shows a Save-As dialog

### Serving inline (browser preview)

Pass `inline=True` to ask the browser to display the file instead of downloading it:

```python
@get("/logo")
async def logo(self) -> Response:
    return await Response.file("static/logo.png", inline=True)
```

The `Content-Disposition` header becomes `inline; filename="logo.png"`.

### Overriding the MIME type

`Response.file()` uses `mimetypes.guess_type()` automatically. Override it when the extension is absent or wrong:

```python
return await Response.file("/tmp/export", media_type="application/vnd.ms-excel", filename="export.xls")
```

### Full signature

```python
await Response.file(
    path,                           # str or Path
    *,
    media_type: str | None = None,  # auto-detected when None
    filename: str | None = None,    # defaults to the basename of path
    inline: bool = False,           # True → inline, False → attachment
    chunk_size: int = 65536,        # read buffer in bytes (default 64 KB)
    headers=None,                   # extra response headers
) -> Response
```

Raises `FileNotFoundError` when `path` does not point to an existing file. Map it to a 404 with an exception handler:

```python
from lauren import exception_handler
from lauren.exceptions import NotFoundError

@exception_handler(FileNotFoundError)
async def on_missing_file(request, exc: FileNotFoundError) -> Response:
    return Response(b"file not found", status=404)
```

### Serving user-generated content safely

Always validate the filename against an allowlist or resolve it within a trusted base directory to prevent path traversal:

```python
import pathlib

BASE = pathlib.Path("/var/user-files").resolve()

@get("/download/{name}")
async def serve(self, name: str) -> Response:
    resolved = (BASE / name).resolve()
    if not str(resolved).startswith(str(BASE)):
        return Response(b"", status=403)
    return await Response.file(resolved, filename=name)
```

For a full static-file server with ETag, cache headers, and built-in traversal protection, use [`StaticFilesModule`](proxy-and-static-files.md) instead.

---

## `Response.xml()` — XML responses

`Response.xml()` is a synchronous factory that sets `Content-Type: application/xml`:

```python title="app/feeds.py"
from lauren import Response, controller, get

@controller("/feed")
class FeedController:
    @get("/atom")
    async def atom(self) -> Response:
        xml = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>My Feed</title>
  <entry><title>Hello</title></entry>
</feed>"""
        return Response.xml(xml)
```

`data` can be a `str` (encoded to UTF-8) or `bytes`:

```python
# bytes form — already encoded
return Response.xml(b"<root/>", status=201)
```

### Signature

```python
Response.xml(
    data: str | bytes,
    *,
    status: int = 200,
    headers=None,
) -> Response
```

---

## Choosing the right factory

| Goal | Factory |
|---|---|
| Download a file from disk | `await Response.file(path)` |
| Display a file in the browser | `await Response.file(path, inline=True)` |
| Stream file with a custom name | `await Response.file(path, filename="report.pdf")` |
| Return XML | `Response.xml("<root/>")` |
| Return raw bytes (in-memory) | `Response.bytes(data, media_type="...")` |
| Serve many static files from a directory | `StaticFilesModule` |
