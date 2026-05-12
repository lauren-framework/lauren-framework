"""Integration tests for the Presigned URL skill (Skill 32).

Tests verify URL generation, signature validation, and expiry checking.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode

from lauren import (
    LaurenFactory,
    Query,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    Json,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class PresignedUrlService:
    def __init__(
        self,
        secret: str = "presign-secret",
        base_url: str = "https://storage.example.com",
    ) -> None:
        self._secret = secret
        self._base_url = base_url

    def generate_upload_url(self, bucket: str, key: str, ttl_seconds: int = 900) -> dict:
        expires = int(time.time()) + ttl_seconds
        to_sign = f"{bucket}/{key}:{expires}"
        sig = hmac.new(self._secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        params = urlencode({"bucket": bucket, "key": key, "expires": expires, "signature": sig})
        return {
            "url": f"{self._base_url}/upload?{params}",
            "method": "PUT",
            "expires_in": ttl_seconds,
        }

    def verify_url(self, bucket: str, key: str, expires: int, signature: str) -> bool:
        if time.time() > expires:
            return False
        to_sign = f"{bucket}/{key}:{expires}"
        expected = hmac.new(self._secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


@controller("/storage")
class StorageController:
    def __init__(self, svc: PresignedUrlService) -> None:
        self._svc = svc

    @post("/presign")
    async def generate_presigned_url(self, body: Json[dict]) -> dict:
        bucket = body.get("bucket", "default")
        key = body.get("key", "file.bin")
        ttl = int(body.get("ttl_seconds", 900))
        return self._svc.generate_upload_url(bucket, key, ttl)

    @get("/verify")
    async def verify(
        self,
        bucket: Query[str],
        key: Query[str],
        expires: Query[int],
        signature: Query[str],
    ) -> dict:
        valid = self._svc.verify_url(bucket, key, expires, signature)
        return {"valid": valid}


@module(controllers=[StorageController], providers=[PresignedUrlService])
class PresignedUrlModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(PresignedUrlModule))


class TestPresignedUrls:
    def test_generate_returns_url_and_method(self) -> None:
        client = build_app()
        r = client.post("/storage/presign", json={"bucket": "my-bucket", "key": "video.mp4"})
        assert r.status_code == 200
        body = r.json()
        assert "url" in body
        assert body["method"] == "PUT"
        assert "expires_in" in body

    def test_generated_url_contains_bucket_and_key(self) -> None:
        client = build_app()
        r = client.post("/storage/presign", json={"bucket": "photos", "key": "image.jpg"})
        url = r.json()["url"]
        assert "photos" in url
        assert "image.jpg" in url

    def test_generated_url_contains_signature(self) -> None:
        client = build_app()
        r = client.post("/storage/presign", json={"bucket": "b", "key": "k"})
        url = r.json()["url"]
        assert "signature=" in url

    def test_verify_valid_url(self) -> None:
        svc = PresignedUrlService(secret="test-secret")
        result = svc.generate_upload_url("bucket", "file.txt", ttl_seconds=300)
        from urllib.parse import urlparse, parse_qs

        parsed = parse_qs(urlparse(result["url"]).query)
        bucket = parsed["bucket"][0]
        key = parsed["key"][0]
        expires = int(parsed["expires"][0])
        signature = parsed["signature"][0]

        _ = build_app()
        # Use the service directly for this check
        assert svc.verify_url(bucket, key, expires, signature) is True

    def test_verify_expired_url_returns_false(self) -> None:
        svc = PresignedUrlService(secret="test-secret")
        expired_time = int(time.time()) - 100  # already expired
        to_sign = f"bucket/file.txt:{expired_time}"
        sig = hmac.new(svc._secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        assert svc.verify_url("bucket", "file.txt", expired_time, sig) is False

    def test_verify_wrong_signature_returns_false(self) -> None:
        svc = PresignedUrlService(secret="test-secret")
        expires = int(time.time()) + 300
        assert svc.verify_url("bucket", "file.txt", expires, "badsignature") is False

    def test_verify_endpoint(self) -> None:
        svc = PresignedUrlService(secret="presign-secret")
        result = svc.generate_upload_url("b", "k.txt", ttl_seconds=600)
        from urllib.parse import urlparse, parse_qs

        parsed = parse_qs(urlparse(result["url"]).query)
        expires = parsed["expires"][0]
        signature = parsed["signature"][0]

        client = build_app()
        r = client.get(f"/storage/verify?bucket=b&key=k.txt&expires={expires}&signature={signature}")
        assert r.status_code == 200
        assert r.json()["valid"] is True

    def test_custom_ttl(self) -> None:
        client = build_app()
        r = client.post("/storage/presign", json={"bucket": "b", "key": "k", "ttl_seconds": 60})
        assert r.json()["expires_in"] == 60
