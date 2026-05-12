"""Tests for the ``PipeMeta`` marker and ``|`` composition.

The pipe mechanism follows lauren's universal marker-attribute pattern:
``@pipe()`` (or the equivalent inline ``pipe(fn_or_cls)``) attaches a
:class:`PipeMeta` instance to the target as ``target.__lauren_pipe__``
\u2014 the same shape as ``__lauren_controller__`` / ``__lauren_module__`` /
``__lauren_injectable__``. The target itself is returned unchanged; a
decorated function stays a function, a decorated class stays a class.

Composition uses ``|``::

    user_id: Path[int] = PathField(ge=1) | pipe(validate) | path_is_string | UserLookup

Every term after the first must carry the marker. The first term is a
``FieldDescriptor`` (from ``PathField`` / ``QueryField`` etc.) \u2014 that's
the only object in the chain whose ``__or__`` Python can dispatch to.
"""

# intentional: no ``from __future__ import annotations``. Tests declare
# classes inside methods and need live annotations for the compiler.

from typing import Annotated

import pytest

from lauren import (
    Lauren,
    LaurenFactory,
    PIPE_META,
    Path,
    PathField,
    PipeMeta,
    Query,
    controller,
    get,
    injectable,
    is_pipe,
    module,
    pipe,
)
from lauren.extractors import _ParamSpec
from lauren.testing import TestClient


# ---------------------------------------------------------------------------
# @pipe() as a decorator attaches PipeMeta and preserves target identity
# ---------------------------------------------------------------------------


class TestPipeMarkerAttribute:
    def test_pipe_decorator_on_function_attaches_meta(self):
        @pipe()
        def path_is_string(value, ctx):
            return str(value)

        meta = getattr(path_is_string, PIPE_META)
        assert isinstance(meta, PipeMeta)
        assert meta.target is path_is_string
        assert is_pipe(path_is_string)
        # The function is still a callable function \u2014 not wrapped.
        assert callable(path_is_string)
        assert type(path_is_string).__name__ == "function"
        # The signature is preserved.
        import inspect

        sig = inspect.signature(path_is_string)
        assert list(sig.parameters) == ["value", "ctx"]

    def test_pipe_decorator_on_class_attaches_meta(self):
        @pipe()
        class UserLookup:
            def transform(self, value, ctx):
                return value

        meta = getattr(UserLookup, PIPE_META)
        assert isinstance(meta, PipeMeta)
        assert meta.target is UserLookup
        assert is_pipe(UserLookup)
        # Still a class, still instantiable.
        assert isinstance(UserLookup, type)
        inst = UserLookup()
        assert inst.transform(10, None) == 10

    def test_pipe_inline_on_function_attaches_meta(self):
        def validator(value, ctx):
            return value

        result = pipe(validator)

        assert result is validator  # returns the same object
        assert is_pipe(validator)
        assert isinstance(getattr(validator, PIPE_META), PipeMeta)

    def test_pipe_inline_on_class_attaches_meta(self):
        class LookUp:
            def transform(self, value, ctx):
                return value

        result = pipe(LookUp)

        assert result is LookUp
        assert is_pipe(LookUp)

    def test_pipe_is_idempotent(self):
        @pipe()
        def f(value, ctx):
            return value

        meta1 = getattr(f, PIPE_META)
        # Marking again via the inline form must not replace the existing meta.
        returned = pipe(f)
        assert returned is f
        meta2 = getattr(f, PIPE_META)
        assert meta1 is meta2

    def test_bare_decorator_equivalent_to_empty_parens(self):
        """``@pipe`` without parens is accepted and behaves identically to
        ``@pipe()`` since ``pipe`` does the same thing whether called with
        a target or as a decorator factory."""

        @pipe
        def bare(value, ctx):
            return value

        assert is_pipe(bare)

    def test_pipe_rejects_non_callable(self):
        with pytest.raises(TypeError, match="can only decorate"):
            pipe(42)  # not a class or callable


# ---------------------------------------------------------------------------
# | composition: FieldDescriptor | pipe | pipe | class
# ---------------------------------------------------------------------------


