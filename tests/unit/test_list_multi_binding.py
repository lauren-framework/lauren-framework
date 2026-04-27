"""Unit tests for ``list[T]`` handler & DI parameter resolution.

Lauren already supports *multi-bindings* — multiple providers registered
for the same protocol with ``multi=True``. Resolving the bare protocol
token returns a list of every instance:

.. code-block:: python

    c.resolve(EmailSender)  # -> [Smtp(), Sms()]

This test module pins down the **type-correct** counterpart: declaring a
dependency as ``list[EmailSender]`` has to deliver the same list — and,
crucially, it has to do so for every injection site the framework
supports (``__init__`` parameters, class-body annotations, function
providers). The compiler must also refuse confusing registrations
(``list[T]`` when ``T`` has a single non-multi binding) rather than
silently wrapping a solo instance.

See :func:`lauren._di._multi_binding_element_type` for the exact shape
the container treats as a multi-binding request.
"""

from __future__ import annotations

from typing import List, Protocol, runtime_checkable

import pytest

from lauren import DIContainer, Scope, injectable
from lauren._di import _multi_binding_element_type
from lauren.exceptions import (
    CircularDependencyError,
    DIScopeViolationError,
    MissingProviderError,
    ProtocolAmbiguityError,
)


# ---------------------------------------------------------------------------
# The helper is the single truth source for "is this token ``list[T]``?"
# ---------------------------------------------------------------------------


class TestMultiBindingElementTypeHelper:
    """``_multi_binding_element_type`` isolates ``list[T]`` recognition."""

    def test_recognises_builtin_generic_list(self):
        assert _multi_binding_element_type(list[int]) is int

    def test_recognises_typing_List_generic(self):
        # ``typing.List[T]`` normalises to the same origin (``list``)
        # in Python 3.9+, so the helper must not care which spelling
        # the user reached for.
        assert _multi_binding_element_type(List[str]) is str

    def test_recognises_list_of_protocol(self):
        @runtime_checkable
        class P(Protocol):
            def go(self) -> None: ...

        assert _multi_binding_element_type(list[P]) is P

    def test_bare_list_has_no_element_type(self):
        assert _multi_binding_element_type(list) is None

    def test_other_generics_are_not_multi_binding(self):
        assert _multi_binding_element_type(dict[str, int]) is None
        assert _multi_binding_element_type(tuple[int, ...]) is None
        assert _multi_binding_element_type(set[int]) is None

    def test_plain_types_are_not_multi_binding(self):
        assert _multi_binding_element_type(int) is None
        assert _multi_binding_element_type(str) is None

        class Plain:
            pass

        assert _multi_binding_element_type(Plain) is None


# ---------------------------------------------------------------------------
# ``DIContainer.resolve(list[T])`` — the core resolver path.
# ---------------------------------------------------------------------------


@runtime_checkable
class Sender(Protocol):
    """Shared Protocol used across the resolver tests."""

    def name(self) -> str: ...


@injectable(provides=[Sender], multi=True)
class Smtp:
    def name(self) -> str:
        return "smtp"


@injectable(provides=[Sender], multi=True)
class Sms:
    def name(self) -> str:
        return "sms"


class TestDirectResolveListT:
    """``resolve(list[T])`` returns every multi-bound instance."""

    @pytest.mark.asyncio
    async def test_resolve_list_of_protocol_returns_all_multi_bindings(self):
        c = DIContainer()
        c.register(Smtp)
        c.register(Sms)
        c.compile()

        result = await c.resolve(list[Sender])  # type: ignore[type-abstract]

        assert isinstance(result, list)
        assert len(result) == 2
        names = sorted(s.name() for s in result)
        assert names == ["sms", "smtp"]

    @pytest.mark.asyncio
    async def test_resolve_preserves_registration_order(self):
        """Registration order is a user-visible contract — tests that
        rely on \"the first provider wins\" would silently break if we
        reordered the list."""
        c = DIContainer()
        c.register(Smtp)
        c.register(Sms)
        c.compile()

        names = [s.name() for s in await c.resolve(list[Sender])]  # type: ignore[type-abstract]
        assert names == ["smtp", "sms"]

        # Reverse the registration and re-check.
        c2 = DIContainer()
        c2.register(Sms)
        c2.register(Smtp)
        c2.compile()
        names2 = [s.name() for s in await c2.resolve(list[Sender])]  # type: ignore[type-abstract]
        assert names2 == ["sms", "smtp"]

    @pytest.mark.asyncio
    async def test_resolve_works_with_typing_List(self):
        c = DIContainer()
        c.register(Smtp)
        c.register(Sms)
        c.compile()

        result = await c.resolve(List[Sender])  # type: ignore[type-abstract]
        assert sorted(s.name() for s in result) == ["sms", "smtp"]

    @pytest.mark.asyncio
    async def test_single_multi_binding_still_returns_list(self):
        c = DIContainer()
        c.register(Smtp)  # only one, but registered with ``multi=True``
        c.compile()

        result = await c.resolve(list[Sender])  # type: ignore[type-abstract]
        assert result == [result[0]]
        assert isinstance(result, list)
        assert result[0].name() == "smtp"


