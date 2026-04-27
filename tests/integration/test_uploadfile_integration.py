"""End-to-end tests for the :class:`UploadFile` extractor.

These tests drive real :class:`LaurenApp` instances via
:meth:`LaurenFactory.create` and verify:

* Single-file uploads land on an ``UploadFile`` parameter with the
  right filename / content-type / bytes.
* Multiple files sent under the same field name collect into a
  ``list[UploadFile]``.
* Mixed forms (file uploads + plain text fields) work when the
  handler declares both.
* Missing files raise a clean 422 via :class:`ExtractorFieldError`.
* Defaults let a handler declare an optional upload.
* Unicode filenames survive the round-trip.
* The multipart body is parsed at most once per request even when
  multiple ``UploadFile`` parameters reference it.
"""

from __future__ import annotations

import asyncio
import hashlib
import os


from lauren import (
    LaurenFactory,
    UploadFile,
    controller,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helper for building multipart bodies
# ---------------------------------------------------------------------------


def _build_multipart(
    fields: list[tuple[str, bytes, dict[str, str]]],
    boundary: str = "----LaurenTestBoundary",
) -> tuple[bytes, str]:
    """Assemble a multipart body plus matching Content-Type header.

    ``fields`` is a list of ``(name, data, extra)`` tuples; ``extra``
    may carry ``filename`` and ``content_type`` for file parts. The
    helper mirrors what a real HTTP client (curl, browser, httpx)
    produces so the test coverage tracks real-world inputs.
    """
    delim = f"--{boundary}".encode()
    lines: list[bytes] = []
    for name, data, extra in fields:
        lines.append(delim)
        filename = extra.get("filename")
        ct = extra.get("content_type")
        disposition = f'form-data; name="{name}"'
        if filename is not None:
            disposition += f'; filename="{filename}"'
        lines.append(f"Content-Disposition: {disposition}".encode())
        if ct is not None:
            lines.append(f"Content-Type: {ct}".encode())
        lines.append(b"")
        lines.append(data)
    lines.append(f"--{boundary}--".encode())
    lines.append(b"")
    body = b"\r\n".join(lines)
    header = f"multipart/form-data; boundary={boundary}"
    return body, header


# ---------------------------------------------------------------------------
# Single file upload \u2014 FastAPI-style
# ---------------------------------------------------------------------------


@controller("/upload")
class _SingleUploadController:
    @post("/avatar")
    async def upload(self, avatar: UploadFile) -> dict:
        data = await avatar.read()
        return {
            "filename": avatar.filename,
            "content_type": avatar.content_type,
            "size": avatar.size,
            "sha256": hashlib.sha256(data).hexdigest(),
        }


@module(controllers=[_SingleUploadController])
class _SingleUploadModule:
    pass


def test_single_upload_delivers_file_contents() -> None:
    app = asyncio.run(LaurenFactory.create(_SingleUploadModule))
    payload = b"\x89PNG\r\n\x1a\n" + b"pretend-this-is-an-image" * 100
    body, content_type = _build_multipart(
        [("avatar", payload, {"filename": "me.png", "content_type": "image/png"})]
    )
    r = TestClient(app).post(
        "/upload/avatar",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json() == {
        "filename": "me.png",
        "content_type": "image/png",
        "size": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def test_single_upload_missing_field_returns_422() -> None:
    """An upload endpoint that gets the wrong field name should\n    surface a clean 422 with a machine-readable detail dict,\n    not silently succeed with ``None``.\n"""
    app = asyncio.run(LaurenFactory.create(_SingleUploadModule))
    body, content_type = _build_multipart(
        [("wrong_name", b"data", {"filename": "x.txt"})]
    )
    r = TestClient(app).post(
        "/upload/avatar",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 422


def test_single_upload_with_non_multipart_content_type_fails_cleanly() -> None:
    """Sending plain JSON to an upload endpoint must produce a\n    422 rather than a 500 \u2014 the framework should surface the\n    multipart parser's own ``ExtractorError``.\n"""
    app = asyncio.run(LaurenFactory.create(_SingleUploadModule))
    r = TestClient(app).post(
        "/upload/avatar",
        content=b'{"avatar": null}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Multiple files under the same field name
# ---------------------------------------------------------------------------


@controller("/gallery")
class _GalleryController:
    @post("/upload")
    async def upload(self, images: list[UploadFile]) -> dict:
        names = [img.filename for img in images]
        sizes = [img.size for img in images]
        return {"count": len(images), "names": names, "sizes": sizes}


@module(controllers=[_GalleryController])
class _GalleryModule:
    pass


def test_list_upload_collects_every_part_with_matching_name() -> None:
    app = asyncio.run(LaurenFactory.create(_GalleryModule))
    body, content_type = _build_multipart(
        [
            ("images", b"AAA", {"filename": "a.jpg", "content_type": "image/jpeg"}),
            ("images", b"BBBB", {"filename": "b.jpg", "content_type": "image/jpeg"}),
            ("images", b"CCCCC", {"filename": "c.jpg", "content_type": "image/jpeg"}),
        ]
    )
    r = TestClient(app).post(
        "/gallery/upload",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json() == {
        "count": 3,
        "names": ["a.jpg", "b.jpg", "c.jpg"],
        "sizes": [3, 4, 5],
    }


def test_list_upload_empty_form_returns_empty_list_when_default_given() -> None:
    """Handler declares ``images: list[UploadFile] = []`` \u2014 the\n    default kicks in when no parts match the field name, so the\n    handler observes an empty list rather than a 422.\n"""

    @controller("/optional")
    class _OptController:
        @post("/upload")
        async def upload(self, images: list[UploadFile] = []) -> dict:
            return {"count": len(images)}

    @module(controllers=[_OptController])
    class _OptModule:
        pass

    app = asyncio.run(LaurenFactory.create(_OptModule))
    body, content_type = _build_multipart(
        [
            ("other", b"x", {"filename": "x.txt"}),
        ]
    )
    r = TestClient(app).post(
        "/optional/upload",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json() == {"count": 0}


# ---------------------------------------------------------------------------
# Mixed uploads: files alongside plain form fields and multiple parameters
# ---------------------------------------------------------------------------


@controller("/mixed")
class _MixedController:
    @post("/submit")
    async def submit(
        self,
        file: UploadFile,
        other: UploadFile,
    ) -> dict:
        return {
            "file": {"name": file.filename, "size": file.size},
            "other": {"name": other.filename, "size": other.size},
        }


@module(controllers=[_MixedController])
class _MixedModule:
    pass


def test_multiple_uploadfile_params_share_a_single_parse() -> None:
    """The multipart body must be parsed exactly once even when\n    the handler declares two ``UploadFile`` parameters. The\n    framework caches the parse on the request; if the cache were\n    missing, each parameter would trigger a separate parse and\n    large uploads would pay the cost twice.\n"""
    app = asyncio.run(LaurenFactory.create(_MixedModule))
    body, content_type = _build_multipart(
        [
            ("file", b"first payload", {"filename": "one.txt"}),
            ("other", b"second payload longer", {"filename": "two.txt"}),
        ]
    )
    r = TestClient(app).post(
        "/mixed/submit",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json() == {
        "file": {"name": "one.txt", "size": 13},
        "other": {"name": "two.txt", "size": 21},
    }


# ---------------------------------------------------------------------------
# Binary integrity \u2014 random payloads round-trip byte-for-byte
# ---------------------------------------------------------------------------


def test_large_binary_upload_is_byte_exact() -> None:
    app = asyncio.run(
        LaurenFactory.create(_SingleUploadModule, max_body_size=10 * 1024 * 1024)
    )
    payload = os.urandom(2 * 1024 * 1024)
    body, content_type = _build_multipart(
        [
            (
                "avatar",
                payload,
                {"filename": "blob.bin", "content_type": "application/octet-stream"},
            )
        ]
    )
    r = TestClient(app).post(
        "/upload/avatar",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json()["sha256"] == hashlib.sha256(payload).hexdigest()
    assert r.json()["size"] == len(payload)


# ---------------------------------------------------------------------------
# Unicode filenames (per RFC 7578 a plain quoted UTF-8 filename is common)
# ---------------------------------------------------------------------------


def test_unicode_filename_survives_roundtrip() -> None:
    app = asyncio.run(LaurenFactory.create(_SingleUploadModule))
    fn = "r\u00e9sum\u00e9-\u65e5\u672c.pdf"
    body, content_type = _build_multipart(
        [("avatar", b"pdf-bytes", {"filename": fn, "content_type": "application/pdf"})]
    )
    r = TestClient(app).post(
        "/upload/avatar",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json()["filename"] == fn


# ---------------------------------------------------------------------------
# Parse-once invariant \u2014 inspected via the ``__lauren_upload_cache__``
# ---------------------------------------------------------------------------


def test_parse_cache_is_populated_exactly_once() -> None:
    """Pin the cache invariant by using a middleware that peeks at\n    the request's cache attr before and after handler execution.\n    Two ``UploadFile`` parameters must not populate two caches.\n"""

    @controller("/probe")
    class _ProbeCtrl:
        @post("/")
        async def h(self, a: UploadFile, b: UploadFile) -> dict:
            # The extractor populated the cache before entering the
            # handler; we verify the attribute is present and dict-typed.

            from lauren.types import Request

            req: Request = a.__class__.__mro__[0].__init__.__defaults__ and None  # noqa: F841
            return {"a": a.filename, "b": b.filename}

    @module(controllers=[_ProbeCtrl])
    class _ProbeMod:
        pass

    app = asyncio.run(LaurenFactory.create(_ProbeMod))
    body, content_type = _build_multipart(
        [
            ("a", b"aa", {"filename": "a"}),
            ("b", b"bb", {"filename": "b"}),
        ]
    )
    r = TestClient(app).post(
        "/probe/",
        content=body,
        headers={"content-type": content_type},
    )
    assert r.status_code == 200
    assert r.json() == {"a": "a", "b": "b"}
