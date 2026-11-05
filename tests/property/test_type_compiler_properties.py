"""Hypothesis properties of the native type compiler (PRD F02 §10.7)."""

from __future__ import annotations

import dataclasses
import datetime
import enum
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren.validation import TypeCompiler, ValidationFailed  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=150)


class Mood(enum.Enum):
    HAPPY = "happy"
    SAD = "sad"


@dataclasses.dataclass
class Leaf:
    count: int
    label: str
    flag: bool | None
    ratio: float
    mood: Mood


@dataclasses.dataclass
class Branch:
    leaf: Leaf
    leaves: list[Leaf]
    scores: dict[str, int]
    when: datetime.date
    note: str | None = None


_finite = st.floats(allow_nan=False, allow_infinity=False, width=64)
_leaves = st.builds(
    Leaf,
    count=st.integers(min_value=-(10**12), max_value=10**12),
    label=st.text(max_size=20),
    flag=st.none() | st.booleans(),
    ratio=_finite,
    mood=st.sampled_from(Mood),
)
_branches = st.builds(
    Branch,
    leaf=_leaves,
    leaves=st.lists(_leaves, max_size=3),
    scores=st.dictionaries(st.text(max_size=5), st.integers(), max_size=3),
    when=st.dates(),
    note=st.none() | st.text(max_size=10),
)

_COMPILED = TypeCompiler().compile(Branch)


@_TOLERANT
@given(_branches)
def test_valid_instances_round_trip_through_projection(branch: Branch) -> None:
    assert _COMPILED.validate(_COMPILED.project(branch)) == branch


def _leaf_paths(value: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    if isinstance(value, dict):
        out = [path] if path else []
        for key, item in value.items():
            out.extend(_leaf_paths(item, (*path, key)))
        return out
    if isinstance(value, list):
        out = [path]
        for index, item in enumerate(value):
            out.extend(_leaf_paths(item, (*path, index)))
        return out
    return [path]


def _replace(value: Any, path: tuple[Any, ...], new: Any) -> Any:
    if not path:
        return new
    head, rest = path[0], path[1:]
    if isinstance(value, dict):
        return {k: (_replace(v, rest, new) if k == head else v) for k, v in value.items()}
    return [(_replace(v, rest, new) if i == head else v) for i, v in enumerate(value)]


def _lookup(value: Any, path: tuple[Any, ...]) -> Any:
    for part in path:
        value = value[part]
    return value


@_TOLERANT
@given(_branches, st.data())
def test_a_mutated_leaf_is_reported_at_or_above_its_path(branch: Branch, data: st.DataObject) -> None:
    payload = _COMPILED.project(branch)
    paths = [p for p in _leaf_paths(payload) if p]
    path = data.draw(st.sampled_from(paths))
    original = _lookup(payload, path)
    wrong: Any = [] if isinstance(original, dict) else {}
    mutated = _replace(payload, path, wrong)

    with pytest.raises(ValidationFailed) as info:
        _COMPILED.validate(mutated)

    assert any(tuple(issue.loc) == path[: len(issue.loc)] for issue in info.value.issues)


_json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | _finite | st.text(max_size=5),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=5), children, max_size=3)
    ),
    max_leaves=10,
)


@_TOLERANT
@given(_json_values)
def test_validation_only_ever_raises_validation_failed(value: Any) -> None:
    try:
        _COMPILED.validate(value)
    except ValidationFailed as exc:
        assert exc.issues


# ---------------------------------------------------------------------------
# Differential test against pydantic.TypeAdapter (lax mode)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class Plain:
    count: int
    label: str
    flag: bool
    ratio: float
    tags: list[int]
    maybe: int | None = None


# Documented, deliberate differences are left out of the pools: the native
# engine rejects booleans for numbers, floats for booleans, single-letter
# boolean tokens ("y", "t") and Unix timestamps for dates; pydantic accepts them.
_INT_POOL = [0, 1, -3, "7", " 8 ", "x", "", 1.0, 1.5, None, [], {}, "1.5"]
_STR_POOL = ["", "a", 1, None, [], {}, True]
_BOOL_POOL = [True, False, 0, 1, 2, "true", "False", "yes", "off", "maybe", None, [], "1", "0"]
_FLOAT_POOL = [0, 1, 1.5, "2.5", "x", None, [], {}, "1e3"]
_TAGS_POOL = [[], [1, "2"], ["x"], "1", None, {}, [1.0], [None]]


@settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=300)
@given(
    count=st.sampled_from(_INT_POOL),
    label=st.sampled_from(_STR_POOL),
    flag=st.sampled_from(_BOOL_POOL),
    ratio=st.sampled_from(_FLOAT_POOL),
    tags=st.sampled_from(_TAGS_POOL),
    maybe=st.sampled_from([*_INT_POOL, "absent"]),
)
def test_native_lax_validation_agrees_with_pydantic(
    count: Any, label: Any, flag: Any, ratio: Any, tags: Any, maybe: Any
) -> None:
    pydantic = pytest.importorskip("pydantic")
    adapter = pydantic.TypeAdapter(Plain)
    payload = {"count": count, "label": label, "flag": flag, "ratio": ratio, "tags": tags}
    if maybe != "absent":
        payload["maybe"] = maybe

    try:
        adapter.validate_python(payload)
        pydantic_ok = True
    except pydantic.ValidationError:
        pydantic_ok = False
    try:
        TypeCompiler().compile(Plain).validate(payload)
        native_ok = True
    except ValidationFailed:
        native_ok = False

    assert native_ok is pydantic_ok, payload
