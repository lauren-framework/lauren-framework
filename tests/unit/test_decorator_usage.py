"""Enforce that configurable decorators require parentheses.

``@controller``, ``@injectable``, ``@module`` and the HTTP verb decorators
all accept configuration arguments with sensible defaults. Used bare
(``@controller`` instead of ``@controller()``) Python would quietly pass
the decorated object as the first positional argument \u2014 almost always
producing a silently-broken registration or a cryptic downstream error.

These tests lock in the contract: bare usage raises
:class:`DecoratorUsageError` with an actionable message, and empty
parentheses (``@controller()``) continues to work.
"""

from __future__ import annotations

import pytest

from lauren import (
    controller,
    delete,
    get,
    head,
    injectable,
    module,
    options,
    patch,
    post,
    put,
)
from lauren.exceptions import DecoratorUsageError


class TestBareDecoratorUsage:
    def test_controller_bare_rejected(self):
        with pytest.raises(DecoratorUsageError) as ei:

            @controller
            class C:
                pass

        msg = str(ei.value)
        assert "@controller must be used with parentheses" in msg
        assert "@controller()" in msg

    def test_injectable_bare_rejected(self):
        with pytest.raises(DecoratorUsageError) as ei:

            @injectable
            class I:
                pass

        assert "@injectable must be used with parentheses" in str(ei.value)

    def test_module_bare_rejected(self):
        with pytest.raises(DecoratorUsageError) as ei:

            @module
            class M:
                pass

        assert "@module must be used with parentheses" in str(ei.value)

    @pytest.mark.parametrize(
        "decorator, name",
        [
            (get, "get"),
            (post, "post"),
            (put, "put"),
            (patch, "patch"),
            (delete, "delete"),
            (head, "head"),
            (options, "options"),
        ],
    )
    def test_http_verb_bare_rejected(self, decorator, name):
        with pytest.raises(DecoratorUsageError) as ei:

            @decorator
            def handler():
                pass

        assert f"@{name} must be used with parentheses" in str(ei.value)


class TestCorrectUsageStillWorks:
    def test_empty_parens_work_on_all(self):
        @controller()
        class C1:
            pass

        @injectable()
        class I1:
            pass

        @module()
        class M1:
            pass

        @get()
        def h_get():
            pass

        @post()
        def h_post():
            pass

        # Each must be correctly marked.
        assert hasattr(C1, "__lauren_controller__")
        assert hasattr(I1, "__lauren_injectable__")
        assert hasattr(M1, "__lauren_module__")
        assert h_get.__lauren_route__[0].method == "GET"
        assert h_post.__lauren_route__[0].method == "POST"

    def test_full_arguments_work(self):
        @controller("/prefix", tags=["x"])
        class C:
            pass

        @get("/path", summary="hello", operation_id="op")
        def h():
            pass

        @module(controllers=[C])
        class M:
            pass

        assert C.__lauren_controller__.prefix == "/prefix"
        assert h.__lauren_route__[0].path == "/path"
        assert M.__lauren_module__.controllers == (C,)

    def test_error_detail_carries_target_and_decorator_name(self):
        with pytest.raises(DecoratorUsageError) as ei:

            @controller
            class SomeClass:
                pass

        detail = ei.value.detail
        assert detail["decorator"] == "controller"
        assert "SomeClass" in detail["target"]
