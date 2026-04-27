"""Unit tests for the module system."""

from __future__ import annotations

import sys
from typing import ForwardRef

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


class TestForwardRefImports:
    """ForwardRef / string forward references in @module(imports=[...])."""

    def test_forwardref_resolves_from_own_module_globals(self):
        """ForwardRef that names a class present in the same module's globals."""

        @module(providers=[ServiceA], exports=[ServiceA])
        class SharedFwdRef: ...

        # Register SharedFwdRef in this test module's namespace so the
        # resolver can find it via the declaring class's __module__.
        test_mod = sys.modules[__name__]
        test_mod.SharedFwdRef = SharedFwdRef  # type: ignore[attr-defined]
        try:

            @module(imports=[ForwardRef("SharedFwdRef")])
            class ConsumerFwdRef: ...

            g = ModuleGraph()
            g.compile(ConsumerFwdRef)
            assert ServiceA in g.modules[ConsumerFwdRef].providers
        finally:
            del test_mod.SharedFwdRef  # type: ignore[attr-defined]

    def test_string_import_resolves_from_own_module_globals(self):
        """Plain string forward reference behaves identically to ForwardRef."""

        @module(providers=[ServiceB], exports=[ServiceB])
        class SharedStr: ...

        test_mod = sys.modules[__name__]
        test_mod.SharedStr = SharedStr  # type: ignore[attr-defined]
        try:

            @module(imports=["SharedStr"])
            class ConsumerStr: ...

            g = ModuleGraph()
            g.compile(ConsumerStr)
            assert ServiceB in g.modules[ConsumerStr].providers
        finally:
            del test_mod.SharedStr  # type: ignore[attr-defined]

    def test_dotted_forwardref_resolves_via_sys_modules(self):
        """Dotted ForwardRef('pkg.mod.ClassName') resolves through sys.modules."""
        import types

        fake_mod = types.ModuleType("_lauren_test_fake_mod")

        @module(providers=[ServiceA], exports=[ServiceA])
        class DottedTarget: ...

        fake_mod.DottedTarget = DottedTarget  # type: ignore[attr-defined]
        sys.modules["_lauren_test_fake_mod"] = fake_mod
        try:

            @module(imports=[ForwardRef("_lauren_test_fake_mod.DottedTarget")])
            class DottedConsumer: ...

            g = ModuleGraph()
            g.compile(DottedConsumer)
            assert ServiceA in g.modules[DottedConsumer].providers
        finally:
            del sys.modules["_lauren_test_fake_mod"]

    def test_forwardref_circular_dependency_pattern(self):
        """The typical cross-file circular-import pattern: A imports B via
        ForwardRef so that A's file need not import B at module load time."""

        @module(providers=[ServiceA], exports=[ServiceA])
        class CircB: ...

        # Simulate A being defined without importing CircB directly:
        # CircB is in sys.modules under its declaring module's namespace but
        # NOT in locals. The resolver finds it via the sys.modules scan.
        @module(imports=[ForwardRef("CircB")])
        class CircA: ...

        # Put CircB somewhere the scan will find it (it's already in the
        # test module's local scope, but the resolver scans sys.modules).
        test_mod = sys.modules[__name__]
        test_mod.CircB = CircB  # type: ignore[attr-defined]
        try:
            g = ModuleGraph()
            g.compile(CircA)
            assert ServiceA in g.modules[CircA].providers
        finally:
            del test_mod.CircB  # type: ignore[attr-defined]

    def test_unresolvable_forwardref_raises(self):
        """A ForwardRef that names a non-existent class raises ValueError."""

        @module(imports=[ForwardRef("ThisClassDoesNotExist_XYZ_99")])
        class BadImporter: ...

        g = ModuleGraph()
        with pytest.raises(ValueError, match="ThisClassDoesNotExist_XYZ_99"):
            g.compile(BadImporter)

    def test_real_class_still_works(self):
        """Plain class references continue to work unchanged (regression guard)."""

        @module(providers=[ServiceA], exports=[ServiceA])
        class RealShared: ...

        @module(imports=[RealShared])
        class RealConsumer: ...

        g = ModuleGraph()
        g.compile(RealConsumer)
        assert ServiceA in g.modules[RealConsumer].providers

    def test_mixed_list_real_and_forwardref(self):
        """imports=[RealClass, ForwardRef(...)] — both entries resolve."""

        @module(providers=[ServiceA], exports=[ServiceA])
        class MixedReal: ...

        @module(providers=[ServiceB], exports=[ServiceB])
        class MixedFwd: ...

        test_mod = sys.modules[__name__]
        test_mod.MixedFwd = MixedFwd  # type: ignore[attr-defined]
        try:

            @module(imports=[MixedReal, ForwardRef("MixedFwd")])
            class MixedConsumer: ...

            g = ModuleGraph()
            g.compile(MixedConsumer)
            compiled = g.modules[MixedConsumer]
            assert ServiceA in compiled.providers
            assert ServiceB in compiled.providers
        finally:
            del test_mod.MixedFwd  # type: ignore[attr-defined]

    def test_dotted_string_resolves_via_sys_modules(self):
        """Plain dotted string 'pkg.mod.Class' works identically to ForwardRef."""
        import types

        fake = types.ModuleType("_lauren_test_dotted_str")

        @module(providers=[ServiceB], exports=[ServiceB])
        class DottedStrTarget: ...

        fake.DottedStrTarget = DottedStrTarget  # type: ignore[attr-defined]
        sys.modules["_lauren_test_dotted_str"] = fake
        try:

            @module(imports=["_lauren_test_dotted_str.DottedStrTarget"])
            class DottedStrConsumer: ...

            g = ModuleGraph()
            g.compile(DottedStrConsumer)
            assert ServiceB in g.modules[DottedStrConsumer].providers
        finally:
            del sys.modules["_lauren_test_dotted_str"]

    def test_ambiguous_name_raises_with_hint(self):
        """Two loaded modules exposing a class with the same simple name raises
        ValueError and the message suggests using a dotted name."""
        import types

        @module(providers=[ServiceA], exports=[ServiceA])
        class Ambiguous: ...

        mod1 = types.ModuleType("_lauren_test_amb1")
        mod2 = types.ModuleType("_lauren_test_amb2")
        mod1.Ambiguous = Ambiguous  # type: ignore[attr-defined]

        # Create a *distinct* class with the same name in mod2.
        AmbiguousCopy = type("Ambiguous", (), {})
        mod2.Ambiguous = AmbiguousCopy  # type: ignore[attr-defined]

        sys.modules["_lauren_test_amb1"] = mod1
        sys.modules["_lauren_test_amb2"] = mod2
        try:

            @module(imports=[ForwardRef("Ambiguous")])
            class AmbConsumer: ...

            g = ModuleGraph()
            with pytest.raises(ValueError, match="ambiguous"):
                g.compile(AmbConsumer)
        finally:
            del sys.modules["_lauren_test_amb1"]
            del sys.modules["_lauren_test_amb2"]

    def test_invalid_import_entry_raises(self):
        """A non-class, non-ForwardRef, non-string entry raises ValueError."""

        @module(imports=[42])  # type: ignore[list-item]
        class BogusImporter: ...

        g = ModuleGraph()
        with pytest.raises(ValueError, match="Invalid entry"):
            g.compile(BogusImporter)

    def test_dotted_forwardref_missing_module_raises(self):
        """Dotted name whose parent module isn't loaded raises ValueError."""

        @module(imports=[ForwardRef("nonexistent.pkg.SomeModule")])
        class MissingPkg: ...

        g = ModuleGraph()
        with pytest.raises(ValueError, match="nonexistent.pkg.SomeModule"):
            g.compile(MissingPkg)

    def test_forwardref_chain_three_modules(self):
        """A -> ForwardRef(B) -> ForwardRef(C): each hop resolves lazily."""

        @module(providers=[ServiceA], exports=[ServiceA])
        class ChainC: ...

        @module(imports=[ForwardRef("ChainC")], exports=[ServiceA])
        class ChainB: ...

        test_mod = sys.modules[__name__]
        test_mod.ChainC = ChainC  # type: ignore[attr-defined]
        test_mod.ChainB = ChainB  # type: ignore[attr-defined]
        try:

            @module(imports=[ForwardRef("ChainB")])
            class ChainA: ...

            g = ModuleGraph()
            g.compile(ChainA)
            assert ServiceA in g.modules[ChainA].providers
        finally:
            del test_mod.ChainC  # type: ignore[attr-defined]
            del test_mod.ChainB  # type: ignore[attr-defined]

    def test_forwardref_module_still_detected_as_circular(self):
        """A true circular dependency expressed via ForwardRef is still caught."""

        class FwdCircA: ...

        class FwdCircB: ...

        test_mod = sys.modules[__name__]
        test_mod.FwdCircA = FwdCircA  # type: ignore[attr-defined]
        test_mod.FwdCircB = FwdCircB  # type: ignore[attr-defined]
        try:
            module(imports=[ForwardRef("FwdCircB")])(FwdCircA)
            module(imports=[ForwardRef("FwdCircA")])(FwdCircB)

            g = ModuleGraph()
            with pytest.raises(CircularModuleError):
                g.compile(FwdCircA)
        finally:
            del test_mod.FwdCircA  # type: ignore[attr-defined]
            del test_mod.FwdCircB  # type: ignore[attr-defined]
