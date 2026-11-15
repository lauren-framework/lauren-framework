"""One app combining converters, hosts, any-method fallbacks, a custom 404 and static files.

A scripted crawl sends ``HEAD`` and ``GET`` to every documented route: no
``HEAD`` answer carries a body, none is a 405, and every ``HEAD`` mirrors
its ``GET``'s status and ``Content-Length``.
"""

from __future__ import annotations

import pathlib
from typing import Any

from lauren import (
    HostParam,
    LaurenFactory,
    Request,
    Response,
    RouteNotFoundError,
    RoutingConfig,
    any_method,
    controller,
    exception_handler,
    get,
    module,
)
from lauren._staticfiles import StaticFilesModule
from lauren.testing import TestClient


@controller("/catalog")
class Catalog:
    @get("/")
    async def index(self) -> dict:
        return {"items": [1, 2, 3]}

    @get("/{id:int}")
    async def item(self, id: int) -> dict:
        return {"id": id}

    @get("/{slug:slug}")
    async def by_slug(self, slug: str) -> dict:
        return {"slug": slug}

    @get("/files/{rest:path}")
    async def files(self, rest: str) -> dict:
        return {"rest": rest}


@controller("/", host="{tenant:slug}.shop.test")
class Storefront:
    @get("/storefront")
    async def home(self, tenant: HostParam[str]) -> dict:
        return {"tenant": tenant}


@controller("/legacy")
class LegacyProxy:
    @any_method("/{*rest}")
    async def proxy(self, rest: str, request: Request) -> Response:
        # The method travels in a header so HEAD and GET bodies have the same length.
        return Response.json({"proxied": rest}).with_header("x-method", request.method)


@exception_handler(RouteNotFoundError)
class FriendlyNotFound:
    async def catch(self, exc: RouteNotFoundError, request: Request) -> Response:
        return Response.html("<p>Nothing here.</p>", status=404)


def _app(static_dir: pathlib.Path) -> Any:
    @module(
        controllers=[Catalog, Storefront, LegacyProxy],
        imports=[StaticFilesModule.for_root("/static", directory=static_dir)],
    )
    class ShopModule:
        pass

    return LaurenFactory.create(
        ShopModule,
        global_exception_handlers=[FriendlyNotFound],
        routing=RoutingConfig(auto_options=True),
        openapi_url="/openapi.json",
    )


def test_head_crawl_mirrors_get_without_bodies(tmp_path: pathlib.Path) -> None:
    (tmp_path / "site.css").write_text("main { margin: 0 }", encoding="utf-8")
    (tmp_path / "index.html").write_text("<h1>shop</h1>", encoding="utf-8")
    crawl: list[tuple[str, dict[str, str]]] = [
        ("/catalog", {}),
        ("/catalog/42", {}),
        ("/catalog/blue-mug", {}),
        ("/catalog/files/a/b.txt", {}),
        ("/storefront", {"host": "acme.shop.test"}),
        ("/legacy/old/page", {}),
        ("/static/site.css", {}),
        ("/static/", {}),
        ("/openapi.json", {}),
        ("/catalog/NOT_A_SLUG", {}),
    ]

    with TestClient(_app(tmp_path)) as client:
        for path, headers in crawl:
            got = client.get(path, headers=headers)
            head = client.head(path, headers=headers)

            assert head.status_code != 405, path
            assert head.status_code == got.status_code, path
            assert head.content == b"", path
            assert head.header("content-length") == got.header("content-length"), path


def test_crawl_outcomes(tmp_path: pathlib.Path) -> None:
    (tmp_path / "index.html").write_text("<h1>shop</h1>", encoding="utf-8")

    with TestClient(_app(tmp_path)) as client:
        assert client.get("/catalog/42").json() == {"id": 42}
        assert client.get("/catalog/NOT_A_SLUG").text == "<p>Nothing here.</p>"
        assert client.get("/storefront", headers={"host": "acme.shop.test"}).json() == {"tenant": "acme"}
        assert client.get("/storefront", headers={"host": "shop.test"}).status_code == 404
        assert client.delete("/legacy/x").header("x-method") == "DELETE"
        assert client.options("/catalog/42").header("allow") == "GET, HEAD, OPTIONS"
        assert client.get("/static/").text == "<h1>shop</h1>"
