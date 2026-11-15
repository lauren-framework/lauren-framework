"""The presigned-upload skill: Lauren-owned URLs, method binding and key rotation."""

from __future__ import annotations

from urllib.parse import urlsplit

from lauren import Json, LaurenFactory, Request, UrlBuilder, UrlSigningConfig
from lauren import controller, injectable, module, post, put, use_guards
from lauren.testing import TestClient
from lauren.urls import ValidSignature


class Clock:
    def __init__(self) -> None:
        self.now = 1_800_000_000

    def __call__(self) -> int:
        return self.now


@injectable()
class UploadLinks:
    def __init__(self, urls: UrlBuilder) -> None:
        self.urls = urls

    def issue(self, bucket: str, key: str, ttl: int = 900) -> dict:
        url = self.urls.signed_url_for(StorageController.upload, bucket=bucket, key=key, expires_in=ttl)
        return {"url": url, "method": "PUT", "expires_in": ttl}


@controller("/storage", name="storage")
class StorageController:
    def __init__(self, links: UploadLinks) -> None:
        self._links = links

    @post("/presign")
    async def presign(self, body: Json[dict]) -> dict:
        return self._links.issue(body["bucket"], body["key"], body.get("ttl_seconds", 900))

    @put("/upload/{bucket}/{key}", name="upload")
    @use_guards(ValidSignature)
    async def upload(self, bucket: str, key: str, request: Request) -> dict:
        return {"bucket": bucket, "key": key, "size": len(await request.body())}


@module(controllers=[StorageController], providers=[UploadLinks])
class UploadModule:
    pass


def _app(clock: Clock, secret: str | list[str] = "new-secret"):
    return LaurenFactory.create(
        UploadModule,
        public_base_url="https://uploads.example.com",
        url_signing=UrlSigningConfig(secret=secret, bind_method=True, clock=clock),
    )


def test_upload_link_round_trips_through_guard() -> None:
    app = _app(Clock())

    with TestClient(app, base_url="https://uploads.example.com") as client:
        issued = client.post("/storage/presign", json={"bucket": "photos", "key": "image.jpg"}).json()
        uploaded = client.put(issued["url"], content=b"photo")

    assert issued["method"] == "PUT"
    assert issued["expires_in"] == 900
    assert urlsplit(issued["url"]).path == "/storage/upload/photos/image.jpg"
    assert uploaded.json() == {"bucket": "photos", "key": "image.jpg", "size": 5}


def test_custom_ttl_is_returned() -> None:
    with TestClient(_app(Clock()), base_url="https://uploads.example.com") as client:
        issued = client.post("/storage/presign", json={"bucket": "b", "key": "k", "ttl_seconds": 60}).json()

    assert issued["expires_in"] == 60


def test_tampering_a_parameter_fails() -> None:
    app = _app(Clock())

    with TestClient(app, base_url="https://uploads.example.com") as client:
        url = app.urls.signed_url_for(StorageController.upload, bucket="b", key="k")
        response = client.put(url.replace("/b/k?", "/b/other?"), content=b"hello")

    assert response.status_code == 403
    assert response.json()["error"]["detail"]["reason"] == "signature_invalid"


def test_expiry_fails_without_sleeping() -> None:
    clock = Clock()
    app = _app(clock)

    with TestClient(app, base_url="https://uploads.example.com") as client:
        url = app.urls.signed_url_for(StorageController.upload, bucket="b", key="k", expires_in=60)
        clock.now += 61
        response = client.put(url, content=b"hello")

    assert response.status_code == 403
    assert response.json()["error"]["detail"]["reason"] == "signature_expired"


def test_previous_secret_verifies_after_rotation() -> None:
    clock = Clock()
    old = _app(clock, secret="old-secret")
    rotated = _app(clock, secret=["new-secret", "old-secret"])
    url = old.urls.signed_url_for(StorageController.upload, bucket="b", key="k")

    with TestClient(rotated, base_url="https://uploads.example.com") as client:
        response = client.put(url, content=b"ok")

    assert response.json()["size"] == 2
