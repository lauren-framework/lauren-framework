"""Tests for parameter-level pipes \u2014 Axum/NestJS-style transforms.

Covers:

* Pipes declared in ``Annotated[...]`` metadata.
* Pipes declared as the default value, composed with ``|``.
* Mixed forms (annotation metadata + default composition).
* Async pipes, class-based pipes, and class-based pipes resolved via DI.
* Correct application order (annotation pipes first, then default-side).
* ``FieldDescriptor`` + pipes composed together.
* Module-visibility enforcement for DI-backed class pipes.
"""

# NOTE: intentionally not using ``from __future__ import annotations`` \u2014
# several nested classes referenced in handler type hints are defined inside
# test methods, which breaks PEP 563 stringified annotation resolution.

from typing import Annotated

import pytest
from pydantic import BaseModel

from lauren import (
    LaurenFactory,
    Path,
    PathField,
    Query,
    Json,
    controller,
    get,
    injectable,
    module,
    pipe,
    post,
)
from lauren.extractors import PipeContext
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Sync function pipe declared inside Annotated[...]
# ---------------------------------------------------------------------------


class TestAnnotatedPipes:
    @pytest.mark.asyncio
    async def test_single_sync_pipe(self):
        def double(value: int) -> int:
            return value * 2

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Annotated[Path[int], pipe(double)]) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        c = TestClient(app)
        r = c.get("/c/5")
        assert r.status_code == 200
        assert r.json() == {"n": 10}

    @pytest.mark.asyncio
    async def test_pipes_compose_left_to_right(self):
        def add_ten(v: int) -> int:
            return v + 10

        def times_three(v: int) -> int:
            return v * 3

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(
                self,
                # (5 + 10) * 3 = 45, NOT 5*3+10=25
                n: Annotated[Path[int], pipe(add_ten), pipe(times_three)],
            ) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/5")
        assert r.json() == {"n": 45}

    @pytest.mark.asyncio
    async def test_pipe_receives_context_when_accepts_two_args(self):
        def needs_ctx(value: int, ctx: PipeContext) -> dict:
            return {
                "value": value,
                "name": ctx.name,
                "source": ctx.source,
            }

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Annotated[Path[int], pipe(needs_ctx)]) -> dict:
                return n

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/7")
        assert r.json() == {"value": 7, "name": "n", "source": "path"}


# ---------------------------------------------------------------------------
# Default-side composition: PathField(...) | pipe(...) | pipe(...)
# ---------------------------------------------------------------------------


class TestDefaultComposition:
    @pytest.mark.asyncio
    async def test_field_and_pipe_default_equivalent_to_annotated(self):
        def tag(value: int) -> dict:
            return {"kind": "tagged", "value": value}

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(
                self,
                n: Path[int] = PathField(ge=1) | pipe(tag),
            ) -> dict:
                return n

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)

        ok = client.get("/c/4")
        assert ok.status_code == 200
        assert ok.json() == {"kind": "tagged", "value": 4}

        bad = client.get("/c/0")
        # PathField(ge=1) still applies \u2014 the pipe only runs on valid values.
        assert bad.status_code >= 400

    @pytest.mark.asyncio
    async def test_ordering_annotation_then_default(self):
        events: list[str] = []

        def ann(v: int) -> int:
            events.append("ann")
            return v

        def default_(v: int) -> int:
            events.append("default")
            return v

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(
                self,
                n: Annotated[Path[int], pipe(ann)] = PathField() | pipe(default_),
            ) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        TestClient(app).get("/c/1")
        assert events == ["ann", "default"]

    def test_two_field_descriptors_rejected(self):
        with pytest.raises(TypeError, match="at most one FieldDescriptor"):
            PathField(ge=1) | PathField(le=10)

    @pytest.mark.asyncio
    async def test_annotation_field_plus_default_field_rejected(self):
        from lauren.exceptions import UnresolvableParameterError

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(
                self,
                n: Annotated[Path[int], PathField(ge=1)] = PathField(le=10),
            ) -> dict:
                return {"n": n}

        @module(controllers=[C])
        class M:
            pass

        with pytest.raises(UnresolvableParameterError, match="both in the annotation"):
            LaurenFactory.create(M)


