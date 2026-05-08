---
name: presigned-url-uploads
description: Generates and verifies time-limited HMAC-signed URLs for direct client-to-storage uploads. Use when you need to let browsers upload files directly to S3/GCS/MinIO without routing the bytes through the application server.
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Presigned URL Generation for Direct Uploads

## Overview

`PresignedUrlService` signs a `(bucket, key, expires)` tuple with HMAC-SHA256
and embeds the signature in a query string. The same service can verify incoming
requests from clients who received the URL. This is pure Python — no S3 SDK
required for signing. In production you would delegate signing to
`boto3.generate_presigned_url` or the GCS equivalent; the Lauren controller
simply calls the service to produce the JSON response.

## PresignedUrlService

```python
import hashlib
import hmac
import time
from urllib.parse import urlencode
from lauren import injectable, Scope

@injectable(scope=Scope.SINGLETON)
class PresignedUrlService:
    def __init__(self, secret: str = "presign-secret", base_url: str = "https://storage.example.com"):
        self._secret = secret
        self._base_url = base_url

    def generate_upload_url(self, bucket: str, key: str, ttl_seconds: int = 900) -> dict:
        expires = int(time.time()) + ttl_seconds
        to_sign = f"{bucket}/{key}:{expires}"
        sig = hmac.new(self._secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        params = urlencode({"bucket": bucket, "key": key, "expires": expires, "signature": sig})
        return {"url": f"{self._base_url}/upload?{params}", "method": "PUT", "expires_in": ttl_seconds}

    def verify_url(self, bucket: str, key: str, expires: int, signature: str) -> bool:
        if time.time() > expires:
            return False
        to_sign = f"{bucket}/{key}:{expires}"
        expected = hmac.new(self._secret.encode(), to_sign.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature)
```

## Controller

```python
from lauren import controller, post, get, module, Path, Query, Json

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
        bucket: str = Query(),
        key: str = Query(),
        expires: int = Query(),
        signature: str = Query(),
    ) -> dict:
        valid = self._svc.verify_url(bucket, key, expires, signature)
        return {"valid": valid}

@module(controllers=[StorageController], providers=[PresignedUrlService])
class PresignedUrlModule:
    pass
```

## Wiring

```python
svc = PresignedUrlService(secret="my-secret", base_url="https://cdn.example.com")

app = LaurenFactory.create(
    AppModule,
    global_providers=[use_value(provide=PresignedUrlService, value=svc)],
)
```

## Production notes

For real S3 presigned URLs delegate to boto3:

```python
import boto3

s3 = boto3.client("s3")
url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
    ExpiresIn=ttl_seconds,
)
```

The Lauren controller shape stays the same — only the `PresignedUrlService`
implementation changes.
