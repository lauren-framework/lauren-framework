"""``ProxyConfig`` through a running app: request URL model with trusted and untrusted peers."""

from __future__ import annotations

import logging

import pytest

from lauren import LaurenFactory, controller, get, middleware, module
from lauren.exceptions import StartupError
from lauren.testing import TestClient
from lauren.types import Request, Response
from lauren.urls import ProxyConfig

SPOOF = {
    "x-forwarded-for": "198.51.100.66",
    "x-forwarded-proto": "https",
    "x-forwarded-host": "evil.example",
    "x-forwarded-prefix": "/evil",
}
PROXIED = {
    "x-forwarded-for": "198.51.100.66, 203.0.113.9",
    "x-forwarded-proto": "https",
    "x-forwarded-host": "api.example.com",
    "x-forwarded-prefix": "/backend",
}


def _view(request: Request) -> dict:
    return {
        "client": request.client.host,
        "scheme": request.scheme,
        "host": request.host,
        "port": request.port,
        "root_path": request.root_path,
        "base_url": request.base_url,
        "full_url": request.full_url,
    }


@controller("/whoami")
class WhoAmI:
    @get("/")
    async def whoami(self, request: Request) -> dict:
        return _view(request)


@middleware()
class SeenByMiddleware:
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        return response.with_header("x-seen-client", str(request.client.host))


@module(controllers=[WhoAmI])
class AppModule:
    pass


def _app(**kwargs: object):
    return LaurenFactory.create(AppModule, global_middlewares=[SeenByMiddleware], **kwargs)


TRUSTING = ProxyConfig(trusted=["10.0.0.0/8"], trust_prefix=True)


def test_without_proxy_config_forwarding_headers_are_ignored() -> None:
    with TestClient(_app(), client=("10.0.0.5", 4000)) as client:
        body = client.get("/whoami/", headers=PROXIED).json()

    assert body == {
        "client": "10.0.0.5",
        "scheme": "http",
        "host": "testserver",
        "port": 80,
        "root_path": "",
        "base_url": "http://testserver/",
        "full_url": "http://testserver/whoami/",
    }


def test_trusted_proxy_sets_client_scheme_host_and_prefix() -> None:
    with TestClient(_app(proxy=TRUSTING), client=("10.0.0.5", 4000)) as client:
        response = client.get("/whoami/", headers=PROXIED)

    assert response.json() == {
        "client": "203.0.113.9",
        "scheme": "https",
        "host": "api.example.com",
        "port": 443,
        "root_path": "/backend",
        "base_url": "https://api.example.com/backend/",
        "full_url": "https://api.example.com/backend/whoami/",
    }
    assert response.header("x-seen-client") == "203.0.113.9"


def test_untrusted_peer_cannot_spoof_anything() -> None:
    with TestClient(_app(proxy=TRUSTING), client=("198.51.100.1", 4000)) as client:
        body = client.get("/whoami/", headers=SPOOF).json()

    assert (body["client"], body["scheme"], body["host"], body["root_path"]) == (
        "198.51.100.1",
        "http",
        "testserver",
        "",
    )


def test_forwarded_port_applies_to_the_host_header() -> None:
    with TestClient(_app(proxy=TRUSTING), client=("10.0.0.5", 4000)) as client:
        body = client.get(
            "/whoami/",
            headers={"host": "api.example.com", "x-forwarded-proto": "https", "x-forwarded-port": "8443"},
        ).json()

    assert body["base_url"] == "https://api.example.com:8443/"


def test_rfc7239_forwarded_header() -> None:
    proxy = ProxyConfig(trusted=["10.0.0.0/8"], headers="forwarded")

    with TestClient(_app(proxy=proxy), client=("10.0.0.5", 4000)) as client:
        body = client.get(
            "/whoami/", headers={"forwarded": 'for="[2001:db8::7]";proto=https;host=v6.example'}
        ).json()

    assert (body["client"], body["scheme"], body["host"]) == ("2001:db8::7", "https", "v6.example")


