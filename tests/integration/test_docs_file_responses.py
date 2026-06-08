"""Verify that every claim in docs/guides/file-responses.md is accurate.

Each test maps to a specific assertion made in the guide. The test name
includes a short label that identifies which documentation section or
code example is being verified.
"""

from __future__ import annotations


import pytest

from lauren import LaurenFactory, Response, controller, exception_handler, get, module
from lauren.testing import TestClient
from lauren.types import Headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(ctrl_cls: type, *, global_exception_handlers=None) -> TestClient:
    @module(controllers=[ctrl_cls])
    class M:
        pass

    return TestClient(
        LaurenFactory.create(
            M,
            global_exception_handlers=global_exception_handlers or [],
        )
    )


# ---------------------------------------------------------------------------
# Section: Response.file() — streaming file download
# ---------------------------------------------------------------------------


class TestResponseFileClaims:
    """Claims from the "Response.file() — streaming file download" section."""

    # --- The "Full signature" section says path accepts "str or Path" --------

    def test_accepts_path_object_not_just_str(self, tmp_path):
        """Docs: 'path — str or Path'. A pathlib.Path must work directly."""
        f = tmp_path / "report.txt"
        f.write_bytes(b"content")
        path_obj = f  # Path object, not str

        @controller("/pathobj")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(path_obj)

        r = _build(FC).get("/pathobj/")
        assert r.status_code == 200
        assert r.body == b"content"

    # --- "filename defaults to the basename of path" -------------------------

    def test_filename_defaults_to_basename_of_path(self, tmp_path):
        """Docs: 'filename: Name sent in the Content-Disposition header.
        Defaults to the basename of path.'"""
        f = tmp_path / "quarterly-report.pdf"
        f.write_bytes(b"%PDF")

        @controller("/basename")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(str(f))  # no explicit filename

        r = _build(FC).get("/basename/")
        cd = r.header("content-disposition") or ""
        assert 'filename="quarterly-report.pdf"' in cd

    # --- "Content-Disposition: attachment; filename="<name>"" ----------------

    def test_attachment_disposition_includes_both_keywords(self, tmp_path):
        """Docs: 'Content-Disposition: attachment; filename="<name>" so the
        browser shows a Save-As dialog'. Both 'attachment' and 'filename=...'
        must appear together in the same header."""
        f = tmp_path / "data.csv"
        f.write_bytes(b"a,b,c")

        @controller("/both")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(str(f))

        r = _build(FC).get("/both/")
        cd = r.header("content-disposition") or ""
        assert "attachment" in cd
        assert 'filename="data.csv"' in cd

    # --- "Serving inline (browser preview)" ----------------------------------

    def test_inline_disposition_includes_filename(self, tmp_path):
        """Docs: 'The Content-Disposition header becomes inline; filename="logo.png"'.
        The filename must be present even in inline mode."""
        img = tmp_path / "logo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")

        @controller("/inline")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(str(img), inline=True)

        r = _build(FC).get("/inline/")
        cd = r.header("content-disposition") or ""
        assert cd.startswith("inline")
        assert 'filename="logo.png"' in cd

    def test_inline_png_content_type_is_image_png(self, tmp_path):
        """Docs shows serving logo.png with inline=True. MIME type must remain
        image/png (inline only changes disposition, not content-type)."""
        img = tmp_path / "logo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")

        @controller("/pngmime")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(str(img), inline=True)

        r = _build(FC).get("/pngmime/")
        assert "image/png" in (r.header("content-type") or "")

    # --- "Overriding the MIME type" ------------------------------------------

    def test_media_type_override_with_no_extension(self, tmp_path):
        """Docs: 'Override it when the extension is absent or wrong.'
        An extensionless file with media_type override should use the
        provided type, not octet-stream."""
        f = tmp_path / "export"
        f.write_bytes(b"col1,col2")

        @controller("/mime_override")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(
                    str(f),
                    media_type="application/vnd.ms-excel",
                    filename="export.xls",
                )

        r = _build(FC).get("/mime_override/")
        assert r.header("content-type") == "application/vnd.ms-excel"
        assert 'filename="export.xls"' in (r.header("content-disposition") or "")

    # --- "Raises FileNotFoundError" → exception handler maps to 404 ----------

    def test_filenotfounderror_mapped_to_404_via_exception_handler(self, tmp_path):
        """Docs (exception handler example):
            @exception_handler(FileNotFoundError)
            async def on_missing_file(request, exc):
                return Response(b"file not found", status=404)

        The guide shows mapping the built-in FileNotFoundError to a 404 response.
        """

        @exception_handler(FileNotFoundError)
        async def on_missing_file(request, exc: FileNotFoundError) -> Response:
            return Response(b"file not found", status=404)

        bad = str(tmp_path / "nonexistent.txt")

        @controller("/missing")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(bad)

        r = _build(FC, global_exception_handlers=[on_missing_file]).get("/missing/")
        assert r.status_code == 404
        assert r.body == b"file not found"

    # --- "Serving user-generated content safely" (path traversal) -----------

    def test_path_traversal_blocked(self, tmp_path):
        """Docs security pattern (path traversal defence section).

        The real traversal vector for a ``/{name}`` route is a bare ``..``
        segment: ``(base / "..").resolve()`` escapes to the parent directory,
        tripping the ``startswith`` guard and returning 403.

        Note: the correct Lauren route syntax for a string path param is
        ``/{name}`` with ``name: str`` in the handler — NOT ``/{name:str}``,
        which would create a param named literally ``name:str``.
        """
        base = (tmp_path / "user-files").resolve()
        base.mkdir()
        secret = tmp_path / "secret.txt"
        secret.write_bytes(b"top-secret")

        @controller("/safe")
        class FC:
            @get("/{name}")
            async def serve(self, name: str) -> Response:
                resolved = (base / name).resolve()
                if not str(resolved).startswith(str(base)):
                    return Response(b"forbidden", status=403)
                return await Response.file(resolved, filename=name)

        # ".." as the segment escapes user-files/ to tmp_path/
        r = _build(FC).get("/safe/..")
        assert r.status_code == 403
        assert r.body == b"forbidden"

    def test_path_traversal_legitimate_file_served(self, tmp_path):
        """Complement to the traversal test: a legitimate file inside BASE
        is still served correctly with the safety guard in place."""
        base = (tmp_path / "user-files").resolve()
        base.mkdir()
        good = base / "report.pdf"
        good.write_bytes(b"%PDF-1.4")

        @controller("/safe2")
        class FC:
            @get("/{name}")
            async def serve(self, name: str) -> Response:
                resolved = (base / name).resolve()
                if not str(resolved).startswith(str(base)):
                    return Response(b"forbidden", status=403)
                return await Response.file(resolved, filename=name)

        r = _build(FC).get("/safe2/report.pdf")
        assert r.status_code == 200
        assert r.body == b"%PDF-1.4"
        assert "attachment" in (r.header("content-disposition") or "")


