"""Unit tests for ``@exception_handler`` and ``@use_exception_handlers`` —
focused on the decorator's metadata-only contract.

The companion integration suite (``test_exception_handlers.py``) exercises
the runtime dispatch path; here we lock in the *static* shape: every
decorator must do nothing more than attach an attribute to the decorated
entity, leaving the entity's identity (class / function) and behaviour
untouched.
"""

from __future__ import annotations

import pytest

from lauren import (
    exception_handler,
    use_exception_handlers,
)
from lauren.decorators import (
    EXCEPTION_HANDLER_META,
    USE_EXCEPTION_HANDLERS,
    ExceptionHandlerMeta,
)
from lauren.exceptions import ExceptionHandlerConfigError


class MyError(Exception):
    pass


class OtherError(Exception):
    pass


# ---------------------------------------------------------------------------
# @exception_handler attaches metadata only
# ---------------------------------------------------------------------------


class TestExceptionHandlerIsMetadataOnly:
    def test_class_identity_preserved(self):
        @exception_handler(MyError)
        class H:
            async def catch(self, exc, request):
                return None

        assert H.__name__ == "H"
        assert isinstance(H, type)
        # The decorator must not subclass / wrap the class.
        assert H.__mro__[1] is object

    def test_function_identity_preserved(self):
        @exception_handler(MyError)
        async def fn(exc, request):
            return None

        assert fn.__name__ == "fn"
        assert callable(fn)

    def test_attribute_is_correct_type(self):
        @exception_handler(MyError, OtherError)
        class H:
            async def catch(self, exc, request):
                return None

        meta = getattr(H, EXCEPTION_HANDLER_META)
        assert isinstance(meta, ExceptionHandlerMeta)
        assert meta.exceptions == (MyError, OtherError)

    def test_stacking_accumulates_exception_types_on_a_function(self):
        # Stacking is sugar for @exception_handler(MyError, OtherError):
        # both types are registered, top decorator first.
        @exception_handler(MyError)
        @exception_handler(OtherError)
        def fn(exc, request):
            return None

        meta = getattr(fn, EXCEPTION_HANDLER_META)
        assert meta.exceptions == (MyError, OtherError)

    def test_stacking_accumulates_on_a_class_and_preserves_injectable(self):
        from lauren.decorators import INJECTABLE_META

        @exception_handler(MyError)
        @exception_handler(OtherError)
        class H:
            async def catch(self, exc, request):
                return None

        meta = getattr(H, EXCEPTION_HANDLER_META)
        assert meta.exceptions == (MyError, OtherError)
        # The class form is still auto-injectable (the 2nd application is a no-op).
        assert INJECTABLE_META in H.__dict__
        assert hasattr(H, "catch")

    def test_stacking_dedupes_overlapping_types_preserving_order(self):
        class A(Exception): ...

        class B(Exception): ...

        class C(Exception): ...

        @exception_handler(A, B)
        @exception_handler(B, C)
        def fn(exc, request):
            return None

        # B appears once; order is top-to-bottom of first appearance.
        assert getattr(fn, EXCEPTION_HANDLER_META).exceptions == (A, B, C)

    def test_single_decorator_forms_are_unchanged(self):
        @exception_handler(MyError)
        def one(exc, request):
            return None

        @exception_handler(MyError, OtherError)
        def many(exc, request):
            return None

        assert getattr(one, EXCEPTION_HANDLER_META).exceptions == (MyError,)
        assert getattr(many, EXCEPTION_HANDLER_META).exceptions == (MyError, OtherError)

    def test_subclass_redecoration_does_not_absorb_parent_types(self):
        # Own-__dict__ read: a re-decorated subclass keeps only the types it
        # declares, never merging an inherited base's scope (strict inheritance).
        @exception_handler(MyError)
        class Parent:
            async def catch(self, exc, request):
                return None

        @exception_handler(OtherError)
        class Child(Parent):
            async def catch(self, exc, request):
                return None

        assert Parent.__dict__[EXCEPTION_HANDLER_META].exceptions == (MyError,)
        assert Child.__dict__[EXCEPTION_HANDLER_META].exceptions == (OtherError,)


