"""Unit tests for ``Test.create_testing_module`` override recording (no app build)."""

from __future__ import annotations

import pytest

from lauren import exception_handler, injectable, module, use_class, use_existing, use_factory, use_value
from lauren._di._overrides import OverridePlan, ProviderOverride, active_override_plan, building_with
from lauren.exceptions import DecoratorUsageError
from lauren.testing import Test, TestingModuleBuilder
from lauren.types import Scope


@injectable()
class Repo:
    pass


class Guard:
    async def can_activate(self, context) -> bool:
        return True


@exception_handler(ValueError)
def on_value_error(exc, request):
    return {"handled": True}


@module(providers=[Repo])
class AppModule:
    pass


def _builder(**kw) -> TestingModuleBuilder:
    return Test.create_testing_module(AppModule, **kw)


def test_create_testing_module_returns_a_builder() -> None:
    assert isinstance(_builder(), TestingModuleBuilder)


def test_test_classes_are_not_collected_by_pytest() -> None:
    assert Test.__test__ is False and TestingModuleBuilder.__test__ is False


def test_fluent_calls_return_the_builder() -> None:
    builder = _builder()
    assert builder.override_provider(Repo).use_value(object()) is builder


@pytest.mark.parametrize(
    ("configure", "expected"),
    [
        (lambda b: b.override_provider(Repo).use_value(1), {"kind": "value", "value": 1}),
        (
            lambda b: b.override_provider(Repo).use_class(dict),
            {"kind": "class", "use_class": dict, "scope": None},
        ),
        (
            lambda b: b.override_provider(Repo).use_class(dict, scope=Scope.REQUEST),
            {"kind": "class", "scope": Scope.REQUEST},
        ),
        (
            lambda b: b.override_provider(Repo).use_factory(dict, injects=["X"]),
            {"kind": "factory", "injects": ("X",)},
        ),
        (
            lambda b: b.override_provider(Repo).use_existing("OTHER"),
            {"kind": "existing", "existing": "OTHER"},
        ),
        (lambda b: b.override_provider(Repo).use_values([1, 2]), {"kind": "values", "values": (1, 2)}),
    ],
)
def test_provider_override_records(configure, expected) -> None:
    builder = _builder()
    configure(builder)
    record = builder._providers[Repo]
    for key, value in expected.items():
        assert getattr(record, key) == value


def test_optional_marks_the_record() -> None:
    builder = _builder()
    builder.override_provider(Repo).optional().use_value(1)
    assert builder._providers[Repo].optional is True


def test_overriding_a_token_twice_is_rejected() -> None:
    builder = _builder().override_provider(Repo).use_value(1)
    with pytest.raises(DecoratorUsageError, match="already overridden"):
        builder.override_provider(Repo).use_value(2)


def test_handler_and_provider_overrides_share_the_conflict_check() -> None:
    builder = _builder().override_exception_handler(on_value_error).use_value(lambda e, r: None)
    with pytest.raises(DecoratorUsageError, match="already overridden"):
        builder.override_exception_handler(on_value_error).use_value(lambda e, r: None)


def test_module_overridden_twice_is_rejected() -> None:
    @module()
    class Fake:
        pass

    builder = _builder().override_module(AppModule).with_module(Fake)
    with pytest.raises(DecoratorUsageError, match="already overridden"):
        builder.override_module(AppModule).with_module(Fake)


@pytest.mark.parametrize(
    "misuse",
    [
        lambda b: b.override_provider(Repo).use_class("not a class"),
        lambda b: b.override_provider(Repo).use_factory("not callable"),
        lambda b: b.override_provider(Repo).use_values([]),
        lambda b: b.override_guard("not a class"),
        lambda b: b.override_guard(Guard).use_value(object()),
        lambda b: b.override_guard(Guard).use_class(dict),
        lambda b: b.override_interceptor("x"),
        lambda b: b.override_middleware("x"),
        lambda b: b.override_exception_handler(Repo),
        lambda b: b.override_exception_handler(on_value_error).use_value(42),
        lambda b: b.override_exception_handler(on_value_error).use_class(dict),
        lambda b: b.override_module("x"),
        lambda b: b.override_module(AppModule).with_module(dict),
    ],
)
def test_misuse_is_rejected_at_builder_time(misuse) -> None:
    with pytest.raises(DecoratorUsageError):
        misuse(_builder())


def test_unknown_factory_kwargs_are_rejected() -> None:
    with pytest.raises(DecoratorUsageError, match="unknown"):
        Test.create_testing_module(AppModule, not_a_kwarg=1)


def test_declarative_overrides_become_records() -> None:
    builder = _builder(
        overrides=[
            use_value(provide="A", value=1),
            use_class(provide="B", use=dict),
            use_factory(provide="C", factory=dict, injects=["A"]),
            use_existing(provide="D", existing="A"),
        ]
    )
    assert [builder._providers[t].kind for t in "ABCD"] == ["value", "class", "factory", "existing"]
    assert builder._providers["C"].injects == ("A",)


def test_declarative_multi_values_merge_into_one_set_override() -> None:
    builder = _builder(
        overrides=[use_value(provide="P", value=1, multi=True), use_value(provide="P", value=2, multi=True)]
    )
    assert (builder._providers["P"].kind, builder._providers["P"].values) == ("values", (1, 2))


def test_declarative_overrides_must_be_custom_providers() -> None:
    with pytest.raises(DecoratorUsageError):
        _builder(overrides=[Repo])


def test_override_plan_is_scoped_to_the_build_block() -> None:
    plan = OverridePlan(providers={"T": ProviderOverride(token="T", kind="value", value=1)})
    assert active_override_plan() is None
    with building_with(plan):
        assert active_override_plan() is plan
    assert active_override_plan() is None


def test_override_plan_get_tolerates_unhashable_tokens() -> None:
    assert OverridePlan().get([]) is None


def test_container_rejects_a_non_class_use_class_override() -> None:
    from lauren._di import DIContainer
    from lauren.exceptions import UnresolvableParameterError

    plan = OverridePlan(providers={Repo: ProviderOverride(token=Repo, kind="class", use_class=42)})  # type: ignore[arg-type]
    with pytest.raises(UnresolvableParameterError, match="needs a class"):
        DIContainer(overrides=plan).register(Repo)


async def test_factory_override_with_an_optional_dependency() -> None:
    from lauren._di import DIContainer
    from lauren._di.custom import OptionalDep

    plan = OverridePlan(
        providers={
            Repo: ProviderOverride(
                token=Repo,
                kind="factory",
                factory=lambda missing: ("built", missing),
                injects=(OptionalDep("NOPE"),),
            )
        }
    )
    container = DIContainer(overrides=plan)
    container.register(Repo)
    container.compile()
    assert await container.resolve(Repo) == ("built", None)
    assert container.applied_overrides() == frozenset({Repo})
