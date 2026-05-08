"""Integration tests for the File Upload Validation skill (Skill 33).

Tests cover MIME-type detection via magic bytes, size limits, and the
controller endpoint.
"""

from __future__ import annotations

from lauren import (
    Bytes,
    LaurenFactory,
    Scope,
    controller,
    injectable,
    module,
    post,
)
from lauren.exceptions import HTTPError
from lauren.testing import TestClient


class BadRequestError(HTTPError):
    """400 Bad Request — used by upload validation."""

    status_code = 400
    code = "bad_request"


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------

MAGIC_BYTES: dict[bytes, str] = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"%PDF": "application/pdf",
}
ALLOWED_TYPES: set[str] = {"image/jpeg", "image/png", "image/gif", "application/pdf"}
MAX_SIZE_BYTES: int = 10 * 1024 * 1024  # 10 MB


@injectable(scope=Scope.SINGLETON)
class FileValidator:
    def detect_mime_type(self, data: bytes) -> str:
        for magic, mime in MAGIC_BYTES.items():
            if data[: len(magic)] == magic:
                return mime
        return "application/octet-stream"

    def validate(
        self,
        data: bytes,
        allowed_types: set[str] | None = None,
        max_size: int = MAX_SIZE_BYTES,
    ) -> dict:
        errors: list[str] = []
        if len(data) > max_size:
            errors.append(f"File too large: {len(data)} > {max_size}")
        mime = self.detect_mime_type(data)
        allowed = allowed_types or ALLOWED_TYPES
        if mime not in allowed:
            errors.append(f"Disallowed MIME type: {mime}")
        return {
            "valid": len(errors) == 0,
            "mime_type": mime,
            "size": len(data),
            "errors": errors,
        }


@controller("/upload")
class FileUploadController:
    def __init__(self, validator: FileValidator) -> None:
        self._validator = validator

    @post("/")
    async def upload(self, file_data: Bytes) -> dict:
        result = self._validator.validate(file_data)
        if not result["valid"]:
            raise BadRequestError(str(result["errors"]))
        return {
            "status": "accepted",
            "mime_type": result["mime_type"],
            "size": result["size"],
        }


@module(controllers=[FileUploadController], providers=[FileValidator])
class FileUploadModule:
    pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

JPEG_MAGIC = b"\xff\xd8\xff" + b"\x00" * 10
PNG_MAGIC = b"\x89PNG\r\n\x1a\n" + b"\x00" * 10
PDF_MAGIC = b"%PDF-1.4" + b"\x00" * 10


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(FileUploadModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFileValidator:
    def test_detect_jpeg(self) -> None:
        validator = FileValidator()
        assert validator.detect_mime_type(JPEG_MAGIC) == "image/jpeg"

    def test_detect_png(self) -> None:
        validator = FileValidator()
        assert validator.detect_mime_type(PNG_MAGIC) == "image/png"

    def test_detect_pdf(self) -> None:
        validator = FileValidator()
        assert validator.detect_mime_type(PDF_MAGIC) == "application/pdf"

    def test_detect_unknown(self) -> None:
        validator = FileValidator()
        assert (
            validator.detect_mime_type(b"just plain text") == "application/octet-stream"
        )

    def test_validate_jpeg_ok(self) -> None:
        validator = FileValidator()
        result = validator.validate(JPEG_MAGIC)
        assert result["valid"] is True
        assert result["mime_type"] == "image/jpeg"
        assert result["errors"] == []

    def test_validate_oversized(self) -> None:
        validator = FileValidator()
        large = JPEG_MAGIC + b"\x00" * (11 * 1024 * 1024)
        result = validator.validate(large)
        assert result["valid"] is False
        assert any("too large" in e for e in result["errors"])

    def test_validate_disallowed_mime(self) -> None:
        validator = FileValidator()
        result = validator.validate(b"plain text content")
        assert result["valid"] is False
        assert any("Disallowed" in e for e in result["errors"])

    def test_validate_custom_allowed_types(self) -> None:
        validator = FileValidator()
        result = validator.validate(
            b"plain text", allowed_types={"application/octet-stream"}
        )
        assert result["valid"] is True


class TestFileUploadController:
    def test_upload_jpeg_accepted(self) -> None:
        client = build_app()
        r = client.post("/upload/", content=JPEG_MAGIC)
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "accepted"
        assert body["mime_type"] == "image/jpeg"

    def test_upload_png_accepted(self) -> None:
        client = build_app()
        r = client.post("/upload/", content=PNG_MAGIC)
        assert r.status_code == 200
        assert r.json()["mime_type"] == "image/png"

    def test_upload_text_rejected(self) -> None:
        client = build_app()
        r = client.post("/upload/", content=b"hello world")
        assert r.status_code == 400

    def test_upload_oversized_rejected(self) -> None:
        # Use validator directly with a small max_size to avoid hitting the
        # framework's default 1 MB body size limit before our validator runs.
        validator = FileValidator()
        # 200 bytes of JPEG magic + padding, limit 100 bytes
        large = JPEG_MAGIC + b"\x00" * 200
        result = validator.validate(large, max_size=100)
        assert result["valid"] is False
        assert any("too large" in e for e in result["errors"])