# ---------------------------------------------------------------------------
# @use_exception_handlers attaches metadata only
# ---------------------------------------------------------------------------


class TestUseExceptionHandlersIsMetadataOnly:
    @staticmethod
    def _h(exc_cls=MyError):
        @exception_handler(exc_cls)
        class H:
            async def catch(self, exc, request):
                return None

        return H

    def test_attaches_class_dict_entry(self):
        H = self._h()

        @use_exception_handlers(H)
        class C:
            pass

        # Stored in the class's OWN __dict__ — not inherited.
        assert USE_EXCEPTION_HANDLERS in C.__dict__
        assert C.__dict__[USE_EXCEPTION_HANDLERS] == [H]

    def test_attaches_function_attribute(self):
        H = self._h()

        @use_exception_handlers(H)
        def handler():
            pass

        assert getattr(handler, USE_EXCEPTION_HANDLERS) == [H]

    def test_stacking_appends_in_decoration_order(self):
        H1 = self._h(MyError)
        H2 = self._h(OtherError)

        # Decorators apply bottom-up. The bottom decorator runs first
        # and appends H2; the top decorator then appends H1, giving the
        # order [H2, H1].
        @use_exception_handlers(H1)
        @use_exception_handlers(H2)
        class C:
            pass

        assert C.__dict__[USE_EXCEPTION_HANDLERS] == [H2, H1]

    def test_subclass_does_not_inherit(self):
        H = self._h()

        @use_exception_handlers(H)
        class Parent:
            pass

        class Child(Parent):
            pass

        assert USE_EXCEPTION_HANDLERS in Parent.__dict__
        assert USE_EXCEPTION_HANDLERS not in Child.__dict__


# ---------------------------------------------------------------------------
# Validation surface
# ---------------------------------------------------------------------------


class TestValidation:
    def test_exception_handler_requires_at_least_one_exception(self):
        with pytest.raises(ExceptionHandlerConfigError):

            @exception_handler()
            class _:
                async def catch(self, exc, request):
                    return None

    def test_exception_handler_rejects_non_exception_types(self):
        with pytest.raises(ExceptionHandlerConfigError):
            exception_handler(int)  # type: ignore[arg-type]

    def test_exception_handler_rejects_class_without_catch(self):
        with pytest.raises(ExceptionHandlerConfigError) as ei:

            @exception_handler(MyError)
            class _:
                pass

        assert "catch" in str(ei.value)

    def test_use_exception_handlers_rejects_undecorated_target(self):
        class NotAHandler:
            async def catch(self, exc, request):
                return None

        with pytest.raises(ExceptionHandlerConfigError):
            use_exception_handlers(NotAHandler)

    def test_use_exception_handlers_filters_none(self):
        @exception_handler(MyError)
        class H:
            async def catch(self, exc, request):
                return None

        @use_exception_handlers(None, H, None)
        class C:
            pass

        assert C.__dict__[USE_EXCEPTION_HANDLERS] == [H]

    def test_all_none_yields_empty_list(self):
        @use_exception_handlers(None, None)
        class C:
            pass

        assert C.__dict__[USE_EXCEPTION_HANDLERS] == []


# ---------------------------------------------------------------------------
# Subclass relationships are exercised correctly
# ---------------------------------------------------------------------------


class TestExceptionMatchSemantics:
    """``isinstance(exc, meta.exceptions)`` is what the dispatcher uses.

    We don't exercise dispatch here — those tests live in the integration
    suite — but we do verify the data shape supports a subclass match
    (which is a Python ``isinstance`` invariant).
    """

    def test_subclass_match(self):
        class A(Exception): ...

        class B(A): ...

        @exception_handler(A)
        class H:
            async def catch(self, exc, request):
                return None

        meta = getattr(H, EXCEPTION_HANDLER_META)
        assert isinstance(B(), meta.exceptions)

    def test_unrelated_does_not_match(self):
        class A(Exception): ...

        class C(Exception): ...

        @exception_handler(A)
        class H:
            async def catch(self, exc, request):
                return None

        meta = getattr(H, EXCEPTION_HANDLER_META)
        assert not isinstance(C(), meta.exceptions)
