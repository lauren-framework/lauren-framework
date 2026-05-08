"""Integration tests for the Redis caching layer (Skill 13).

Uses fakeredis so no real Redis server is required.
"""

from __future__ import annotations

import json

import fakeredis

from lauren import (
    LaurenFactory,
    Path,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# CacheService
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class CacheService:
    def __init__(self) -> None:
        self._redis = fakeredis.FakeRedis(decode_responses=True)

    def get(self, key: str):
        val = self._redis.get(key)
        return json.loads(val) if val is not None else None

    def set(self, key: str, value, ttl: int = 300) -> None:
        self._redis.setex(key, ttl, json.dumps(value))

    def delete(self, key: str) -> None:
        self._redis.delete(key)

    def invalidate_prefix(self, prefix: str) -> int:
        keys = self._redis.keys(f"{prefix}:*")
        if keys:
            return self._redis.delete(*keys)
        return 0


# ---------------------------------------------------------------------------
# Fake "DB" and controller
# ---------------------------------------------------------------------------

_DB: dict[str, dict] = {"1": {"id": "1", "name": "Widget"}}
_fetch_count = 0


@controller("/items")
class CachedItemController:
    def __init__(self, cache: CacheService) -> None:
        self._cache = cache

    @get("/{item_id}")
    async def get_item(self, item_id: Path[str]) -> dict:
        global _fetch_count
        key = f"item:{item_id}"
        cached = self._cache.get(key)
        if cached is not None:
            return {**cached, "source": "cache"}
        _fetch_count += 1
        data = _DB.get(item_id, {"id": item_id, "name": "Unknown"})
        self._cache.set(key, data, ttl=60)
        return {**data, "source": "db"}

    @post("/{item_id}/invalidate")
    async def invalidate(self, item_id: Path[str]) -> dict:
        self._cache.delete(f"item:{item_id}")
        return {"invalidated": item_id}

    @post("/invalidate-all")
    async def invalidate_all(self) -> dict:
        count = self._cache.invalidate_prefix("item")
        return {"deleted": count}


@module(controllers=[CachedItemController], providers=[CacheService])
class CacheModule:
    pass


def build_app():
    global _fetch_count
    _fetch_count = 0
    return TestClient(LaurenFactory.create(CacheModule))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRedisCaching:
    def test_cache_miss_fetches_from_db(self):
        client = build_app()
        r = client.get("/items/1")
        assert r.status_code == 200
        assert r.json()["source"] == "db"

    def test_second_call_returns_cached_value(self):
        client = build_app()
        client.get("/items/1")  # prime cache
        r = client.get("/items/1")
        assert r.status_code == 200
        assert r.json()["source"] == "cache"
        assert r.json()["name"] == "Widget"

    def test_db_only_fetched_once_on_repeated_reads(self):
        global _fetch_count
        client = build_app()
        for _ in range(5):
            client.get("/items/1")
        assert _fetch_count == 1  # only the first request hit the "DB"

    def test_invalidation_clears_single_key(self):
        client = build_app()
        client.get("/items/1")  # prime
        client.post("/items/1/invalidate")
        r = client.get("/items/1")  # should miss cache again
        assert r.json()["source"] == "db"

    def test_prefix_invalidation_clears_all_item_keys(self):
        global _fetch_count
        client = build_app()
        client.get("/items/1")  # prime item:1
        r = client.post("/items/invalidate-all")
        assert r.json()["deleted"] >= 1
        # After invalidation the next request hits DB again
        r2 = client.get("/items/1")
        assert r2.json()["source"] == "db"
