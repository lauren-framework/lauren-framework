"""Integration tests for the Object Storage skill (Skill 31).

Tests drive a real LaurenApp via TestClient to verify upload, download,
delete, and exists operations against an in-memory ObjectStore.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from lauren import (
    LaurenFactory,
    Path,
    Scope,
    controller,
    delete,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Domain
# ---------------------------------------------------------------------------


class ObjectStore(ABC):
    @abstractmethod
    async def upload(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str: ...

    @abstractmethod
    async def download(self, bucket: str, key: str) -> bytes: ...

    @abstractmethod
    async def delete(self, bucket: str, key: str) -> None: ...

    @abstractmethod
    async def exists(self, bucket: str, key: str) -> bool: ...


class InMemoryObjectStore(ObjectStore):
    def __init__(self) -> None:
        self._store: dict[str, dict[str, bytes]] = {}

    async def upload(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
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


@injectable(scope=Scope.SINGLETON)
class StorageService:
    def __init__(self) -> None:
        self._store: ObjectStore = InMemoryObjectStore()

    async def upload_file(
        self,
        bucket: str,
        key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        return await self._store.upload(bucket, key, data, content_type)

    async def download_file(self, bucket: str, key: str) -> bytes:
        return await self._store.download(bucket, key)

    async def delete_file(self, bucket: str, key: str) -> None:
        return await self._store.delete(bucket, key)

    async def file_exists(self, bucket: str, key: str) -> bool:
        return await self._store.exists(bucket, key)


# ---------------------------------------------------------------------------
# Controller & Module
# ---------------------------------------------------------------------------


@controller("/storage")
class StorageController:
    def __init__(self, svc: StorageService) -> None:
        self._svc = svc

    @post("/upload/{bucket}/{key}")
    async def upload(self, bucket: Path[str], key: Path[str]) -> dict:
        url = await self._svc.upload_file(bucket, key, b"hello world")
        return {"url": url}

    @get("/download/{bucket}/{key}")
    async def download(self, bucket: Path[str], key: Path[str]) -> dict:
        data = await self._svc.download_file(bucket, key)
        return {"size": len(data), "data": data.decode()}

    @delete("/delete/{bucket}/{key}")
    async def delete_object(self, bucket: Path[str], key: Path[str]) -> dict:
        await self._svc.delete_file(bucket, key)
        return {"deleted": True}

    @get("/exists/{bucket}/{key}")
    async def exists(self, bucket: Path[str], key: Path[str]) -> dict:
        found = await self._svc.file_exists(bucket, key)
        return {"exists": found}


@module(controllers=[StorageController], providers=[StorageService])
class StorageModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def build_app() -> TestClient:
    return TestClient(LaurenFactory.create(StorageModule))


class TestObjectStorage:
    def test_upload_returns_url(self) -> None:
        client = build_app()
        r = client.post("/storage/upload/my-bucket/readme.txt")
        assert r.status_code == 200
        assert "url" in r.json()
        assert "my-bucket" in r.json()["url"]

    def test_download_after_upload(self) -> None:
        client = build_app()
        client.post("/storage/upload/bucket/file.txt")
        r = client.get("/storage/download/bucket/file.txt")
        assert r.status_code == 200
        assert r.json()["data"] == "hello world"

    def test_exists_after_upload(self) -> None:
        client = build_app()
        # not yet uploaded
        r = client.get("/storage/exists/bucket/missing.txt")
        assert r.status_code == 200
        assert r.json()["exists"] is False

        client.post("/storage/upload/bucket/present.txt")
        r = client.get("/storage/exists/bucket/present.txt")
        assert r.json()["exists"] is True

    def test_delete_removes_object(self) -> None:
        client = build_app()
        client.post("/storage/upload/bucket/to-delete.txt")
        r = client.get("/storage/exists/bucket/to-delete.txt")
        assert r.json()["exists"] is True

        client.delete("/storage/delete/bucket/to-delete.txt")
        r = client.get("/storage/exists/bucket/to-delete.txt")
        assert r.json()["exists"] is False

    def test_upload_multiple_keys(self) -> None:
        client = build_app()
        client.post("/storage/upload/b/key1")
        client.post("/storage/upload/b/key2")

        r1 = client.get("/storage/exists/b/key1")
        r2 = client.get("/storage/exists/b/key2")
        assert r1.json()["exists"] is True
        assert r2.json()["exists"] is True

    def test_inmemory_store_upload_download_direct(self) -> None:
        import asyncio

        store = InMemoryObjectStore()
        loop = asyncio.new_event_loop()
        try:
            url = loop.run_until_complete(store.upload("b", "k", b"test", "text/plain"))
            assert url == "/b/k"
            data = loop.run_until_complete(store.download("b", "k"))
            assert data == b"test"
            assert loop.run_until_complete(store.exists("b", "k")) is True
            loop.run_until_complete(store.delete("b", "k"))
            assert loop.run_until_complete(store.exists("b", "k")) is False
        finally:
            loop.close()