def test_unix_socket_peer_can_be_trusted() -> None:
    with TestClient(_app(proxy=ProxyConfig(trusted=["unix"])), client=None) as client:
        body = client.get("/whoami/", headers={"x-forwarded-for": "203.0.113.9"}).json()

    assert body["client"] == "203.0.113.9"


def test_server_applied_proxy_headers_are_not_applied_twice() -> None:
    proxy = ProxyConfig(trusted=["10.0.0.0/8"], server_applied=True)

    with TestClient(_app(proxy=proxy), client=("10.0.0.5", 4000)) as client:
        body = client.get("/whoami/", headers=PROXIED).json()

    assert (body["client"], body["scheme"], body["host"]) == ("10.0.0.5", "http", "api.example.com")


def test_mounted_sub_app_receives_the_resolved_scope() -> None:
    seen: dict = {}

    async def sub_app(scope, receive, send) -> None:
        seen.update(
            client=scope["client"],
            scheme=scope["scheme"],
            root_path=scope["root_path"],
            host=dict(scope["headers"])[b"host"],
        )
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    app = _app(proxy=TRUSTING, mounts={"/legacy": sub_app})
    with TestClient(app, client=("10.0.0.5", 4000)) as client:
        client.get("/legacy/x", headers=PROXIED)

    assert seen == {
        "client": ("203.0.113.9", 0),
        "scheme": "https",
        "root_path": "/backend/legacy",
        "host": b"api.example.com",
    }


def test_mounted_lauren_app_builds_urls_from_the_resolved_scope() -> None:
    child = LaurenFactory.create(AppModule)
    app = _app(proxy=TRUSTING, mounts={"/v1": child})

    with TestClient(app, client=("10.0.0.5", 4000)) as client:
        body = client.get("/v1/whoami/", headers=PROXIED).json()

    assert body["full_url"] == "https://api.example.com/backend/v1/whoami/"
    assert body["client"] == "203.0.113.9"


def test_mount_from_an_untrusted_peer_keeps_the_original_scope() -> None:
    child = LaurenFactory.create(AppModule)
    app = _app(proxy=TRUSTING, mounts={"/v1": child})

    with TestClient(app, client=("198.51.100.1", 4000)) as client:
        body = client.get("/v1/whoami/", headers=SPOOF).json()

    assert body["full_url"] == "http://testserver/v1/whoami/"


@pytest.mark.parametrize("trusted", [["10.0.0.0/33"], ["nope"], []])
def test_bad_trusted_entries_fail_at_startup(trusted: list[str]) -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule, proxy=ProxyConfig(trusted=trusted))

    assert info.value.detail["reason"] == "invalid_proxy_config"


def test_trusting_every_peer_logs_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="lauren"):
        LaurenFactory.create(AppModule, proxy=ProxyConfig(trusted=["*"]))

    assert any("client-controlled" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("value", ["api.example.com", "ftp://h", "https://h/?q", 42])
def test_invalid_public_base_url_fails_at_startup(value: object) -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule, public_base_url=value)  # type: ignore[arg-type]

    assert info.value.detail["reason"] == "invalid_public_base_url"


def test_interior_hop_does_not_trust_unattributed_proto_or_host() -> None:
    proxy = ProxyConfig(trusted=["10.0.0.0/8"], max_hops=2)
    headers = {
        "x-forwarded-for": "203.0.113.9, 10.0.0.7",
        "x-forwarded-proto": "https",  # too few values for hop 2
        "x-forwarded-host": "evil.example",
    }

    with TestClient(_app(proxy=proxy), client=("10.0.0.5", 4000)) as client:
        body = client.get("/whoami/", headers=headers).json()

    assert body["client"] == "203.0.113.9"
    assert body["scheme"] == "http"
    assert body["host"] == "testserver"