# ---------------------------------------------------------------------------
# Error paths: the compiler rejects mis-shaped registrations early.
# ---------------------------------------------------------------------------


class TestListResolutionErrors:
    """``list[T]`` yields typed errors before runtime."""

    def test_unregistered_element_raises_missing_provider(self):
        @runtime_checkable
        class Nothing(Protocol):
            def x(self) -> None: ...

        c = DIContainer()
        c.compile()
        import asyncio

        with pytest.raises(MissingProviderError):
            asyncio.run(c.resolve(list[Nothing]))  # type: ignore[type-abstract]

    def test_single_non_multi_binding_for_list_raises(self):
        """A lone non-multi provider can be resolved as ``T`` directly,
        but asking for ``list[T]`` signals intent to collect multiple
        implementations — silently wrapping the sole binding would hide
        a registration mistake."""

        @runtime_checkable
        class LoneProto(Protocol):
            def x(self) -> None: ...

        @injectable(provides=[LoneProto])  # note: no multi=True
        class Only:
            def x(self) -> None: ...

        c = DIContainer()
        c.register(Only)
        c.compile()

        import asyncio

        with pytest.raises(ProtocolAmbiguityError) as excinfo:
            asyncio.run(c.resolve(list[LoneProto]))  # type: ignore[type-abstract]
        assert "multi=True" in str(excinfo.value)

    def test_mixed_multi_and_non_multi_raises_at_compile(self):
        """Mixing ``multi=True`` and non-multi providers for the same
        token is already an ambiguity error; the ``list[T]`` path
        surfaces it too."""

        @runtime_checkable
        class P(Protocol):
            def x(self) -> None: ...

        @injectable(provides=[P])
        class A:
            def x(self) -> None: ...

        @injectable(provides=[P], multi=True)
        class B:
            def x(self) -> None: ...

        c = DIContainer()
        c.register(A)
        c.register(B)
        with pytest.raises(ProtocolAmbiguityError):
            c.compile()

    def test_missing_provider_error_detail_is_actionable(self):
        """The error's ``detail`` carries the token name so callers can
        format a user-friendly message without re-parsing strings."""

        @runtime_checkable
        class Missing(Protocol):
            def x(self) -> None: ...

        c = DIContainer()
        c.compile()
        import asyncio

        with pytest.raises(MissingProviderError) as excinfo:
            asyncio.run(c.resolve(list[Missing]))  # type: ignore[type-abstract]
        detail = getattr(excinfo.value, "detail", {}) or {}
        assert "list" in detail.get("token", "")


# ---------------------------------------------------------------------------
# Constructor injection — ``__init__(self, deps: list[T])``.
# ---------------------------------------------------------------------------


