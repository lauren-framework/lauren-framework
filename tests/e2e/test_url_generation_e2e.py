"""Every link the app emits resolves to the intended handler — behind a proxy prefix and a mount."""

from __future__ import annotations

from urllib.parse import urlsplit

from lauren import LaurenFactory, controller, get, module, post, use_guards
from lauren.pagination import OffsetPagination, Page, paginate_sequence
from lauren.responses import Created
from lauren.testing import TestClient
from lauren.types import Request
from lauren.urls import ProxyConfig, UrlBuilder, UrlSigningConfig, ValidSignature

PROXY_HEADERS = {
    "x-forwarded-for": "203.0.113.9",
    "x-forwarded-proto": "https",
    "x-forwarded-host": "api.example.com",
    "x-forwarded-prefix": "/edge",
}


@controller("/articles", name="articles")
class Articles:
    def __init__(self, urls: UrlBuilder) -> None:
        self.urls = urls

    @get("/", name="index")
    async def index(self, request: Request, paging: OffsetPagination) -> Page[dict]:
        items = [
            {"id": i, "href": request.url_for(Articles.show, slug=f"post-{i}")}
            for i in range(paging.offset, paging.offset + 3)
        ]
        return paginate_sequence(items, OffsetPagination(limit=paging.limit, offset=0))

    @get("/{slug}", name="show")
    async def show(self, slug: str, request: Request) -> dict:
        return {
            "slug": slug,
            "links": {
                "self": request.url_for("blog.articles.show", slug=slug),
                "index": request.url_path_for("blog.articles.index"),
                "download": self.urls.signed_url_for("blog.articles.download", slug=slug, expires_in=300),
            },
        }

    @post("/")
    async def create(self, request: Request) -> Created[dict]:
        return Created({"slug": "new"}, location=request.url_for(Articles.show, slug="new"))

    @get("/{slug}/download", name="download")
    @use_guards(ValidSignature)
    async def download(self, slug: str) -> dict:
        return {"download": slug}


@module(controllers=[Articles], name="blog")
class BlogModule:
    pass


@module()
class RootModule:
    pass


def _client() -> tuple[TestClient, str]:
    blog = LaurenFactory.create(
        BlogModule,
        public_base_url="https://api.example.com/edge/v2",
        url_signing=UrlSigningConfig(secret="e2e-secret"),
    )
    root = LaurenFactory.create(
        RootModule, mounts={"/v2": blog}, proxy=ProxyConfig(trusted=["10.0.0.0/8"], trust_prefix=True)
    )
    client = TestClient(
        root, client=("10.0.0.5", 4000), base_url="http://internal:8080", headers=PROXY_HEADERS
    )
    return client, "https://api.example.com"


def _local(url: str, origin: str) -> str:
    """The path the proxy forwards for a public URL (``/edge`` stripped, as a proxy does)."""
    assert url.startswith(origin), url
    parts = urlsplit(url)
    assert parts.path.startswith("/edge/"), url
    path = parts.path.removeprefix("/edge")
    return f"{path}?{parts.query}" if parts.query else path


def test_every_emitted_link_resolves() -> None:
    client, origin = _client()
    with client:
        created = client.post("/v2/articles/")
        location = created.header("location")
        assert location == "https://api.example.com/edge/v2/articles/new"

        shown = client.get(_local(location, origin)).json()
        assert shown["slug"] == "new"

        links = shown["links"]
        assert client.get(_local(links["self"], origin)).json()["slug"] == "new"
        assert links["index"] == "/edge/v2/articles"
        listing = client.get(links["index"].removeprefix("/edge") + "?limit=3")
        assert listing.status_code == 200
        hrefs = [item["href"] for item in listing.json()["items"]]
        assert [client.get(_local(href, origin)).json()["slug"] for href in hrefs] == [
            "post-0",
            "post-1",
            "post-2",
        ]

        assert links["download"].startswith("https://api.example.com/edge/v2/articles/new/download?exp=")


def test_signed_link_from_the_builder_verifies_through_the_mount() -> None:
    client, _origin = _client()
    with client:
        links = client.get("/v2/articles/new").json()["links"]
        # The builder signs app-relative paths, so the link verifies however
        # the request reaches the app (here: proxy prefix /edge + mount /v2).
        query = urlsplit(links["download"]).query
        ok = client.get(f"/v2/articles/new/download?{query}")
        tampered = client.get(f"/v2/articles/other/download?{query}")

    assert ok.json() == {"download": "new"}
    assert tampered.status_code == 403
