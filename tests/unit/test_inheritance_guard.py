"""Tests: decorators do NOT leak via class inheritance.

The rule: a subclass is ``@controller`` / ``@injectable`` / ``@module`` /
``@middleware`` ONLY if it is decorated explicitly. Re-using a parent's
decoration by inheritance alone is rejected at startup.
"""

from __future__ import annotations


import pytest

from lauren import (
    LaurenFactory,
    controller,
    get,
    injectable,
    middleware,
    module,
)
from lauren.exceptions import MetadataInheritanceError


def _build(root_module):
    return LaurenFactory.create(root_module)


# ---------------------------------------------------------------------------
# @injectable: inheritance -> MetadataInheritanceError
# ---------------------------------------------------------------------------


class TestInjectableInheritance:
    def test_subclass_inherits_but_is_not_registered_without_decoration(self):
        @injectable()
        class Base:
            pass

        class Child(Base):
            pass

        from lauren._di import DIContainer

        c = DIContainer()
        # Base registers fine; Child would inherit the marker, which we reject.
        c.register(Base)
        with pytest.raises(MetadataInheritanceError):
            c.register(Child)

    def test_re_decorating_subclass_is_allowed(self):
        @injectable()
        class Base:
            pass

        @injectable()  # explicit opt-in
        class Child(Base):
            pass

        from lauren._di import DIContainer

        c = DIContainer()
        c.register(Base)
        c.register(Child)  # no error
        c.compile()


# ---------------------------------------------------------------------------
# @controller: inheritance -> MetadataInheritanceError at startup
# ---------------------------------------------------------------------------


class TestControllerInheritance:
    def test_undecorated_subclass_rejected_at_startup(self):
        @controller("/base")
        class Base:
            @get("/")
            async def index(self) -> dict:
                return {}

        class Child(Base):  # intentionally NOT decorated
            pass

        @module(controllers=[Child])
        class Mod: ...

        with pytest.raises(MetadataInheritanceError):
            _build(Mod)

    def test_re_decorated_subclass_works(self):
        @controller("/base")
        class Base:
            @get("/")
            async def index(self) -> dict:
                return {"scope": "base"}

        @controller("/derived")
        class Derived(Base):
            pass

        @module(controllers=[Derived])
        class Mod: ...

        app = _build(Mod)
        routes = [(r.method, r.path_template) for r in app.routes()]
        # Derived inherits the handler method, so the route exists at
        # the derived prefix.
        assert ("GET", "/derived") in routes


# ---------------------------------------------------------------------------
# @module: inheritance -> MetadataInheritanceError
# ---------------------------------------------------------------------------


class TestModuleInheritance:
    def test_undecorated_module_subclass_rejected(self):
        @module()
        class Root:
            pass

        class ChildRoot(Root):
            pass

        with pytest.raises(MetadataInheritanceError):
            _build(ChildRoot)


# ---------------------------------------------------------------------------
# @middleware: subclass must re-decorate to be auto-registered
# ---------------------------------------------------------------------------


class TestMiddlewareInheritance:
    def test_subclass_of_middleware_is_not_middleware(self):
        @middleware
        class BaseMW:
            async def dispatch(self, request, call_next):
                return await call_next(request)

        class ChildMW(BaseMW):
            pass

        # When the framework tries to auto-register ChildMW as injectable for
        # use_middlewares, it must raise to prevent silent inheritance.
        from lauren._asgi import _ensure_injectable

        with pytest.raises(MetadataInheritanceError):
            _ensure_injectable(ChildMW)
