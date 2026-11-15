"""Hypothesis properties of the compile-time metadata merge (F23 §10).

For random stacks of levels: ``union`` is associative and order-preserving,
``override`` equals "the last level that set the key", and merging is
idempotent.
"""

from __future__ import annotations

from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren import MetadataKey, MetadataSetKey  # noqa: E402
from lauren._metadata import merge_metadata_levels  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=60)

ROLES = MetadataSetKey[str]("prop.roles")
LIMIT = MetadataKey[int]("prop.limit", default=0)

_items = st.lists(st.sampled_from("abcdef"), max_size=4).map(tuple)
_set_levels = st.lists(st.one_of(st.none(), _items), min_size=1, max_size=5)
_scalar_levels = st.lists(st.one_of(st.none(), st.integers(-5, 5)), min_size=1, max_size=5)


def _merge(key: Any, levels: list[Any]) -> Any:
    stack = [
        (
            f"l{i}",
            {} if value is None else {key.name: key._normalize(value)},
            {} if value is None else {key.name: key},
        )
        for i, value in enumerate(levels)
    ]
    return merge_metadata_levels(stack).get(key.name)


def _expected_union(levels: list[Any]) -> tuple[str, ...] | None:
    out: list[str] = []
    seen_any = False
    for value in levels:
        if value is None:
            continue
        seen_any = True
        out.extend(item for item in value if item not in out)
    return tuple(out) if seen_any else None


@_TOLERANT
@given(_set_levels)
def test_union_is_the_ordered_union_outer_first(levels: list[Any]) -> None:
    assert _merge(ROLES, levels) == _expected_union(levels)


@_TOLERANT
@given(_set_levels, _set_levels)
def test_union_is_associative(left: list[Any], right: list[Any]) -> None:
    whole = _merge(ROLES, left + right)
    split = _merge(ROLES, [_merge(ROLES, left), _merge(ROLES, right)])

    assert whole == split


@_TOLERANT
@given(_scalar_levels)
def test_override_is_the_last_level_that_set_the_key(levels: list[Any]) -> None:
    present = [v for v in levels if v is not None]

    assert _merge(LIMIT, levels) == (present[-1] if present else None)


@_TOLERANT
@given(_set_levels)
def test_merging_is_idempotent(levels: list[Any]) -> None:
    once = _merge(ROLES, levels)

    assert _merge(ROLES, [once, once]) == once
