"""Hypothesis property tests for health-status aggregation.

The aggregate is a pure function of the ``(critical, status)`` pairs — order
does not matter — and it is monotonic: making any check worse never makes the
aggregate better. Skipped automatically when hypothesis is not installed.
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._health._types import aggregate  # noqa: E402
from lauren.health import HealthStatus  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])
_RANK = {HealthStatus.UP: 0, HealthStatus.DEGRADED: 1, HealthStatus.DOWN: 2}

pairs = st.lists(st.tuples(st.booleans(), st.sampled_from(list(HealthStatus))), max_size=12)


@_TOLERANT
@given(pairs, st.randoms())
def test_aggregation_ignores_order(outcomes: list[tuple[bool, HealthStatus]], rnd: object) -> None:
    shuffled = list(outcomes)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]

    assert aggregate(shuffled) is aggregate(outcomes)


@_TOLERANT
@given(pairs, st.data())
def test_making_a_check_worse_never_improves_the_aggregate(
    outcomes: list[tuple[bool, HealthStatus]], data: st.DataObject
) -> None:
    if not outcomes:
        return
    index = data.draw(st.integers(min_value=0, max_value=len(outcomes) - 1))
    critical, status = outcomes[index]
    worse = data.draw(st.sampled_from([s for s in HealthStatus if _RANK[s] >= _RANK[status]]))
    degraded = [*outcomes[:index], (critical, worse), *outcomes[index + 1 :]]

    assert _RANK[aggregate(degraded)] >= _RANK[aggregate(outcomes)]


@_TOLERANT
@given(pairs)
def test_the_aggregate_is_down_exactly_when_a_critical_check_is_down(
    outcomes: list[tuple[bool, HealthStatus]],
) -> None:
    critical_down = any(critical and status is HealthStatus.DOWN for critical, status in outcomes)

    assert (aggregate(outcomes) is HealthStatus.DOWN) is critical_down


@_TOLERANT
@given(pairs)
def test_the_aggregate_is_up_exactly_when_every_check_is_up(
    outcomes: list[tuple[bool, HealthStatus]],
) -> None:
    all_up = all(status is HealthStatus.UP for _critical, status in outcomes)

    assert (aggregate(outcomes) is HealthStatus.UP) is all_up
