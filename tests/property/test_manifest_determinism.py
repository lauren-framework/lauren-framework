"""Manifest snapshots stay stable for generated route/module declarations."""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings, strategies as st  # noqa: E402

from lauren import LaurenFactory, controller, describe_app, get, module  # noqa: E402


def _controller(index: int) -> type:
    async def show(self) -> dict:
        return {"index": index}

    return controller(f"/generated/{index}")(type(f"Controller{index}", (), {"show": get("/")(show)}))


@given(st.lists(st.integers(min_value=0, max_value=30), min_size=1, max_size=6, unique=True))
@settings(max_examples=20, deadline=None)
def test_manifest_is_stable_across_repeated_builds(indices: list[int]) -> None:
    root = module(controllers=[_controller(index) for index in indices])(type("Root", (), {}))
    first = describe_app(LaurenFactory.create(root))
    second = describe_app(LaurenFactory.create(root))

    assert first.to_json() == second.to_json()
    assert [r.path for r in first.routes] == sorted(r.path for r in first.routes)
