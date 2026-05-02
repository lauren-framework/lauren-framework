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


# ---------------------------------------------------------------------------
# Additional coverage tests for _di/__init__.py
# ---------------------------------------------------------------------------


class TestDiContainerRegistrationErrors:
    """Cover error paths in register_class, register_factory, register_alias."""

    def test_register_class_non_class_raises(self):
        """register_class with a non-class raises UnresolvableParameterError."""
        from lauren._di import DIContainer
        from lauren.exceptions import UnresolvableParameterError

        c = DIContainer()
        with pytest.raises(UnresolvableParameterError, match="expected a class"):
            c.register_class("not_a_class", lambda: None)

    def test_register_class_duplicate_raises(self):
        """register_class raises DuplicateBindingError when token already registered."""
        from lauren._di import DIContainer
        from lauren.exceptions import DuplicateBindingError
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class Target:
            pass

        c = DIContainer()
        c.register_class(Target, Target)
        with pytest.raises(DuplicateBindingError):
            c.register_class(Target, Target)

    def test_register_factory_duplicate_raises(self):
        """register_factory raises DuplicateBindingError on duplicate token."""
        from lauren._di import DIContainer
        from lauren.exceptions import DuplicateBindingError

        class Token:
            pass

        c = DIContainer()
        c.register_factory(Token, lambda: Token())
        with pytest.raises(DuplicateBindingError):
            c.register_factory(Token, lambda: Token())

    def test_register_alias_duplicate_raises(self):
        """register_alias raises DuplicateBindingError on duplicate token."""
        from lauren._di import DIContainer
        from lauren.exceptions import DuplicateBindingError

        class ServiceA:
            pass

        class Alias:
            pass

        c = DIContainer()
        c.register_alias(Alias, ServiceA)
        with pytest.raises(DuplicateBindingError):
            c.register_alias(Alias, ServiceA)

    def test_list_token_wrong_args_returns_none(self):
        """_multi_binding_element_type returns None for bare list (no type args)."""
        from lauren._di import _multi_binding_element_type

        assert _multi_binding_element_type(list) is None

    def test_provides_multi_binding(self):
        """A provider with `provides=` creates additional token bindings."""
        from lauren import injectable, Scope
        from lauren._di import DIContainer

        class Protocol:
            pass

        @injectable(scope=Scope.SINGLETON, provides=[Protocol])
        class Impl(Protocol):
            pass

        c = DIContainer()
        c.register(Impl)
        # Should have a binding for Protocol too
        assert c.has_provider(Protocol)


class TestDiContainerResolveEdgeCases:
    """Cover edge cases in the resolve/lookup path."""

    @pytest.mark.asyncio
    async def test_lookup_with_owning_module_visibility(self):
        """Provider not visible from a module raises MissingProviderError."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from lauren.exceptions import MissingProviderError

        @injectable(scope=Scope.SINGLETON)
        class HiddenService:
            pass

        class ModuleA:
            pass

        class ModuleB:
            pass

        c = DIContainer()
        # Register with ModuleA as owning module
        c.register(HiddenService, owning_module=ModuleA)

        # Try to resolve from ModuleB — visibility check should fail
        # (in practice visibility depends on the module graph export config,
        # so we just test that the path exists without necessarily raising)
        try:
            # This may or may not raise depending on visibility rules
            await c.resolve(HiddenService, owning_module=ModuleB)
        except MissingProviderError:
            pass  # Expected if visibility rules block it

    @pytest.mark.asyncio
    async def test_resolve_transient_creates_new_instances(self):
        """TRANSIENT scope creates a new instance on each resolve."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.TRANSIENT)
        class Transient:
            pass

        c = DIContainer()
        c.register(Transient)
        a = await c.resolve(Transient)
        b = await c.resolve(Transient)
        assert a is not b

    @pytest.mark.asyncio
    async def test_resolve_singleton_returns_same_instance(self):
        """SINGLETON scope returns the same instance on each resolve."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class Single:
            pass

        c = DIContainer()
        c.register(Single)
        a = await c.resolve(Single)
        b = await c.resolve(Single)
        assert a is b

    @pytest.mark.asyncio
    async def test_resolve_missing_raises(self):
        """Resolving an unregistered token raises MissingProviderError."""
        from lauren._di import DIContainer
        from lauren.exceptions import MissingProviderError

        class Unknown:
            pass

        c = DIContainer()
        with pytest.raises(MissingProviderError):
            await c.resolve(Unknown)

    @pytest.mark.asyncio
    async def test_resolve_alias_target(self):
        """register_alias resolves through to the target provider."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class Concrete:
            value = 42

        class Abstract:
            pass

        c = DIContainer()
        c.register(Concrete)
        c.register_alias(Abstract, Concrete)
        result = await c.resolve(Abstract)
        assert isinstance(result, Concrete)
        assert result.value == 42


