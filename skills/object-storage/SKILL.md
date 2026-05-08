---
name: object-storage
description: Integrates object storage (S3, GCS, MinIO) via an abstract ObjectStore interface. Use when you need to upload, download, delete, or check existence of blobs with a swappable backend (in-memory for tests, S3/GCS/MinIO in production).
---

> Use `codemap find "SymbolName"` to locate any symbol before reading — it gives
> exact file + line range and is faster than grep across the whole repo.

# Object Storage Integration (S3 / GCS / MinIO)

## Overview

`ObjectStore` is an abstract base class defining the blob storage contract.
`InMemoryObjectStore` is the test/dev implementation. Swap it for `S3ObjectStore`
(boto3), `GCSObjectStore` (google-cloud-storage), or `MinIOObjectStore` (minio
SDK) in production. `StorageService` wraps the store and is the DI-injectable
singleton controllers consume.

## Abstract Interface

```python
from abc import ABC, abstractmethod
from lauren import injectable, Scope

class ObjectStore(ABC):
    @abstractmethod
    async def upload(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    @abstractmethod
    async def download(self, bucket: str, key: str) -> bytes: ...
    @abstractmethod
    async def delete(self, bucket: str, key: str) -> None: ...
    @abstractmethod
    async def exists(self, bucket: str, key: str) -> bool: ...
```

## In-Memory Implementation (tests / local dev)

```python
class InMemoryObjectStore(ObjectStore):
    def __init__(self):
        self._store: dict[str, dict[str, bytes]] = {}

    async def upload(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._store.setdefault(bucket, {})[key] = data
        return f"/{bucket}/{key}"

    async def download(self, bucket: str, key: str) -> bytes:
        if bucket not in self._store or key not in self._store[bucket]:
            raise KeyError(f"Object {bucket}/{key} not found")
        return self._store[bucket][key]

    async def delete(self, bucket: str, key: str) -> None:
        self._store.get(bucket, {}).pop(key, None)

    async def exists(self, bucket: str, key: str) -> bool:
        return key in self._store.get(bucket, {})
```

## S3 Implementation (boto3)

```python
import boto3
from botocore.exceptions import ClientError

class S3ObjectStore(ObjectStore):
    def __init__(self, region: str = "us-east-1"):
        self._s3 = boto3.client("s3", region_name=region)

    async def upload(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        self._s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return f"s3://{bucket}/{key}"

    async def download(self, bucket: str, key: str) -> bytes:
        obj = self._s3.get_object(Bucket=bucket, Key=key)
        return obj["Body"].read()

    async def delete(self, bucket: str, key: str) -> None:
        self._s3.delete_object(Bucket=bucket, Key=key)

    async def exists(self, bucket: str, key: str) -> bool:
        try:
            self._s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError:
            return False
```

## StorageService (injectable singleton)

```python
@injectable(scope=Scope.SINGLETON)
class StorageService:
    def __init__(self, store: ObjectStore | None = None):
        self._store = store or InMemoryObjectStore()

    async def upload_file(self, bucket: str, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        return await self._store.upload(bucket, key, data, content_type)

    async def download_file(self, bucket: str, key: str) -> bytes:
        return await self._store.download(bucket, key)

    async def delete_file(self, bucket: str, key: str) -> None:
        return await self._store.delete(bucket, key)

    async def file_exists(self, bucket: str, key: str) -> bool:
        return await self._store.exists(bucket, key)
```

## HTTP Controller

```python
from lauren import controller, get, post, delete, module, Path, Json

@controller("/storage")
class StorageController:
    def __init__(self, svc: StorageService) -> None:
        self._svc = svc

    @post("/upload/{bucket}/{key}")
    async def upload(self, bucket: str = Path(), key: str = Path(), body: Json[dict] | None = None) -> dict:
        data = (body or {}).get("data", "").encode()
        url = await self._svc.upload_file(bucket, key, data)
        return {"url": url}

    @get("/download/{bucket}/{key}")
    async def download(self, bucket: str = Path(), key: str = Path()) -> dict:
        data = await self._svc.download_file(bucket, key)
        return {"size": len(data)}

    @delete("/delete/{bucket}/{key}")
    async def delete_object(self, bucket: str = Path(), key: str = Path()) -> dict:
        await self._svc.delete_file(bucket, key)
        return {"deleted": True}

@module(controllers=[StorageController], providers=[StorageService])
class StorageModule:
    pass
```

## Wiring for production (S3)

Pass a pre-built `S3ObjectStore` via `use_value`:

```python
from lauren._di.custom import use_value

s3_store = S3ObjectStore(region="eu-west-1")
service = StorageService(store=s3_store)

app = LaurenFactory.create(
    AppModule,
    global_providers=[use_value(provide=StorageService, value=service)],
)
```

## GCS / MinIO notes

- **GCS**: replace boto3 calls with `google.cloud.storage.Client`.
- **MinIO**: use the `minio` SDK or configure boto3 with a custom endpoint URL
  (`endpoint_url="http://minio:9000"`).
