"""Hypothesis properties of response projection (PRD F01 §10.8).

Arbitrary extra keys at every depth never survive the projection onto a
declared dataclass tree, and projecting an already-projected value is a no-op.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._schema._contract import build_projector  # noqa: E402
from lauren.validation import TypeCompiler  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=150)


@dataclasses.dataclass
class Leaf:
    count: int
    label: str


@dataclasses.dataclass
class Branch:
    leaf: Leaf
    leaves: list[Leaf]
    tags: dict[str, Leaf]
    note: str | None = None


DECLARED_BRANCH = {"leaf", "leaves", "tags", "note"}
DECLARED_LEAF = {"count", "label"}

_extra_keys = st.dictionaries(
    st.text(min_size=1, max_size=8).filter(lambda k: k not in DECLARED_BRANCH | DECLARED_LEAF),
    st.integers() | st.text(max_size=5) | st.none(),
    max_size=3,
)


@st.composite
def _leaf_payloads(draw: Any) -> dict[str, Any]:
    return {"count": draw(st.integers()), "label": draw(st.text(max_size=10)), **draw(_extra_keys)}


@st.composite
def _branch_payloads(draw: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "leaf": draw(_leaf_payloads()),
        "leaves": draw(st.lists(_leaf_payloads(), max_size=3)),
        "tags": draw(st.dictionaries(st.text(max_size=4), _leaf_payloads(), max_size=3)),
        **draw(_extra_keys),
    }
    if draw(st.booleans()):
        payload["note"] = draw(st.none() | st.text(max_size=6))
    return payload


def _projector(strict: bool) -> Any:
    compiled = TypeCompiler().compile(Branch, mode="strict" if strict else "lax")
    return build_projector(compiled.node, strict=strict, limit=50)


_FILTER = _projector(strict=False)
_STRICT = _projector(strict=True)


def _assert_declared_only(branch: dict[str, Any]) -> None:
    assert set(branch) <= DECLARED_BRANCH
    leaves = [branch["leaf"], *branch["leaves"], *branch["tags"].values()]
    for leaf in leaves:
        assert set(leaf) == DECLARED_LEAF


@_TOLERANT
@given(_branch_payloads())
def test_projected_keys_are_a_subset_of_declared_keys(payload: dict[str, Any]) -> None:
    _assert_declared_only(_FILTER(payload))


@_TOLERANT
@given(_branch_payloads())
def test_strict_projection_of_valid_payloads_keeps_only_declared_keys(payload: dict[str, Any]) -> None:
    _assert_declared_only(_STRICT(payload))


@_TOLERANT
@given(_branch_payloads())
def test_projection_is_idempotent(payload: dict[str, Any]) -> None:
    once = _FILTER(payload)

    assert _FILTER(once) == once