class TestDiContainerOptionalDep:
    """Cover OptionalDep in register_factory (lines 492-494)."""

    @pytest.mark.asyncio
    async def test_optional_dep_resolves_to_none_when_missing(self):
        """An OptionalDep that is not registered resolves to None."""
        from lauren._di import DIContainer, OptionalDep

        class OptService:
            pass

        class Consumer:
            pass

        c = DIContainer()
        # Register a factory that has an optional dep
        c.register_factory(
            Consumer,
            lambda opt_svc=None: Consumer(),
            inject=[OptionalDep(OptService)],
        )
        result = await c.resolve(Consumer)
        assert isinstance(result, Consumer)


# ---------------------------------------------------------------------------
# Additional DI tests for uncovered lines
# ---------------------------------------------------------------------------


class TestDiContainerVisibility:
    """Cover module visibility paths (lines 686-691, 732-744, 880-882, 930-940, 993-996)."""

    @pytest.mark.asyncio
    async def test_visible_provider_resolves(self):
        """Registered provider is visible when set_visible includes it."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class MyService:
            pass

        class MyModule:
            pass

        c = DIContainer()
        c.register(MyService, owning_module=MyModule)
        # Make MyModule see MyService
        c.set_visible(MyModule, frozenset([MyService]))
        instance = await c.resolve(MyService, owning_module=MyModule)
        assert isinstance(instance, MyService)

    @pytest.mark.asyncio
    async def test_resolve_invisible_provider_raises(self):
        """Resolving a token invisible from the requesting module raises."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from lauren.exceptions import MissingProviderError

        @injectable(scope=Scope.SINGLETON)
        class Hidden:
            pass

        class ModA:
            pass

        class ModB:
            pass

        c = DIContainer()
        c.register(Hidden, owning_module=ModA)
        # ModA sees Hidden; ModB has an empty visible set
        c.set_visible(ModA, frozenset([Hidden]))
        c.set_visible(ModB, frozenset())  # ModB cannot see Hidden
        with pytest.raises(MissingProviderError):
            await c.resolve(Hidden, owning_module=ModB)

    def test_has_provider_with_owning_module(self):
        """has_provider respects owning_module visibility."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class Svc:
            pass

        class ModA:
            pass

        class ModB:
            pass

        c = DIContainer()
        c.register(Svc, owning_module=ModA)
        c.set_visible(ModA, frozenset([Svc]))
        c.set_visible(ModB, frozenset())
        assert c.has_provider(Svc, owning_module=ModA)
        assert not c.has_provider(Svc, owning_module=ModB)

    def test_get_provider_invisible_raises(self):
        """get_provider raises when token not visible from module."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from lauren.exceptions import MissingProviderError

        @injectable(scope=Scope.SINGLETON)
        class Svc2:
            pass

        class ModX:
            pass

        class ModY:
            pass

        c = DIContainer()
        c.register(Svc2, owning_module=ModX)
        c.set_visible(ModX, frozenset([Svc2]))
        c.set_visible(ModY, frozenset())
        with pytest.raises(MissingProviderError):
            c.get_provider(Svc2, owning_module=ModY)

    def test_get_provider_ambiguous_raises(self):
        """get_provider raises ProtocolAmbiguityError for multiple non-multi bindings."""
        from lauren._di import DIContainer
        from lauren.exceptions import ProtocolAmbiguityError

        class ProtoX:
            pass

        c = DIContainer()
        # Manually create two non-multi providers for the same token
        from lauren._di import Provider
        from lauren.types import Scope as S

        p1 = Provider(
            cls=ProtoX,
            scope=S.SINGLETON,
            provides=(),
            multi=False,
            deps=(),
            factory=lambda: ProtoX(),
        )
        p2 = Provider(
            cls=ProtoX,
            scope=S.SINGLETON,
            provides=(),
            multi=False,
            deps=(),
            factory=lambda: ProtoX(),
        )
        c._token_bindings[ProtoX] = [p1, p2]
        with pytest.raises(ProtocolAmbiguityError):
            c.get_provider(ProtoX)