class TestConstructorListInjection:
    """A class declaring ``list[T]`` in ``__init__`` gets every binding."""

    @pytest.mark.asyncio
    async def test_constructor_receives_all_multi_bindings(self):
        @injectable()
        class Dispatcher:
            def __init__(self, senders: list[Sender]) -> None:  # type: ignore[type-arg]
                self.senders = senders

        c = DIContainer()
        c.register(Smtp)
        c.register(Sms)
        c.register(Dispatcher)
        c.compile()

        d = await c.resolve(Dispatcher)
        assert len(d.senders) == 2
        assert {s.name() for s in d.senders} == {"smtp", "sms"}

    def test_compile_fails_when_list_dep_has_no_provider(self):
        @runtime_checkable
        class NoOne(Protocol):
            def x(self) -> None: ...

        @injectable()
        class Needer:
            def __init__(self, deps: list[NoOne]) -> None:  # type: ignore[type-arg]
                self.deps = deps

        c = DIContainer()
        c.register(Needer)
        with pytest.raises(MissingProviderError):
            c.compile()

    @pytest.mark.asyncio
    async def test_optional_list_dep_with_default_falls_back_gracefully(self):
        """A constructor parameter typed ``list[T] = <default>`` falls
        back to its default when no providers exist for ``T`` — the
        same escape hatch the container offers for any other optional
        dep (see :func:`_inspect_callable_deps`).

        This keeps the multi-binding feature consistent with the rest
        of the optional-deps story: users can declare "give me every
        plugin you find, but it's fine if there are none".
        """

        @runtime_checkable
        class Plug(Protocol):
            def x(self) -> None: ...

        @injectable()
        class App:
            def __init__(self, plugins: list[Plug] | None = None) -> None:  # type: ignore[type-arg]
                self.plugins = plugins or []

        c = DIContainer()
        c.register(App)
        c.compile()  # no plugins registered — still compiles

        instance = await c.resolve(App)
        assert instance.plugins == []


# ---------------------------------------------------------------------------
# Field injection — class-body ``senders: list[T]``.
# ---------------------------------------------------------------------------


class TestFieldListInjection:
    """Class-body ``list[T]`` annotations are DI fields."""

    @pytest.mark.asyncio
    async def test_class_body_list_annotation_is_injected(self):
        @injectable()
        class Dispatcher:
            senders: list[Sender]  # type: ignore[type-arg]

            def summary(self) -> list[str]:
                return [s.name() for s in self.senders]

        c = DIContainer()
        c.register(Smtp)
        c.register(Sms)
        c.register(Dispatcher)
        c.compile()

        d = await c.resolve(Dispatcher)
        assert set(d.summary()) == {"smtp", "sms"}

    def test_plain_data_list_field_is_not_mistaken_for_di(self):
        """``tags: list[str] = []`` is a plain-data field, not a DI
        dependency — the container must not touch it."""

        @injectable()
        class HasData:
            tags: list[str]  # ``str`` is not injectable

            def __init__(self) -> None:
                self.tags = []

        c = DIContainer()
        c.register(HasData)
        c.compile()  # no error — ``list[str]`` is not harvested


# ---------------------------------------------------------------------------
# Scope narrowing still applies per provider of the ``list[T]`` collection.
# ---------------------------------------------------------------------------


class TestListScopeNarrowing:
    """Each element of the list carries its own scope edge."""

    def test_singleton_consumer_rejects_request_scoped_multi_member(self):
        @runtime_checkable
        class P(Protocol):
            def x(self) -> None: ...

        @injectable(provides=[P], multi=True, scope=Scope.SINGLETON)
        class A:
            def x(self) -> None: ...

        @injectable(provides=[P], multi=True, scope=Scope.REQUEST)
        class B:  # request-scoped!
            def x(self) -> None: ...

        @injectable(scope=Scope.SINGLETON)
        class Aggregator:
            def __init__(self, items: list[P]) -> None:  # type: ignore[type-arg]
                self.items = items

        c = DIContainer()
        c.register(A)
        c.register(B)
        c.register(Aggregator)
        with pytest.raises(DIScopeViolationError) as excinfo:
            c.compile()
        detail = getattr(excinfo.value, "detail", {}) or {}
        # The offending member is the REQUEST-scoped B.
        assert detail.get("dependency_scope") == "request"
        assert detail.get("dependent_scope") == "singleton"

    def test_singleton_consumer_of_all_singleton_multi_is_ok(self):
        @runtime_checkable
        class P(Protocol):
            def x(self) -> None: ...

        @injectable(provides=[P], multi=True, scope=Scope.SINGLETON)
        class A:
            def x(self) -> None: ...

        @injectable(provides=[P], multi=True, scope=Scope.SINGLETON)
        class B:
            def x(self) -> None: ...

        @injectable(scope=Scope.SINGLETON)
        class Aggregator:
            def __init__(self, items: list[P]) -> None:  # type: ignore[type-arg]
                self.items = items

        c = DIContainer()
        c.register(A)
        c.register(B)
        c.register(Aggregator)
        c.compile()  # clean


# ---------------------------------------------------------------------------
# Module visibility — a consumer must only receive bindings its module
# can see.
# ---------------------------------------------------------------------------


