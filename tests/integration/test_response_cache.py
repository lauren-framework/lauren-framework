"""``@cache_response`` end to end: hits, safety rules, guards, keys, tags, startup checks."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from lauren import (
    CallHandler,
    ExecutionContext,
    LaurenFactory,
    NotFoundError,
    Request,
    Response,
    Session,
    SessionConfig,
    StartupError,
    controller,
    get,
    head,
    injectable,
    interceptor,
    module,
    post,
    use_guards,
    use_interceptors,
)
from lauren.cache import Cache, CacheModule, cache_response
from lauren.stores import MemoryStore
from lauren.testing import AsyncTestClient, TestClient


class ManualClock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


@injectable()
class Calls:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}

    def bump(self, name: str) -> int:
        self.counts[name] = self.counts.get(name, 0) + 1
        return self.counts[name]


@injectable()
class AdminOnly:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return ctx.request.headers.get("x-role") == "admin"


@interceptor()
class Stamp:
    async def intercept(self, ctx: ExecutionContext, call_handler: CallHandler) -> Response:
        response = await call_handler.handle()
        return response.with_header("x-stamped", "yes")


class _Chunks:
    def __init__(self) -> None:
        self._sent = False

    def __aiter__(self) -> _Chunks:
        return self

    async def __anext__(self) -> bytes:
        if self._sent:
            raise StopAsyncIteration
        self._sent = True
        return b"chunk"


@controller("/c")
class CatalogController:
    def __init__(self, calls: Calls, cache: Cache) -> None:
        self.calls = calls
        self.cache = cache

    @get("/plain")
    @cache_response(ttl=30, debug_header=True)
    async def plain(self) -> dict:
        return {"n": self.calls.bump("plain")}

    @get("/cookie")
    @cache_response(ttl=30)
    async def cookie(self) -> Response:
        return Response.json({"n": self.calls.bump("cookie")}).with_cookie("pref", "dark")

    @get("/private")
    @cache_response(ttl=30)
    async def private(self) -> Response:
        return Response.json({"n": self.calls.bump("private")}).with_header("cache-control", "private")

    @get("/stream")
    @cache_response(ttl=30)
    async def stream(self) -> Response:
        self.calls.bump("stream")
        return Response.stream(_Chunks(), media_type="text/plain")

    @post("/write")
    @cache_response(ttl=30)
    async def write(self) -> dict:
        return {"n": self.calls.bump("write")}

    @get("/lang")
    @cache_response(ttl=30, vary=("accept-language",))
    async def lang(self) -> dict:
        return {"n": self.calls.bump("lang")}

    @get("/admin")
    @use_guards(AdminOnly)
    @cache_response(ttl=30)
    async def admin(self) -> dict:
        return {"n": self.calls.bump("admin")}

    @get("/me")
    @cache_response(ttl=30, key=lambda ctx: ctx.request.headers.get("x-user"))
    async def me(self) -> dict:
        return {"n": self.calls.bump("me")}

    @get("/products/{pid}")
    @cache_response(ttl=30, tags=lambda ctx: (f"product:{ctx.request.path_params['pid']}", "catalog"))
    async def product(self, pid: str) -> dict:
        return {"pid": pid, "n": self.calls.bump(f"product:{pid}")}

    @post("/products/{pid}/touch")
    async def touch(self, pid: str) -> dict:
        await self.cache.invalidate_tags(f"product:{pid}")
        return {"ok": True}

    @get("/search")
    @cache_response(ttl=30)
    async def search(self) -> dict:
        return {"n": self.calls.bump("search")}

    @get("/both")
    @head("/both")
    @cache_response(ttl=30)
    async def both(self) -> dict:
        return {"n": self.calls.bump("both")}

    @get("/fresh")
    @cache_response(ttl=30, respect_client_no_cache=True)
    async def fresh(self) -> dict:
        return {"n": self.calls.bump("fresh")}

    @get("/stamped")
    @use_interceptors(Stamp)
    @cache_response(ttl=30)
    async def stamped(self) -> dict:
        return {"n": self.calls.bump("stamped")}

    @get("/missing")
    @cache_response(ttl=30)
    async def missing(self) -> Response:
        return Response.json({"n": self.calls.bump("missing")}, status=404)

    @get("/raises")
    @cache_response(ttl=30)
    async def raises(self) -> dict:
        self.calls.bump("raises")
        raise NotFoundError("gone")

    @get("/short")
    @cache_response(ttl=5)
    async def short(self) -> dict:
        return {"n": self.calls.bump("short")}

    @get("/whoami")
    @cache_response(ttl=30)
    async def whoami(self, request: Request) -> dict:
        self.calls.bump("whoami")
        return {"user": request.headers.get("authorization", "anonymous")}

    @get("/shared-catalog")
    @cache_response(ttl=30, allow_authenticated=True)
    async def shared_catalog(self) -> dict:
        return {"n": self.calls.bump("shared-catalog")}

    @get("/per-token")
    @cache_response(ttl=30, vary=("authorization",))
    async def per_token(self, request: Request) -> dict:
        self.calls.bump("per-token")
        return {"user": request.headers.get("authorization")}

    @get("/paged")
    @cache_response(ttl=30, query=("page",))
    async def paged(self, page: int = 1) -> dict:
        return {"page": page, "n": self.calls.bump("paged")}


def build(clock: ManualClock | None = None) -> Any:
    store = MemoryStore(max_entries=1_000, clock=clock) if clock is not None else None

    @module(
        imports=[CacheModule.for_root(store=store, clock=clock)],
        providers=[Calls, AdminOnly, Stamp],
        controllers=[CatalogController],
    )
    class AppModule:
        pass

    return LaurenFactory.create(AppModule)


@pytest.fixture
def client() -> Any:
    with TestClient(build()) as c:
        yield c


def calls(client: TestClient, name: str) -> int:
    return client.resolve(Calls).counts.get(name, 0)


def test_second_identical_get_is_served_from_the_cache(client: TestClient) -> None:
    first, second = client.get("/c/plain"), client.get("/c/plain")
    assert first.json() == second.json() == {"n": 1}
    assert (first.headers["x-cache"], second.headers["x-cache"]) == ("MISS", "HIT")
    assert "age" not in first.headers and second.headers["age"] == "0"
    assert calls(client, "plain") == 1


def test_age_reflects_the_time_since_the_entry_was_stored() -> None:
    clock = ManualClock()
    with TestClient(build(clock)) as c:
        c.get("/c/plain")
        clock.now += 12
        assert c.get("/c/plain").headers["age"] == "12"


def test_entries_expire_after_ttl() -> None:
    clock = ManualClock()
    with TestClient(build(clock)) as c:
        c.get("/c/short")
        clock.now += 6
        assert c.get("/c/short").json() == {"n": 2}


@pytest.mark.parametrize("path", ["/c/cookie", "/c/private", "/c/stream"])
def test_unsafe_responses_are_never_cached(client: TestClient, path: str) -> None:
    client.get(path)
    second = client.get(path)
    assert calls(client, path.rsplit("/", 1)[1]) == 2
    assert "age" not in second.headers


def test_a_cookie_is_never_replayed_to_another_client(client: TestClient) -> None:
    assert "pref=dark" in client.get("/c/cookie").headers["set-cookie"]


def test_post_is_never_cached(client: TestClient) -> None:
    assert client.post("/c/write").json() == {"n": 1}
    assert client.post("/c/write").json() == {"n": 2}


def test_vary_headers_key_separate_entries(client: TestClient) -> None:
    en = client.get("/c/lang", headers={"accept-language": "en"}).json()
    fr = client.get("/c/lang", headers={"accept-language": "fr"}).json()
    en_again = client.get("/c/lang", headers={"accept-language": "en"}).json()
    assert (en, fr, en_again) == ({"n": 1}, {"n": 2}, {"n": 1})


def test_guards_run_before_the_cache_lookup(client: TestClient) -> None:
    assert client.get("/c/admin", headers={"x-role": "admin"}).status_code == 200
    assert client.get("/c/admin").status_code == 403
    assert client.get("/c/admin", headers={"x-role": "admin"}).json() == {"n": 1}


def test_per_user_keys_keep_users_apart(client: TestClient) -> None:
    alice = client.get("/c/me", headers={"x-user": "alice"}).json()
    bob = client.get("/c/me", headers={"x-user": "bob"}).json()
    assert (alice, bob) == ({"n": 1}, {"n": 2})
    assert client.get("/c/me", headers={"x-user": "alice"}).json() == {"n": 1}


def test_a_key_function_returning_none_bypasses_the_cache(client: TestClient) -> None:
    client.get("/c/me")
    client.get("/c/me")
    assert calls(client, "me") == 2


def test_tag_invalidation_evicts_only_the_tagged_entries(client: TestClient) -> None:
    client.get("/c/products/1")
    client.get("/c/products/2")
    client.post("/c/products/1/touch")
    assert client.get("/c/products/1").json() == {"pid": "1", "n": 2}
    assert client.get("/c/products/2").json() == {"pid": "2", "n": 1}


def test_query_parameter_order_does_not_split_entries(client: TestClient) -> None:
    client.get("/c/search?a=1&b=2")
    assert client.get("/c/search?b=2&a=1").json() == {"n": 1}
    assert client.get("/c/search?a=2&b=2").json() == {"n": 2}


def test_head_shares_the_get_entry(client: TestClient) -> None:
    client.get("/c/both")
    assert client.head("/c/both").status_code == 200
    assert calls(client, "both") == 1


def test_a_head_request_never_populates_the_get_entry(client: TestClient) -> None:
    client.head("/c/both")
    client.get("/c/both")
    assert calls(client, "both") == 2
    client.get("/c/both")
    assert calls(client, "both") == 2


def test_client_no_cache_forces_a_refresh_when_allowed(client: TestClient) -> None:
    client.get("/c/fresh")
    assert client.get("/c/fresh", headers={"cache-control": "no-cache"}).json() == {"n": 2}
    assert client.get("/c/fresh").json() == {"n": 2}
    client.get("/c/plain")
    assert client.get("/c/plain", headers={"cache-control": "no-cache"}).json() == {"n": 1}


def test_user_interceptors_also_see_cache_hits(client: TestClient) -> None:
    client.get("/c/stamped")
    hit = client.get("/c/stamped")
    assert hit.headers["x-stamped"] == "yes" and "age" in hit.headers


def test_returned_404_is_cached_but_a_raised_error_is_not(client: TestClient) -> None:
    assert client.get("/c/missing").status_code == 404
    assert client.get("/c/missing").status_code == 404
    assert calls(client, "missing") == 1
    client.get("/c/raises")
    client.get("/c/raises")
    assert calls(client, "raises") == 2


def test_credentialed_requests_bypass_a_shared_entry(client: TestClient) -> None:
    alice = client.get("/c/whoami", headers={"authorization": "Bearer alice"}).json()
    bob = client.get("/c/whoami", headers={"authorization": "Bearer bob"}).json()
    assert (alice, bob) == ({"user": "Bearer alice"}, {"user": "Bearer bob"})
    client.get("/c/whoami")
    anonymous = client.get("/c/whoami")
    assert anonymous.json() == {"user": "anonymous"} and "age" in anonymous.headers
    assert client.get("/c/whoami", headers={"authorization": "Bearer carol"}).json() == {
        "user": "Bearer carol"
    }
    assert calls(client, "whoami") == 4


def test_allow_authenticated_shares_one_entry_between_callers(client: TestClient) -> None:
    client.get("/c/shared-catalog", headers={"authorization": "Bearer alice"})
    assert client.get("/c/shared-catalog", headers={"authorization": "Bearer bob"}).json() == {"n": 1}


def test_vary_on_authorization_keeps_one_entry_per_token(client: TestClient) -> None:
    client.get("/c/per-token", headers={"authorization": "Bearer alice"})
    client.get("/c/per-token", headers={"authorization": "Bearer alice"})
    assert client.get("/c/per-token", headers={"authorization": "Bearer bob"}).json() == {
        "user": "Bearer bob"
    }
    assert calls(client, "per-token") == 2


def test_a_query_allow_list_stops_cache_busting_parameters(client: TestClient) -> None:
    client.get("/c/paged?page=2&_=1")
    assert client.get("/c/paged?page=2&_=2").json() == {"page": 2, "n": 1}
    assert client.get("/c/paged?page=3").json() == {"page": 3, "n": 2}


def test_openapi_marks_cached_operations() -> None:
    app = build()
    paths = app.openapi()["paths"]
    assert paths["/c/plain"]["get"]["x-cache-ttl"] == 30.0
    assert "x-cache-ttl" not in paths["/c/products/{pid}/touch"]["post"]


async def test_concurrent_misses_run_the_handler_once() -> None:
    gate = asyncio.Event()
    count = 0

    @controller("/slow")
    class Slow:
        @get("/")
        @cache_response(ttl=30)
        async def slow(self) -> dict:
            nonlocal count
            count += 1
            await gate.wait()
            return {"n": count}

    @module(imports=[CacheModule.for_root()], controllers=[Slow])
    class SlowModule:
        pass

    async with AsyncTestClient(LaurenFactory.create(SlowModule)) as client:
        requests = [asyncio.ensure_future(client.get("/slow/")) for _ in range(5)]
        while count == 0:
            await asyncio.sleep(0.001)
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        responses = await asyncio.gather(*requests)
    assert [r.json() for r in responses] == [{"n": 1}] * 5
    assert count == 1


async def test_concurrent_requests_do_not_share_an_unstorable_response() -> None:
    gate = asyncio.Event()
    count = 0

    @controller("/per-request")
    class PerRequest:
        @get("/")
        @cache_response(ttl=30)
        async def handler(self) -> Response:
            nonlocal count
            count += 1
            mine = count
            await gate.wait()
            return Response.json({"n": mine}).with_cookie("id", str(mine))

    @module(imports=[CacheModule.for_root()], controllers=[PerRequest])
    class PerRequestModule:
        pass

    async with AsyncTestClient(LaurenFactory.create(PerRequestModule)) as client:
        requests = [asyncio.ensure_future(client.get("/per-request/")) for _ in range(3)]
        while count == 0:
            await asyncio.sleep(0.001)
        for _ in range(20):
            await asyncio.sleep(0)
        gate.set()
        responses = await asyncio.gather(*requests)
    assert sorted(r.json()["n"] for r in responses) == [1, 2, 3]


# ---------------------------------------------------------------------------
# Startup validation and warnings
# ---------------------------------------------------------------------------


def test_cache_response_without_a_cache_fails_startup() -> None:
    @controller("/x")
    class NoCache:
        @get("/")
        @cache_response(ttl=5)
        async def handler(self) -> dict:
            return {}  # pragma: no cover

    @module(controllers=[NoCache])
    class NoCacheModule:
        pass

    with pytest.raises(StartupError) as info:
        LaurenFactory.create(NoCacheModule)
    assert info.value.detail == {
        "reason": "cache_module_missing",
        "handler": "NoCache.handler",
        "method": "GET",
        "path": "/x",
    }


def test_a_user_registered_cache_satisfies_the_startup_check() -> None:
    from lauren import use_value

    @controller("/x")
    class Ctl:
        @get("/")
        @cache_response(ttl=5)
        async def handler(self) -> dict:
            return {"ok": True}

    cache = Cache(MemoryStore(max_entries=10))

    @module(controllers=[Ctl], providers=[use_value(provide=Cache, value=cache)])
    class Own:
        pass

    with TestClient(LaurenFactory.create(Own)) as client:
        client.get("/x/")
        assert client.get("/x/").headers.get("age") == "0"


def test_session_routes_without_a_key_log_a_warning(caplog: pytest.LogCaptureFixture) -> None:
    @controller("/acct")
    class Account:
        @get("/")
        @cache_response(ttl=5)
        async def handler(self, session: Session) -> dict:
            return {}  # pragma: no cover

        @get("/keyed")
        @cache_response(ttl=5, key=lambda ctx: ctx.request.state.get("user"))
        async def keyed(self, session: Session) -> dict:
            return {}  # pragma: no cover

    @module(imports=[CacheModule.for_root()], controllers=[Account])
    class AccountModule:
        pass

    with caplog.at_level(logging.WARNING, logger="lauren"):
        LaurenFactory.create(AccountModule, sessions=SessionConfig(secret="s" * 32))
    messages = [
        r.getMessage() for r in caplog.records if "read the session but have no key=" in r.getMessage()
    ]
    assert len(messages) == 1 and "GET /acct" in messages[0] and "/acct/keyed" not in messages[0]
