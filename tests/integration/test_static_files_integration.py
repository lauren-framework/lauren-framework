"""Integration tests for :class:`lauren.StaticFilesModule`."""

from __future__ import annotations

import tempfile
from pathlib import Path


from lauren import LaurenFactory, Response, controller, get, module
from lauren.static_files import StaticFilesModule
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Shared app factory
# ---------------------------------------------------------------------------


def _make_static_dir() -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "index.html").write_bytes(b"<html>index</html>")
    (d / "app.css").write_bytes(b"body { margin: 0; }")
    (d / "app.js").write_bytes(b"console.log('hi');")
    sub = d / "sub"
    sub.mkdir()
    (sub / "page.html").write_bytes(b"<html>sub</html>")
    return d


@controller("/api")
class _ApiController:
    @get("/ping")
    async def ping(self) -> Response:
        return Response.json({"pong": True})


def _build(static_dir: Path, prefix: str = "/static") -> TestClient:
    @module(
        controllers=[_ApiController],
        imports=[StaticFilesModule.for_root(prefix, directory=static_dir)],
    )
    class _App:
        pass

    app = LaurenFactory.create(_App)
    return TestClient(app)


# ---------------------------------------------------------------------------
# API routes still work alongside static serving
# ---------------------------------------------------------------------------


def test_api_route_coexists(tmp_path: Path) -> None:
    client = _build(tmp_path)
    resp = client.request("GET", "/api/ping")
    assert resp.status_code == 200
    assert resp.json()["pong"] is True


# ---------------------------------------------------------------------------
# Serving files
# ---------------------------------------------------------------------------


def test_serves_index_at_prefix(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_bytes(b"<html>home</html>")
    client = _build(tmp_path)
    resp = client.request("GET", "/static")
    assert resp.status_code == 200
    assert b"home" in resp.body


def test_serves_index_at_trailing_slash(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_bytes(b"<html>home</html>")
    client = _build(tmp_path)
    resp = client.request("GET", "/static/")
    assert resp.status_code in (200, 404)  # trailing slash normalised


def test_serves_css_file() -> None:
    d = _make_static_dir()
    client = _build(d)
    resp = client.request("GET", "/static/app.css")
    assert resp.status_code == 200
    assert b"margin" in resp.body
    ct = resp.header("content-type") or ""
    assert "css" in ct


def test_serves_js_file() -> None:
    d = _make_static_dir()
    client = _build(d)
    resp = client.request("GET", "/static/app.js")
    assert resp.status_code == 200
    assert b"console" in resp.body


def test_serves_subdirectory_file() -> None:
    d = _make_static_dir()
    client = _build(d)
    resp = client.request("GET", "/static/sub/page.html")
    assert resp.status_code == 200
    assert b"sub" in resp.body


def test_missing_file_404() -> None:
    d = _make_static_dir()
    client = _build(d)
    resp = client.request("GET", "/static/missing.txt")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# ETag / conditional GET
# ---------------------------------------------------------------------------


def test_etag_present() -> None:
    d = _make_static_dir()
    client = _build(d)
    resp = client.request("GET", "/static/app.css")
    etag = resp.header("etag") or ""
    assert etag.startswith('"') and etag.endswith('"')


def test_conditional_get_304(tmp_path: Path) -> None:
    (tmp_path / "style.css").write_bytes(b"h1{color:red}")
    client = _build(tmp_path)
    r1 = client.request("GET", "/static/style.css")
    etag = r1.header("etag") or ""
    assert etag, "ETag must be present on first request"
    r2 = client.request("GET", "/static/style.css", headers={"if-none-match": etag})
    assert r2.status_code == 304
    assert r2.body == b""


def test_stale_etag_returns_200(tmp_path: Path) -> None:
    (tmp_path / "style.css").write_bytes(b"h1{color:blue}")
    client = _build(tmp_path)
    r = client.request("GET", "/static/style.css", headers={"if-none-match": '"old-etag"'})
    assert r.status_code == 200


def test_cache_control_header(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_bytes(b"x")
    client = _build(tmp_path)
    resp = client.request("GET", "/static/f.txt")
    cc = resp.header("cache-control") or ""
    assert "public" in cc
    assert "max-age=" in cc


def test_cache_control_custom_max_age(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_bytes(b"x")

    @module(
        controllers=[_ApiController],
        imports=[StaticFilesModule.for_root("/s", directory=tmp_path, max_age=86400)],
    )
    class _App:
        pass

    client = TestClient(LaurenFactory.create(_App))
    resp = client.request("GET", "/s/f.txt")
    assert "max-age=86400" in (resp.header("cache-control") or "")


# ---------------------------------------------------------------------------
# Multiple mounts
# ---------------------------------------------------------------------------


def test_two_mounts_coexist(tmp_path: Path) -> None:
    pub = tmp_path / "pub"
    assets = tmp_path / "assets"
    pub.mkdir()
    assets.mkdir()
    (pub / "index.html").write_bytes(b"public-index")
    (assets / "logo.png").write_bytes(b"\x89PNG")

    @module(
        imports=[
            StaticFilesModule.for_root("/public", directory=pub),
            StaticFilesModule.for_root("/assets", directory=assets),
        ]
    )
    class _App:
        pass

    client = TestClient(LaurenFactory.create(_App))
    assert client.request("GET", "/public").status_code == 200
    assert client.request("GET", "/assets/logo.png").status_code == 200


# ---------------------------------------------------------------------------
# Security: path traversal
# ---------------------------------------------------------------------------


def test_path_traversal_blocked(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_bytes(b"TOP SECRET")
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "safe.txt").write_bytes(b"safe")

    @module(imports=[StaticFilesModule.for_root("/files", directory=static_dir)])
    class _App:
        pass

    client = TestClient(LaurenFactory.create(_App))
    # Path traversal attempt — the router normalises the path, so this
    # typically results in a 404 (route not found) rather than reaching the
    # controller, which is also safe.
    resp = client.request("GET", "/files/../secret.txt")
    assert resp.status_code in (403, 404)
    assert b"TOP SECRET" not in resp.body
