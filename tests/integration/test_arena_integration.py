"""End-to-end tests for the per-request arena allocator.

These tests drive a real :class:`LaurenApp` produced by
:meth:`LaurenFactory.create` and verify that the arena is wired into
the request lifecycle:

* Every HTTP request causes exactly one bundle lease (``misses + hits
  == request_count``).
* After traffic subsides the pool contains pooled bundles ready for
  reuse.
* ``Request`` instances are reused across requests via
  :meth:`Request.reset`, with no state leaking between calls.
* Concurrent requests each receive their own bundle — no cross-talk
  in ``request_cache`` / ``kwargs``.
* A user-supplied :class:`RequestArena` overrides the default, and
  disabling pooling (``arena_capacity=0``) produces correct results
  with zero reuse.
* Request-scoped DI instances and their pre_destruct hooks still run
  correctly under the arena — a subtle correctness concern because
  the arena clears the cache on lease release.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import (
    Depends,
    LaurenFactory,
    Path,
    Query,
    Scope,
    controller,
    get,
    injectable,
    module,
    post,
    pre_destruct,
)
from lauren._arena import RequestArena
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Baseline wiring — the arena is a first-class member of LaurenApp
# ---------------------------------------------------------------------------


@controller("/hello")
class _HelloController:
    @get("/{name}")
    async def greet(self, name: Path[str]) -> dict:
        return {"hello": name}


@module(controllers=[_HelloController])
class _HelloModule:
    pass


def test_app_exposes_an_arena_by_default() -> None:
    app = asyncio.run(LaurenFactory.create(_HelloModule))
    assert isinstance(app.arena, RequestArena)
    # Default capacity is 256 per the arena's own docs.
    assert app.arena.capacity == 256


def test_single_request_produces_a_single_bundle_miss() -> None:
    app = asyncio.run(LaurenFactory.create(_HelloModule))
    TestClient(app).get("/hello/world")
    stats = app.arena.stats
    assert stats.misses == 1
    assert stats.hits == 0
    assert stats.in_flight == 0
    # Bundle returned to the pool after the request.
    assert app.arena.pooled() == 1


def test_repeated_requests_reuse_pooled_bundles() -> None:
    app = asyncio.run(LaurenFactory.create(_HelloModule))
    client = TestClient(app)
    for _ in range(10):
        r = client.get("/hello/world")
        assert r.status_code == 200
    stats = app.arena.stats
    # One miss (first request) then nine hits.
    assert stats.misses == 1
    assert stats.hits == 9
    assert stats.in_flight == 0


def test_request_instances_are_reused_across_requests() -> None:
    app = asyncio.run(LaurenFactory.create(_HelloModule))
    client = TestClient(app)
    for _ in range(5):
        client.get("/hello/world")
    # After five requests, exactly one Request instance was allocated
    # and reused four times.
    assert app.arena.stats.request_misses == 1
    assert app.arena.stats.request_hits == 4


# ---------------------------------------------------------------------------
# State isolation — pooled dicts must never leak across requests
# ---------------------------------------------------------------------------


@injectable(scope=Scope.REQUEST)
class _Counter:
    """Request-scoped counter — a new instance is constructed per
    request so its value should always reflect *this* request only."""

    def __init__(self) -> None:
        self.n = 0

    def tick(self) -> int:
        self.n += 1
        return self.n


@controller("/ticks")
class _TickController:
    @get("/once")
    async def once(self, c: Depends[_Counter]) -> dict:
        return {"tick": c.tick()}


@module(controllers=[_TickController], providers=[_Counter])
class _TickModule:
    pass


def test_request_scoped_di_is_freshly_built_per_request() -> None:
    """If the arena's ``request_cache`` leaked across requests, the
    second call would see the counter already at ``n=1`` and return
    ``tick=2``. The arena contract demands that each lease yields an
    empty cache, so every request must observe ``tick=1``.
    """
    app = asyncio.run(LaurenFactory.create(_TickModule))
    client = TestClient(app)
    r1 = client.get("/ticks/once")
    r2 = client.get("/ticks/once")
    r3 = client.get("/ticks/once")
    assert r1.json() == {"tick": 1}
    assert r2.json() == {"tick": 1}
    assert r3.json() == {"tick": 1}


def test_path_params_do_not_leak_between_pooled_requests() -> None:
    @controller("/echo")
    class EchoController:
        @get("/{tag}")
        async def echo(self, tag: Path[str]) -> dict:
            return {"tag": tag}

    @module(controllers=[EchoController])
    class EchoModule:
        pass

    app = asyncio.run(LaurenFactory.create(EchoModule))
    client = TestClient(app)
    assert client.get("/echo/alpha").json() == {"tag": "alpha"}
    assert client.get("/echo/beta").json() == {"tag": "beta"}
    assert client.get("/echo/gamma").json() == {"tag": "gamma"}


def test_query_params_do_not_leak_between_pooled_requests() -> None:
    @controller("/search")
    class SearchController:
        @get("/")
        async def search(self, q: Query[str]) -> dict:
            return {"q": q}

    @module(controllers=[SearchController])
    class SearchModule:
        pass

    app = asyncio.run(LaurenFactory.create(SearchModule))
    client = TestClient(app)
    assert client.get("/search/?q=first").json() == {"q": "first"}
    assert client.get("/search/?q=second").json() == {"q": "second"}


# ---------------------------------------------------------------------------
# Pre-destruct hook still fires under the arena
# ---------------------------------------------------------------------------


_DESTRUCT_LOG: list[str] = []


@injectable(scope=Scope.REQUEST)
class _TrackedResource:
    def __init__(self) -> None:
        self.opened = True

    @pre_destruct
    async def close(self) -> None:
        _DESTRUCT_LOG.append("closed")


@controller("/res")
class _ResController:
    @get("/")
    async def hit(self, r: Depends[_TrackedResource]) -> dict:
        return {"opened": r.opened}


@module(controllers=[_ResController], providers=[_TrackedResource])
class _ResModule:
    pass


def test_pre_destruct_runs_even_when_arena_clears_cache() -> None:
    """The dispatcher snapshots the request cache *before* the arena's
    ``finally`` clears it; otherwise pre_destruct hooks would silently
    stop running. This test pins that ordering.
    """
    _DESTRUCT_LOG.clear()
    app = asyncio.run(LaurenFactory.create(_ResModule))
    client = TestClient(app)
    client.get("/res/")
    client.get("/res/")
    client.get("/res/")
    assert _DESTRUCT_LOG == ["closed", "closed", "closed"]


# ---------------------------------------------------------------------------
# User-supplied arena
# ---------------------------------------------------------------------------


def test_user_supplied_arena_is_honoured() -> None:
    custom_arena = RequestArena(capacity=7)
    app = asyncio.run(LaurenFactory.create(_HelloModule, arena=custom_arena))
    assert app.arena is custom_arena
    assert app.arena.capacity == 7


def test_user_supplied_capacity_shortcut_is_honoured() -> None:
    app = asyncio.run(LaurenFactory.create(_HelloModule, arena_capacity=13))
    assert app.arena.capacity == 13


def test_passing_both_arena_and_capacity_raises_startup_error() -> None:
    from lauren.exceptions import StartupError

    custom_arena = RequestArena()
    with pytest.raises(StartupError, match="either `arena` or `arena_capacity`"):
        asyncio.run(
            LaurenFactory.create(_HelloModule, arena=custom_arena, arena_capacity=5)
        )


def test_pooling_disabled_when_capacity_is_zero() -> None:
    """With zero capacity every request allocates fresh bundles —
    useful for benchmarks and A/B measurements. The request semantics
    must be identical to pooled mode.
    """
    app = asyncio.run(LaurenFactory.create(_HelloModule, arena_capacity=0))
    client = TestClient(app)
    for _ in range(5):
        assert client.get("/hello/world").status_code == 200
    # No hits — pool was always empty.
    assert app.arena.stats.hits == 0
    # Five misses, five drops (release with full/zero-cap pool drops).
    assert app.arena.stats.misses == 5
    assert app.arena.stats.drops == 5


# ---------------------------------------------------------------------------
# Concurrency — simultaneous requests each get their own lease
# ---------------------------------------------------------------------------


def test_concurrent_requests_do_not_share_bundles() -> None:
    """Run several in-flight requests against the ASGI entry point and
    verify each one observed its own isolated request-scoped DI cache.

    Each handler ``await``\\s ``asyncio.sleep(0)`` to deliberately hand
    control back to the event loop, which lets the other tasks
    acquire their own lease from the arena before the first one
    releases. If ``request_cache`` were shared across leases, the
    ``_TagHolder`` instance would be shared between requests and the
    assertion inside the handler would trip.
    """
    request_tags: list[str] = []

    @injectable(scope=Scope.REQUEST)
    class _TagHolder:
        def __init__(self) -> None:
            self.tag: str | None = None

    @controller("/conc")
    class ConcController:
        @post("/{tag}")
        async def hit(
            self,
            tag: Path[str],
            holder: Depends[_TagHolder],
        ) -> dict:
            holder.tag = tag
            # Yield to the loop so sibling tasks get to hold the lease.
            await asyncio.sleep(0)
            # If another request's holder had overwritten this one's
            # tag we'd see a mismatch here — proof that each lease
            # produced an isolated request_cache.
            assert holder.tag == tag
            request_tags.append(tag)
            return {"tag": tag}

    @module(controllers=[ConcController], providers=[_TagHolder])
    class ConcModule:
        pass

    async def run_one(app: Any, tag: str) -> None:
        scope = {
            "type": "http",
            "method": "POST",
            "path": f"/conc/{tag}",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 80),
        }

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(msg: dict) -> None:
            pass

        await app(scope, receive, send)

    async def main() -> Any:
        # ``LaurenFactory.create`` calls ``startup()`` itself, so we
        # must not call it again. We drive the ASGI entry point
        # directly rather than through TestClient (which serialises
        # requests) so we can launch all five concurrently.
        app = await LaurenFactory.create(ConcModule)
        await asyncio.gather(*(run_one(app, t) for t in "ABCDE"))
        return app

    app = asyncio.run(main())
    # Five distinct tags each processed once, no interference.
    assert sorted(request_tags) == ["A", "B", "C", "D", "E"]
    # Arena observed five bundle leases — hits + misses == 5.
    stats = app.arena.stats
    assert stats.hits + stats.misses == 5
