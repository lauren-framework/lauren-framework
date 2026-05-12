"""Unit tests for the subscript pipe syntax.

Covers ``Path[int, pipe1, pipe2]`` / ``Query[str, pipe1]`` and all edge
cases of the ``ExtractionMarker.__class_getitem__`` expansion.

NOTE: intentionally *not* using ``from __future__ import annotations`` so that
subscript expressions are evaluated immediately (not stringified by PEP 563).
"""

from typing import Annotated, get_args, get_origin

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Path,
    Query,
    Json,
    Header,
    PathField,
    controller,
    get,
    module,
    pipe,
    post,
)
from lauren.extractors import (
    PipeContext,
    is_pipe,
    parse_extractor_hint,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# parse_extractor_hint integration: subscript expansion produces correct plan
# ---------------------------------------------------------------------------


class TestSubscriptExpansion:
    """Verify that Path[T, p1, p2] expands to the right Annotated form
    and that parse_extractor_hint collects the pipes correctly."""

    def test_single_pipe_in_subscript(self):
        @pipe()
        def double(v: int) -> int:
            return v * 2

        annotation = Path[int, double]
        # Should be Annotated[int, Path, double]
        assert get_origin(annotation) is Annotated
        args = get_args(annotation)
        assert args[0] is int
        assert args[1] is Path

        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "path"
        assert inner is int
        assert len(pipes) == 1
        assert is_pipe(pipes[0])

    def test_multiple_pipes_in_subscript(self):
        @pipe()
        def add_one(v: int) -> int:
            return v + 1

        @pipe()
        def double(v: int) -> int:
            return v * 2

        @pipe()
        def to_str(v: int) -> str:
            return str(v)

        annotation = Path[int, add_one, double, to_str]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "path"
        assert inner is int
        assert len(pipes) == 3

    def test_query_subscript(self):
        @pipe()
        def clamp(v: int) -> int:
            return min(max(v, 0), 100)

        annotation = Query[int, clamp]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "query"
        assert len(pipes) == 1

    def test_plain_callable_auto_wrapped(self):
        """A plain function (no @pipe) passed in subscript is auto-wrapped."""

        def plain(v: int) -> int:
            return v + 10

        annotation = Path[int, plain]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "path"
        assert len(pipes) == 1
        assert is_pipe(pipes[0])

    def test_field_descriptor_in_subscript(self):
        @pipe()
        def double(v: int) -> int:
            return v * 2

        annotation = Path[int, PathField(ge=1), double]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "path"
        assert fd is not None
        assert fd.ge == 1
        assert len(pipes) == 1

    def test_trailing_comma_single_item(self):
        """Path[int,] (trailing comma) is treated like Path[int]."""
        annotation = Path[int,]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "path"
        assert inner is int
        assert pipes == ()

    def test_simple_path_unchanged(self):
        """Path[int] (no extra args) still works as before."""
        annotation = Path[int]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "path"
        assert inner is int
        assert pipes == ()

    def test_class_based_pipe_in_subscript(self):
        @pipe()
        class Uppercase:
            def transform(self, value: str, ctx: PipeContext) -> str:
                return value.upper()

        annotation = Query[str, Uppercase]
        source, inner, _, _, fd, pipes = parse_extractor_hint(annotation)
        assert source == "query"
        assert len(pipes) == 1
        assert is_pipe(pipes[0])

    def test_pipes_order_preserved(self):
        """Pipes must arrive in declaration order (left-to-right)."""
        events: list[int] = []

        @pipe()
        def first(v: int) -> int:
            events.append(1)
            return v

        @pipe()
        def second(v: int) -> int:
            events.append(2)
            return v

        @pipe()
        def third(v: int) -> int:
            events.append(3)
            return v

        annotation = Path[int, first, second, third]
        _, _, _, _, _, pipes = parse_extractor_hint(annotation)
        assert len(pipes) == 3
        # Verify identity by calling them in order
        for p in pipes:
            p(0)
        assert events == [1, 2, 3]


# ---------------------------------------------------------------------------
# End-to-end: subscript pipes run correctly at request time
# ---------------------------------------------------------------------------


class TestSubscriptPipesEndToEnd:
    @pytest.mark.asyncio
    async def test_single_pipe_path_subscript(self):
        @pipe()
        def double(v: int) -> int:
            return v * 2

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, double]) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/5")
        assert r.status_code == 200
        assert r.json() == {"n": 10}

    @pytest.mark.asyncio
    async def test_multiple_pipes_left_to_right(self):
        """(5 + 1) * 3 = 18, not 5*3+1=16"""

        @pipe()
        def add_one(v: int) -> int:
            return v + 1

        @pipe()
        def times_three(v: int) -> int:
            return v * 3

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, add_one, times_three]) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/5")
        assert r.json() == {"n": 18}

    @pytest.mark.asyncio
    async def test_query_subscript_pipes(self):
        @pipe()
        def ensure_positive(v: int, ctx: PipeContext) -> int:
            if v <= 0:
                from lauren.exceptions import ExtractorFieldError

                raise ExtractorFieldError(f"{ctx.name} must be positive")
            return v

        @pipe()
        def cap_at_fifty(v: int) -> int:
            return min(v, 50)

        @controller("/c")
        class C:
            @get("/")
            async def h(self, q: Query[int, ensure_positive, cap_at_fifty]) -> dict:
                return {"q": q}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)

        r = client.get("/c/?q=100")
        assert r.status_code == 200
        assert r.json() == {"q": 50}

        r = client.get("/c/?q=30")
        assert r.json() == {"q": 30}

        r = client.get("/c/?q=-1")
        assert r.status_code >= 400

    @pytest.mark.asyncio
    async def test_field_descriptor_plus_pipe_subscript(self):
        @pipe()
        def square(v: int) -> int:
            return v * v

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, PathField(ge=1), square]) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)

        ok = client.get("/c/4")
        assert ok.status_code == 200
        assert ok.json() == {"n": 16}

        bad = client.get("/c/0")
        assert bad.status_code >= 400

    @pytest.mark.asyncio
    async def test_plain_callable_auto_wrapped(self):
        """A plain function (no @pipe) is auto-wrapped."""

        def add_hundred(v: int) -> int:
            return v + 100

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, add_hundred]) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/5")
        assert r.json() == {"n": 105}

    @pytest.mark.asyncio
    async def test_multiple_params_each_with_subscript_pipes(self):
        """Canonical example from the feature request."""

        @pipe()
        def ensure_int(v: int) -> int:
            # coerce happens before pipes; just ensure it's positive
            if not isinstance(v, int):
                raise ValueError("not an int")
            return v

        @pipe()
        def ensure_gt_zero(v: int, ctx: PipeContext) -> int:
            if v <= 0:
                from lauren.exceptions import ExtractorFieldError

                raise ExtractorFieldError(f"{ctx.name} must be > 0")
            return v

        @pipe()
        def ensure_less_than_fifty(v: int, ctx: PipeContext) -> int:
            if v >= 50:
                from lauren.exceptions import ExtractorFieldError

                raise ExtractorFieldError(f"{ctx.name} must be < 50")
            return v

        @controller("/c")
        class C:
            @get("/{user_id}")
            async def get_user(
                self,
                user_id: Path[int, ensure_int, ensure_gt_zero],
                q: Query[int, ensure_int, ensure_gt_zero, ensure_less_than_fifty],
            ) -> dict:
                return {"user_id": user_id, "q": q}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)

        ok = client.get("/c/3?q=10")
        assert ok.status_code == 200
        assert ok.json() == {"user_id": 3, "q": 10}

        bad_id = client.get("/c/0?q=10")
        assert bad_id.status_code >= 400

        bad_q = client.get("/c/3?q=60")
        assert bad_q.status_code >= 400

    @pytest.mark.asyncio
    async def test_subscript_pipe_receives_context(self):
        @pipe()
        def with_ctx(v: int, ctx: PipeContext) -> dict:
            return {"v": v, "source": ctx.source, "name": ctx.name}

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, with_ctx]) -> dict:
                return n

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/7")
        assert r.json() == {"v": 7, "source": "path", "name": "n"}

    @pytest.mark.asyncio
    async def test_subscript_pipes_combine_with_default_pipes(self):
        """Annotation-side pipes run before default-side pipes."""
        events: list[str] = []

        @pipe()
        def annotated_pipe(v: int) -> int:
            events.append("annotation")
            return v

        @pipe()
        def default_pipe(v: int) -> int:
            events.append("default")
            return v

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, annotated_pipe] = pipe(default_pipe)) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app).get("/c/1")
        assert events == ["annotation", "default"]

    @pytest.mark.asyncio
    async def test_async_pipe_in_subscript(self):
        @pipe()
        async def load(n: int) -> dict:
            return {"loaded": n}

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Path[int, load]) -> dict:
                return n

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/3")
        assert r.json() == {"loaded": 3}

    @pytest.mark.asyncio
    async def test_json_body_with_subscript_pipe(self):
        class Payload(BaseModel):
            name: str

        @pipe()
        def normalize(p: Payload) -> dict:
            return {"name": p.name.strip().lower()}

        @controller("/c")
        class C:
            @post("/")
            async def h(self, body: Json[Payload, normalize]) -> dict:
                return body

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).post("/c/", json={"name": "  Alice "})
        assert r.json() == {"name": "alice"}

    @pytest.mark.asyncio
    async def test_header_with_subscript_pipe(self):
        @pipe()
        def upper(v: str) -> str:
            return v.upper()

        @controller("/c")
        class C:
            @get("/")
            async def h(self, x_token: Header[str, upper]) -> dict:
                return {"token": x_token}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/", headers={"x-token": "abc"})
        assert r.json() == {"token": "ABC"}
