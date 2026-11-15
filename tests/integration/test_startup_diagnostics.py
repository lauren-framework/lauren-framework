"""Startup diagnostics keep existing errors while adding actionable hints."""

from __future__ import annotations

import pytest

from lauren import (
    LaurenFactory,
    MissingProviderError,
    UnresolvableParameterError,
    controller,
    get,
    injectable,
    module,
)


@injectable()
class _UsersService:
    pass


@module(providers=[_UsersService])
class _UsersModule:
    pass


@injectable()
class _OrdersService:
    def __init__(self, users: _UsersService) -> None:
        self.users = users


@module(imports=[_UsersModule], providers=[_OrdersService])
class _OrdersModule:
    pass


def test_missing_export_identifies_owner_and_fix_without_replacing_error() -> None:
    with pytest.raises(MissingProviderError) as caught:
        LaurenFactory.create(_OrdersModule)

    error = caught.value
    assert error.detail["token"] == "_UsersService"
    assert "visible from module _OrdersModule" in str(error)
    assert any("_UsersModule" in hint and "exports=[_UsersService]" in hint for hint in error.detail["hints"])
    assert error.detail["source"]["line"] > 0
    assert error.detail["docs_url"].endswith("#missing-provider")
    assert any("help:" in note for note in error.__notes__)


@controller("/broken")
class _TypoController:
    @get("/")
    async def show(self, repo: _UsersServce) -> dict:  # type: ignore[name-defined]  # noqa: F821
        return {}


@module(providers=[_UsersService], controllers=[_TypoController])
class _TypoModule:
    pass


def test_unresolved_handler_annotation_suggests_close_provider_name() -> None:
    with pytest.raises(UnresolvableParameterError) as caught:
        LaurenFactory.create(_TypoModule)

    error = caught.value
    assert error.detail["param"] == "repo"
    assert any("_UsersService" in hint for hint in error.detail["hints"])
    assert error.detail["source"]["symbol"].endswith("_TypoController.show")


@injectable()
class _OutsideService:
    pass


@controller("/missing")
class _MissingController:
    @get("/")
    async def show(self, service: _OutsideService) -> dict:
        return {}


@module(controllers=[_MissingController])
class _MissingModule:
    pass


def test_unregistered_service_hint_names_providers_list() -> None:
    with pytest.raises(UnresolvableParameterError) as caught:
        LaurenFactory.create(_MissingModule)

    assert any("providers=[_OutsideService]" in hint for hint in caught.value.detail["hints"])


def test_unknown_provider_suggests_registered_name() -> None:
    @injectable()
    class RealRepo:
        pass

    @injectable()
    class Consumer:
        def __init__(self, repo: RealRep) -> None:  # type: ignore[name-defined]  # noqa: F821
            self.repo = repo

    @module(providers=[RealRepo, Consumer])
    class AppModule:
        pass

    with pytest.raises(MissingProviderError) as caught:
        LaurenFactory.create(AppModule)

    assert "Did you mean RealRepo?" in caught.value.detail["hints"]


def test_router_conflict_names_both_handler_locations() -> None:
    from lauren import RouterConflictError

    @controller("/same")
    class First:
        @get("/")
        async def one(self) -> dict:
            return {}

    @controller("/same")
    class Second:
        @get("/")
        async def two(self) -> dict:
            return {}

    @module(controllers=[First, Second])
    class AppModule:
        pass

    with pytest.raises(RouterConflictError) as caught:
        LaurenFactory.create(AppModule)

    error = caught.value
    assert error.detail["source"]["symbol"].endswith("Second.two")
    assert any(
        "First.one" in hint and "test_startup_diagnostics.py" in hint for hint in error.detail["hints"]
    )
    assert "#router-conflict" in error.detail["docs_url"]


def test_module_export_violation_names_the_missing_declaration() -> None:
    from lauren import ModuleExportViolation

    @module(exports=[_UsersService])
    class BrokenModule:
        pass

    with pytest.raises(ModuleExportViolation) as caught:
        LaurenFactory.create(BrokenModule)

    assert "providers=[...]" in caught.value.detail["hints"][0]
    assert caught.value.detail["docs_url"].endswith("#module_export_violation".replace("_", "-"))


def test_scope_violation_adds_an_operation_scope_hint() -> None:
    from lauren import DIScopeViolationError, Scope

    @injectable(scope=Scope.REQUEST)
    class RequestOnly:
        pass

    @injectable(scope=Scope.SINGLETON)
    class Singleton:
        def __init__(self, scoped: RequestOnly) -> None:
            self.scoped = scoped

    @module(providers=[RequestOnly, Singleton])
    class AppModule:
        pass

    with pytest.raises(DIScopeViolationError) as caught:
        LaurenFactory.create(AppModule)

    assert any("request-scoped" in hint for hint in caught.value.detail["hints"])


def test_diagnostics_are_idempotent_on_the_same_exception() -> None:
    from lauren._diagnostics import enrich_startup_error

    with pytest.raises(MissingProviderError) as caught:
        LaurenFactory.create(_OrdersModule)
    error = caught.value
    notes = list(error.__notes__)

    same = enrich_startup_error(error, graph=None)

    assert same is error
    assert same.__notes__ == notes


@injectable()
class _CycleA:
    def __init__(self, b: _CycleB) -> None:
        self.b = b


@injectable()
class _CycleB:
    def __init__(self, a: _CycleA) -> None:
        self.a = a


def test_cycle_diagnostics_name_each_parameter_and_source() -> None:
    from lauren import CircularDependencyError

    @module(providers=[_CycleA, _CycleB])
    class Root:
        pass

    with pytest.raises(CircularDependencyError) as caught:
        LaurenFactory.create(Root)
    edges = caught.value.detail["dependency_path"]
    assert [edge["parameter"] for edge in edges] == ["b", "a"]
    assert all(edge["source"]["line"] > 0 for edge in edges)
    assert caught.value.detail["source"]["symbol"].endswith("_CycleA")


def test_duplicate_provider_declaration_names_both_modules() -> None:
    from lauren import ModuleExportViolation

    @module(providers=[_UsersService])
    class First:
        pass

    @module(providers=[_UsersService])
    class Second:
        pass

    @module(imports=[First, Second])
    class Root:
        pass

    with pytest.raises(ModuleExportViolation) as caught:
        LaurenFactory.create(Root)
    hint = caught.value.detail["hints"][0]
    assert "First" in hint and "Second" in hint and "declare it once" in hint
