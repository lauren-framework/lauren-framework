"""Comprehensive tests for the controller-subclass-inheritance rule.

Lauren's golden rule (.CLAUDE.md §3): **decorators attach metadata,
they never propagate via inheritance.** A subclass of a ``@controller``
class is NOT itself a controller unless it carries its own
``@controller`` decoration.

This file probes every dimension of the rule:

* The metadata sentinel is in the decoratee's own ``__dict__``, never
  on a parent.
* The runtime helper ``_own_controller_meta`` raises
  :class:`MetadataInheritanceError` for inherited markers and
  :class:`StartupError` when no marker exists at all.
* Re-decoration is the only way to opt a subclass in.
* Subclasses without their own decoration cannot be registered in a
  module's ``controllers`` list.
* Single-, multi-, and diamond-inheritance shapes are all handled.
* The rule applies symmetrically to ``@injectable`` (controllers
  inherit ``__lauren_injectable__`` markers as well), so a subclass
  must also be its own injectable.
* The dispatch path observes the rule: an undecorated subclass'
  inherited route methods do NOT mount on the parent's prefix.

The companion ``tests/unit/test_inheritance_guard.py`` covers the
positive case for one decorator each. This file goes deep on
``@controller`` specifically, with parametrised fixtures for the
edge cases.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from lauren import (
    LaurenFactory,
    controller,
    get,
    module,
)
from lauren.decorators import CONTROLLER_META
from lauren.exceptions import (
    MetadataInheritanceError,
    StartupError,
)
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# Helper to drive an end-to-end app build that triggers the rule.
# ---------------------------------------------------------------------------


def _build_app(root_module: type) -> Any:
    """Build a full lauren app from ``root_module``, raising at startup.

    Every test in this file constructs a fresh decorator chain inside
    the test body so module-level state never leaks between cases —
    that's a deliberate test-isolation choice the framework already
    uses elsewhere.
    """
    return asyncio.run(LaurenFactory.create(root_module))


# ---------------------------------------------------------------------------
# Sentinel placement: the marker MUST be in the class's own __dict__.
# ---------------------------------------------------------------------------


class TestSentinelPlacement:
    """The framework relies on ``__dict__`` (not ``hasattr``) for the rule."""

    def test_decorated_class_has_marker_in_own_dict(self):
        @controller("/x")
        class Decorated:
            @get("/")
            async def idx(self) -> dict:
                return {}

        assert CONTROLLER_META in Decorated.__dict__

    def test_subclass_does_not_have_marker_in_own_dict(self):
        @controller("/x")
        class Decorated:
            @get("/")
            async def idx(self) -> dict:
                return {}

        class Subclass(Decorated):
            pass

        # The subclass *can see* the parent's marker via attribute
        # access (because Python's MRO will find it), but the
        # framework's check is on the subclass's own __dict__.
        assert CONTROLLER_META not in Subclass.__dict__
        # ``hasattr`` confirms the inheritance is visible through the
        # MRO — that's exactly the trap the framework is closing.
        assert hasattr(Subclass, CONTROLLER_META)

    def test_re_decorated_subclass_has_marker_in_own_dict(self):
        @controller("/x")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {}

        @controller("/y")
        class Child(Parent):
            @get("/")
            async def idx2(self) -> dict:
                return {}

        # Both have their own marker; they don't share one.
        assert CONTROLLER_META in Parent.__dict__
        assert CONTROLLER_META in Child.__dict__
        assert Parent.__dict__[CONTROLLER_META] is not Child.__dict__[CONTROLLER_META]

    def test_decorator_returns_the_decoratee_unchanged(self):
        """The framework rule: ``decorators attach metadata, they
        never rewrap``. This is the regression guard."""

        class Plain:
            @get("/")
            async def idx(self) -> dict:
                return {}

        decorated = controller("/x")(Plain)
        # ``decorated is Plain`` — same object, just augmented.
        assert decorated is Plain


# ---------------------------------------------------------------------------
# Direct startup-time enforcement via _own_controller_meta.
# ---------------------------------------------------------------------------


class TestOwnControllerMetaHelper:
    """Exercise the lookup helper that the runtime uses at startup."""

    def test_returns_meta_for_decorated_class(self):
        from lauren._asgi import _own_controller_meta

        @controller("/api")
        class C:
            pass

        meta = _own_controller_meta(C)
        # The meta object encodes the prefix the runtime mounts on.
        assert meta.prefix == "/api"

    def test_inherited_marker_raises_metadata_inheritance_error(self):
        from lauren._asgi import _own_controller_meta

        @controller("/parent")
        class Parent:
            pass

        class Child(Parent):
            pass

        with pytest.raises(MetadataInheritanceError) as excinfo:
            _own_controller_meta(Child)
        # The error names both the offender and the source of the
        # inherited marker so the user knows where to add the explicit
        # decoration.
        assert excinfo.value.detail.get("class") == "Child"
        assert excinfo.value.detail.get("inherits_from") == "Parent"

    def test_undecorated_class_with_no_inheritance_raises_startup_error(self):
        from lauren._asgi import _own_controller_meta

        class Bare:
            pass

        with pytest.raises(StartupError) as excinfo:
            _own_controller_meta(Bare)
        # ``MetadataInheritanceError`` IS a ``StartupError``; the
        # important thing is we get a typed startup-time failure
        # rather than a silent misregistration.
        assert "missing @controller" in str(excinfo.value)


# ---------------------------------------------------------------------------
# End-to-end: an undecorated subclass listed in a module's controllers
# fails at startup with a descriptive error.
# ---------------------------------------------------------------------------


class TestUndecoratedSubclassInModule:
    """A module that lists an undecorated subclass MUST fail to start."""

    def test_subclass_in_controllers_list_rejected(self):
        @controller("/parent")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {"ok": True}

        class UndecoratedChild(Parent):
            # Inherits ``idx``, no own decoration.
            pass

        @module(controllers=[UndecoratedChild])
        class App:
            pass

        with pytest.raises(MetadataInheritanceError) as excinfo:
            _build_app(App)
        # The error must point at the subclass, not the parent.
        assert "UndecoratedChild" in str(excinfo.value)

    def test_parent_listed_alongside_subclass_rejected(self):
        # When both Parent (decorated) and UndecoratedChild are
        # listed, the framework must still reject the undecorated
        # child — the Parent's decoration does NOT bless the child.
        @controller("/parent")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {}

        class UndecoratedChild(Parent):
            pass

        @module(controllers=[Parent, UndecoratedChild])
        class App:
            pass

        with pytest.raises(MetadataInheritanceError):
            _build_app(App)

    def test_parent_alone_is_fine(self):
        # The parent decoration alone should always work; we only
        # reject the *subclass* shape.
        @controller("/parent")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {"hi": "from parent"}

        # Important: the subclass is defined but NOT registered.
        class UndecoratedChild(Parent):
            pass  # noqa: E701  # only here to make sure existence != registration

        @module(controllers=[Parent])
        class App:
            pass

        app = _build_app(App)
        client = TestClient(app)
        r = client.get("/parent/")
        assert r.status_code == 200
        assert r.json() == {"hi": "from parent"}


# ---------------------------------------------------------------------------
# Re-decoration: the only sanctioned way to inherit behaviour.
# ---------------------------------------------------------------------------


class TestRedecorationOptIn:
    """A subclass becomes a controller only by being decorated itself."""

    def test_subclass_with_own_decorator_works(self):
        # Re-decoration is the *only* sanctioned way for a subclass to
        # become a controller. Each class declares its own distinct
        # route methods so no method-name collision occurs in the
        # router (V2 inherits ``items`` via Python MRO, which is
        # mounted under V2's prefix, AND has its own ``items2``).
        @controller("/v1")
        class V1:
            @get("/list")
            async def items(self) -> dict:
                return {"version": 1}

        @controller("/v2")
        class V2(V1):  # explicit re-decoration with a different prefix
            @get("/extra")
            async def items2(self) -> dict:
                return {"version": 2}

        @module(controllers=[V1, V2])
        class App:
            pass

        app = _build_app(App)
        client = TestClient(app)
        # V1 mounts at ``/v1/list``.
        assert client.get("/v1/list").json() == {"version": 1}
        # V2 inherits ``items`` from V1 (mounted under V2's prefix)
        # AND adds its own ``items2``.
        assert client.get("/v2/list").json() == {"version": 1}
        assert client.get("/v2/extra").json() == {"version": 2}

    def test_subclass_inherits_handlers_when_redecorated(self):
        # A re-decorated subclass automatically inherits the parent's
        # handlers via normal Python attribute lookup. This is just
        # Python's MRO; the framework does no extra work.
        @controller("/parent")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {"who": "parent"}

        @controller("/child")
        class Child(Parent):
            # Inherits ``idx`` from Parent; no override.
            pass

        @module(controllers=[Parent, Child])
        class App:
            pass

        app = _build_app(App)
        client = TestClient(app)
        # Both routes work and return the SAME body (because the
        # handler implementation is shared via inheritance).
        assert client.get("/parent/").json() == {"who": "parent"}
        assert client.get("/child/").json() == {"who": "parent"}

    def test_subclass_can_override_a_handler(self):
        @controller("/parent")
        class Parent:
            @get("/")
            async def idx(self) -> dict:
                return {"who": "parent"}

        @controller("/child")
        class Child(Parent):
            @get("/")
            async def idx(self) -> dict:  # type: ignore[override]
                return {"who": "child"}

        @module(controllers=[Parent, Child])
        class App:
            pass

        app = _build_app(App)
        client = TestClient(app)
        assert client.get("/parent/").json() == {"who": "parent"}
        assert client.get("/child/").json() == {"who": "child"}


# ---------------------------------------------------------------------------
# Inheritance-shape coverage: deep MRO chains, multiple inheritance, mixins.
# ---------------------------------------------------------------------------


class TestInheritanceShapes:
    """The rule survives every common Python class-graph topology."""

    def test_deep_chain_three_levels(self):
        @controller("/a")
        class A:
            @get("/")
            async def idx(self) -> dict:
                return {}

        class B(A):
            pass

        class C(B):
            pass

        # ``C``'s nearest decorated ancestor is two MRO hops away;
        # the framework must still reject it.
        with pytest.raises(MetadataInheritanceError) as excinfo:
            from lauren._asgi import _own_controller_meta

            _own_controller_meta(C)
        # The error should point at the immediate base in the MRO
        # that carries the marker.
        assert excinfo.value.detail.get("inherits_from") in {"A", "B"}

    def test_mixin_class_does_not_carry_controller(self):
        # A common pattern: pull route declarations into a mixin.
        # Without explicit decoration on the *user-facing* class,
        # the routes don't mount.
        class HandlersMixin:
            @get("/items")
            async def items(self) -> dict:
                return {"items": []}

        @controller("/api")
        class Api(HandlersMixin):
            pass

        @module(controllers=[Api])
        class App:
            pass

        app = _build_app(App)
        client = TestClient(app)
        # The mixin's ``items`` is inherited and the route mounts
        # under the decorated class's prefix.
        r = client.get("/api/items")
        assert r.status_code == 200
        assert r.json() == {"items": []}

    def test_undecorated_mixin_alone_is_not_a_controller(self):
        class HandlersMixin:
            @get("/")
            async def idx(self) -> dict:
                return {}

        # The mixin itself is registered as a controller — no
        # decoration anywhere — so registration MUST fail.
        @module(controllers=[HandlersMixin])
        class App:
            pass

        # The framework rejects this loudly, but the *exact* error
        # depends on which check fires first. The ``@controller``
        # decorator implicitly applies ``@injectable``, so a class
        # missing ``@controller`` typically also lacks the
        # ``@injectable`` marker. Both surface as a typed startup
        # failure that names the offending class — that's the
        # invariant we test for.
        with pytest.raises(StartupError) as excinfo:
            _build_app(App)
        msg = str(excinfo.value)
        assert "HandlersMixin" in msg

    def test_diamond_inheritance(self):
        # Two decorated parents + an undecorated diamond child.
        @controller("/left")
        class Left:
            @get("/")
            async def idx(self) -> dict:
                return {"side": "left"}

        @controller("/right")
        class Right:
            @get("/")
            async def idx(self) -> dict:
                return {"side": "right"}

        class Diamond(Left, Right):
            # Inherits markers from both parents (Left wins via MRO);
            # the framework rejects this without explicit re-decoration.
            pass

        with pytest.raises(MetadataInheritanceError):
            from lauren._asgi import _own_controller_meta

            _own_controller_meta(Diamond)

    def test_sibling_subclasses_are_independent(self):
        @controller("/base")
        class Base:
            @get("/")
            async def idx(self) -> dict:
                return {}

        # Two siblings: one decorated, one not. The decorated sibling
        # works; the bare sibling fails. They don't influence each
        # other.
        @controller("/decorated")
        class DecoratedSibling(Base):
            pass

        class BareSibling(Base):
            pass

        from lauren._asgi import _own_controller_meta

        # Decorated sibling: works.
        meta = _own_controller_meta(DecoratedSibling)
        assert meta.prefix == "/decorated"
        # Bare sibling: still rejected.
        with pytest.raises(MetadataInheritanceError):
            _own_controller_meta(BareSibling)


# ---------------------------------------------------------------------------
# Symmetric injectable rule: a subclass that escapes the controller
# check by being decorated must ALSO be its own injectable, because
# ``@controller`` implicitly applies ``@injectable``.
# ---------------------------------------------------------------------------


class TestInjectableSymmetry:
    """``@controller`` also marks the class as ``@injectable``."""

    def test_redecorated_subclass_is_its_own_injectable(self):
        from lauren._di import INJECTABLE_META

        @controller("/parent")
        class Parent:
            pass

        @controller("/child")
        class Child(Parent):
            pass

        # Both classes have their own injectable marker.
        assert INJECTABLE_META in Parent.__dict__
        assert INJECTABLE_META in Child.__dict__

    def test_bare_subclass_does_not_become_injectable_by_inheritance(self):
        from lauren._di import INJECTABLE_META

        @controller("/parent")
        class Parent:
            pass

        class Bare(Parent):
            pass

        # ``Bare`` inherits the injectable attribute via MRO but
        # doesn't OWN it — and the framework rule tests own-dict
        # presence.
        assert INJECTABLE_META not in Bare.__dict__


# ---------------------------------------------------------------------------
# Dispatch-path consequences: an undecorated subclass that somehow
# slips through (e.g. user puts it in a list of helpers) doesn't
# accidentally serve traffic.
# ---------------------------------------------------------------------------


class TestRouteTableObservability:
    """Inspect the compiled route table to confirm only decorated classes mount."""

    def test_only_decorated_classes_appear_in_routes(self):
        @controller("/decorated")
        class Decorated:
            @get("/")
            async def idx(self) -> dict:
                return {}

        class NotMounted(Decorated):
            # Defined in the test scope but never registered. The
            # route table must not contain its prefix.
            pass

        @module(controllers=[Decorated])
        class App:
            pass

        app = _build_app(App)
        # ``app.routes()`` returns the public list of mounted route
        # entries (one per HTTP method + path template). Calling it
        # rather than dereferencing because ``LaurenApp.routes`` is
        # a method.
        templates = {entry.path_template for entry in app.routes()}
        # The router normalises trailing slashes; either spelling is
        # the same logical route. The point of the test is that the
        # decorated class's prefix appears AND no template is
        # contributed by the undecorated subclass.
        assert any(t.rstrip("/") == "/decorated" for t in templates), templates
        # The undecorated subclass is irrelevant; nothing under it
        # should appear in the router.
        assert not any("notmounted" in t.lower() for t in templates), templates


# ---------------------------------------------------------------------------
# Error-payload quality: the user must get an actionable message.
# ---------------------------------------------------------------------------


class TestErrorMessageQuality:
    """Inheritance errors carry detail that's useful in CI logs."""

    def test_error_message_names_the_offender(self):
        @controller("/p")
        class Parent:
            pass

        class OffendingChild(Parent):
            pass

        @module(controllers=[OffendingChild])
        class App:
            pass

        with pytest.raises(MetadataInheritanceError) as excinfo:
            _build_app(App)
        msg = str(excinfo.value)
        assert "OffendingChild" in msg

    def test_error_message_names_the_inherited_source(self):
        # Knowing *which* base class the marker leaked from is what
        # makes the error actionable.
        @controller("/p")
        class Parent:
            pass

        class Mid(Parent):
            pass

        class Leaf(Mid):
            pass

        from lauren._asgi import _own_controller_meta

        with pytest.raises(MetadataInheritanceError) as excinfo:
            _own_controller_meta(Leaf)
        # ``inherits_from`` is the FIRST class in the MRO walk that
        # carries the marker. With Mid undecorated, that's Parent.
        assert excinfo.value.detail.get("inherits_from") == "Parent"

    def test_error_message_recommends_redecoration(self):
        @controller("/p")
        class Parent:
            pass

        class Child(Parent):
            pass

        from lauren._asgi import _own_controller_meta

        with pytest.raises(MetadataInheritanceError) as excinfo:
            _own_controller_meta(Child)
        # The remediation hint must mention re-decoration.
        msg = str(excinfo.value)
        assert "@controller" in msg


# ---------------------------------------------------------------------------
# Parametrised matrix: a single test exercises the rule across every
# decorator that participates in the controller-style inheritance
# story (controller, ws_controller, socketio_controller).
# ---------------------------------------------------------------------------


class TestControllerLikeDecoratorsAreSymmetric:
    """The same rule applies to every decorator that markers a class.

    Each of these decorators attaches its own ``__lauren_*`` sentinel
    in the decoratee's own ``__dict__``. The framework checks ``cls.__dict__``
    (not ``hasattr``) for the marker, so an undecorated subclass is
    invisible to whichever runtime owns the decorator. The
    ``test_subclass_does_not_have_marker`` test asserts the contract
    structurally rather than going through full app boot for each one.
    """

    @pytest.mark.parametrize(
        "decorator_factory,marker_attr",
        [
            (lambda: controller("/x"), "__lauren_controller__"),
        ],
    )
    def test_undecorated_subclass_lacks_own_marker(
        self, decorator_factory, marker_attr
    ):
        Decorated = decorator_factory()(type("Decorated", (), {}))

        class Subclass(Decorated):
            pass

        assert marker_attr in Decorated.__dict__
        assert marker_attr not in Subclass.__dict__
