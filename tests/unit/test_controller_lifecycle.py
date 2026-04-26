"""Tests for @post_construct / @pre_destruct on controllers.

Controllers are REQUEST-scoped injectables. Their lifecycle hooks must fire
per-request: ``@post_construct`` immediately after the DI container builds
the controller, and ``@pre_destruct`` at the end of the request (before the
request-scoped cache is discarded).
"""

from __future__ import annotations

import pytest

from lauren import (
    LaurenFactory,
    Request,
    controller,
    get,
    module,
    post_construct,
    pre_destruct,
)
from lauren.types import Headers


def _make_request(path: str = "/") -> Request:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    return Request(
        method="GET",
        path=path,
        raw_query_string=b"",
        headers=Headers([]),
        receive=receive,
    )


class TestControllerLifecycleHooks:
    @pytest.mark.asyncio
    async def test_post_construct_fires_every_request(self):
        events: list[str] = []

        @controller("/c")
        class Ctrl:
            @post_construct
            def _init(self):
                events.append("post")

            @get("/ping")
            async def ping(self) -> dict:
                events.append("handle")
                return {"ok": True}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = await LaurenFactory.create(M)
        await app.handle(_make_request("/c/ping"))
        await app.handle(_make_request("/c/ping"))
        # Two requests \u2192 two constructions \u2192 two post hooks.
        assert events == ["post", "handle", "post", "handle"]

    @pytest.mark.asyncio
    async def test_pre_destruct_fires_at_request_end(self):
        events: list[str] = []

        @controller("/c")
        class Ctrl:
            @post_construct
            def _init(self):
                events.append("post")

            @pre_destruct
            def _done(self):
                events.append("pre")

            @get("/x")
            async def x(self) -> dict:
                events.append("handle")
                return {"ok": True}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = await LaurenFactory.create(M)
        await app.handle(_make_request("/c/x"))
        # Order: post \u2192 handle \u2192 pre (finalization in the request's finally)
        assert events == ["post", "handle", "pre"]

    @pytest.mark.asyncio
    async def test_async_hooks_are_awaited(self):
        events: list[str] = []

        @controller("/c")
        class Ctrl:
            @post_construct
            async def _init(self):
                events.append("post-begin")
                events.append("post-end")

            @pre_destruct
            async def _done(self):
                events.append("pre-begin")
                events.append("pre-end")

            @get("/")
            async def root(self) -> dict:
                return {}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = await LaurenFactory.create(M)
        await app.handle(_make_request("/c/"))
        assert events == ["post-begin", "post-end", "pre-begin", "pre-end"]

    @pytest.mark.asyncio
    async def test_pre_destruct_exception_does_not_break_response(self):
        @controller("/c")
        class Ctrl:
            @pre_destruct
            def _bad(self):
                raise RuntimeError("cleanup failed")

            @get("/")
            async def root(self) -> dict:
                return {"ok": True}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = await LaurenFactory.create(M)
        # The broken pre_destruct must not propagate to the client.
        resp = await app.handle(_make_request("/c/"))
        assert resp.status == 200