class TestListModuleVisibility:
    """Visibility filtering applies per-element: invisible multi-bindings
    are excluded from the returned list."""

    @pytest.mark.asyncio
    async def test_visibility_gate_narrows_the_returned_list(self):
        from lauren import module

        @runtime_checkable
        class P(Protocol):
            def x(self) -> str: ...

        @injectable(provides=[P], multi=True)
        class Visible:
            def x(self) -> str:
                return "visible"

        @injectable(provides=[P], multi=True)
        class Hidden:
            def x(self) -> str:
                return "hidden"

        @module(providers=[Visible], exports=[P])
        class SharedModule:
            pass

        @module(providers=[Hidden])
        class SecretModule:
            pass

        # A consumer that imports ``SharedModule`` only sees ``Visible``.
        @injectable()
        class Consumer:
            items: list[P]  # type: ignore[type-arg]

        @module(providers=[Consumer], imports=[SharedModule])
        class ConsumerModule:
            pass

        # Hand-wire everything: build the container the way the module
        # compiler does, so we can assert on the visibility filter alone
        # without needing the whole app stack.
        c = DIContainer()
        # Register every provider and track owning module so visibility
        # narrowing is honoured.
        c.register(Visible, owning_module=SharedModule)
        c.register(Hidden, owning_module=SecretModule)
        c.register(Consumer, owning_module=ConsumerModule)
        # Module-level visibility: ``set_visible`` declares which tokens
        # a module can see. Consumer only imports SharedModule, so
        # Hidden is invisible to it.
        c.set_visible(ConsumerModule, frozenset({Consumer, Visible, P}))
        c.set_visible(SharedModule, frozenset({Visible, P}))
        c.set_visible(SecretModule, frozenset({Hidden, P}))
        c.compile()

        instance = await c.resolve(Consumer, owning_module=ConsumerModule)
        assert [s.x() for s in instance.items] == ["visible"]


# ---------------------------------------------------------------------------
# Cycle detection still works for list[T] deps.
# ---------------------------------------------------------------------------


class TestListCycleDetection:
    """Each member of a ``list[T]`` participates in cycle detection."""

    def test_cycle_through_list_dep_is_caught(self):
        """If a multi-bound provider in ``list[T]`` depends back on the
        consumer of ``list[T]``, the container must catch the cycle at
        compile time — not silently recurse forever or raise at
        request time."""

        @runtime_checkable
        class Listener(Protocol):
            def on(self) -> None: ...

        # Forward declarations are not needed because the cycle goes
        # through registration order; we just have to make ``A`` need
        # ``Hub`` which needs ``list[Listener]`` which contains ``A``.
        class _Box:
            Hub: type | None = None

        @injectable(provides=[Listener], multi=True)
        class A:
            # Constructor depends on the as-yet-undefined ``Hub`` class
            # via late binding through the ``_Box``.
            def __init__(self) -> None: ...

        @injectable()
        class Hub:
            def __init__(self, listeners: list[Listener]) -> None:  # type: ignore[type-arg]
                self.listeners = listeners

        # Now retrofit A to depend on Hub — that closes the cycle:
        # Hub -> list[Listener] -> A -> Hub.
        # original_init = A.__init__

        def cyclic_init(self, hub: Hub) -> None:  # type: ignore[no-redef]
            self.hub = hub

        A.__init__ = cyclic_init  # type: ignore[method-assign]
        A.__annotations__ = {"hub": Hub}
        # Refresh the dep collection by re-registering. We have to use
        # a fresh container because the prior registration captured
        # the old (empty) signature.
        c = DIContainer()
        c.register(A)
        c.register(Hub)
        with pytest.raises(CircularDependencyError):
            c.compile()


# ---------------------------------------------------------------------------
# Request-scope caching: each request sees stable list members.
# ---------------------------------------------------------------------------


class TestListRequestCaching:
    """Request-scoped multi-bindings share a cache within one request."""

    @pytest.mark.asyncio
    async def test_request_scoped_members_share_cache_in_one_request(self):
        @runtime_checkable
        class P(Protocol):
            pass

        @injectable(provides=[P], multi=True, scope=Scope.REQUEST)
        class A:
            pass

        @injectable(provides=[P], multi=True, scope=Scope.REQUEST)
        class B:
            pass

        c = DIContainer()
        c.register(A)
        c.register(B)
        c.compile()

        cache: dict = {}
        first = await c.resolve(list[P], request_cache=cache)  # type: ignore[type-abstract]
        second = await c.resolve(list[P], request_cache=cache)  # type: ignore[type-abstract]
        # Both resolutions within the same request return the same instances.
        assert first[0] is second[0]
        assert first[1] is second[1]