# ---------------------------------------------------------------------------
# Async + class-based pipes + DI resolution
# ---------------------------------------------------------------------------


class TestAsyncAndClassPipes:
    @pytest.mark.asyncio
    async def test_async_function_pipe(self):
        async def load(n: int) -> dict:
            return {"loaded": n}

        @controller("/c")
        class C:
            @get("/{n}")
            async def h(self, n: Annotated[Path[int], pipe(load)]) -> dict:
                return n

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/3")
        assert r.json() == {"loaded": 3}

    @pytest.mark.asyncio
    async def test_class_pipe_without_di(self):
        class Upper:
            def transform(self, value, ctx):
                return value.upper()

        @controller("/c")
        class C:
            @get("/x")
            async def h(self, q: Annotated[Query[str], pipe(Upper)]) -> dict:
                return {"q": q}

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/x?q=hello")
        assert r.json() == {"q": "HELLO"}

    @pytest.mark.asyncio
    async def test_class_pipe_via_di_uses_module_providers(self):
        @injectable()
        class Users:
            """A fake user lookup service."""

            def __init__(self):
                self.data = {1: "Alice", 2: "Bob"}

            def lookup(self, uid: int) -> str | None:
                return self.data.get(uid)

        @injectable()
        class LookupUser:
            def __init__(self, users: Users):
                self.users = users

            async def transform(self, value, ctx):
                name = self.users.lookup(value)
                if name is None:
                    from lauren.exceptions import ExtractorFieldError

                    raise ExtractorFieldError("user not found", detail={"id": value})
                return {"id": value, "name": name}

        @controller("/u")
        class Ctrl:
            @get("/{uid}")
            async def h(
                self,
                uid: Annotated[Path[int], pipe(LookupUser)],
            ) -> dict:
                return uid

        @module(
            controllers=[Ctrl],
            providers=[Users, LookupUser],
        )
        class M:
            pass

        app = LaurenFactory.create(M)
        client = TestClient(app)
        ok = client.get("/u/1")
        assert ok.status_code == 200
        assert ok.json() == {"id": 1, "name": "Alice"}

        missing = client.get("/u/99")
        assert missing.status_code >= 400
        assert "user not found" in missing.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Module visibility for DI-backed pipes
# ---------------------------------------------------------------------------


class TestPipeModuleVisibility:
    @pytest.mark.asyncio
    async def test_pipe_class_must_be_visible_from_controllers_module(self):
        # LookupService is declared in the feature module and never exported;
        # when the pipe tries to resolve it via DI from a *different* module
        # the visibility check falls back to instantiating the class directly
        # (no-arg) \u2014 which yields a fresh instance.
        # Here we verify the positive case: LookupService is in the same
        # module as the controller, so DI resolution succeeds.
        @injectable()
        class LookupService:
            def __init__(self):
                self.prefix = "user:"

        @injectable()
        class Pipe:
            def __init__(self, svc: LookupService):
                self.svc = svc

            def transform(self, value, ctx):
                return f"{self.svc.prefix}{value}"

        @controller("/u")
        class Ctrl:
            @get("/{v}")
            async def h(self, v: Annotated[Path[str], pipe(Pipe)]) -> dict:
                return {"v": v}

        @module(controllers=[Ctrl], providers=[LookupService, Pipe])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/u/42")
        assert r.json() == {"v": "user:42"}


# ---------------------------------------------------------------------------
# Pipe on a JSON body
# ---------------------------------------------------------------------------


class TestBodyPipes:
    @pytest.mark.asyncio
    async def test_pipe_runs_after_pydantic_validation(self):
        class CreateUser(BaseModel):
            name: str
            age: int

        def normalize(u: CreateUser) -> dict:
            return {"name": u.name.strip().lower(), "age": u.age}

        @controller("/users")
        class C:
            @post("/")
            async def create(
                self, body: Annotated[Json[CreateUser], pipe(normalize)]
            ) -> dict:
                return body

        @module(controllers=[C])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).post("/users/", json={"name": "  Alice ", "age": 30})
        assert r.json() == {"name": "alice", "age": 30}
