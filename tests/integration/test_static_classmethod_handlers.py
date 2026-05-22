"""Integration tests for static, classmethod, instance, and custom-descriptor
route handlers.

These tests confirm that the ``__get__``-based dispatch introduced alongside
``CompiledHandler.raw_descriptor`` works correctly for every binding style,
including an arbitrary custom descriptor that implements ``__get__``.
"""

from __future__ import annotations

import functools
from typing import Any


from lauren import LaurenFactory, controller, get, module
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Custom descriptor that implements __get__
# ---------------------------------------------------------------------------


class _RouteDescriptor:
    """A minimal custom descriptor wrapping a route handler.

    Satisfies the three requirements for being detected and dispatched by
    lauren's startup pipeline:

    1. **callable** — ``__call__`` is defined so ``callable(descriptor)``
       returns ``True`` and ``_unwrap_handler_descriptor`` doesn't discard it.
    2. **proper signature** — ``functools.update_wrapper`` copies
       ``__wrapped__``, ``__name__``, ``__doc__``, and the ``__dict__``
       (which contains the ``ROUTE_META`` marker from ``@get``).
       ``inspect.signature`` follows ``__wrapped__`` to find the handler's
       real parameter list; ``inspect.iscoroutinefunction`` does the same.
    3. **``__get__``** — invoked by the dispatcher with the DI-built
       instance so the bound callable receives the correct ``self``.

    Records every ``(instance, owner)`` pair passed to ``__get__`` so tests
    can assert the descriptor protocol was exercised correctly.
    """

    get_calls: list[tuple[Any, type]] = []

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        functools.update_wrapper(self, fn)  # sets __wrapped__, copies __dict__

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        _RouteDescriptor.get_calls.append((obj, objtype))  # type: ignore[arg-type]
        if obj is None:
            return self
        # Return a partial that prepends obj so it behaves like a bound method.
        return functools.partial(self, obj)


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


@controller("/dispatch")
class DispatchController:
    received_cls: type | None = None
    received_self: Any = None

    @get("/static")
    @staticmethod
    def static_handler() -> dict:
        return {"binding": "static"}

    @get("/cls")
    @classmethod
    def classmethod_handler(cls) -> dict:
        DispatchController.received_cls = cls
        return {"binding": "classmethod", "cls": cls.__name__}

    @get("/inst")
    def instance_handler(self) -> dict:
        DispatchController.received_self = self
        return {"binding": "instance"}


@module(controllers=[DispatchController])
class AppModule:
    pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestStaticHandler:
    def test_static_handler_returns_200(self):
        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/dispatch/static")
        assert r.status_code == 200
        assert r.json() == {"binding": "static"}

    def test_static_handler_no_receiver_in_kwargs(self):
        """The static handler must receive no implicit first argument."""
        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/dispatch/static")
        assert r.status_code == 200


class TestClassmethodHandler:
    def test_classmethod_handler_returns_200(self):
        DispatchController.received_cls = None
        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/dispatch/cls")
        assert r.status_code == 200
        body = r.json()
        assert body["binding"] == "classmethod"
        assert body["cls"] == "DispatchController"

    def test_classmethod_handler_receives_correct_class(self):
        """``cls`` passed to the classmethod must be the controller class itself."""
        DispatchController.received_cls = None
        client = TestClient(LaurenFactory.create(AppModule))
        client.get("/dispatch/cls")
        assert DispatchController.received_cls is DispatchController


class TestInstanceHandler:
    def test_instance_handler_returns_200(self):
        DispatchController.received_self = None
        client = TestClient(LaurenFactory.create(AppModule))
        r = client.get("/dispatch/inst")
        assert r.status_code == 200
        assert r.json() == {"binding": "instance"}

    def test_instance_handler_receives_controller_instance(self):
        """``self`` passed to the instance method must be a ``DispatchController``."""
        DispatchController.received_self = None
        client = TestClient(LaurenFactory.create(AppModule))
        client.get("/dispatch/inst")
        assert isinstance(DispatchController.received_self, DispatchController)