# ---------------------------------------------------------------------------
# Concrete (non-Protocol) tokens are equally valid multi-binding subjects.
# ---------------------------------------------------------------------------


class TestListConcreteToken:
    """``list[T]`` works when ``T`` is a regular class, not just a Protocol."""

    @pytest.mark.asyncio
    async def test_concrete_class_token_collects_all_subclasses(self):
        class Tag:
            pass

        @injectable(provides=[Tag], multi=True)
        class Red(Tag):
            pass

        @injectable(provides=[Tag], multi=True)
        class Blue(Tag):
            pass

        c = DIContainer()
        c.register(Red)
        c.register(Blue)
        c.compile()

        tags = await c.resolve(list[Tag])
        assert {t.__class__.__name__ for t in tags} == {"Red", "Blue"}


# ---------------------------------------------------------------------------
# Optional ``list[T]`` constructor parameters fall back to their default
# when no provider is registered — same contract as scalar deps.
# ---------------------------------------------------------------------------


class TestOptionalListParameter:
    """A ``list[T]`` constructor parameter with a default behaves like
    any other optional dep: the default is used when no provider exists.
    """

    @pytest.mark.asyncio
    async def test_default_used_when_no_providers_registered(self):
        @runtime_checkable
        class P(Protocol):
            def x(self) -> None: ...

        @injectable()
        class Consumer:
            def __init__(self, items: list[P] | None = None) -> None:  # type: ignore[type-arg]
                self.items = items or []

        c = DIContainer()
        c.register(Consumer)
        c.compile()  # no provider for ``P`` — default kicks in

        result = await c.resolve(Consumer)
        assert result.items == []

    @pytest.mark.asyncio
    async def test_providers_override_default(self):
        """When the parameter is the bare ``list[T]`` shape (no Union
        wrapper) and a provider is registered, the DI container injects
        the multi-binding list rather than honouring the parameter's
        default. This mirrors how scalar deps behave: a registered
        provider takes precedence over the parameter default.
        """

        @runtime_checkable
        class P(Protocol):
            def x(self) -> str: ...

        @injectable(provides=[P], multi=True)
        class Real:
            def x(self) -> str:
                return "real"

        # Note the *bare* ``list[P]`` annotation: ``list[P] | None`` is a
        # Union that the DI core deliberately does not unwrap (the same
        # is true for scalar ``Service | None`` annotations — the union
        # is treated as a non-injectable shape, so the default applies).
        @injectable()
        class Consumer:
            def __init__(self, items: list[P] = ()) -> None:  # type: ignore[type-arg, assignment]
                self.items = items

        c = DIContainer()
        c.register(Real)
        c.register(Consumer)
        c.compile()

        result = await c.resolve(Consumer)
        assert [i.x() for i in result.items] == ["real"]


# ---------------------------------------------------------------------------
# ``has_provider`` recognises ``list[T]`` for both registered & missing tokens.
# ---------------------------------------------------------------------------


class TestHasProviderForList:
    """The handler compiler relies on ``has_provider`` to distinguish a
    DI-resolvable parameter from one it should reject."""

    def test_has_provider_true_for_list_with_multi_bindings(self):
        c = DIContainer()
        c.register(Smtp)
        c.register(Sms)
        c.compile()

        assert c.has_provider(list[Sender]) is True  # type: ignore[type-abstract]
        # The bare protocol is also resolvable — sanity check.
        assert c.has_provider(Sender) is True  # type: ignore[type-abstract]

    def test_has_provider_false_for_list_of_unregistered_token(self):
        @runtime_checkable
        class Nothing(Protocol):
            def x(self) -> None: ...

        c = DIContainer()
        c.compile()
        assert c.has_provider(list[Nothing]) is False  # type: ignore[type-abstract]

    def test_has_provider_false_for_other_generics(self):
        c = DIContainer()
        c.register(Smtp)
        c.compile()
        # Only ``list[T]`` is recognised; ``dict[str, T]`` is not.
        assert c.has_provider(dict[str, Sender]) is False  # type: ignore[type-abstract]
        assert c.has_provider(tuple[Sender, ...]) is False  # type: ignore[type-abstract]
