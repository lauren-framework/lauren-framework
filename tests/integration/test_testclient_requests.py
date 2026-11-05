"""Request building: host / base_url / root_path, bodies, redirects, cookies and defaults."""

from __future__ import annotations

import pytest

from lauren import LaurenFactory, Request, Response, UploadFile, controller, get, module, post
from lauren.testing import TestClient


@controller("/")
class EchoController:
    @get("/echo")
    async def echo(self, request: Request) -> dict:
        return {
            "host": request.headers.get("host"),
            "cookie": request.headers.get("cookie"),
            "x-default": request.headers.get("x-default"),
            "x-request": request.headers.get("x-request"),
            "query": request.query_params,
            "path": request.path,
        }

    @post("/form")
    async def form(self, request: Request) -> dict:
        return await request.form()

    @post("/upload")
    async def upload(self, avatar: UploadFile) -> dict:
        return {"filename": avatar.filename, "type": avatar.content_type, "size": len(await avatar.read())}

    @post("/raw")
    async def raw(self, request: Request) -> dict:
        return {"body": (await request.body()).decode(), "type": request.headers.get("content-type")}

    @get("/redirect")
    async def redirect(self) -> Response:
        return Response.redirect("/echo?from=redirect", status=302)

    @post("/see-other")
    async def see_other(self) -> Response:
        return Response.redirect("/echo", status=303)

    @post("/temporary")
    async def temporary(self) -> Response:
        return Response.redirect("/raw", status=307)

    @get("/loop")
    async def loop(self) -> Response:
        return Response.redirect("/loop", status=302)

    @get("/elsewhere")
    async def elsewhere(self) -> Response:
        return Response.redirect("http://other.test/echo", status=302)

    @get("/set-cookie")
    async def set_cookie(self) -> Response:
        return Response.json({}).with_cookie("theme", "dark")

    @get("/login-redirect")
    async def login_redirect(self) -> Response:
        return Response.redirect("/echo", status=302).with_cookie("sid", "s1")


@module(controllers=[EchoController])
class EchoModule:
    pass


@pytest.fixture
def client():
    with TestClient(LaurenFactory.create(EchoModule)) as c:
        yield c


def test_host_header_comes_from_base_url(client: TestClient) -> None:
    assert client.get("/echo").json()["host"] == "testserver"


def test_custom_base_url_with_port_and_prefix() -> None:
    with TestClient(LaurenFactory.create(EchoModule), base_url="http://api.local:8080/") as c:
        response = c.get("/echo")
    assert response.json()["host"] == "api.local:8080"
    assert response.url == "http://api.local:8080/echo"


def test_base_url_path_prefix_is_joined() -> None:
    @controller("/api")
    class Prefixed:
        @get("/users")
        async def users(self, request: Request) -> dict:
            return {"path": request.path}

    @module(controllers=[Prefixed])
    class PrefixModule:
        pass

    with TestClient(LaurenFactory.create(PrefixModule), base_url="http://testserver/api") as c:
        assert c.get("/users").json() == {"path": "/api/users"}
        assert c.get("").status_code == 404


def test_root_path_is_passed_in_the_scope() -> None:
    seen: dict = {}

    async def app(scope, receive, send):
        if scope["type"] != "http":
            return
        seen.update(scope)
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    with TestClient(app, root_path="/mounted", base_url="https://example.test") as c:
        c.get("/x?y=1")
    assert (seen["root_path"], seen["scheme"], seen["server"], seen["query_string"]) == (
        "/mounted",
        "https",
        ("example.test", 443),
        b"y=1",
    )


def test_explicit_host_header_wins(client: TestClient) -> None:
    assert client.get("/echo", headers={"Host": "custom"}).json()["host"] == "custom"


def test_default_headers_merge_under_request_headers() -> None:
    with TestClient(
        LaurenFactory.create(EchoModule), headers={"x-default": "d", "x-request": "default"}
    ) as c:
        body = c.get("/echo", headers={"X-Request": "r"}).json()
    assert (body["x-default"], body["x-request"]) == ("d", "r")


def test_params_mapping_and_pairs(client: TestClient) -> None:
    assert client.get("/echo?a=1", params={"b": ["2", "3"]}).json()["query"] == {"a": ["1"], "b": ["2", "3"]}
    assert client.get("/echo", params=[("c", 4)]).json()["query"] == {"c": ["4"]}


def test_form_data(client: TestClient) -> None:
    response = client.post("/form", data={"name": "Ada", "tags": ["x", "y"]})
    assert response.json() == {"name": ["Ada"], "tags": ["x", "y"]}


def test_multipart_upload(client: TestClient) -> None:
    response = client.post(
        "/upload", files={"avatar": ("me.png", b"\x89PNG....", "image/png")}, data={"caption": "hello"}
    )
    assert response.json() == {"filename": "me.png", "type": "image/png", "size": 8}


