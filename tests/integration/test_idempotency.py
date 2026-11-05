"""``@idempotent`` end to end: replay, in-flight 409, mismatch 422, missing header 400."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import LaurenFactory, Response, StartupError, controller, injectable, module, post, put
from lauren.cache import CacheModule, idempotent
from lauren.testing import AsyncTestClient, TestClient


@injectable()
class Ledger:
    def __init__(self) -> None:
        self.runs = 0


@controller("/pay")
class PaymentController:
    def __init__(self, ledger: Ledger) -> None:
        self.ledger = ledger

    @post("/")
    @idempotent(ttl=3600)
    async def create(self) -> Response:
        self.ledger.runs += 1
        return Response.json({"payment": self.ledger.runs}, status=201).with_cookie("receipt", "r1")

    @post("/optional")
    @idempotent(required=False)
    async def optional(self) -> dict:
        self.ledger.runs += 1
        return {"run": self.ledger.runs}

    @post("/flaky")
    @idempotent()
    async def flaky(self) -> dict:
        self.ledger.runs += 1
        if self.ledger.runs == 1:
            raise RuntimeError("downstream timeout")
        return {"run": self.ledger.runs}

    @post("/fails")
    @idempotent()
    async def fails(self) -> Response:
        self.ledger.runs += 1
        return Response.json({"run": self.ledger.runs}, status=503)

    @put("/tenant")
    @idempotent(scope=lambda ctx: ctx.request.headers.get("x-tenant"), header="X-Request-Key")
    async def tenant(self) -> dict:
        self.ledger.runs += 1
        return {"run": self.ledger.runs}


@module(imports=[CacheModule.for_root()], providers=[Ledger], controllers=[PaymentController])
class PaymentsModule:
    pass


@pytest.fixture
def client() -> Any:
    with TestClient(LaurenFactory.create(PaymentsModule)) as c:
        yield c


def runs(client: TestClient) -> int:
    return client.resolve(Ledger).runs


def test_a_retry_with_the_same_key_replays_the_stored_response(client: TestClient) -> None:
    first = client.post("/pay/", headers={"Idempotency-Key": "k1"}, json={"amount": 5})
    retry = client.post("/pay/", headers={"Idempotency-Key": "k1"}, json={"amount": 5})
    assert (first.status_code, retry.status_code) == (201, 201)
    assert first.json() == retry.json() == {"payment": 1}
    assert retry.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert runs(client) == 1


def test_a_replay_never_carries_set_cookie(client: TestClient) -> None:
    first = client.post("/pay/", headers={"Idempotency-Key": "k1"})
    retry = client.post("/pay/", headers={"Idempotency-Key": "k1"})
    assert "set-cookie" in first.headers
    assert "set-cookie" not in retry.headers


def test_a_different_key_runs_the_handler_again(client: TestClient) -> None:
    client.post("/pay/", headers={"Idempotency-Key": "k1"})
    assert client.post("/pay/", headers={"Idempotency-Key": "k2"}).json() == {"payment": 2}


def test_the_same_key_with_another_payload_is_rejected_with_422(client: TestClient) -> None:
    client.post("/pay/", headers={"Idempotency-Key": "k1"}, json={"amount": 5})
    mismatch = client.post("/pay/", headers={"Idempotency-Key": "k1"}, json={"amount": 6})
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "idempotency_key_mismatch"
    assert runs(client) == 1


def test_a_missing_required_key_is_rejected_with_400(client: TestClient) -> None:
    response = client.post("/pay/")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_missing"
    assert response.json()["error"]["detail"] == {"header": "Idempotency-Key"}
    assert runs(client) == 0


def test_an_over_long_key_is_rejected_with_400(client: TestClient) -> None:
    response = client.post("/pay/", headers={"Idempotency-Key": "k" * 256})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "idempotency_key_invalid"


def test_an_optional_key_may_be_omitted(client: TestClient) -> None:
    assert client.post("/pay/optional").json() == {"run": 1}
    assert client.post("/pay/optional").json() == {"run": 2}
    assert client.post("/pay/optional", headers={"Idempotency-Key": "a"}).json() == {"run": 3}
    assert client.post("/pay/optional", headers={"Idempotency-Key": "a"}).json() == {"run": 3}


def test_a_handler_that_raised_stores_nothing_so_the_retry_runs(client: TestClient) -> None:
    assert client.post("/pay/flaky", headers={"Idempotency-Key": "k"}).status_code == 500
    retry = client.post("/pay/flaky", headers={"Idempotency-Key": "k"})
    assert retry.json() == {"run": 2}
    assert "idempotent-replayed" not in retry.headers


def test_server_errors_are_not_stored(client: TestClient) -> None:
    client.post("/pay/fails", headers={"Idempotency-Key": "k"})
    assert client.post("/pay/fails", headers={"Idempotency-Key": "k"}).json() == {"run": 2}


def test_scope_partitions_keys_and_the_header_name_is_configurable(client: TestClient) -> None:
    a = client.put("/pay/tenant", headers={"X-Request-Key": "k", "x-tenant": "a"}).json()
    b = client.put("/pay/tenant", headers={"X-Request-Key": "k", "x-tenant": "b"}).json()
    a_again = client.put("/pay/tenant", headers={"X-Request-Key": "k", "x-tenant": "a"}).json()
    assert (a, b, a_again) == ({"run": 1}, {"run": 2}, {"run": 1})


async def test_the_same_key_while_in_flight_answers_409() -> None:
    gate = asyncio.Event()
    started = asyncio.Event()

    @controller("/slow")
    class Slow:
        @post("/")
        @idempotent()
        async def handler(self) -> dict:
            started.set()
            await gate.wait()
            return {"done": True}

    @module(imports=[CacheModule.for_root()], controllers=[Slow])
    class SlowModule:
        pass

    async with AsyncTestClient(LaurenFactory.create(SlowModule)) as client:
        first = asyncio.ensure_future(client.post("/slow/", headers={"Idempotency-Key": "k"}))
        await started.wait()
        conflict = await client.post("/slow/", headers={"Idempotency-Key": "k"})
        gate.set()
        done = await first
        replay = await client.post("/slow/", headers={"Idempotency-Key": "k"})
    assert conflict.status_code == 409
    assert conflict.headers["retry-after"] == "1"
    assert conflict.json()["error"]["code"] == "idempotency_key_in_use"
    assert done.json() == replay.json() == {"done": True}
    assert replay.headers["idempotent-replayed"] == "true"


def test_idempotent_without_a_cache_fails_startup() -> None:
    @controller("/x")
    class NoCache:
        @post("/")
        @idempotent()
        async def handler(self) -> dict:
            return {}  # pragma: no cover

    @module(controllers=[NoCache])
    class NoCacheModule:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(NoCacheModule)
    assert info.value.detail["reason"] == "cache_module_missing"
    assert "@idempotent" in str(info.value)


class _RacingStore:
    """Delegates to the real store; the lease succeeds just after the other request finished."""

    def __init__(self, inner: Any, finish: Any) -> None:
        self._inner = inner
        self._finish = finish

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def set_if_absent(self, key: str, value: bytes, *, ttl: float | None = None) -> bool:
        if key.startswith("lock:"):
            await self._finish(key.removeprefix("lock:"))
        return await self._inner.set_if_absent(key, value, ttl=ttl)


def test_a_request_finishing_between_the_check_and_the_lease_is_replayed(client: TestClient) -> None:
    from lauren._cache._idempotency import IdempotencyInterceptor

    client.post("/pay/", headers={"Idempotency-Key": "other"})
    interceptor = client.resolve(IdempotencyInterceptor)
    real = interceptor._store

    async def finish(digest: str) -> None:
        # Simulate the concurrent first request storing its response just now.
        record = await real.get(f"rec:{other_digest}")
        await real.set(f"rec:{digest}", record)

    import hashlib

    other_digest = hashlib.sha256("\0other".encode()).hexdigest()
    interceptor._store = _RacingStore(real, finish)
    raced = client.post("/pay/", headers={"Idempotency-Key": "raced"})
    assert raced.headers["idempotent-replayed"] == "true"
    assert runs(client) == 1
