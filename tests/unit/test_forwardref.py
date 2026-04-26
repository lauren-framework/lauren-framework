"""Tests for :mod:`lauren._typing.forwardref`.

The resolver is used at startup by the DI container and the ASGI adapter,
so every scenario exercised here has a real analogue in user code:

* PEP 563 stringified annotations via ``from __future__ import annotations``
* self-referential class annotations (``left: "Node"`` inside ``Node``)
* forward refs that point at nested-scope types (only resolvable by
  walking the caller frame stack)
* unresolvable names \u2014 the lenient strategy returns a ``ForwardRef``
  while the strict strategy raises.
"""

from __future__ import annotations

from typing import Annotated, ForwardRef, Optional, Union

import pytest

from lauren._typing import (
    ForwardRefError,
    ResolutionStrategy,
    resolve_forwardref,
    resolve_type_hints,
)


# ---------------------------------------------------------------------------
# Plain forward refs & string annotations
# ---------------------------------------------------------------------------


def test_resolve_forwardref_builtin_name() -> None:
    assert resolve_forwardref("int") is int
    assert resolve_forwardref(ForwardRef("str")) is str


def test_resolve_forwardref_typing_generic() -> None:
    result = resolve_forwardref("list[int]")
    assert result == list[int]


def test_resolve_forwardref_owner_module_globals() -> None:
    class Widget:
        pass

    # ``Widget`` lives in this test module's globals after the class
    # statement runs \u2014 resolvable by module-level lookup.
    globals()["Widget"] = Widget
    try:
        resolved = resolve_forwardref("Widget", owner=Widget)
        assert resolved is Widget
    finally:
        del globals()["Widget"]


def test_resolve_forwardref_lenient_returns_forwardref() -> None:
    resolved = resolve_forwardref("DefinitelyNotDefined")
    assert isinstance(resolved, ForwardRef)
    assert resolved.__forward_arg__ == "DefinitelyNotDefined"


def test_resolve_forwardref_strict_raises() -> None:
    with pytest.raises(ForwardRefError):
        resolve_forwardref("DefinitelyNotDefined", strategy=ResolutionStrategy.STRICT)


def test_resolve_forwardref_replace_any() -> None:
    from typing import Any as _Any

    resolved = resolve_forwardref(
        "StillUndefined", strategy=ResolutionStrategy.REPLACE_ANY
    )
    assert resolved is _Any


# ---------------------------------------------------------------------------
# Function annotations
# ---------------------------------------------------------------------------


class _Repo:
    pass


def _handler(repo: "_Repo", count: int = 0) -> "_Repo":
    return repo


def test_resolve_type_hints_stringified_function() -> None:
    hints = resolve_type_hints(_handler, include_extras=False)
    assert hints["repo"] is _Repo
    assert hints["count"] is int
    assert hints["return"] is _Repo


def test_resolve_type_hints_generic_with_forwardref() -> None:
    def handler(items: "list[_Repo]") -> None: ...

    hints = resolve_type_hints(handler, include_extras=False)
    assert hints["items"] == list[_Repo]


def test_resolve_type_hints_optional_forwardref() -> None:
    def handler(repo: "Optional[_Repo]") -> None: ...

    hints = resolve_type_hints(handler, include_extras=False)
    # Optional[X] normalises to Union[X, None] under get_args.
    assert hints["repo"] == Optional[_Repo]


# ---------------------------------------------------------------------------
# Class annotations (self-referential)
# ---------------------------------------------------------------------------


class Node:
    value: int
    left: "Node | None"
    right: "Node | None"


def test_resolve_type_hints_self_referential_class() -> None:
    hints = resolve_type_hints(Node, include_extras=False)
    assert hints["value"] is int
    assert hints["left"] == (Node | None)
    assert hints["right"] == (Node | None)


# ---------------------------------------------------------------------------
# ``Annotated`` metadata preservation
# ---------------------------------------------------------------------------


class _PathMarker:
    pass


def test_resolve_type_hints_preserves_annotated_metadata() -> None:
    def handler(
        user_id: Annotated[int, _PathMarker],
    ) -> None: ...

    hints = resolve_type_hints(handler, include_extras=True)
    ann = hints["user_id"]
    assert ann.__origin__ is int
    assert _PathMarker in ann.__metadata__


def test_resolve_type_hints_strips_annotated_when_not_requested() -> None:
    def handler(
        user_id: Annotated[int, _PathMarker],
    ) -> None: ...

    hints = resolve_type_hints(handler, include_extras=False)
    assert hints["user_id"] is int


# ---------------------------------------------------------------------------
# Typing-collision guard
# ---------------------------------------------------------------------------


def test_nested_user_class_does_not_shadow_typing_same_name() -> None:
    """A user class ``Counter`` defined in a nested scope should NOT
    silently resolve to ``typing.Counter`` when the stdlib fast path
    raises ``NameError``. The resolver must keep the annotation as a
    :class:`ForwardRef` so an outer frame-walk can recover it.
    """

    def make():
        class Counter:
            pass

        class Service:
            def __init__(self, c: Counter) -> None: ...

        return Counter, Service

    Counter, Service = make()
    hints = resolve_type_hints(Service.__init__, include_extras=False)
    # Lenient resolver should surface an unresolved ForwardRef rather
    # than picking up typing.Counter from the fallback layer.
    c_hint = hints["c"]
    assert isinstance(c_hint, ForwardRef)
    assert c_hint.__forward_arg__ == "Counter"


# ---------------------------------------------------------------------------
# Union / PEP 604 shapes
# ---------------------------------------------------------------------------


def test_resolve_type_hints_pep604_union() -> None:
    def handler(x: "int | str | None") -> None: ...

    hints = resolve_type_hints(handler, include_extras=False)
    assert hints["x"] == (int | str | None)


def test_resolve_type_hints_nested_union_with_forwardref() -> None:
    def handler(x: "Union[_Repo, int]") -> None: ...

    hints = resolve_type_hints(handler, include_extras=False)
    assert hints["x"] == Union[_Repo, int]
