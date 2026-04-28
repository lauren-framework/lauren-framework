"""Tests for ``@use_guards`` and ``@use_middleware`` applied to controllers."""

from __future__ import annotations


from lauren import (
    ExecutionContext,
    LaurenFactory,
    Request,
    controller,
    get,
    middleware,
    module,
    use_guards,
    use_middleware,
)
from lauren.exceptions import ForbiddenError
from lauren.testing import TestClient


class DenyGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        raise ForbiddenError("denied by class-level guard")


class AllowGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


@middleware
class StampMW:
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        return response.with_header("x-stamp", "class-level")


class TestClassLevelGuards:
    def test_guards_outside_controller_decoration(self):
        """@use_guards applied above @controller."""

        @use_guards(DenyGuard)
        @controller("/x1")
        class C:
            @get("/")
            async def idx(self) -> dict:
                return {}  # handler unreachable

        @module(controllers=[C])
        class Mod: ...

        client = TestClient(LaurenFactory.create(Mod))
        r = client.get("/x1/")
        assert r.status_code == 403

    def test_guards_inside_controller_decoration(self):
        """@use_guards applied below @controller (inner)."""

        @controller("/x2")
        @use_guards(DenyGuard)
        class C:
            @get("/")
            async def idx(self) -> dict:
                return {}

        @module(controllers=[C])
        class Mod: ...

        client = TestClient(LaurenFactory.create(Mod))
        r = client.get("/x2/")
        assert r.status_code == 403

    def test_class_guards_run_for_every_handler(self):
        @use_guards(DenyGuard)
        @controller("/x3")
        class C:
            @get("/a")
            async def a(self) -> dict:
                return {}

            @get("/b")
            async def b(self) -> dict:
                return {}

        @module(controllers=[C])
        class Mod: ...

        client = TestClient(LaurenFactory.create(Mod))
        assert client.get("/x3/a").status_code == 403
        assert client.get("/x3/b").status_code == 403

    def test_class_and_method_guards_compose(self):
        """Class guards run first, then method guards."""
        order: list[str] = []

        class FirstGuard:
            async def can_activate(self, ctx):
                order.append("class")
                return True

        class SecondGuard:
            async def can_activate(self, ctx):
                order.append("method")
                return True

        @use_guards(FirstGuard)
        @controller("/x4")
        class C:
            @get("/")
            @use_guards(SecondGuard)
            async def idx(self) -> dict:
                order.append("handler")
                return {}

        @module(controllers=[C])
        class Mod: ...

        client = TestClient(LaurenFactory.create(Mod))
        client.get("/x4/")
        assert order == ["class", "method", "handler"]


class TestClassLevelMiddleware:
    def test_middleware_on_controller(self):
        @use_middleware(StampMW)
        @controller("/m1")
        class C:
            @get("/")
            async def idx(self) -> dict:
                return {"ok": True}

        @module(controllers=[C])
        class Mod: ...

        client = TestClient(LaurenFactory.create(Mod))
        r = client.get("/m1/")
        assert r.header("x-stamp") == "class-level"


class TestSubclassDoesNotInheritGuards:
    """If a subclass doesn't re-declare guards, the parent's guards do not
    silently carry over \u2014 preserving the "explicit is better" principle.
    """

    def test_subclass_starts_fresh_for_guards(self):
        @use_guards(DenyGuard)
        @controller("/p")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {"ok": True}

        @controller("/q")
        class Child(Parent):
            # Intentionally omits @use_guards(DenyGuard) \u2014 should NOT
            # inherit the parent's guards.
            pass

        @module(controllers=[Parent, Child])
        class Mod: ...

        client = TestClient(LaurenFactory.create(Mod))
        assert client.get("/p/").status_code == 403  # parent guarded
        assert client.get("/q/").status_code == 200  # child is guard-free
