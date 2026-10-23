"""Property tests for event dispatch ordering (``lauren._events.DispatchTable``).

For any set of handler declarations — a priority, a subscribed class from a
small hierarchy, and the method declaring it — delivering an event class
runs every matching method exactly once, in the stable order of
``(-priority, discovery index)``.
"""

from __future__ import annotations

from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._events import DispatchTable, HandlerPlan  # noqa: E402


class Base:
    pass


class Mid(Base):
    pass


class Leaf(Mid):
    pass


class Other:
    pass


HIERARCHY = (Base, Mid, Leaf, Other)

declarations = st.lists(
    st.tuples(
        st.integers(min_value=-3, max_value=3),  # priority
        st.sampled_from(HIERARCHY),  # subscribed class
        st.integers(min_value=0, max_value=5),  # which method declares it
    ),
    max_size=24,
)


def build(decls: list[tuple[int, type, int]]) -> tuple[DispatchTable, list[HandlerPlan]]:
    methods: dict[int, Any] = {}
    plans: list[HandlerPlan] = []
    for index, (priority, event_type, method) in enumerate(decls):
        entry = methods.setdefault(method, object())
        plans.append(
            HandlerPlan(
                index=index,
                name=f"M.m{method}",
                event_type=event_type,
                mode="inline",
                priority=priority,
                errors="propagate",
                entry=entry,  # type: ignore[arg-type]
                event_param="event",
                context_params=(),
                di_params=(),
                needs_instance=True,
                needs_scope=False,
            )
        )
    return DispatchTable(plans), plans


@settings(max_examples=200, deadline=None)
@given(declarations)
def test_every_matching_method_runs_once_in_priority_then_discovery_order(
    decls: list[tuple[int, type, int]],
) -> None:
    table, plans = build(decls)

    for event_type in HIERARCHY:
        resolved = table.lookup(event_type)

        # Each method at most once, and exactly the methods subscribed along the MRO.
        entries = [id(p.entry) for p in resolved]
        assert len(entries) == len(set(entries))
        matching = {id(p.entry) for p in plans if issubclass(event_type, p.event_type)}
        assert set(entries) == matching
        # The order is the stable sort by (-priority, index).
        keys = [(-p.priority, p.index) for p in resolved]
        assert keys == sorted(keys)


@settings(max_examples=200, deadline=None)
@given(declarations)
def test_the_most_specific_subscription_of_a_method_wins(decls: list[tuple[int, type, int]]) -> None:
    table, plans = build(decls)

    for event_type in HIERARCHY:
        for plan in table.lookup(event_type):
            same_method = [p for p in plans if p.entry is plan.entry and issubclass(event_type, p.event_type)]
            mro = event_type.__mro__
            closest = min(mro.index(p.event_type) for p in same_method)
            assert mro.index(plan.event_type) == closest
            # Among equally specific subscriptions, the first declared is kept.
            assert plan.index == min(p.index for p in same_method if mro.index(p.event_type) == closest)
