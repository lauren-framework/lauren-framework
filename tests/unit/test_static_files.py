"""Unit tests for :class:`lauren.StaticFilesModule`."""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path


from lauren import LaurenFactory, module
from lauren.static_files import StaticFilesModule
from lauren.decorators import MODULE_META


# ---------------------------------------------------------------------------
# for_root — return-value contract
# ---------------------------------------------------------------------------


def test_for_root_returns_a_class() -> None:
    d = Path(tempfile.mkdtemp())
    result = StaticFilesModule.for_root("/static", directory=d)
    assert isinstance(result, type)


def test_for_root_returns_module_class() -> None:
    d = Path(tempfile.mkdtemp())
    result = StaticFilesModule.for_root("/static", directory=d)
    assert hasattr(result, MODULE_META)


def test_two_calls_produce_distinct_module_classes() -> None:
    d = Path(tempfile.mkdtemp())
    m1 = StaticFilesModule.for_root("/a", directory=d)
    m2 = StaticFilesModule.for_root("/b", directory=d)
    assert m1 is not m2


def test_different_directories_produce_distinct_classes() -> None:
    d1 = Path(tempfile.mkdtemp())
    d2 = Path(tempfile.mkdtemp())
    m1 = StaticFilesModule.for_root("/s", directory=d1)
    m2 = StaticFilesModule.for_root("/s", directory=d2)
    assert m1 is not m2


# ---------------------------------------------------------------------------
# Module can be built into a LaurenApp (no errors at startup)
# ---------------------------------------------------------------------------


def test_module_compiles_successfully(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_bytes(b"<html/>")

    StaticMod = StaticFilesModule.for_root("/static", directory=tmp_path)

    @module(imports=[StaticMod])
    class _App:
        pass

    app = asyncio.run(LaurenFactory.create(_App))
    assert app is not None


def test_multiple_mounts_compile_successfully(tmp_path: Path) -> None:
    pub = tmp_path / "pub"
    pub.mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    (pub / "index.html").write_bytes(b"<html/>")
    (assets / "logo.png").write_bytes(b"\xff\xd8")

    @module(
        imports=[
            StaticFilesModule.for_root("/public", directory=pub),
            StaticFilesModule.for_root("/assets", directory=assets),
        ]
    )
    class _App:
        pass

    app = asyncio.run(LaurenFactory.create(_App))
    assert app is not None


# ---------------------------------------------------------------------------
# _serve_file helper (internal — tests the core logic directly)
# ---------------------------------------------------------------------------


def _fake_request(if_none_match: str = "") -> object:
    class _FakeHeaders:
        def get(self, key: str, default: str = "") -> str:
            if key == "if-none-match":
                return if_none_match
            return default

    class _FakeReq:
        headers = _FakeHeaders()

    return _FakeReq()


def test_serve_file_200(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "hello.txt").write_bytes(b"hello")
    resp = _serve_file(tmp_path, "hello.txt", _fake_request(), max_age=0)
    assert resp.status == 200
    assert resp.body == b"hello"


def test_serve_file_404_missing(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    resp = _serve_file(tmp_path, "nope.txt", _fake_request(), max_age=0)
    assert resp.status == 404


def test_serve_file_403_traversal(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    secret = tmp_path.parent / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    resp = _serve_file(static_dir, "../secret.txt", _fake_request(), max_age=0)
    assert resp.status == 403


def test_serve_file_etag_present(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "f.txt").write_bytes(b"data")
    resp = _serve_file(tmp_path, "f.txt", _fake_request(), max_age=0)
    etag = resp.headers.get("etag", "")
    assert etag.startswith('"') and etag.endswith('"')


def test_serve_file_304_on_matching_etag(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "f.txt").write_bytes(b"data")
    r1 = _serve_file(tmp_path, "f.txt", _fake_request(), max_age=0)
    etag = r1.headers.get("etag", "")
    r2 = _serve_file(tmp_path, "f.txt", _fake_request(if_none_match=etag), max_age=0)
    assert r2.status == 304
    assert r2.body == b""


def test_serve_file_cache_control(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "f.txt").write_bytes(b"x")
    resp = _serve_file(tmp_path, "f.txt", _fake_request(), max_age=3600)
    assert "max-age=3600" in resp.headers.get("cache-control", "")


def test_serve_file_no_cache_when_zero(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "f.txt").write_bytes(b"x")
    resp = _serve_file(tmp_path, "f.txt", _fake_request(), max_age=0)
    assert resp.headers.get("cache-control", "") == ""


def test_serve_file_index_html_fallback(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "index.html").write_bytes(b"<html/>")
    resp = _serve_file(tmp_path, "", _fake_request(), max_age=0)
    assert resp.status == 200
    assert resp.body == b"<html/>"


def test_serve_file_content_type_css(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "app.css").write_bytes(b"body{}")
    resp = _serve_file(tmp_path, "app.css", _fake_request(), max_age=0)
    assert "css" in resp.headers.get("content-type", "")


def test_serve_file_unknown_type_octet_stream(tmp_path: Path) -> None:
    from lauren._staticfiles import _serve_file

    (tmp_path / "data.xyzzy").write_bytes(b"\xff")
    resp = _serve_file(tmp_path, "data.xyzzy", _fake_request(), max_age=0)
    assert resp.headers.get("content-type", "").startswith("application/octet-stream")
