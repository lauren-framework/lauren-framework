"""Unit tests for the DI container."""

# NOTE: we intentionally do NOT use `from __future__ import annotations` here
# because several tests define classes inside test methods whose annotations
# reference each other. PEP 563 stringified annotations cannot be resolved by
# get_type_hints in a nested scope.

from typing import Protocol, runtime_checkable

import pytest

from lauren import Scope, injectable, post_construct, pre_destruct
from lauren._di import DIContainer
from lauren.exceptions import (
    CircularDependencyError,
    DIScopeViolationError,
    DuplicateBindingError,
    MetadataInheritanceError,
    MissingProviderError,
    ProtocolAmbiguityError,
    UnresolvableParameterError,
)


# ---------------------------------------------------------------------------
# Basic registration & resolution
# ---------------------------------------------------------------------------


@injectable()
class ServiceA:
    def __init__(self):
        self.val = "A"


@injectable()
class ServiceB:
    def __init__(self, a: ServiceA):
        self.a = a


class TestBasicDI:
    @pytest.mark.asyncio
    async def test_register_and_resolve(self):
        c = DIContainer()
        c.register(ServiceA)
        c.compile()
        instance = await c.resolve(ServiceA)
        assert isinstance(instance, ServiceA)

    @pytest.mark.asyncio
    async def test_dependency_injection(self):
        c = DIContainer()
        c.register(ServiceA)
        c.register(ServiceB)
        c.compile()
        b = await c.resolve(ServiceB)
        assert isinstance(b.a, ServiceA)

    @pytest.mark.asyncio
    async def test_singleton_returns_same_instance(self):
        c = DIContainer()
        c.register(ServiceA)
        c.compile()
        x = await c.resolve(ServiceA)
        y = await c.resolve(ServiceA)
        assert x is y

    @pytest.mark.asyncio
    async def test_transient_returns_new_instance(self):
        @injectable(scope=Scope.TRANSIENT)
        class Trans:
            def __init__(self): ...

        c = DIContainer()
        c.register(Trans)
        c.compile()
        x = await c.resolve(Trans)
        y = await c.resolve(Trans)
        assert x is not y

    @pytest.mark.asyncio
    async def test_request_scope_caching(self):
        @injectable(scope=Scope.REQUEST)
        class Req:
            def __init__(self): ...

        c = DIContainer()
        c.register(Req)
        c.compile()
        cache = {}
        x = await c.resolve(Req, request_cache=cache)
        y = await c.resolve(Req, request_cache=cache)
        assert x is y
        other = {}
        z = await c.resolve(Req, request_cache=other)
        assert z is not x


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestDIErrors:
    def test_register_non_injectable(self):
        class Foo: ...

        c = DIContainer()
        with pytest.raises(MissingProviderError):
            c.register(Foo)

    def test_duplicate_binding(self):
        c = DIContainer()
        c.register(ServiceA)
        with pytest.raises(DuplicateBindingError):
            c.register(ServiceA)

    def test_missing_dependency(self):
        @injectable()
        class Dangling:
            def __init__(self, missing: ServiceA): ...

        c = DIContainer()
        c.register(Dangling)  # ServiceA not registered
        with pytest.raises(MissingProviderError):
            c.compile()

    def test_circular_dependency(self):
        # Build two classes then register them; their __init__ annotations
        # reference each other forming a cycle.
        class X: ...

        class Y: ...

        def x_init(self, y: Y): ...
        def y_init(self, x: X): ...

        x_init.__annotations__ = {"y": Y}
        y_init.__annotations__ = {"x": X}
        X.__init__ = x_init  # type: ignore[method-assign]
        Y.__init__ = y_init  # type: ignore[method-assign]
        injectable()(X)
        injectable()(Y)

        c = DIContainer()
        c.register(X)
        c.register(Y)
        with pytest.raises(CircularDependencyError):
            c.compile()

    def test_unresolvable_parameter(self):
        @injectable()
        class NoAnn:
            def __init__(self, x): ...  # no annotation, no default

        c = DIContainer()
        with pytest.raises(UnresolvableParameterError):
            c.register(NoAnn)

    def test_scope_violation(self):
        @injectable(scope=Scope.REQUEST)
        class Req:
            def __init__(self): ...

        @injectable(scope=Scope.SINGLETON)
        class Single:
            def __init__(self, r: Req): ...

        c = DIContainer()
        c.register(Req)
        c.register(Single)
        with pytest.raises(DIScopeViolationError):
            c.compile()

    def test_metadata_inheritance_forbidden(self):
        @injectable()
        class Base: ...

        class Derived(Base):
            pass

        c = DIContainer()
        with pytest.raises(MetadataInheritanceError):
            c.register(Derived)


# ---------------------------------------------------------------------------
# Protocol binding
# ---------------------------------------------------------------------------


@runtime_checkable
class EmailSender(Protocol):
    def send(self, to: str, msg: str) -> None: ...


class TestProtocolBinding:
    @pytest.mark.asyncio
    async def test_single_protocol_provider(self):
        @injectable(provides=[EmailSender])
        class SmtpSender:
            def send(self, to, msg): ...

        c = DIContainer()
        c.register(SmtpSender)
        c.compile()
        instance = await c.resolve(EmailSender)  # type: ignore[type-abstract]
        assert isinstance(instance, SmtpSender)

    def test_protocol_ambiguity(self):
        @injectable(provides=[EmailSender])
        class A:
            def send(self, to, msg): ...

        @injectable(provides=[EmailSender])
        class B:
            def send(self, to, msg): ...

        c = DIContainer()
        c.register(A)
        c.register(B)
        with pytest.raises(ProtocolAmbiguityError):
            c.compile()

    @pytest.mark.asyncio
    async def test_multi_binding_list(self):
        @injectable(provides=[EmailSender], multi=True)
        class A:
            def send(self, to, msg): ...

        @injectable(provides=[EmailSender], multi=True)
        class B:
            def send(self, to, msg): ...

        c = DIContainer()
        c.register(A)
        c.register(B)
        c.compile()
        results = await c.resolve(EmailSender)  # type: ignore[type-abstract]
        assert isinstance(results, list)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleMarkers:
    def test_post_construct_marker(self):
        @injectable()
        class Svc:
            @post_construct
            def init_hook(self): ...

        c = DIContainer()
        provider = c.register(Svc)
        assert provider.post_construct is not None

    def test_pre_destruct_marker(self):
        @injectable()
        class Svc:
            @pre_destruct
            async def cleanup(self): ...

        c = DIContainer()
        provider = c.register(Svc)
        assert provider.pre_destruct is not None
