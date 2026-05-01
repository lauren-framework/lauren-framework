"""Unit tests for static/classmethod route binding detection.

These tests exercise :func:`_unwrap_handler_descriptor` directly so the
marker-preservation contract is documented in isolation. The integration
suite (``tests/integration/test_static_classmethod_routes.py``) drives
the same machinery through a real :class:`LaurenApp`.

Coverage:

* Plain functions return ``(fn, "instance")``.
* :class:`staticmethod` descriptors return the underlying function and
  ``"static"``.
* :class:`classmethod` descriptors return the underlying function and
  ``"classmethod"``.
* Markers applied *above* the descriptor (landing on the descriptor
  itself) are propagated down to ``__func__`` so downstream tools see
  them in a single canonical place.
* Non-callables and unrecognised entries produce ``(None, ...)``.
"""

from __future__ import annotations


from lauren import get
from lauren._asgi import _unwrap_handler_descriptor
from lauren.decorators import ROUTE_META, USE_MIDDLEWARES


class TestUnwrapDescriptor:
    def test_plain_function(self):
        async def fn():
            return None

        out, binding = _unwrap_handler_descriptor(fn)
        assert out is fn
        assert binding == "instance"

    def test_staticmethod_descriptor(self):
        async def raw():
            return None

        sm = staticmethod(raw)
        out, binding = _unwrap_handler_descriptor(sm)
        assert out is raw
        assert binding == "static"

    def test_classmethod_descriptor(self):
        async def raw(cls):
            return None

        cm = classmethod(raw)
        out, binding = _unwrap_handler_descriptor(cm)
        assert out is raw
        assert binding == "classmethod"

    def test_non_callable_rejected(self):
        out, binding = _unwrap_handler_descriptor(42)
        assert out is None
        # Binding defaults to instance for rejected entries \u2014 the caller
        # never uses it because ``out`` is None.
        assert binding == "instance"


class TestMarkerPropagation:
    def test_route_marker_on_staticmethod_above(self):
        # Decorator order: @get above @staticmethod \u2014 the marker lands
        # on the staticmethod descriptor. After unwrap the underlying
        # function must carry the marker so the route discovery loop
        # sees it.
        class C:
            @get("/x")
            @staticmethod
            async def h():
                return None

        raw = C.__dict__["h"]
        assert isinstance(raw, staticmethod)
        out, binding = _unwrap_handler_descriptor(raw)
        assert binding == "static"
        assert hasattr(out, ROUTE_META)
        metas = getattr(out, ROUTE_META)
        assert any(m.path == "/x" for m in metas)

    def test_route_marker_on_classmethod_above(self):
        class C:
            @get("/y")
            @classmethod
            async def h(cls):
                return None

        raw = C.__dict__["h"]
        assert isinstance(raw, classmethod)
        out, binding = _unwrap_handler_descriptor(raw)
        assert binding == "classmethod"
        assert hasattr(out, ROUTE_META)

    def test_route_marker_below_staticmethod(self):
        # Opposite ordering \u2014 @staticmethod above @get \u2014 places the
        # marker on the raw function and wraps AFTER. The descriptor
        # has no marker of its own, but the function does, so unwrap
        # should still find it.
        class C:
            @staticmethod
            @get("/z")
            async def h():
                return None

        raw = C.__dict__["h"]
        assert isinstance(raw, staticmethod)
        out, binding = _unwrap_handler_descriptor(raw)
        assert binding == "static"
        assert hasattr(out, ROUTE_META)

    def test_middleware_marker_propagated(self):
        # Any marker attribute on the descriptor must flow through, not
        # just the route marker — we rely on this for @use_middlewares /
        # @use_guards stacked above @staticmethod.
        from lauren import use_middlewares, middleware, CallNext, Request, Response

        @middleware
        class MW:
            async def dispatch(self, request: Request, call_next: CallNext) -> Response:
                return await call_next(request)

        class C:
            @use_middlewares(MW)
            @staticmethod
            async def h():
                return None

        raw = C.__dict__["h"]
        out, _ = _unwrap_handler_descriptor(raw)
        assert getattr(out, USE_MIDDLEWARES, []) == [MW]
