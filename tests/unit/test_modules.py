"""Unit tests for the module system."""

from __future__ import annotations

import pytest

from lauren import controller, get, injectable, module
from lauren._modules import ModuleGraph
from lauren.exceptions import CircularModuleError, ModuleExportViolation


@injectable()
class ServiceA:
    def __init__(self): ...


@injectable()
class ServiceB:
    def __init__(self): ...


@controller("/a")
class ControllerA:
    @get("/")
    async def root(self): ...


class TestModuleGraph:
    def test_simple_module(self):
        @module(controllers=[ControllerA], providers=[ServiceA])
        class RootModule: ...

        g = ModuleGraph()
        g.compile(RootModule)
        assert ServiceA in g.all_providers
        assert ControllerA in g.all_controllers

    def test_module_imports_exports(self):
        @module(providers=[ServiceA], exports=[ServiceA])
        class SharedModule: ...

        @module(imports=[SharedModule])
        class AppModule: ...

        g = ModuleGraph()
        g.compile(AppModule)
        compiled = g.modules[AppModule]
        assert ServiceA in compiled.providers

    def test_export_violation(self):
        @module(exports=[ServiceA])  # doesn't declare or import
        class Bad: ...

        g = ModuleGraph()
        with pytest.raises(ModuleExportViolation):
            g.compile(Bad)

    def test_circular_module(self):
        # Need placeholders first
        class AMod: ...

        class BMod: ...

        module(imports=[BMod])(AMod)
        module(imports=[AMod])(BMod)

        g = ModuleGraph()
        with pytest.raises(CircularModuleError):
            g.compile(AMod)

    def test_nested_modules(self):
        @module(providers=[ServiceA], exports=[ServiceA])
        class L3: ...

        @module(imports=[L3], providers=[ServiceB], exports=[ServiceB, ServiceA])
        class L2: ...

        @module(imports=[L2])
        class L1: ...

        g = ModuleGraph()
        g.compile(L1)
        top = g.modules[L1]
        assert ServiceA in top.providers
        assert ServiceB in top.providers
