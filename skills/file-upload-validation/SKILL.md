---
name: file-upload-validation
description: Validates uploaded file bytes for MIME type (via magic bytes), maximum size, and extensible checks. Use when you need to reject disallowed file types or oversized uploads before storing them.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# File Upload Validation (MIME / Size / Virus Scan)

## Overview

`FileValidator` inspects the raw bytes of an upload using a magic-bytes table
(no `python-magic` C dependency). `FileUploadController` uses Lauren's `Bytes`
extractor to receive the raw request body and delegates to the validator. Extend
`validate` with a mock or real virus-scan call.

## Magic-Bytes Table

```python
MAGIC_BYTES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"%PDF": "application/pdf",
}
ALLOWED_TYPES = {"image/jpeg", "image/png", "image/gif", "application/pdf"}
MAX_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
```

## FileValidator

```python
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class FileValidator:
    def detect_mime_type(self, data: bytes) -> str:
        for magic, mime in MAGIC_BYTES.items():
            if data[:len(magic)] == magic:
                return mime
        return "application/octet-stream"

    def validate(self, data: bytes, allowed_types: set[str] | None = None, max_size: int = MAX_SIZE_BYTES) -> dict:
        errors = []
        if len(data) > max_size:
            errors.append(f"File too large: {len(data)} > {max_size}")
        mime = self.detect_mime_type(data)
        allowed = allowed_types or ALLOWED_TYPES
        if mime not in allowed:
            errors.append(f"Disallowed MIME type: {mime}")
        return {"valid": len(errors) == 0, "mime_type": mime, "size": len(data), "errors": errors}
```

## Controller

```python
from lauren import controller, post, module, Bytes
from lauren.exceptions import BadRequestError

@controller("/upload")
class FileUploadController:
    def __init__(self, validator: FileValidator) -> None:
        self._validator = validator

    @post("/")
    async def upload(self, file_data: Bytes) -> dict:
        result = self._validator.validate(file_data)
        if not result["valid"]:
            raise BadRequestError(str(result["errors"]))
        return {"status": "accepted", "mime_type": result["mime_type"], "size": result["size"]}

@module(controllers=[FileUploadController], providers=[FileValidator])
class FileUploadModule:
    pass
```

## Virus scan hook

Add a `scan(data: bytes) -> bool` method to `FileValidator` backed by ClamAV
or a cloud scanning API:

```python
def scan(self, data: bytes) -> bool:
    # Production: submit data to ClamAV socket or REST API
    # Return False if a threat is detected
    return True  # clean

def validate(self, data: bytes, ...) -> dict:
    ...
    if not self.scan(data):
        errors.append("Virus detected")
    ...
```

## Notes

- `Bytes` extracts the raw request body as `bytes`.
- For multipart form uploads use `Form[T]` or parse `multipart/form-data` with
  a dedicated library (e.g. `python-multipart`).
- Add `Content-Type` header validation as a second check on top of magic bytes
  to prevent header spoofing.
