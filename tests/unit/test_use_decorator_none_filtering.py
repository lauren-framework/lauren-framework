"""Tests for ``@use_guards`` / ``@use_middlewares`` None-filtering.

The pattern we support::

    @use_guards(
        AlwaysGuard,
        AdminGuard if is_admin_route else None,
        BetaGuard if settings.beta else None,
    )

makes conditional guard / middleware lists ergonomic. ``None`` entries are
dropped silently before validation so the decorator never asks for
``can_activate`` / ``dispatch`` on ``NoneType``.
"""

# intentional: no `from __future__ import annotations` since several test
# classes are defined inside test methods.

import pytest

from lauren import (
    CallNext,
    Lauren,
    LaurenFactory,
    Request,
    Response,
    controller,
    get,
    middleware,
    module,
    use_guards,
    use_middlewares,
)
from lauren.testing import TestClient
from lauren.types import ExecutionContext


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@middleware()
class StampA:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        resp = await call_next(request)
        return resp.with_header("x-a", "1")


@middleware()
class StampB:
    async def dispatch(self, request: Request, call_next: CallNext) -> Response:
        resp = await call_next(request)
        return resp.with_header("x-b", "1")


class AllowAllGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return True


class DenyGuard:
    async def can_activate(self, ctx: ExecutionContext) -> bool:
        return False


# ---------------------------------------------------------------------------
# use_middlewares None-filtering
# ---------------------------------------------------------------------------


class TestUseMiddlewareFiltersNone:
    def test_none_entries_dropped_from_attached_list(self):
        @use_middlewares(StampA, None, StampB, None)
        class Ctrl:
            pass

        # The marker attribute only contains the real middleware classes.
        assert getattr(Ctrl, "__lauren_use_middlewares__") == [StampA, StampB]

    def test_all_none_yields_empty_list(self):
        @use_middlewares(None, None, None)
        class Ctrl:
            pass

        assert getattr(Ctrl, "__lauren_use_middlewares__") == []

    def test_none_never_hits_validation(self):
        """Validation runs only on non-None entries so ``None.dispatch``
        is never probed."""
        # If filtering were broken, this would raise AttributeError or
        # MiddlewareConfigError because ``None`` lacks ``dispatch``.
        decorator = use_middlewares(None, StampA, None)
        assert callable(decorator)

    @pytest.mark.asyncio
    async def test_conditional_middleware_applied_end_to_end(self):
        """The canonical use-case: a feature-flag toggles whether a
        middleware is attached to a controller."""

        def build_app(feature_flag: bool):
            @use_middlewares(StampA, StampB if feature_flag else None)
            @controller("/c")
            class Ctrl:
                @get("/")
                async def root(self) -> dict:
                    return {"ok": True}

            @module(controllers=[Ctrl])
            class M:
                pass

            return M

        on = LaurenFactory.create(build_app(True))
        off = LaurenFactory.create(build_app(False))

        r_on = TestClient(on).get("/c/")
        assert r_on.header("x-a") == "1"
        assert r_on.header("x-b") == "1"

        r_off = TestClient(off).get("/c/")
        assert r_off.header("x-a") == "1"
        assert r_off.header("x-b") is None


# ---------------------------------------------------------------------------
# use_guards None-filtering
# ---------------------------------------------------------------------------


class TestUseGuardsFiltersNone:
    def test_none_entries_dropped_from_attached_list(self):
        @use_guards(AllowAllGuard, None, DenyGuard, None)
        class Ctrl:
            pass

        assert getattr(Ctrl, "__lauren_use_guards__") == [AllowAllGuard, DenyGuard]

    def test_all_none_yields_empty_list(self):
        @use_guards(None, None)
        class Ctrl:
            pass

        assert getattr(Ctrl, "__lauren_use_guards__") == []

    def test_none_bypasses_can_activate_validation(self):
        # If filtering ran AFTER the ``hasattr(cls, "can_activate")`` check,
        # we'd get a confusing AttributeError on ``None``.
        decorator = use_guards(None, AllowAllGuard, None)
        assert callable(decorator)

    @pytest.mark.asyncio
    async def test_conditional_guard_applied_end_to_end(self):
        def build_app(admin_mode: bool):
            @controller("/resource")
            class Ctrl:
                @use_guards(AllowAllGuard, DenyGuard if admin_mode else None)
                @get("/")
                async def root(self) -> dict:
                    return {"ok": True}

            @module(controllers=[Ctrl])
            class M:
                pass

            return M

        normal = LaurenFactory.create(build_app(False))
        locked = LaurenFactory.create(build_app(True))

        assert TestClient(normal).get("/resource/").status_code == 200
        # DenyGuard makes this forbidden.
        assert TestClient(locked).get("/resource/").status_code == 403


# ---------------------------------------------------------------------------
# Stacking @use_* decorators interacts correctly with filtering
# ---------------------------------------------------------------------------


class TestStackingWithNone:
    def test_multiple_use_guards_applications_compose(self):
        # Two @use_guards calls on the same class, each with Nones \u2014
        # the attached list should be the concatenation of non-Nones.
        @use_guards(None, AllowAllGuard)
        @use_guards(DenyGuard, None)
        class Ctrl:
            pass

        assert getattr(Ctrl, "__lauren_use_guards__") == [DenyGuard, AllowAllGuard]

    def test_class_and_method_guards_both_filter_none(self):
        @use_guards(AllowAllGuard, None)
        class Ctrl:
            def handler(self):
                pass

        use_guards(None, DenyGuard)(Ctrl.handler)

        assert getattr(Ctrl, "__lauren_use_guards__") == [AllowAllGuard]
        assert getattr(Ctrl.handler, "__lauren_use_guards__") == [DenyGuard]


# ---------------------------------------------------------------------------
# FastAPI-style Lauren app: conditional guards on @app.get
# ---------------------------------------------------------------------------


class TestLaurenAppConditionalGuards:
    @pytest.mark.asyncio
    async def test_conditional_guard_on_fastapi_style_handler(self):
        """The combined value proposition: conditional guards on routes
        registered via ``@app.get`` with the FastAPI-style API."""

        def make(env: str):
            app = Lauren(docs_url=None, redoc_url=None, openapi_url=None)

            @app.get("/admin")
            @use_guards(
                AllowAllGuard,
                DenyGuard if env == "prod" else None,
            )
            async def admin() -> dict:
                return {"env": env}

            return app

        dev = make("dev")
        prod = make("prod")

        assert TestClient(dev).get("/admin").status_code == 200
        assert TestClient(prod).get("/admin").status_code == 403


# ---------------------------------------------------------------------------
# Invalid entries still surface as errors \u2014 ``None`` is the only
# allowed sentinel.
# ---------------------------------------------------------------------------


class TestInvalidNonNoneStillRejected:
    def test_string_in_use_guards_still_raises(self):
        from lauren.exceptions import GuardConfigError

        with pytest.raises(GuardConfigError):
            use_guards("not a guard")

    def test_string_in_use_middleware_still_raises(self):
        from lauren.exceptions import MiddlewareConfigError

        with pytest.raises(MiddlewareConfigError):
            use_middlewares(42)  # type: ignore[arg-type]
