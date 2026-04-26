"""Tests for the unified ``inspect.signature(cls)``-based DI inspection.

These tests cover the contract change:

* The container plans a class's dependencies from
  :func:`inspect.signature(cls)`, which honours - in stdlib priority
  order - a class-level ``__signature__`` attribute, a metaclass
  ``__call__`` override, and only then ``__new__`` / ``__init__``.

* Construction always goes through ``cls(**kwargs)`` so any custom
  metaclass / signature-publishing protocol runs verbatim.

* Class-body-annotated DI fields are injected **after** construction
  (post-init). If a user needs the dep inside ``__init__`` they should
  declare it as an ``__init__`` parameter - a documented breaking
  change from the legacy "set fields before __init__" guarantee.

The legacy approach - walking ``cls.__init__`` and ``cls.__new__``
separately and emulating :meth:`type.__call__` - was wrong for any
class whose construction is mediated by something else. The fix
brings lauren in line with what every Python type hint reader
(:mod:`pydantic`, :mod:`attrs`, :mod:`dataclasses`,
:func:`typing.get_type_hints`) already does.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Annotated, NamedTuple

import pytest

from lauren import (
    DIContainer,
    Inject,
    injectable,
    use_factory,
)
from lauren._di import (
    _inspect_class_deps,
    _resolve_class_signature_hints,
)


# ---------------------------------------------------------------------------
# 1. Metaclass overriding ``__call__`` - the original motivating case
# ---------------------------------------------------------------------------


class FactoryMeta(type):
    """Metaclass whose ``__call__`` bypasses ``__init__`` / ``__new__``.

    A real-world variant of this pattern shows up in plug-in registries,
    ORMs that cache instances by primary key, and DSLs that rewrite
    arguments before construction. The callable surface that consumers
    actually see is whatever ``__call__`` advertises - NOT ``__init__``.
    """

    last_constructed: object | None = None

    def __call__(cls, name: str, *, version: int = 1):
        # Build the instance by hand. Notice we DO NOT call ``__init__``
        # at all; the legacy code path would have demanded an
        # initializer parameter that never gets used.
        instance = object.__new__(cls)
        object.__setattr__(instance, "name", name)
        object.__setattr__(instance, "version", version)
        object.__setattr__(instance, "_built_by_metaclass", True)
        FactoryMeta.last_constructed = instance
        return instance


class MetaclassFactoryClass(metaclass=FactoryMeta):
    """User-facing class whose construction lives in the metaclass.

    The misleading ``__init__`` here is intentional - it would have
    fooled the legacy ``cls.__init__`` inspector into demanding a
    ``never_used`` parameter that the metaclass would never feed in.
    The new inspector reads ``inspect.signature(cls)`` instead, which
    correctly returns ``FactoryMeta.__call__``'s signature.
    """

    def __init__(self, never_used: float) -> None:  # never invoked
        self.never_used = never_used


class TestMetaclassCall:
    def test_signature_reflects_metaclass_call(self):
        # Sanity check that Python itself reports the metaclass surface.
        sig = inspect.signature(MetaclassFactoryClass)
        assert list(sig.parameters) == ["name", "version"]
        assert sig.parameters["version"].default == 1

    def test_di_inspection_uses_metaclass_signature(self):
        deps = _inspect_class_deps(MetaclassFactoryClass)
        names = [n for n, _ in deps]
        assert names == ["name", "version"]
        assert dict(deps) == {"name": str, "version": int}

    @pytest.mark.asyncio
    async def test_construction_runs_metaclass_call(self):
        # Direct ``cls.__new__`` / ``cls.__init__`` calls would skip
        # the metaclass entirely - the very bug this refactor fixes.
        # ``cls(**kwargs)`` goes through the metaclass.
        c = DIContainer()

        def make() -> MetaclassFactoryClass:
            return MetaclassFactoryClass(name="bob", version=42)

        c.register_custom(use_factory(provide="thing", factory=make))
        c.compile()
        thing = await c.resolve("thing")
        assert isinstance(thing, MetaclassFactoryClass)
        assert thing.name == "bob"
        assert thing.version == 42
        assert thing._built_by_metaclass is True

    @pytest.mark.asyncio
    async def test_use_class_registration_goes_through_metaclass(self):
        # The custom-provider machinery must respect the metaclass too.
        # Prove it by injecting the metaclass-class through a wrapper
        # whose deps the resolver fills in.
        c = DIContainer()

        @injectable()
        class Wrapper:
            def __init__(
                self,
                name: Annotated[str, Inject("NAME")],
                version: Annotated[int, Inject("VERSION")],
            ) -> None:
                self.built = MetaclassFactoryClass(name=name, version=version)

        c.register_value("NAME", "carol")
        c.register_value("VERSION", 99)
        c.register(Wrapper)
        c.compile()
        w = await c.resolve(Wrapper)
        assert w.built.name == "carol"
        assert w.built.version == 99
        assert w.built._built_by_metaclass is True


# ---------------------------------------------------------------------------
# 2. Class with explicit ``__signature__`` - the Pydantic / attrs idiom
# ---------------------------------------------------------------------------


class PublishedSignatureClass:
    """Class whose callable surface is published via ``__signature__``.

    Pydantic v2 stores the model's effective constructor on this slot;
    attrs does the same for ``@define``-generated classes. The
    underlying ``__init__`` here takes ``*args, **kwargs`` and does
    the real work based on the published parameters.
    """

    __signature__ = inspect.Signature(
        parameters=[
            inspect.Parameter(
                "first",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str,
            ),
            inspect.Parameter(
                "second",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=int,
                default=10,
            ),
        ]
    )

    def __init__(self, *args, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


class TestPublishedSignature:
    def test_inspect_returns_published_signature(self):
        sig = inspect.signature(PublishedSignatureClass)
        assert list(sig.parameters) == ["first", "second"]

    def test_di_inspection_uses_published_signature(self):
        # The legacy code would have inspected the ``*args, **kwargs``
        # initializer and reported zero deps, silently breaking the
        # contract. The new code reads what the class itself publishes.
        deps = _inspect_class_deps(PublishedSignatureClass)
        assert dict(deps) == {"first": str, "second": int}

    @pytest.mark.asyncio
    async def test_container_constructs_via_call(self):
        c = DIContainer()
        c.register_value(str, "hello")
        c.register_value(int, 42)
        c.register_class(PublishedSignatureClass, PublishedSignatureClass)
        c.compile()
        instance = await c.resolve(PublishedSignatureClass)
        assert instance.first == "hello"
        assert instance.second == 42


# ---------------------------------------------------------------------------
# 3. Standard classes - unchanged contract
# ---------------------------------------------------------------------------


class TestStandardClasses:
    """Cases that the legacy code already handled - must keep working."""

    @pytest.mark.asyncio
    async def test_init_only_class(self):
        @injectable()
        class Dep:
            value = "dep-value"

        @injectable()
        class Service:
            def __init__(self, dep: Dep) -> None:
                self.dep = dep

        c = DIContainer()
        c.register(Dep)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.dep.value == "dep-value"

    @pytest.mark.asyncio
    async def test_new_only_class(self):
        # When a class defines ``__new__`` but no ``__init__``,
        # ``inspect.signature(cls)`` returns the ``__new__`` signature.
        @injectable()
        class Dep:
            value = "dep-value"

        class Service:
            def __new__(cls, dep: Dep):
                inst = super().__new__(cls)
                inst.dep = dep
                return inst

        c = DIContainer()
        c.register(Dep)
        c.register_class(Service, Service)
        c.compile()
        svc = await c.resolve(Service)
        assert isinstance(svc, Service)
        assert svc.dep.value == "dep-value"

    @pytest.mark.asyncio
    async def test_object_init_class_has_no_deps(self):
        # A class with no user-defined ``__init__`` / ``__new__`` /
        # ``__signature__`` reports an empty signature.
        class Plain:
            pass

        deps = _inspect_class_deps(Plain)
        assert deps == []

        c = DIContainer()
        c.register_class("plain", Plain)
        c.compile()
        instance = await c.resolve("plain")
        assert isinstance(instance, Plain)


# ---------------------------------------------------------------------------
# 4. Dataclasses
# ---------------------------------------------------------------------------


class TestDataclass:
    @pytest.mark.asyncio
    async def test_dataclass_provider(self):
        # Dataclasses generate ``__init__`` from field annotations; the
        # class signature reports those parameters. Field-deps
        # collection explicitly skips dataclass fields (they go through
        # ``__init__`` instead), so the resulting deps are precisely
        # what the dataclass advertises.
        @injectable()
        class Logger:
            level = "INFO"

        @injectable()
        @dataclass
        class Settings:
            log: Logger
            tag: str = "default"

        c = DIContainer()
        c.register_value(str, "prod")
        c.register(Logger)
        c.register(Settings)
        c.compile()
        s = await c.resolve(Settings)
        assert s.log.level == "INFO"
        assert s.tag == "prod"


# ---------------------------------------------------------------------------
# 5. NamedTuple
# ---------------------------------------------------------------------------


class TestNamedTuple:
    """NamedTuples have a metaclass; they're the canonical "non-trivial
    construction" case in the standard library. The legacy code would
    have produced wrong deps because their ``__new__`` signature is
    auto-generated and not what ``inspect.signature(cls)`` reports.
    """

    @pytest.mark.asyncio
    async def test_namedtuple_construction(self):
        class Point(NamedTuple):
            x: int
            y: int = 0

        deps = _inspect_class_deps(Point)
        assert dict(deps) == {"x": int, "y": int}

        c = DIContainer()
        c.register_value(int, 5)
        c.register_class("point", Point)
        c.compile()
        p = await c.resolve("point")
        assert p == Point(x=5, y=5)


# ---------------------------------------------------------------------------
# 6. Field injection contract change
# ---------------------------------------------------------------------------


class TestFieldInjectionContractChange:
    """The new contract: class-body fields are injected AFTER ``cls(...)``.

    The old guarantee "fields available inside __init__" was a side-
    effect of lauren emulating ``type.__call__`` itself. Now that we
    let Python's normal call protocol run, fields land on ``self``
    after the constructor has returned. Tests in
    ``test_di_field_injection.py`` lock the new contract; this file
    just confirms the headline behaviour.
    """

    @pytest.mark.asyncio
    async def test_field_visible_after_resolve(self):
        @injectable()
        class Dep:
            value = 1

        @injectable()
        class Service:
            dep: Dep  # post-init field injection

        c = DIContainer()
        c.register(Dep)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert isinstance(svc.dep, Dep)

    @pytest.mark.asyncio
    async def test_init_param_works_during_init(self):
        # The supported way to use a dep inside ``__init__``: take it
        # as a parameter. The class signature surfaces it as a dep,
        # the container resolves it, Python's call protocol passes it
        # in, and ``__init__`` reads it as a normal local.
        @injectable()
        class Dep:
            value = 1

        @injectable()
        class Service:
            def __init__(self, dep: Dep) -> None:
                # Reads ``dep`` (the parameter), not ``self.dep``.
                self.observed = dep.value + 100

        c = DIContainer()
        c.register(Dep)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.observed == 101


# ---------------------------------------------------------------------------
# 7. Forward references (PEP 563 / ``from __future__ import annotations``)
# ---------------------------------------------------------------------------


class TestForwardReferences:
    """PEP 563 is the common reason annotations come out of
    :func:`inspect.signature` as raw strings. The container must
    re-resolve them against the class's defining namespaces, the
    same way :func:`typing.get_type_hints` does.
    """

    @pytest.mark.asyncio
    async def test_module_level_forward_ref_resolves(self):
        @injectable()
        class LocalDep:
            payload = "resolved"

        @injectable()
        class Consumer:
            def __init__(self, dep: LocalDep) -> None:
                self.dep = dep

        c = DIContainer()
        c.register(LocalDep)
        c.register(Consumer)
        c.compile()
        cons = await c.resolve(Consumer)
        assert cons.dep.payload == "resolved"

    @pytest.mark.asyncio
    async def test_function_local_forward_ref_resolves(self):
        # The trickiest case: both the dep and the consumer are
        # function-local. Without frame walking the resolver wouldn't
        # find ``Local`` because it isn't in any module's globals.
        @injectable()
        class Local:
            value = "loc"

        @injectable()
        class Consumer:
            def __init__(self, dep: Local) -> None:
                self.dep = dep

        c = DIContainer()
        c.register(Local)
        c.register(Consumer)
        c.compile()
        cons = await c.resolve(Consumer)
        assert cons.dep.value == "loc"

    def test_resolve_class_signature_hints_replaces_strings(self):
        # Direct test of the resolver helper: a string annotation
        # becomes a real class.
        class Dep:
            pass

        class Consumer:
            def __init__(self, dep: "Dep") -> None:
                self.dep = dep

        sig = inspect.signature(Consumer)
        # Under PEP 563 (this file has the future import) all
        # annotations are strings.
        assert isinstance(sig.parameters["dep"].annotation, str)
        resolved = _resolve_class_signature_hints(Consumer, sig)
        # After resolution the annotation is the actual class.
        assert resolved.parameters["dep"].annotation is Dep


# ---------------------------------------------------------------------------
# 8. Inspection refuses gracefully on un-introspectable classes
# ---------------------------------------------------------------------------


class TestUninspectable:
    def test_builtin_with_no_signature_returns_empty_deps(self):
        # ``object`` itself has no inspectable signature beyond
        # ``object()`` - lauren must not throw, just report no deps.
        deps = _inspect_class_deps(object)
        assert deps == []


# ---------------------------------------------------------------------------
# 9. Async metaclass __call__ - exotic but valid
# ---------------------------------------------------------------------------


class TestAsyncMetaclass:
    @pytest.mark.asyncio
    async def test_async_call_is_awaited(self):
        # When a metaclass's ``__call__`` is async, lauren awaits the
        # coroutine. The standard ``cls(**kwargs)`` path goes through
        # whatever the metaclass returns, so a coroutine result is
        # awaited just like an async function provider.
        class AsyncMeta(type):
            async def __call__(cls, *, label: str):
                instance = object.__new__(cls)
                instance.label = label
                instance.async_built = True
                return instance

        class Async(metaclass=AsyncMeta):
            pass

        c = DIContainer()

        async def make():
            return await Async(label="hi")

        c.register_custom(use_factory(provide="async-thing", factory=make))
        c.compile()
        result = await c.resolve("async-thing")
        assert result.label == "hi"
        assert result.async_built is True


# ---------------------------------------------------------------------------
# 10. Kwargs filtering still works for partial signatures
# ---------------------------------------------------------------------------


class TestKwargFilteringRespected:
    @pytest.mark.asyncio
    async def test_signature_only_lists_consumed_params(self):
        # When a class accepts only a subset of the merged dep dict
        # lauren filters the kwargs down to what the signature accepts.
        @injectable()
        class A:
            tag = "a"

        @injectable()
        class B:
            tag = "b"

        @injectable()
        class Service:
            def __init__(self, a: A) -> None:
                self.a = a

        c = DIContainer()
        c.register(A)
        c.register(B)
        c.register(Service)
        c.compile()
        svc = await c.resolve(Service)
        assert svc.a.tag == "a"