class TestPipeComposition:
    def test_field_or_pipe_produces_paramspec(self):
        def fn(value, ctx):
            return value

        spec = PathField(ge=1) | pipe(fn)
        assert isinstance(spec, _ParamSpec)
        assert spec.field_descriptor is not None
        assert spec.field_descriptor.ge == 1
        assert spec.pipes == (fn,)

    def test_four_element_chain_exactly_like_the_prompt(self):
        """The exact shape the prompt asks for:
        ``PathField(...) | pipe(validate_path) | path_is_string | UserLookup``."""

        def validate_path(value, ctx):
            return value

        @pipe()
        def path_is_string(value, ctx):
            return str(value)

        @pipe()
        class UserLookup:
            def transform(self, value, ctx):
                return value

        spec = PathField(ge=1) | pipe(validate_path) | path_is_string | UserLookup
        assert isinstance(spec, _ParamSpec)
        assert [p for p in spec.pipes] == [validate_path, path_is_string, UserLookup]
        # The FieldDescriptor survives intact.
        assert spec.field_descriptor.ge == 1

    def test_chain_order_preserved_left_to_right(self):
        @pipe()
        def first(v, c):
            return v

        @pipe()
        def second(v, c):
            return v

        @pipe()
        def third(v, c):
            return v

        spec = PathField() | first | second | third
        assert spec.pipes == (first, second, third)

    def test_two_field_descriptors_in_chain_rejected(self):
        with pytest.raises(TypeError, match="at most one FieldDescriptor"):
            PathField(ge=1) | pipe(lambda v, c: v) | PathField(le=10)

    def test_unmarked_callable_in_chain_rejects_with_guidance(self):
        def not_a_pipe(value, ctx):
            return value

        with pytest.raises(TypeError, match="not marked as a pipe"):
            PathField() | not_a_pipe

    def test_unmarked_class_in_chain_rejects(self):
        class PlainClass:
            def transform(self, v, c):
                return v

        with pytest.raises(TypeError, match="not marked as a pipe"):
            PathField() | PlainClass


# ---------------------------------------------------------------------------
# End-to-end execution of the composed chain
# ---------------------------------------------------------------------------


class TestChainEndToEnd:
    @pytest.mark.asyncio
    async def test_prompt_example_runs_top_to_bottom(self):
        events: list[str] = []

        def validate_path(value, ctx):
            events.append("validate_path")
            if value < 1:
                raise ValueError("too small")
            return value

        @pipe()
        def path_is_string(value, ctx):
            events.append("path_is_string")
            return str(value)

        @pipe()
        class UserLookup:
            def transform(self, value, ctx):
                events.append("UserLookup")
                return {"id": value}

        @controller("/u")
        class Ctrl:
            @get("/{uid}")
            async def show(
                self,
                uid: Path[int] = (PathField(ge=1) | pipe(validate_path) | path_is_string | UserLookup),
            ) -> dict:
                return uid

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/u/42")
        assert r.status_code == 200
        # Wire type is int (the handler annotation), pipes transform it.
        assert r.json() == {"id": "42"}
        # Pipes executed in declaration order.
        assert events == ["validate_path", "path_is_string", "UserLookup"]

    @pytest.mark.asyncio
    async def test_bare_marked_function_as_default(self):
        """A marked function used as a bare default value works (without an
        explicit chain): ``= path_is_string`` becomes a one-element chain."""

        @pipe()
        def path_is_string(value, ctx):
            return str(value)

        @controller("/c")
        class Ctrl:
            @get("/{n}")
            async def h(self, n: Path[int] = path_is_string) -> dict:
                return {"n": n}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/7")
        assert r.json() == {"n": "7"}

    @pytest.mark.asyncio
    async def test_bare_marked_class_as_default(self):
        @pipe()
        class UserLookup:
            def transform(self, v, c):
                return {"user": v}

        @controller("/c")
        class Ctrl:
            @get("/{n}")
            async def h(self, n: Path[int] = UserLookup) -> dict:
                return n

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/9")
        assert r.json() == {"user": 9}

    @pytest.mark.asyncio
    async def test_pipe_class_resolved_via_di_respects_module_scope(self):
        """When a pipe class is ``@injectable()`` and visible to the
        controller's module, the framework instantiates it through the DI
        container (with dependencies resolved) rather than using no-args."""

        @injectable()
        class Prefix:
            def __init__(self):
                self.value = "user#"

        @injectable()
        @pipe()
        class Stamp:
            def __init__(self, prefix: Prefix):
                self.prefix = prefix

            def transform(self, value, ctx):
                return f"{self.prefix.value}{value}"

        @controller("/c")
        class Ctrl:
            @get("/{n}")
            async def h(self, n: Path[int] = PathField() | Stamp) -> dict:
                return {"tag": n}

        @module(controllers=[Ctrl], providers=[Prefix, Stamp])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/5")
        assert r.json() == {"tag": "user#5"}


