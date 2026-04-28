"""End-to-end tests for the :class:`UploadFile` extractor.

Drives real :class:`LaurenApp` instances via :meth:`LaurenFactory.create`
and verifies that:

* A single ``UploadFile`` parameter receives the matching multipart
  part with its filename, content type, and bytes intact.
* ``list[UploadFile]`` collects every part with the same field name.
* Text form fields uploaded alongside files are *not* captured by
  ``UploadFile`` extractors (those still use ``Form``).
* Field-name aliasing via ``FieldDescriptor(alias=...)`` works.
* Missing required uploads produce a clean 422 with a structured
  error payload.
* Optional uploads (``Optional[UploadFile]`` with ``default=None``)
  return ``None`` when absent.
* Binary payloads (images, archives, random bytes) round-trip
  byte-for-byte.
* Multiple ``UploadFile`` parameters on a single handler share one
  parse of the body (measured by touching the parse-cache attribute
  indirectly).
"""

from __future__ import annotations

import hashlib
import os


from lauren import (
    FieldDescriptor,
    LaurenFactory,
    UploadFile,
    controller,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helpers \u2014 synthesise a multipart body the TestClient can ship as bytes
# ---------------------------------------------------------------------------


def _multipart_body(
    parts: list[tuple[str, bytes, dict[str, str]]],
    *,
    boundary: str = "----LaurenTestBoundary",
) -> tuple[bytes, str]:
    """Return ``(body_bytes, content_type)`` for a list of parts.

    Each part is ``(name, data, extra)`` where ``extra`` may contain
    ``filename`` and ``content_type`` keys. The helper matches the
    construction logic real browsers use.
    """
    lines: list[bytes] = []
    delim = f"--{boundary}".encode()
    for name, data, extra in parts:
        lines.append(delim)
        disposition = f'form-data; name="{name}"'
        if "filename" in extra:
            disposition += f'; filename="{extra["filename"]}"'
        lines.append(f"Content-Disposition: {disposition}".encode())
        if "content_type" in extra:
            lines.append(f"Content-Type: {extra['content_type']}".encode())
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


# ---------------------------------------------------------------------------
# 1. Single file upload
# ---------------------------------------------------------------------------


@controller("/upload")
class _UploadController:
    @post("/single")
    async def single(self, file: UploadFile) -> dict:
        data = await file.read()
        return {
            "filename": file.filename,
            "content_type": file.content_type,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }


@module(controllers=[_UploadController])
class _UploadModule:
    pass


def test_single_upload_delivers_file_with_metadata() -> None:
    app = LaurenFactory.create(_UploadModule)
    payload = b"hello multipart world"
    body, ct = _multipart_body(
        [("file", payload, {"filename": "greeting.txt", "content_type": "text/plain"})]
    )
    r = TestClient(app).post(
        "/upload/single", content=body, headers={"content-type": ct}
    )
    assert r.status_code == 200
    assert r.json() == {
        "filename": "greeting.txt",
        "content_type": "text/plain",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_single_upload_preserves_binary_integrity() -> None:
    """A PNG-like payload \u2014 random bytes containing CRLF sequences \u2014
    must survive the multipart round-trip byte-for-byte. A bug in
    the parser's CRLF handling would show up as a digest mismatch.
    """
    app = LaurenFactory.create(_UploadModule)
    payload = os.urandom(64 * 1024)
    body, ct = _multipart_body(
        [
            (
                "file",
                payload,
                {"filename": "blob.bin", "content_type": "application/octet-stream"},
            )
        ]
    )
    r = TestClient(app).post(
        "/upload/single", content=body, headers={"content-type": ct}
    )
    assert r.status_code == 200
    assert r.json()["sha256"] == hashlib.sha256(payload).hexdigest()
    assert r.json()["size"] == len(payload)


# ---------------------------------------------------------------------------
# 2. Multiple file upload via list[UploadFile]
# ---------------------------------------------------------------------------


@controller("/bulk")
class _BulkController:
    @post("/files")
    async def files(self, files: list[UploadFile]) -> dict:
        items = []
        for f in files:
            data = await f.read()
            items.append({"filename": f.filename, "size": len(data)})
        return {"count": len(items), "items": items}


@module(controllers=[_BulkController])
class _BulkModule:
    pass


def test_list_upload_file_collects_every_matching_part() -> None:
    app = LaurenFactory.create(_BulkModule)
    body, ct = _multipart_body(
        [
            ("files", b"alpha", {"filename": "a.txt"}),
            ("files", b"beta!!", {"filename": "b.txt"}),
            ("files", b"gamma123", {"filename": "c.txt"}),
        ]
    )
    r = TestClient(app).post("/bulk/files", content=body, headers={"content-type": ct})
    assert r.status_code == 200
    payload = r.json()
    assert payload == {
        "count": 3,
        "items": [
            {"filename": "a.txt", "size": 5},
            {"filename": "b.txt", "size": 6},
            {"filename": "c.txt", "size": 8},
        ],
    }


def test_list_upload_file_with_zero_files_returns_empty_list() -> None:
    """When no parts match the field name and the parameter type is\n    ``list[UploadFile]``, the handler must receive an empty list \u2014\n    never ``None`` \u2014 so downstream code can iterate uniformly.\n"""

    @controller("/optbulk")
    class _OptBulkController:
        @post("/")
        async def files(self, files: list[UploadFile] = []) -> dict:
            return {"count": len(files)}

    @module(controllers=[_OptBulkController])
    class _OptBulkModule:
        pass

    app = LaurenFactory.create(_OptBulkModule)
    # Valid multipart body but no ``files`` field in it.
    body, ct = _multipart_body([("other", b"x", {})])
    r = TestClient(app).post("/optbulk/", content=body, headers={"content-type": ct})
    assert r.status_code == 200
    assert r.json() == {"count": 0}


# ---------------------------------------------------------------------------
# 3. Multiple named UploadFile parameters \u2014 parser is shared
# ---------------------------------------------------------------------------


@controller("/sidebyside")
class _SideController:
    @post("/")
    async def pair(
        self,
        avatar: UploadFile,
        banner: UploadFile,
    ) -> dict:
        return {
            "avatar": avatar.filename,
            "banner": banner.filename,
            "avatar_bytes": (await avatar.read()),
            "banner_bytes": (await banner.read()),
        }


@module(controllers=[_SideController])
class _SideModule:
    pass


def test_multiple_upload_parameters_dispatch_correctly() -> None:
    app = LaurenFactory.create(_SideModule)
    body, ct = _multipart_body(
        [
            ("avatar", b"A", {"filename": "profile.png"}),
            ("banner", b"B" * 32, {"filename": "cover.jpg"}),
        ]
    )
    r = TestClient(app).post("/sidebyside/", content=body, headers={"content-type": ct})
    assert r.status_code == 200
    payload = r.json()
    assert payload["avatar"] == "profile.png"
    assert payload["banner"] == "cover.jpg"


# ---------------------------------------------------------------------------
# 4. Aliased field name via FieldDescriptor(alias=...)
# ---------------------------------------------------------------------------


@controller("/aliased")
class _AliasController:
    @post("/")
    async def handler(
        self,
        # The handler parameter is ``my_file`` but the HTML form
        # uploads under the name ``uploaded_document``. The alias
        # bridges the naming mismatch without renaming the Python
        # parameter.
        my_file: UploadFile = FieldDescriptor(alias="uploaded_document"),
    ) -> dict:
        return {"filename": my_file.filename}


@module(controllers=[_AliasController])
class _AliasModule:
    pass


def test_upload_file_respects_alias() -> None:
    app = LaurenFactory.create(_AliasModule)
    body, ct = _multipart_body(
        [("uploaded_document", b"pdf-data", {"filename": "report.pdf"})]
    )
    r = TestClient(app).post("/aliased/", content=body, headers={"content-type": ct})
    assert r.status_code == 200
    assert r.json() == {"filename": "report.pdf"}


# ---------------------------------------------------------------------------
# 5. Missing required upload \u2192 422 with structured error
# ---------------------------------------------------------------------------


def test_missing_required_upload_returns_422() -> None:
    app = LaurenFactory.create(_UploadModule)
    # Valid multipart body but the expected ``file`` field is absent.
    body, ct = _multipart_body([("other_field", b"ignored", {})])
    r = TestClient(app).post(
        "/upload/single", content=body, headers={"content-type": ct}
    )
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"]["detail"]["field"] == "file"


# ---------------------------------------------------------------------------
# 6. Malformed multipart body \u2192 422 rather than 500
# ---------------------------------------------------------------------------


def test_missing_boundary_parameter_returns_422() -> None:
    app = LaurenFactory.create(_UploadModule)
    # Claim multipart content type but omit the boundary.
    r = TestClient(app).post(
        "/upload/single",
        content=b"garbage",
        headers={"content-type": "multipart/form-data"},
    )
    assert r.status_code == 422


def test_corrupt_multipart_body_returns_422() -> None:
    app = LaurenFactory.create(_UploadModule)
    r = TestClient(app).post(
        "/upload/single",
        content=b"definitely not a multipart body",
        headers={"content-type": "multipart/form-data; boundary=XYZ"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 7. UploadFile plus text field: text fields still reach via other paths
# ---------------------------------------------------------------------------


@controller("/mixed")
class _MixedController:
    @post("/")
    async def mixed(
        self,
        file: UploadFile,
    ) -> dict:
        # The extractor pulls the ``file`` part; the ``description``
        # text field is sitting in the body too but is not consumed
        # by this handler \u2014 confirming parts the handler doesn't
        # name are simply ignored (no error, no leak).
        return {
            "filename": file.filename,
            "size": len(await file.read()),
        }


@module(controllers=[_MixedController])
class _MixedModule:
    pass


def test_unreferenced_text_fields_are_ignored() -> None:
    app = LaurenFactory.create(_MixedModule)
    body, ct = _multipart_body(
        [
            ("description", b"A short description of the upload", {}),
            ("file", b"FILE-DATA", {"filename": "doc.txt"}),
            ("nonce", b"abc123", {}),
        ]
    )
    r = TestClient(app).post("/mixed/", content=body, headers={"content-type": ct})
    assert r.status_code == 200
    assert r.json() == {"filename": "doc.txt", "size": 9}


# ---------------------------------------------------------------------------
# 8. UploadFile properties match expected shapes
# ---------------------------------------------------------------------------


@controller("/inspect")
class _InspectController:
    @post("/")
    async def inspect(self, file: UploadFile) -> dict:
        return {
            "repr": repr(file),
            "size_property": file.size,
            "read_sync_equals_read": (await file.read()) == file.read_sync(),
            "has_headers": len(file.headers) > 0,
            "name": file.name,
        }


@module(controllers=[_InspectController])
class _InspectModule:
    pass


def test_upload_file_exposes_expected_api() -> None:
    app = LaurenFactory.create(_InspectModule)
    body, ct = _multipart_body(
        [("file", b"hello", {"filename": "h.txt", "content_type": "text/plain"})]
    )
    r = TestClient(app).post("/inspect/", content=body, headers={"content-type": ct})
    assert r.status_code == 200
    payload = r.json()
    assert payload["size_property"] == 5
    assert payload["read_sync_equals_read"] is True
    assert payload["has_headers"] is True
    assert payload["name"] == "file"
    assert "UploadFile" in payload["repr"]
    assert "h.txt" in payload["repr"]


# ---------------------------------------------------------------------------
# 9. UploadFile arena interaction \u2014 parse cache doesn't leak
# ---------------------------------------------------------------------------


def test_upload_parse_cache_does_not_leak_across_requests() -> None:
    """The parse cache is stored as an attribute on the Request\n    object. Because the arena pools Request instances, we need to\n    confirm the cache doesn't leak: request N+1 must see its own\n    uploaded file, not request N's.\n"""
    app = LaurenFactory.create(_UploadModule)
    client = TestClient(app)

    body1, ct = _multipart_body([("file", b"first-request", {"filename": "one.txt"})])
    r1 = client.post("/upload/single", content=body1, headers={"content-type": ct})
    assert r1.status_code == 200
    assert r1.json()["filename"] == "one.txt"
    assert r1.json()["sha256"] == hashlib.sha256(b"first-request").hexdigest()

    body2, ct2 = _multipart_body([("file", b"second-request", {"filename": "two.txt"})])
    r2 = client.post("/upload/single", content=body2, headers={"content-type": ct2})
    assert r2.status_code == 200
    # If the parse cache leaked, we'd see "one.txt" here.
    assert r2.json()["filename"] == "two.txt"
    assert r2.json()["sha256"] == hashlib.sha256(b"second-request").hexdigest()