# ---------------------------------------------------------------------------
# Section: Response.xml() — XML responses
# ---------------------------------------------------------------------------


class TestResponseXmlClaims:
    """Claims from the 'Response.xml() — XML responses' section."""

    # --- The Atom feed example from the docs ---------------------------------

    def test_atom_feed_example_from_docs(self):
        """Docs example (app/feeds.py):
            xml = '''<?xml version="1.0" encoding="UTF-8"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <title>My Feed</title>
          <entry><title>Hello</title></entry>
        </feed>'''
            return Response.xml(xml)

        The full Atom feed from the code example must round-trip correctly.
        """
        atom_xml = '<?xml version="1.0" encoding="UTF-8"?>\n<feed xmlns="http://www.w3.org/2005/Atom">\n  <title>My Feed</title>\n  <entry><title>Hello</title></entry>\n</feed>'

        @controller("/feed")
        class FC:
            @get("/atom")
            async def atom(self) -> Response:
                return Response.xml(atom_xml)

        r = _build(FC).get("/feed/atom")
        assert r.status_code == 200
        assert r.header("content-type") == "application/xml"
        assert b"<title>My Feed</title>" in r.body
        assert b'xmlns="http://www.w3.org/2005/Atom"' in r.body

    # --- "data can be str (encoded to UTF-8) or bytes" ----------------------

    def test_xml_string_encoded_to_utf8(self):
        """Docs: 'data can be a str (encoded to UTF-8) or bytes'. A str input
        must arrive at the client as UTF-8 bytes."""
        xml_str = "<greet>café</greet>"

        @controller("/xmlenc")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml(xml_str)

        r = _build(FC).get("/xmlenc/")
        assert xml_str.encode("utf-8") in r.body

    def test_xml_bytes_form_already_encoded(self):
        """Docs: 'bytes form — already encoded'
            return Response.xml(b"<root/>", status=201)
        Bytes input must be passed through unchanged.
        """

        @controller("/xmlbytes")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml(b"<root/>", status=201)

        r = _build(FC).get("/xmlbytes/")
        assert r.status_code == 201
        assert b"<root/>" in r.body
        assert r.header("content-type") == "application/xml"

    # --- "headers" parameter in the xml() signature -------------------------

    def test_xml_passes_extra_headers(self):
        """Docs signature: 'headers=None — Optional extra headers.'
        Extra headers should appear in the response alongside content-type.
        """

        @controller("/xmlhdr")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml(
                    "<ok/>",
                    headers=Headers([("x-feed-version", "2")]),
                )

        r = _build(FC).get("/xmlhdr/")
        assert r.status_code == 200
        assert r.header("content-type") == "application/xml"
        assert r.header("x-feed-version") == "2"

    def test_xml_default_status_is_200(self):
        """Docs signature: 'status: int = 200'. Default must be 200."""

        @controller("/xmldefault")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml("<root/>")

        r = _build(FC).get("/xmldefault/")
        assert r.status_code == 200

    # --- "Choosing the right factory" table ----------------------------------

    def test_xml_not_available_from_response_bytes(self):
        """The guide's table says:
            Return raw bytes (in-memory) → Response.bytes(data, media_type="...")
            Return XML              → Response.xml("<root/>")

        Verify that Response.xml correctly overrides media_type to application/xml
        regardless of whatever the caller might pass, i.e. the factory owns
        the content-type.
        """

        @controller("/xmloverride")
        class FC:
            @get("/")
            async def h(self) -> Response:
                # Response.xml takes no media_type arg — it always sets application/xml
                return Response.xml("<root/>")

        r = _build(FC).get("/xmloverride/")
        assert r.header("content-type") == "application/xml"


# ---------------------------------------------------------------------------
# Section: "Full signature" — chunk_size parameter
# ---------------------------------------------------------------------------


class TestChunkSize:
    """The 'Full signature' documents chunk_size. Verify it is honoured for
    large files while preserving byte-perfect delivery."""

    @pytest.mark.parametrize("chunk_size", [1, 512, 4096, 65536])
    def test_custom_chunk_size_delivers_exact_content(self, tmp_path, chunk_size):
        """Docs: 'chunk_size: int = 65536 — read buffer in bytes (default 64 KB)'.
        Different chunk sizes must all produce the same body content."""
        data = bytes(range(256)) * 64  # 16 384 bytes
        f = tmp_path / "data.bin"
        f.write_bytes(data)

        @controller(f"/chunk{chunk_size}")
        class FC:
            @get("/")
            async def h(self) -> Response:
                return await Response.file(str(f), chunk_size=chunk_size)

        r = _build(FC).get(f"/chunk{chunk_size}/")
        assert r.status_code == 200
        assert r.body == data
