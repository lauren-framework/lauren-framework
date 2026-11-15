"""Exercise startup hint policies without instantiating application providers."""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from lauren import Scope
from lauren._diagnostics import (
    _dependency_path,
    _import_chain,
    _missing_provider,
    _source,
    _unresolvable_parameter,
    enrich_startup_error,
)
from lauren.exceptions import (
    CircularDependencyError,
    MissingProviderError,
    ModuleExportViolation,
    RouterConflictError,
    StartupError,
    UnresolvableParameterError,
)


class Service:
    pass


class Owner:
    pass


class Middle:
    pass


class Consumer:
    pass


def handler(service: Service) -> None:
    pass


def _provider(cls: type = Service, **kwargs: object) -> SimpleNamespace:
    values = dict(cls=cls, deps=(), field_deps=(), scope=Scope.SINGLETON, factory=cls, provider_kind=None)
    values.update(kwargs)
    return SimpleNamespace(**values)


def _container(*providers: object) -> SimpleNamespace:
    return SimpleNamespace(registered_providers=lambda: providers)


def _graph(*, imported: bool = True, exported: bool = True, middle: bool = False) -> SimpleNamespace:
    owner = SimpleNamespace(own_providers={Service}, exported={Service} if exported else set(), imports=())
    consumer = SimpleNamespace(
        own_providers=set(), exported=set(), imports=(Middle if middle else Owner,) if imported else ()
    )
    modules = {Owner: owner, Consumer: consumer}
    if middle:
        modules[Middle] = SimpleNamespace(own_providers=set(), exported=set(), imports=(Owner,))
    return SimpleNamespace(
        modules=modules, module_for_controller=lambda cls: Consumer, visible_in=lambda cls: frozenset()
    )


@pytest.mark.parametrize(
    "imported,exported,middle,expected",
    [
        (False, True, False, "imports"),
        (True, False, False, "exports=[Service]"),
        (True, True, True, "re-export Service"),
        (True, True, False, "Import a module"),
    ],
)
def test_missing_provider_explains_import_and_export_chain(
    imported: bool, exported: bool, middle: bool, expected: str
) -> None:
    graph = _graph(imported=imported, exported=exported, middle=middle)
    error = MissingProviderError(detail={"token": "Service", "module": "Consumer"})
    hints, target = _missing_provider(error, graph, _container(_provider()))
    assert expected in hints[0]
    assert target is Consumer


@pytest.mark.parametrize("module", [None, "Owner"])
def test_missing_provider_can_identify_declaration_without_a_consumer(module: str | None) -> None:
    error = MissingProviderError(detail={"token": "Service", "module": module})
    hints, target = _missing_provider(error, _graph(), _container(_provider()))
    assert "Owner" in hints[0]
    assert target is Owner


def test_import_chain_ignores_uncompiled_targets() -> None:
    assert _import_chain(SimpleNamespace(modules={}), Consumer, Owner) is None


def test_source_for_builtins_is_optional(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _source(int) is None
    monkeypatch.setattr(inspect, "getsourcefile", lambda obj: None)
    monkeypatch.setattr(inspect, "getsourcelines", lambda obj: (["def handler(): pass"], 1))
    assert _source(handler) is None


@pytest.mark.parametrize("param,expected", [("missing", None), ("service", "Depends[Service]")])
def test_resolved_parameter_uses_registered_provider_advice(param: str, expected: str | None) -> None:
    graph = _graph()
    graph.visible_in = lambda cls: {Service}
    error = UnresolvableParameterError(detail={"param": param})
    hints, target = _unresolvable_parameter(error, graph, _container(_provider()), handler, Consumer)
    assert (expected in hints[0]) if expected else not hints
    assert target is handler


def test_unresolvable_without_a_handler_has_no_speculative_hint() -> None:
    hints, target = _unresolvable_parameter(UnresolvableParameterError(), _graph(), _container(), None, None)
    assert hints == [] and target is None


def test_unannotated_parameter_requests_an_annotation() -> None:
    def untyped(value):
        pass

    hints, _ = _unresolvable_parameter(
        UnresolvableParameterError(detail={"param": "value"}), _graph(), _container(), untyped, None
    )
    assert "Annotate parameter" in hints[0]


def test_unknown_annotation_without_close_names_requests_registration() -> None:
    def unknown(value: CompletelyDifferentName):  # noqa: F821
        pass

    hints, _ = _unresolvable_parameter(
        UnresolvableParameterError(detail={"param": "value"}), _graph(), _container(), unknown, None
    )
    assert "CompletelyDifferentName" in hints[0]


def test_invisible_registered_parameter_names_the_missing_export() -> None:
    error = UnresolvableParameterError(detail={"param": "service"})
    hints, _ = _unresolvable_parameter(
        error, _graph(exported=False), _container(_provider()), handler, Consumer
    )
    assert "exports=[Service]" in hints[0]


def test_dependency_path_can_skip_unknown_cycle_members() -> None:
    path, target = _dependency_path(
        CircularDependencyError(detail={"cycle": ["unknown", "missing"]}), _container()
    )
    assert path == [] and target is None


def test_dependency_path_handles_an_alias_without_source() -> None:
    error = CircularDependencyError(detail={"cycle": ["int", "str"]})
    path, _ = _dependency_path(error, _container(_provider(int, provider_kind="existing")))
    assert path == [{"from": "int", "to": "str", "parameter": "<alias>"}]


@pytest.mark.parametrize(
    "error",
    [
        RouterConflictError("invalid route"),
        ModuleExportViolation("invalid export"),
        CircularDependencyError("cycle"),
        StartupError("configuration"),
    ],
)
def test_optional_diagnostics_never_replace_the_original_error(error: StartupError) -> None:
    same = enrich_startup_error(error)
    assert same is error
    assert same.detail["docs_url"].endswith(error.code.replace("_", "-"))
    assert same.message == str(error)


def test_structured_validation_reason_does_not_get_a_provider_hint() -> None:
    error = UnresolvableParameterError(detail={"param": "service", "reason": "bad_constraint"})
    hints, target = _unresolvable_parameter(error, _graph(), _container(), handler, Consumer)
    assert hints == [] and target is handler