# ---------------------------------------------------------------------------
# Annotated[...] placement still works, using marker-based pipes
# ---------------------------------------------------------------------------


class TestAnnotatedForm:
    @pytest.mark.asyncio
    async def test_marked_function_inside_annotated(self):
        @pipe()
        def shout(value, ctx):
            return value.upper()

        @controller("/c")
        class Ctrl:
            @get("/x")
            async def h(self, q: Annotated[Query[str], shout]) -> dict:
                return {"q": q}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/x?q=hello")
        assert r.json() == {"q": "HELLO"}

    @pytest.mark.asyncio
    async def test_marked_class_inside_annotated(self):
        @pipe()
        class Envelope:
            def transform(self, value, ctx):
                return {"wrapped": value}

        @controller("/c")
        class Ctrl:
            @get("/{n}")
            async def h(self, n: Annotated[Path[int], Envelope]) -> dict:
                return n

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/7")
        assert r.json() == {"wrapped": 7}

    @pytest.mark.asyncio
    async def test_marked_function_and_class_side_by_side_in_annotated(self):
        """The canonical Annotated form combining both entity kinds:

            async def show(uid: Annotated[Path[int], path_is_string, UserLookup]): ...

        Pipes execute left-to-right in the order they appear inside
        ``Annotated``."""
        events: list[str] = []

        @pipe()
        def path_is_string(value, ctx):
            events.append(f"to-str({value!r})")
            return str(value)

        @pipe()
        class UserLookup:
            def transform(self, value, ctx):
                events.append(f"lookup({value!r})")
                return {"uid": value}

        @controller("/u")
        class Ctrl:
            @get("/{uid}")
            async def show(
                self,
                uid: Annotated[Path[int], path_is_string, UserLookup],
            ) -> dict:
                return uid

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/u/42")
        assert r.status_code == 200
        # path_is_string ran first (int → str), UserLookup second.
        assert r.json() == {"uid": "42"}
        assert events == ["to-str(42)", "lookup('42')"]

    @pytest.mark.asyncio
    async def test_annotated_rejects_unmarked_callable(self):
        """Consistent with | composition: an un-marked callable inside
        ``Annotated[...]`` is silently ignored (treated as unrelated type
        metadata). Users get a clear missing-pipe signal because the
        transform they expected simply doesn't run.

        We lock this in so future changes don't accidentally start
        executing un-marked callables and silently break the contract
        that ``@pipe()`` is the explicit opt-in for transform execution."""

        def unmarked(value, ctx):
            return "SHOULD NOT RUN"

        @controller("/c")
        class Ctrl:
            @get("/{n}")
            async def h(self, n: Annotated[Path[int], unmarked]) -> dict:
                return {"n": n}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/5")
        # The un-marked callable is ignored — handler sees the raw int.
        assert r.json() == {"n": 5}

    @pytest.mark.asyncio
    async def test_mixed_annotated_metadata_and_default_chain(self):
        @pipe()
        def plus_one(value, ctx):
            return value + 1

        @pipe()
        def times_ten(value, ctx):
            return value * 10

        @controller("/c")
        class Ctrl:
            @get("/{n}")
            async def h(
                self,
                # Annotation pipe runs first, default-chain pipes after.
                n: Annotated[Path[int], plus_one] = PathField() | times_ten,
            ) -> dict:
                return {"n": n}

        @module(controllers=[Ctrl])
        class M:
            pass

        app = LaurenFactory.create(M)
        r = TestClient(app).get("/c/3")
        # (3 + 1) * 10 = 40
        assert r.json() == {"n": 40}


# ---------------------------------------------------------------------------
# FastAPI-style Lauren app composition
# ---------------------------------------------------------------------------


class TestLaurenAppPipeComposition:
    @pytest.mark.asyncio
    async def test_chain_works_on_app_get(self):
        def validate(v, c):
            return v

        @pipe()
        def stringify(v, c):
            return str(v)

        @pipe()
        class Envelope:
            def transform(self, v, c):
                return {"wrapped": v}

        app = Lauren(docs_url=None, redoc_url=None, openapi_url=None)

        @app.get("/items/{i}")
        async def show(
            i: Path[int] = PathField(ge=1) | pipe(validate) | stringify | Envelope,
        ) -> dict:
            return i

        r = TestClient(app).get("/items/10")
        assert r.status_code == 200
        assert r.json() == {"wrapped": "10"}
        # Field constraint still applies.
        assert TestClient(app).get("/items/0").status_code >= 400