def test_explicit_content_type_wins_over_the_encoder(client: TestClient) -> None:
    response = client.post("/raw", data={"a": "1"}, headers={"content-type": "text/plain"})
    assert response.json() == {"body": "a=1", "type": "text/plain"}


def test_streamed_content_reaches_request_body(client: TestClient) -> None:
    assert client.post("/raw", content=(c for c in [b"ab", b"cd"])).json()["body"] == "abcd"


def test_redirects_are_not_followed_by_default(client: TestClient) -> None:
    response = client.get("/redirect")
    assert response.status_code == 302 and response.is_redirect


def test_follow_redirects_per_request(client: TestClient) -> None:
    response = client.get("/redirect", follow_redirects=True)
    assert response.json()["query"] == {"from": ["redirect"]}
    assert [r.status_code for r in response.history] == [302]
    assert response.url == "http://testserver/echo?from=redirect"


def test_303_turns_into_get_without_body() -> None:
    with TestClient(LaurenFactory.create(EchoModule), follow_redirects=True) as c:
        response = c.post("/see-other", json={"x": 1})
    assert response.status_code == 200
    assert response.request.method == "GET" and response.request.content == b""


def test_307_keeps_method_and_body() -> None:
    with TestClient(LaurenFactory.create(EchoModule), follow_redirects=True) as c:
        response = c.post("/temporary", content=b"keep-me")
    assert response.json()["body"] == "keep-me"


def test_307_with_a_streamed_body_cannot_be_followed() -> None:
    with TestClient(LaurenFactory.create(EchoModule), follow_redirects=True) as c:
        with pytest.raises(RuntimeError, match="streamed request body"):
            c.post("/temporary", content=iter([b"x"]))


def test_redirect_loop_is_bounded(client: TestClient) -> None:
    with pytest.raises(RuntimeError, match="redirects"):
        client.get("/loop", follow_redirects=True)


def test_cross_origin_redirect_drops_authorization(client: TestClient) -> None:
    response = client.get("/elsewhere", follow_redirects=True, headers={"authorization": "Bearer t"})
    assert response.json()["host"] == "other.test"
    assert all(k.lower() != "authorization" for k, _ in response.request.headers)


def test_cookies_set_during_redirects_are_kept(client: TestClient) -> None:
    response = client.get("/login-redirect", follow_redirects=True)
    assert response.json()["cookie"] == "sid=s1"


def test_lifespan_managed_client_stores_cookies(client: TestClient) -> None:
    client.get("/set-cookie")
    assert client.get("/echo").json()["cookie"] == "theme=dark"


def test_bare_client_does_not_store_cookies_like_1x() -> None:
    client = TestClient(LaurenFactory.create(EchoModule))
    client.get("/set-cookie")
    assert client.get("/echo").json()["cookie"] is None
    assert client.persists_cookies is False and client.timeout is None
    client.close()


def test_bare_client_can_opt_into_cookie_persistence() -> None:
    client = TestClient(LaurenFactory.create(EchoModule), persist_cookies=True, timeout=5)
    client.get("/set-cookie")
    assert client.get("/echo").json()["cookie"] == "theme=dark"
    assert client.timeout == 5
    client.close()


def test_lifespan_client_can_opt_out_of_cookie_persistence() -> None:
    with TestClient(LaurenFactory.create(EchoModule), persist_cookies=False) as c:
        c.get("/set-cookie")
        assert "theme" not in c.cookies


def test_explicit_cookie_header_disables_the_jar(client: TestClient) -> None:
    client.cookies.set("jar", "1")
    assert client.get("/echo", headers={"cookie": "manual=2"}).json()["cookie"] == "manual=2"


def test_explicit_cookie_header_plus_cookies_argument_sends_both_like_1x(client: TestClient) -> None:
    body = client.get("/echo", headers={"cookie": "manual=2"}, cookies={"extra": "3"}).json()
    assert body["cookie"] == "manual=2"


def test_seeded_cookies_are_sent() -> None:
    with TestClient(LaurenFactory.create(EchoModule), cookies={"seed": "s"}) as c:
        assert c.get("/echo").json()["cookie"] == "seed=s"


def test_response_cookies_and_request_record(client: TestClient) -> None:
    response = client.get("/set-cookie")
    assert response.cookies == {"theme": "dark"}
    assert response.request.method == "GET" and response.request.headers.get("host") == "testserver"
    assert response.elapsed.total_seconds() >= 0


def test_relative_and_absolute_urls(client: TestClient) -> None:
    assert client.get("http://testserver/echo").json()["path"] == "/echo"


def test_invalid_base_url_is_rejected() -> None:
    with pytest.raises(ValueError, match="absolute"):
        TestClient(LaurenFactory.create(EchoModule), base_url="testserver")


def test_arequest_rejects_unknown_keywords(client: TestClient) -> None:
    import asyncio

    with pytest.raises(TypeError, match="unexpected"):
        asyncio.run(client.arequest("GET", "/echo", bogus=1))
