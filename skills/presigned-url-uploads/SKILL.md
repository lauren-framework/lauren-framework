---
name: presigned-url-uploads
description: Issues expiring, method-bound URLs for Lauren-owned upload endpoints with UrlSigningConfig and ValidSignature. For direct-to-S3/GCS uploads, use the storage provider's SDK instead.
---

# Signed URLs for Uploads

Use **Lauren's signed URLs** when the bytes pass through a Lauren route.
The app's `UrlBuilder` signs the route path, query parameters and expiry
using the shared key-rotation-capable signer; `ValidSignature` verifies them
before the upload handler runs. A signed URL is readable, not encrypted.

When clients upload **directly to S3, GCS or MinIO** (not to your Lauren
app), use the storage provider's presigning SDK (`boto3.generate_presigned_url`,
GCS `generate_signed_url`, etc.). A Lauren signature is **not** a valid S3 or
GCS signature; do not hand-roll HMAC for their protocols.

## Lauren-owned upload endpoint

```python
from __future__ import annotations

from lauren import Json, LaurenFactory, Request, UrlBuilder, UrlSigningConfig
from lauren import controller, get, injectable, module, post, put, use_guards
from lauren.urls import ValidSignature


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
        content = await request.body()
        # Validate bucket/key against your own storage policy; persist content here.
        return {"bucket": bucket, "key": key, "size": len(content)}


@module(controllers=[StorageController], providers=[UploadLinks])
class UploadModule:
    pass


app = LaurenFactory.create(
    UploadModule,
    public_base_url="https://uploads.example.com",
    url_signing=UrlSigningConfig(secret=["new-secret", "old-secret"], bind_method=True),
)
```

`public_base_url` is the **trusted public URL** (include the public path prefix
if there is one). Never build email or upload links from a client's `Host`
header. The first secret signs, all verify; rotate by prepending a new key.
Give URL signing its own secret, not the session secret. Keep TTLs short,
restrict who may call `/presign`, and do not log URL query strings. For a
reverse proxy see [URLs, Proxies & Signed Links](../../docs/guides/urls-and-proxies.md).

`ValidSignature` rejects missing, tampered or expired links with HTTP 403 and
`detail["reason"]` (`signature_missing`, `signature_invalid`,
`signature_expired`). `bind_method=True` stops a signed `PUT` link from
authorising another HTTP method. Using the guard without `url_signing=` fails
at startup. Test expiry with `UrlSigningConfig(clock=fake_clock)` — do not
sleep or construct signatures in test code.

## Direct-to-storage instead

```python fragment
import boto3

s3 = boto3.client("s3")
url = s3.generate_presigned_url(
    "put_object",
    Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
    ExpiresIn=ttl_seconds,
)
```

Only the storage SDK knows the storage provider's required canonical request,
headers, credentials and signing scheme. Lauren returns the SDK's URL to the
client; it does **not** verify the storage upload (the storage service does).
