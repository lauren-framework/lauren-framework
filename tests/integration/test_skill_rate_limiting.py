"""Integration tests for skill 24: API Rate Limiting (Token Bucket)."""

from __future__ import annotations

import time

from lauren import (
    LaurenFactory,
    Scope,
    controller,
    get,
    injectable,
    module,
    use_guards,
)
from lauren.exceptions import HTTPError
from lauren.testing import TestClient
from lauren.types import ExecutionContext


# ---------------------------------------------------------------------------
# Custom 429 error
# ---------------------------------------------------------------------------


class RateLimitError(HTTPError):
    status_code = 429
    code = "rate_limit_exceeded"


# ---------------------------------------------------------------------------
# Token bucket limiter
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class TokenBucketLimiter:
    def __init__(self, capacity: int = 60, refill_rate: float = 1.0) -> None:
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._buckets: dict[str, dict] = {}

    def _get_bucket(self, key: str) -> dict:
        if key not in self._buckets:
            self._buckets[key] = {
                "tokens": float(self._capacity),
                "last_refill": time.monotonic(),
            }
        return self._buckets[key]

    def consume(self, key: str, tokens: int = 1) -> bool:
        bucket = self._get_bucket(key)
        now = time.monotonic()
        elapsed = now - bucket["last_refill"]
        bucket["tokens"] = min(
            float(self._capacity),
            bucket["tokens"] + elapsed * self._refill_rate,
        )
        bucket["last_refill"] = now
        if bucket["tokens"] >= tokens:
            bucket["tokens"] -= tokens
            return True
        return False

    def set_tokens(self, key: str, tokens: float) -> None:
        """Test helper — forcibly set the token count for a bucket."""
        bucket = self._get_bucket(key)
        bucket["tokens"] = tokens
        bucket["last_refill"] = time.monotonic()


# ---------------------------------------------------------------------------
# Guard
# ---------------------------------------------------------------------------


@injectable(scope=Scope.SINGLETON)
class RateLimitGuard:
    def __init__(self, limiter: TokenBucketLimiter) -> None:
        self._limiter = limiter

    async def can_activate(self, ctx: ExecutionContext) -> bool:
        client_ip = ctx.request.headers.get("x-forwarded-for", "127.0.0.1")
        if not self._limiter.consume(client_ip):
            raise RateLimitError("Rate limit exceeded")
        return True


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


@use_guards(RateLimitGuard)
@controller("/api")
class RateLimitedController:
    @get("/data")
    async def data(self) -> dict:
        return {"data": "ok"}


@module(
    controllers=[RateLimitedController],
    providers=[RateLimitGuard, TokenBucketLimiter],
)
class RateLimitModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTokenBucketLimiter:
    """Unit tests for the limiter itself — no ASGI overhead."""

    def test_consume_within_capacity(self) -> None:
        limiter = TokenBucketLimiter(capacity=5, refill_rate=0)
        assert limiter.consume("client-a") is True
        assert limiter.consume("client-a") is True

    def test_consume_exhausts_bucket(self) -> None:
        limiter = TokenBucketLimiter(capacity=2, refill_rate=0)
        assert limiter.consume("x") is True
        assert limiter.consume("x") is True
        assert limiter.consume("x") is False

    def test_different_clients_have_separate_buckets(self) -> None:
        limiter = TokenBucketLimiter(capacity=1, refill_rate=0)
        assert limiter.consume("alice") is True
        assert limiter.consume("alice") is False
        assert limiter.consume("bob") is True  # bob has its own bucket

    def test_refill_over_time(self) -> None:
        # Use a very high refill rate; exhaust the bucket, then wait a tiny
        # bit and verify tokens are refilled.
        limiter = TokenBucketLimiter(capacity=1, refill_rate=1000.0)
        assert limiter.consume("key") is True
        assert limiter.consume("key") is False
        time.sleep(0.01)  # 10ms → ~10 tokens refilled
        assert limiter.consume("key") is True

    def test_tokens_do_not_exceed_capacity(self) -> None:
        limiter = TokenBucketLimiter(capacity=5, refill_rate=1000.0)
        limiter.set_tokens("key", 5.0)
        time.sleep(0.01)
        # After sleep, refill would push beyond capacity — must be capped at 5
        limiter.consume("key")  # consume 1
        bucket = limiter._get_bucket("key")
        assert bucket["tokens"] <= 5.0


class TestRateLimitGuardIntegration:
    def test_requests_within_limit_succeed(self) -> None:
        client = TestClient(LaurenFactory.create(RateLimitModule))
        for _ in range(5):
            r = client.get("/api/data")
            assert r.status_code == 200

    def test_rate_limited_request_returns_429(self) -> None:
        """Build an app with capacity=2 and fire 3 requests."""

        @injectable(scope=Scope.SINGLETON)
        class TinyLimiter(TokenBucketLimiter):
            def __init__(self) -> None:
                super().__init__(capacity=2, refill_rate=0.0)

        @injectable(scope=Scope.SINGLETON)
        class TinyGuard:
            def __init__(self, limiter: TinyLimiter) -> None:
                self._limiter = limiter

            async def can_activate(self, ctx: ExecutionContext) -> bool:
                if not self._limiter.consume("test-client"):
                    raise RateLimitError("Rate limit exceeded")
                return True

        @use_guards(TinyGuard)
        @controller("/limited")
        class LimitedCtrl:
            @get("/")
            async def index(self) -> dict:
                return {"ok": True}

        @module(controllers=[LimitedCtrl], providers=[TinyGuard, TinyLimiter])
        class LimitedModule:
            pass

        app_client = TestClient(LaurenFactory.create(LimitedModule))

        r1 = app_client.get("/limited/")
        r2 = app_client.get("/limited/")
        r3 = app_client.get("/limited/")

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r3.status_code == 429

    def test_different_ips_are_tracked_separately(self) -> None:
        """Two clients with different IPs should each have their own bucket."""

        @injectable(scope=Scope.SINGLETON)
        class SmallLimiter(TokenBucketLimiter):
            def __init__(self) -> None:
                super().__init__(capacity=1, refill_rate=0.0)

        @injectable(scope=Scope.SINGLETON)
        class SmallGuard:
            def __init__(self, limiter: SmallLimiter) -> None:
                self._limiter = limiter

            async def can_activate(self, ctx: ExecutionContext) -> bool:
                ip = ctx.request.headers.get("x-forwarded-for", "127.0.0.1")
                if not self._limiter.consume(ip):
                    raise RateLimitError("Rate limit exceeded")
                return True

        @use_guards(SmallGuard)
        @controller("/ip-limited")
        class IpCtrl:
            @get("/")
            async def index(self) -> dict:
                return {"ok": True}

        @module(controllers=[IpCtrl], providers=[SmallGuard, SmallLimiter])
        class IpModule:
            pass

        app_client = TestClient(LaurenFactory.create(IpModule))

        # First request from 1.2.3.4 — ok
        r1 = app_client.get("/ip-limited/", headers={"x-forwarded-for": "1.2.3.4"})
        assert r1.status_code == 200

        # Second request from 1.2.3.4 — blocked
        r2 = app_client.get("/ip-limited/", headers={"x-forwarded-for": "1.2.3.4"})
        assert r2.status_code == 429

        # First request from 5.6.7.8 — ok (different bucket)
        r3 = app_client.get("/ip-limited/", headers={"x-forwarded-for": "5.6.7.8"})
        assert r3.status_code == 200