class TestCustomDescriptor:
    """A custom descriptor that implements ``__get__`` works as a route handler.

    Lauren's dispatch now delegates to ``raw_descriptor.__get__(instance, cls)``
    so any object that correctly implements the descriptor protocol can be used
    as a handler — the framework doesn't need an explicit ``isinstance`` branch
    for every new binding style.

    Requirements on the descriptor (documented in the skill guide):
    - Must be callable (``__call__``) so ``_unwrap_handler_descriptor`` detects it.
    - Must carry the lauren route marker (via ``functools.update_wrapper`` or manual copy).
    - Must implement ``__get__`` returning a bound callable.
    """

    def test_custom_descriptor_handler_invoked(self):
        """A handler wrapped in a custom ``__get__`` descriptor returns 200."""
        _RouteDescriptor.get_calls.clear()

        # Use a sync handler — simpler and exercises the thread-pool dispatch
        # path together with ``__get__``.
        def _custom_handler(self) -> dict:
            return {"binding": "custom"}

        from lauren import get as get_decorator

        _decorated = get_decorator("/custom")(_custom_handler)
        descriptor = _RouteDescriptor(_decorated)

        @controller("/cdesc")
        class CDescController:
            pass

        CDescController.custom_handler = descriptor  # type: ignore[attr-defined]

        @module(controllers=[CDescController])
        class CDescModule:
            pass

        client = TestClient(LaurenFactory.create(CDescModule))
        r = client.get("/cdesc/custom")
        assert r.status_code == 200
        assert r.json() == {"binding": "custom"}

    def test_custom_descriptor_get_is_called_with_instance_and_type(self):
        """``__get__`` is called at dispatch with the DI-built instance."""
        _RouteDescriptor.get_calls.clear()

        def _custom_handler(self) -> dict:
            return {"ok": True}

        from lauren import get as get_decorator

        _decorated = get_decorator("/check")(_custom_handler)
        descriptor = _RouteDescriptor(_decorated)

        @controller("/check2")
        class Check2Controller:
            pass

        Check2Controller.check = descriptor  # type: ignore[attr-defined]

        @module(controllers=[Check2Controller])
        class Check2Module:
            pass

        TestClient(LaurenFactory.create(Check2Module)).get("/check2/check")
        # Filter out class-level __get__(None, cls) calls from Python's
        # descriptor protocol during class attribute access; we want the
        # dispatch-time call where obj is the DI-built instance.
        dispatch_calls = [(inst, owner) for inst, owner in _RouteDescriptor.get_calls if inst is not None]
        assert len(dispatch_calls) >= 1
        instance, owner = dispatch_calls[0]
        assert isinstance(instance, Check2Controller)
        assert owner is Check2Controller


# ---------------------------------------------------------------------------
# Non-callable descriptor (no __call__, relies solely on __get__ + __wrapped__)
# ---------------------------------------------------------------------------


class _NonCallableDescriptor:
    """Descriptor that does NOT define ``__call__``.

    Mimics the ``AsyncCachedMethod`` pattern: ``functools.wraps`` copies
    ``__wrapped__`` and the route-marker dict so Lauren can inspect the
    original handler, while ``__get__`` returns the bound callable at
    dispatch time.
    """

    def __init__(self, fn: Any) -> None:
        self._fn = fn
        functools.update_wrapper(self, fn)  # sets __wrapped__, copies __dict__

    def __get__(self, obj: Any, objtype: type | None = None) -> Any:
        if obj is None:
            return self
        return functools.partial(self._fn, obj)


class TestNonCallableDescriptor:
    """Non-callable descriptors with ``__get__`` + ``__wrapped__`` work as handlers.

    Before this fix ``_unwrap_handler_descriptor`` returned ``(None, 'instance')``
    for objects where ``callable(raw)`` was ``False``, silently dropping the route.
    Now it falls through to the ``__get__`` + ``__wrapped__`` branch.
    """

    def test_non_callable_descriptor_route_registers_and_returns_200(self):
        """Route wrapped in a non-callable descriptor is detected and dispatched."""

        def _handler(self) -> dict:
            return {"binding": "non-callable-descriptor"}

        from lauren import get as get_decorator

        _decorated = get_decorator("/ncd")(_handler)
        descriptor = _NonCallableDescriptor(_decorated)

        @controller("/ncd")
        class NcdController:
            pass

        NcdController.ncd_handler = descriptor  # type: ignore[attr-defined]

        @module(controllers=[NcdController])
        class NcdModule:
            pass

        client = TestClient(LaurenFactory.create(NcdModule))
        r = client.get("/ncd/ncd")
        assert r.status_code == 200
        assert r.json() == {"binding": "non-callable-descriptor"}

    def test_non_callable_descriptor_get_called_at_dispatch(self):
        """``__get__`` is invoked with the DI controller instance at dispatch."""
        received: list[Any] = []

        def _handler(self) -> dict:
            return {"ok": True}

        from lauren import get as get_decorator

        _decorated = get_decorator("/ncd2")(_handler)

        class _TrackingDescriptor(_NonCallableDescriptor):
            def __get__(self, obj: Any, objtype: type | None = None) -> Any:
                received.append(obj)
                return super().__get__(obj, objtype)

        descriptor = _TrackingDescriptor(_decorated)

        @controller("/ncd2")
        class Ncd2Controller:
            pass

        Ncd2Controller.ncd_handler = descriptor  # type: ignore[attr-defined]

        @module(controllers=[Ncd2Controller])
        class Ncd2Module:
            pass

        TestClient(LaurenFactory.create(Ncd2Module)).get("/ncd2/ncd2")
        instance_calls = [x for x in received if x is not None]
        assert len(instance_calls) >= 1
        assert isinstance(instance_calls[0], Ncd2Controller)
