"""Signed URLs end to end: ``signed_url_for`` + ``ValidSignature`` through a running app."""

from __future__ import annotations

import pytest

from lauren import LaurenFactory, controller, get, injectable, module, use_guards
from lauren.exceptions import StartupError
from lauren.testing import TestClient
from lauren.urls import UrlBuilder, UrlSigningConfig, ValidSignature
from lauren.websockets import WebSocket, on_connect, ws_controller

NOW = 1_800_000_000.0


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


@controller("/downloads", name="downloads")
class Downloads:
    @get("/{id}", name="file")
    @use_guards(ValidSignature)
    async def file(self, id: int) -> dict:
        return {"file": id}


@ws_controller("/ws/feed/{topic}", name="feed")
@use_guards(ValidSignature)
class Feed:
    @on_connect
    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        await ws.send_json({"ok": True})


@injectable()
class Links:
    def __init__(self, urls: UrlBuilder) -> None:
        self.urls = urls


@module(controllers=[Downloads, Feed], providers=[Links])
class AppModule:
    pass


def _app(clock: Clock | None = None, **signing: object):
    kwargs = {k: signing.pop(k) for k in ("public_base_url", "root_path") if k in signing}
    config = UrlSigningConfig(secret=signing.pop("secret", ["k-new", "k-old"]), clock=clock, **signing)  # type: ignore[arg-type]
    return LaurenFactory.create(AppModule, url_signing=config, **kwargs)


def test_signed_link_round_trip() -> None:
    app = _app()

    with TestClient(app) as client:
        url = app.urls.signed_url_for("downloads.file", id=7, expires_in=60)
        response = client.get(url)

    assert url.startswith("/downloads/7?exp=")
    assert response.status_code == 200
    assert response.json() == {"file": 7}


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda url: url.split("?")[0], "signature_missing"),
        (lambda url: url.replace("/7?", "/8?"), "signature_invalid"),
        (lambda url: url + "&extra=1", "signature_invalid"),
    ],
)
def test_tampered_or_missing_signature_is_forbidden(mutate, reason: str) -> None:
    app = _app()

    with TestClient(app) as client:
        response = client.get(mutate(app.urls.signed_url_for("downloads.file", id=7)))

    assert response.status_code == 403
    assert response.json()["error"]["detail"] == {"reason": reason}


def test_expired_link_is_forbidden() -> None:
    clock = Clock()
    app = _app(clock)

    with TestClient(app) as client:
        url = app.urls.signed_url_for("downloads.file", id=7, expires_in=60)
        clock.now += 120
        response = client.get(url)

    assert response.status_code == 403
    assert response.json()["error"]["detail"] == {"reason": "signature_expired"}


def test_link_signed_with_a_rotated_out_key_still_verifies() -> None:
    old = _app(secret="k-old")
    rotated = _app(secret=["k-new", "k-old"])

    with TestClient(rotated) as client:
        response = client.get(old.urls.signed_url_for("downloads.file", id=1))

    assert response.status_code == 200


def test_signed_query_parameters_are_covered() -> None:
    app = _app()

    with TestClient(app) as client:
        url = app.urls.signed_url_for("downloads.file", id=7, query={"name": "report.pdf"})
        ok = client.get(url)
        tampered = client.get(url.replace("report.pdf", "other.pdf"))

    assert "name=report.pdf" in url
    assert ok.status_code == 200
    assert tampered.status_code == 403


def test_absolute_signed_link_under_a_public_base_url() -> None:
    app = _app(public_base_url="https://files.example.com/api", root_path="/api", bind_host=True)

    url = app.urls.signed_url_for(Downloads.file, id=3)

    assert url.startswith("https://files.example.com/api/downloads/3?exp=")
    with TestClient(app, base_url="https://files.example.com") as client:
        assert client.get(url.removeprefix("https://files.example.com")).status_code == 200


def test_host_bound_link_is_rejected_on_another_host() -> None:
    app = _app(public_base_url="https://files.example.com", bind_host=True)
    url = app.urls.signed_url_for("downloads.file", id=3)

    with TestClient(app, base_url="https://evil.example") as client:
        response = client.get(url.removeprefix("https://files.example.com"))

    assert response.status_code == 403


def test_signed_link_is_valid_under_a_mount_prefix() -> None:
    child = _app()

    @module()
    class Parent:
        pass

    parent = LaurenFactory.create(Parent, mounts={"/v1": child})
    with TestClient(parent) as client:
        response = client.get("/v1" + child.urls.signed_url_for("downloads.file", id=2))

    assert response.status_code == 200


def test_signed_websocket_link() -> None:
    app = _app(public_base_url="https://feeds.example.com")
    url = app.urls.signed_ws_url_for("feed", topic="news")

    assert url.startswith("wss://feeds.example.com/ws/feed/news?exp=")
    path, _, query = url.removeprefix("wss://feeds.example.com").partition("?")
    with TestClient(app) as client:
        with client.websocket_connect(path, query_string=query) as ws:
            assert ws.receive_json() == {"ok": True}


async def test_unsigned_websocket_connection_is_closed_with_4403() -> None:
    from lauren.testing import WsTestClient

    app = _app()
    await app.startup()
    try:
        async with WsTestClient(app).connect("/ws/feed/news") as ws:
            pass
    finally:
        await app.shutdown()

    assert (ws.close_code, ws.close_reason) == (4403, "signature_missing")


def test_signed_ws_url_needs_a_public_base_url() -> None:
    with pytest.raises(RuntimeError, match="public_base_url"):
        _app().urls.signed_ws_url_for("feed", topic="x")


def test_valid_signature_without_url_signing_fails_at_startup() -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule)

    assert info.value.detail["reason"] == "url_signing_disabled"
    assert info.value.detail["guard"] == "ValidSignature"


def test_global_valid_signature_without_url_signing_fails_at_startup() -> None:
    @module()
    class Empty:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(Empty, global_guards=[ValidSignature])

    assert info.value.detail["where"] == "every request (global guard)"


def test_bind_host_without_public_base_url_fails_at_startup() -> None:
    with pytest.raises(StartupError) as info:
        LaurenFactory.create(AppModule, url_signing=UrlSigningConfig(secret="k", bind_host=True))

    assert info.value.detail == {"reason": "invalid_url_signing_config", "field": "bind_host"}