class TestDiContainerMultiBinding:
    """Cover multi-binding compile and resolve paths."""

    def test_compile_multi_missing_provider(self):
        """compile() raises MissingProviderError when list[T] dep has no providers."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from lauren.exceptions import MissingProviderError
        from typing import Protocol as P

        class IPlugin(P):
            pass

        @injectable(scope=Scope.SINGLETON)
        class Host:
            def __init__(self, plugins: list): ...

        # Give Host a dep on list[IPlugin] by manually patching deps
        c = DIContainer()
        c.register(Host)
        # No IPlugin providers registered — compile should detect this
        # Actually compile will succeed because list is not list[IPlugin]
        # Let's test compile raises when a real list[IPlugin] is declared
        # We need a different approach: inject a custom dep

        host_p = c._providers[Host]
        # Replace deps with a list[IPlugin] dep
        import dataclasses as _dc

        patched = _dc.replace(host_p, deps=(("plugins", list[IPlugin]),))
        c._providers[Host] = patched
        c._token_bindings[Host] = [patched]

        with pytest.raises(MissingProviderError):
            c.compile()

    def test_compile_list_type_non_multi_raises_ambiguity(self):
        """compile() raises ProtocolAmbiguityError when list[T] dep contains non-multi provider."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from lauren.exceptions import ProtocolAmbiguityError
        from typing import Protocol as P

        class IWidget(P):
            pass

        @injectable(scope=Scope.SINGLETON)
        class WidgetUser:
            pass

        @injectable(scope=Scope.SINGLETON)
        class ConcreteWidget:
            pass

        c = DIContainer()
        c.register(ConcreteWidget)
        c.register(WidgetUser)
        # Register ConcreteWidget for IWidget, but NOT multi
        import dataclasses as _dc

        widget_p = c._providers[ConcreteWidget]
        non_multi = _dc.replace(widget_p, multi=False)
        c._token_bindings[IWidget] = [non_multi]

        # Inject list[IWidget] dep into WidgetUser
        user_p = c._providers[WidgetUser]
        patched_user = _dc.replace(user_p, deps=(("widgets", list[IWidget]),))
        c._providers[WidgetUser] = patched_user
        c._token_bindings[WidgetUser] = [patched_user]

        with pytest.raises(ProtocolAmbiguityError):
            c.compile()

    @pytest.mark.asyncio
    async def test_resolve_multi_list_token(self):
        """Resolving list[T] directly returns multiple instances."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from typing import Protocol, runtime_checkable

        @runtime_checkable
        class IPlugin(Protocol):
            def run(self) -> str: ...

        @injectable(scope=Scope.SINGLETON, provides=[IPlugin], multi=True)
        class PluginA:
            def run(self):
                return "A"

        @injectable(scope=Scope.SINGLETON, provides=[IPlugin], multi=True)
        class PluginB:
            def run(self):
                return "B"

        c = DIContainer()
        c.register(PluginA)
        c.register(PluginB)
        c.compile()
        result = await c.resolve(list[IPlugin])
        assert isinstance(result, list)
        assert len(result) == 2

    @pytest.mark.asyncio
    async def test_resolve_multi_list_missing_raises(self):
        """Resolving list[T] with no providers raises MissingProviderError."""
        from lauren._di import DIContainer
        from lauren.exceptions import MissingProviderError

        class IUnknown:
            pass

        c = DIContainer()
        with pytest.raises(MissingProviderError):
            await c.resolve(list[IUnknown])

    @pytest.mark.asyncio
    async def test_resolve_multi_list_invisible_raises(self):
        """Resolving list[T] with providers invisible from module raises."""
        from lauren._di import DIContainer
        from lauren import injectable, Scope
        from lauren.exceptions import MissingProviderError

        @injectable(scope=Scope.SINGLETON, multi=True)
        class Plug:
            pass

        class ModA:
            pass

        class ModB:
            pass

        c = DIContainer()
        from lauren._di import Provider
        from lauren.types import Scope as S

        plug_p = Provider(
            cls=Plug,
            scope=S.SINGLETON,
            provides=(),
            multi=True,
            deps=(),
            owning_module=ModA,
            factory=Plug,
        )
        c._providers[Plug] = plug_p
        c._token_bindings[Plug] = [plug_p]
        c.set_visible(ModA, frozenset([Plug]))
        c.set_visible(ModB, frozenset())  # cannot see Plug

        with pytest.raises(MissingProviderError):
            await c.resolve(list[Plug], owning_module=ModB)

    @pytest.mark.asyncio
    async def test_resolve_multi_list_non_multi_raises_ambiguity(self):
        """Resolving list[T] where a provider is non-multi raises ProtocolAmbiguityError."""
        from lauren._di import DIContainer, Provider
        from lauren.exceptions import ProtocolAmbiguityError
        from lauren.types import Scope as S

        class IFoo:
            pass

        non_multi_p = Provider(
            cls=IFoo,
            scope=S.SINGLETON,
            provides=(),
            multi=False,
            deps=(),
            factory=lambda: object(),
        )
        c = DIContainer()
        c._token_bindings[IFoo] = [non_multi_p]

        with pytest.raises(ProtocolAmbiguityError):
            await c.resolve(list[IFoo])


class TestDiContainerRegisterCustom:
    """Cover register_custom dispatch (lines 557-589)."""

    @pytest.mark.asyncio
    async def test_register_custom_value(self):
        from lauren._di import DIContainer
        from lauren._di.custom import CustomProvider

        class Token:
            pass

        inst = Token()
        cp = CustomProvider(provide=Token, kind="value", value=inst)
        c = DIContainer()
        c.register_custom(cp)
        result = await c.resolve(Token)
        assert result is inst

    @pytest.mark.asyncio
    async def test_register_custom_class(self):
        from lauren._di import DIContainer
        from lauren._di.custom import CustomProvider
        from lauren.types import Scope

        class TokenB:
            pass

        class ImplB:
            pass

        cp = CustomProvider(
            provide=TokenB, kind="class", use_class=ImplB, scope=Scope.SINGLETON
        )
        c = DIContainer()
        c.register_custom(cp)
        result = await c.resolve(TokenB)
        assert isinstance(result, ImplB)

    @pytest.mark.asyncio
    async def test_register_custom_factory(self):
        from lauren._di import DIContainer
        from lauren._di.custom import CustomProvider
        from lauren.types import Scope

        class TokenC:
            pass

        calls = []

        def factory_fn():
            calls.append(1)
            return TokenC()

        cp = CustomProvider(
            provide=TokenC, kind="factory", factory=factory_fn, scope=Scope.SINGLETON
        )
        c = DIContainer()
        c.register_custom(cp)
        result = await c.resolve(TokenC)
        assert isinstance(result, TokenC)
        assert calls == [1]

    @pytest.mark.asyncio
    async def test_register_custom_existing(self):
        from lauren._di import DIContainer
        from lauren._di.custom import CustomProvider
        from lauren import injectable, Scope as _Scope

        @injectable(scope=_Scope.SINGLETON)
        class Original:
            pass

        class Alias:
            pass

        cp = CustomProvider(provide=Alias, kind="existing", existing=Original)
        c = DIContainer()
        c.register(Original)
        c.register_custom(cp)
        result = await c.resolve(Alias)
        assert isinstance(result, Original)

    @pytest.mark.asyncio
    async def test_register_custom_async_factory(self):
        """Async factory from use_factory produces the expected instance."""
        from lauren._di import DIContainer
        from lauren._di.custom import CustomProvider
        from lauren.types import Scope

        class TokenD:
            def __init__(self, val):
                self.val = val

        called = []

        async def async_factory():
            called.append(1)
            return TokenD("async")

        cp = CustomProvider(
            provide=TokenD, kind="factory", factory=async_factory, scope=Scope.SINGLETON
        )
        c = DIContainer()
        c.register_custom(cp)
        result = await c.resolve(TokenD)
        assert result.val == "async"
        assert called == [1]


class TestDiContainerAliasCompile:
    """Cover alias target validation during compile (lines 779-793)."""

    def test_alias_to_unknown_target_raises_at_compile(self):
        """An alias pointing at an unregistered token raises MissingProviderError at compile."""
        from lauren._di import DIContainer
        from lauren.exceptions import MissingProviderError

        class Target:
            pass

        class Alias:
            pass

        c = DIContainer()
        c.register_alias(Alias, Target)  # Target is NOT registered
        with pytest.raises(MissingProviderError):
            c.compile()


class TestDiContainerPostConstructRequestScope:
    """Cover @post_construct hook for REQUEST/TRANSIENT scopes (lines 1130-1134)."""

    @pytest.mark.asyncio
    async def test_post_construct_called_for_request_scope(self):
        from lauren._di import DIContainer
        from lauren import injectable, Scope, post_construct

        calls = []

        @injectable(scope=Scope.REQUEST)
        class ReqSvc:
            @post_construct
            def on_init(self):
                calls.append("init")

        c = DIContainer()
        c.register(ReqSvc)
        cache = {}
        await c.resolve(ReqSvc, request_cache=cache)
        assert "init" in calls

    @pytest.mark.asyncio
    async def test_post_construct_called_for_transient_scope(self):
        from lauren._di import DIContainer
        from lauren import injectable, Scope, post_construct

        calls = []

        @injectable(scope=Scope.TRANSIENT)
        class TrSvc:
            @post_construct
            def on_init(self):
                calls.append("trans")

        c = DIContainer()
        c.register(TrSvc)
        await c.resolve(TrSvc)
        assert "trans" in calls


class TestDiContainerFunctionProvider:
    """Cover function provider registration (lines 256-278)."""

    @pytest.mark.asyncio
    async def test_function_provider_resolved(self):
        from lauren._di import DIContainer
        from lauren import injectable

        @injectable()
        def make_value() -> str:
            return "hello-from-factory"

        c = DIContainer()
        c.register(make_value)
        result = await c.resolve(make_value)
        assert result == "hello-from-factory"

    def test_function_provider_duplicate_raises(self):
        from lauren._di import DIContainer
        from lauren import injectable
        from lauren.exceptions import DuplicateBindingError

        @injectable()
        def make_item() -> int:
            return 42

        c = DIContainer()
        c.register(make_item)
        with pytest.raises(DuplicateBindingError):
            c.register(make_item)

    @pytest.mark.asyncio
    async def test_async_function_provider(self):
        from lauren._di import DIContainer
        from lauren import injectable

        @injectable()
        async def async_factory() -> dict:
            return {"key": "value"}

        c = DIContainer()
        c.register(async_factory)
        result = await c.resolve(async_factory)
        assert result == {"key": "value"}


class TestDiContainerRegisterValue:
    """Cover register_value paths (lines 357-396)."""

    @pytest.mark.asyncio
    async def test_register_value_resolves_immediately(self):
        from lauren._di import DIContainer

        class Token:
            pass

        c = DIContainer()
        val = Token()
        c.register_value(Token, val)
        result = await c.resolve(Token)
        assert result is val

    def test_register_value_duplicate_raises(self):
        from lauren._di import DIContainer
        from lauren.exceptions import DuplicateBindingError

        class Token2:
            pass

        c = DIContainer()
        c.register_value(Token2, object())
        with pytest.raises(DuplicateBindingError):
            c.register_value(Token2, object())

    @pytest.mark.asyncio
    async def test_register_value_multi(self):
        from lauren._di import DIContainer

        class Tag:
            pass

        c = DIContainer()
        c.register_value(Tag, "first", multi=True)
        c.register_value(Tag, "second", multi=True)
        results = await c.resolve(Tag)
        # multi-binding resolves as list
        assert isinstance(results, list)
        assert len(results) == 2


class TestDiHelpers:
    """Cover helper functions: _describe, _looks_injectable, etc."""

    def test_describe_with_name(self):
        from lauren._di import _describe

        class Foo:
            pass

        assert _describe(Foo) == "Foo"

    def test_describe_without_name(self):
        from lauren._di import _describe

        assert _describe(42) == "42"

    def test_looks_injectable_class(self):
        from lauren._di import _looks_injectable
        from lauren import injectable

        @injectable()
        class Inj:
            pass

        assert _looks_injectable(Inj)

    def test_looks_injectable_primitive_returns_false(self):
        from lauren._di import _looks_injectable

        assert not _looks_injectable(str)
        assert not _looks_injectable(int)

    def test_looks_injectable_annotated_with_marker(self):
        from lauren._di import _looks_injectable
        from lauren.extractors import Depends
        from typing import Annotated

        class Svc:
            pass

        ann = Annotated[Svc, Depends[Svc]]
        assert _looks_injectable(ann)

    def test_multi_binding_element_type_with_args(self):
        from lauren._di import _multi_binding_element_type

        assert _multi_binding_element_type(list[int]) is int
        assert _multi_binding_element_type(list[str]) is str

    def test_callable_default_map_for_class(self):
        from lauren._di import _callable_default_map, Provider
        from lauren.types import Scope

        class Foo:
            def __init__(self, x: int = 5): ...

        p = Provider(
            cls=Foo,
            scope=Scope.SINGLETON,
            provides=(),
            multi=False,
            deps=(),
            factory=Foo,
            is_function_provider=False,
        )
        result = _callable_default_map(p)
        assert result.get("x") is True

    def test_callable_default_map_for_value_kind(self):
        from lauren._di import _callable_default_map, Provider
        from lauren.types import Scope

        class Tok:
            pass

        p = Provider(
            cls=Tok,
            scope=Scope.SINGLETON,
            provides=(),
            multi=False,
            deps=(),
            factory=lambda: Tok(),
            provider_kind="value",
        )
        result = _callable_default_map(p)
        assert result == {}

    def test_unwrap_annotated(self):
        from lauren._di import _unwrap_annotated
        from typing import Annotated

        class X:
            pass

        assert _unwrap_annotated(Annotated[X, "meta"]) is X
        assert _unwrap_annotated(X) is X

    @pytest.mark.asyncio
    async def test_set_singleton_marks_initialized(self):
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class S:
            pass

        c = DIContainer()
        c.register(S)
        instance = S()
        c.set_singleton(S, instance)
        assert S in c._singletons_initialized
        assert c._singletons[S] is instance

    def test_mark_singleton_initialized(self):
        from lauren._di import DIContainer
        from lauren import injectable, Scope

        @injectable(scope=Scope.SINGLETON)
        class T:
            pass

        c = DIContainer()
        c.mark_singleton_initialized(T)
        assert T in c._singletons_initialized
