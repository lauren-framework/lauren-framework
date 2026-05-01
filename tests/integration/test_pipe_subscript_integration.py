"""Integration tests for the subscript pipe syntax.

End-to-end tests that drive real ``LaurenApp`` instances via ``httpx.AsyncClient``
to confirm ``Path[T, pipe1, pipe2]`` and ``Query[T, pipe1, pipe2, pipe3]`` work
correctly in production-like conditions.

NOTE: intentionally *not* using ``from __future__ import annotations`` so that
subscript expressions are evaluated immediately (not stringified by PEP 563).
"""

import pytest
import pytest_asyncio
import httpx
from httpx import ASGITransport

from lauren import (
    LaurenFactory,
    Path,
    Query,
    controller,
    get,
    module,
    pipe,
)
from lauren.extractors import PipeContext
from lauren.exceptions import ExtractorFieldError


# ---------------------------------------------------------------------------
# Shared pipe library (module-level, as real projects would have)
# ---------------------------------------------------------------------------


@pipe()
def ensure_int(v: int) -> int:
    """No-op validator — value already coerced to int by the extractor."""
    if not isinstance(v, int):
        raise ExtractorFieldError("expected int")
    return v


@pipe()
def ensure_gt_zero(v: int, ctx: PipeContext) -> int:
    if v <= 0:
        raise ExtractorFieldError(f"{ctx.name} must be > 0")
    return v


@pipe()
def ensure_less_than_fifty(v: int, ctx: PipeContext) -> int:
    if v >= 50:
        raise ExtractorFieldError(f"{ctx.name} must be < 50")
    return v


# ---------------------------------------------------------------------------
# App under test
# ---------------------------------------------------------------------------


@controller("/users")
class UserController:
    @get("/{user_id}")
    async def get_user(
        self,
        user_id: Path[int, ensure_int, ensure_gt_zero],
        q: Query[int, ensure_int, ensure_gt_zero, ensure_less_than_fifty],
    ) -> dict:
        return {"user_id": user_id, "q": q}


@module(controllers=[UserController])
class AppModule:
    pass


APP = LaurenFactory.create(AppModule)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client():
    async with httpx.AsyncClient(
        transport=ASGITransport(app=APP), base_url="http://test"
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


class TestSubscriptPipesHappyPath:
    @pytest.mark.asyncio
    async def test_valid_user_and_query(self, client):
        r = await client.get("/users/3?q=10")
        assert r.status_code == 200
        assert r.json() == {"user_id": 3, "q": 10}

    @pytest.mark.asyncio
    async def test_user_id_boundary_min(self, client):
        r = await client.get("/users/1?q=1")
        assert r.status_code == 200
        assert r.json() == {"user_id": 1, "q": 1}

    @pytest.mark.asyncio
    async def test_q_boundary_max(self, client):
        r = await client.get("/users/5?q=49")
        assert r.status_code == 200
        assert r.json() == {"user_id": 5, "q": 49}


# ---------------------------------------------------------------------------
# Error-path tests
# ---------------------------------------------------------------------------


class TestSubscriptPipesErrorPath:
    @pytest.mark.asyncio
    async def test_user_id_zero_rejected(self, client):
        r = await client.get("/users/0?q=10")
        assert r.status_code >= 400

    @pytest.mark.asyncio
    async def test_user_id_negative_rejected(self, client):
        r = await client.get("/users/-5?q=10")
        assert r.status_code >= 400

    @pytest.mark.asyncio
    async def test_q_zero_rejected(self, client):
        r = await client.get("/users/3?q=0")
        assert r.status_code >= 400

    @pytest.mark.asyncio
    async def test_q_fifty_rejected(self, client):
        r = await client.get("/users/3?q=50")
        assert r.status_code >= 400

    @pytest.mark.asyncio
    async def test_q_over_fifty_rejected(self, client):
        r = await client.get("/users/3?q=100")
        assert r.status_code >= 400


# ---------------------------------------------------------------------------
# Mix of subscript and Annotated / default-form syntax in same app
# ---------------------------------------------------------------------------


class TestSubscriptCombinedWithOtherForms:
    """Verify that subscript, Annotated, and | forms coexist correctly."""

    @pytest.mark.asyncio
    async def test_all_three_syntaxes_in_one_app(self):
        from typing import Annotated

        @pipe()
        def tag_subscript(v: int) -> dict:
            return {"via": "subscript", "v": v}

        @pipe()
        def tag_annotated(v: int) -> dict:
            return {"via": "annotated", "v": v}

        @pipe()
        def tag_default(v: int) -> dict:
            return {"via": "default", "v": v}

        @controller("/mixed")
        class Mixed:
            @get("/subscript/{n}")
            async def a(self, n: Path[int, tag_subscript]) -> dict:
                return n

            @get("/annotated/{n}")
            async def b(self, n: Annotated[Path[int], pipe(tag_annotated)]) -> dict:
                return n

            @get("/default/{n}")
            async def c(self, n: Path[int] = pipe(tag_default)) -> dict:
                return n

        @module(controllers=[Mixed])
        class M:
            pass

        app = LaurenFactory.create(M)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            ra = await c.get("/mixed/subscript/7")
            assert ra.json() == {"via": "subscript", "v": 7}

            rb = await c.get("/mixed/annotated/7")
            assert rb.json() == {"via": "annotated", "v": 7}

            rc = await c.get("/mixed/default/7")
            assert rc.json() == {"via": "default", "v": 7}

    @pytest.mark.asyncio
    async def test_subscript_and_default_pipes_order(self):
        """Annotation-side (subscript) pipes run before default-side pipes."""
        events: list[str] = []

        @pipe()
        def ann_pipe(v: int) -> int:
            events.append("ann")
            return v

        @pipe()
        def def_pipe(v: int) -> int:
            events.append("def")
            return v

        @controller("/order")
        class Order:
            @get("/{n}")
            async def h(self, n: Path[int, ann_pipe] = pipe(def_pipe)) -> dict:
                return {"n": n}

        @module(controllers=[Order])
        class M:
            pass

        app = LaurenFactory.create(M)
        async with httpx.AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as c:
            await c.get("/order/1")
        assert events == ["ann", "def"]
