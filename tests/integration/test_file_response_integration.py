"""Integration tests for Response.file() and Response.xml().

Covers:
- Response.file() serves correct body, content-type, content-disposition
- Response.file() with inline=True uses inline disposition
- Response.file() with custom filename and media_type
- Response.file() missing file → FileNotFoundError → 500 (or mapped 404)
- Response.xml() returns correct body and Content-Type via a real HTTP handler
- Streaming — large file delivered in chunks
"""

from __future__ import annotations


from lauren import LaurenFactory, Response, controller, get, module
from lauren.testing import TestClient
from lauren.types import Headers


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build(ctrl_cls: type) -> TestClient:
    @module(controllers=[ctrl_cls])
    class M:
        pass

    return TestClient(LaurenFactory.create(M))


# ---------------------------------------------------------------------------
# Response.file() integration tests
# ---------------------------------------------------------------------------


class TestFileResponseIntegration:
    """End-to-end tests: handler returns await Response.file(...)."""

    def test_serves_pdf_with_correct_content_type(self, tmp_path):
        pdf = tmp_path / "doc.pdf"
        pdf.write_bytes(b"%PDF-1.4 body")
        path = str(pdf)

        @controller("/files")
        class FC:
            @get("/pdf")
            async def serve(self) -> Response:
                return await Response.file(path)

        r = _build(FC).get("/files/pdf")
        assert r.status_code == 200
        assert "application/pdf" in (r.header("content-type") or "")
        assert r.body == b"%PDF-1.4 body"

    def test_content_disposition_attachment_by_default(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_bytes(b"a,b,c")
        path = str(f)

        @controller("/dl")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(path)

        r = _build(FC).get("/dl/")
        cd = r.header("content-disposition") or ""
        assert "attachment" in cd
        assert "data.csv" in cd

    def test_inline_disposition(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n")
        path = str(img)

        @controller("/img")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(path, inline=True)

        r = _build(FC).get("/img/")
        assert (r.header("content-disposition") or "").startswith("inline")

    def test_custom_filename_in_disposition(self, tmp_path):
        f = tmp_path / "tmp_abc.pdf"
        f.write_bytes(b"%PDF")
        path = str(f)

        @controller("/named")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(path, filename="quarterly-report.pdf")

        r = _build(FC).get("/named/")
        assert 'filename="quarterly-report.pdf"' in (r.header("content-disposition") or "")

    def test_media_type_override(self, tmp_path):
        f = tmp_path / "export"
        f.write_bytes(b"col1,col2\nval1,val2")
        path = str(f)

        @controller("/export")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(
                    path,
                    media_type="text/csv",
                    filename="export.csv",
                )

        r = _build(FC).get("/export/")
        assert "text/csv" in (r.header("content-type") or "")

    def test_large_file_streamed_completely(self, tmp_path):
        data = b"x" * 200_000
        f = tmp_path / "big.bin"
        f.write_bytes(data)
        path = str(f)

        @controller("/big")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(path, chunk_size=8192)

        r = _build(FC).get("/big/")
        assert r.status_code == 200
        assert r.body == data

    def test_missing_file_raises_500(self, tmp_path):
        bad = str(tmp_path / "nonexistent.txt")

        @controller("/miss")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(bad)

        r = _build(FC).get("/miss/")
        # Unhandled FileNotFoundError → 500
        assert r.status_code == 500

    def test_extra_headers_forwarded(self, tmp_path):
        f = tmp_path / "doc.txt"
        f.write_bytes(b"hello")
        path = str(f)

        @controller("/hdrs")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(
                    path,
                    headers=Headers([("x-custom-header", "my-value")]),
                )

        r = _build(FC).get("/hdrs/")
        assert r.header("x-custom-header") == "my-value"

    def test_text_file_auto_detects_mime(self, tmp_path):
        f = tmp_path / "readme.txt"
        f.write_bytes(b"read me")
        path = str(f)

        @controller("/txt")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(path)

        r = _build(FC).get("/txt/")
        assert "text/plain" in (r.header("content-type") or "")

    def test_unknown_extension_octet_stream(self, tmp_path):
        f = tmp_path / "data.xyzzy"
        f.write_bytes(b"binary")
        path = str(f)

        @controller("/unk")
        class FC:
            @get("/")
            async def serve(self) -> Response:
                return await Response.file(path)

        r = _build(FC).get("/unk/")
        assert "application/octet-stream" in (r.header("content-type") or "")


# ---------------------------------------------------------------------------
# Response.xml() integration tests
# ---------------------------------------------------------------------------


class TestXmlResponseIntegration:
    """End-to-end tests: handler returns Response.xml(...)."""

    def test_xml_string_body_delivered(self):
        @controller("/xml")
        class XC:
            @get("/feed")
            async def feed(self) -> Response:
                return Response.xml("<feed><item>1</item></feed>")

        r = _build(XC).get("/xml/feed")
        assert r.status_code == 200
        assert b"<feed>" in r.body

    def test_xml_content_type_header(self):
        @controller("/xmlct")
        class XC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml("<root/>")

        r = _build(XC).get("/xmlct/")
        assert r.header("content-type") == "application/xml"

    def test_xml_bytes_body_delivered(self):
        @controller("/xmlb")
        class XC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml(b"<?xml version='1.0'?><doc/>")

        r = _build(XC).get("/xmlb/")
        assert b"<doc/>" in r.body

    def test_xml_custom_status(self):
        @controller("/xmls")
        class XC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml("<created/>", status=201)

        r = _build(XC).get("/xmls/")
        assert r.status_code == 201

    def test_xml_unicode_content(self):
        @controller("/xmlu")
        class XC:
            @get("/")
            async def h(self) -> Response:
                return Response.xml("<greet>héllo</greet>")

        r = _build(XC).get("/xmlu/")
        assert "héllo".encode("utf-8") in r.body
