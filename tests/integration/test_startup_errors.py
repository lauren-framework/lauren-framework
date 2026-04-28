"""Integration tests for startup-time validation errors."""

from __future__ import annotations


import pytest

from lauren import (
    LaurenFactory,
    controller,
    get,
    injectable,
    module,
)
from lauren.exceptions import (
    CircularModuleError,
    MissingProviderError,
    RouterConflictError,
    UnresolvableParameterError,
)


def _run(mod: type):
    return LaurenFactory.create(mod)


class TestRouterConflicts:
    def test_conflicting_routes_across_controllers(self):
        @controller("/x")
        class A:
            @get("/")
            async def a(self):
                return "a"

        @controller("/x")
        class B:
            @get("/")
            async def b(self):
                return "b"

        @module(controllers=[A, B])
        class Mod: ...

        with pytest.raises(RouterConflictError):
            _run(Mod)


class TestMissingDependency:
    def test_controller_depends_on_unregistered(self):
        @injectable()
        class Needed:
            def __init__(self): ...

        @controller("/x")
        class Ctrl:
            def __init__(self, n: Needed): ...

            @get("/")
            async def get_(self):
                return "ok"

        @module(controllers=[Ctrl])  # forgets providers=[Needed]
        class Mod: ...

        with pytest.raises(MissingProviderError):
            _run(Mod)


class TestUnresolvableParameter:
    def test_handler_unknown_param(self):
        @controller("/x")
        class Ctrl:
            @get("/")
            async def get_(self, mystery) -> str:  # no annotation, no default
                return "x"

        @module(controllers=[Ctrl])
        class Mod: ...

        with pytest.raises(UnresolvableParameterError):
            _run(Mod)


class TestCircularModule:
    def test_circular(self):
        class A: ...

        class B: ...

        module(imports=[B])(A)
        module(imports=[A])(B)
        with pytest.raises(CircularModuleError):
            _run(A)
