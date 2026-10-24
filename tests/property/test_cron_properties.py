"""Hypothesis property tests for ``CronExpression``.

For random valid expressions and start instants: ``next_after(t) > t``, the
result matches every field, ``iter_from`` is strictly increasing, and in UTC
``next_after`` agrees with a brute-force minute-by-minute search over a
bounded horizon. Skipped automatically when hypothesis is not installed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import islice

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren.exceptions import DecoratorUsageError  # noqa: E402
from lauren.scheduling import CronExpression  # noqa: E402

_TOLERANT = settings(deadline=None, max_examples=150, suppress_health_check=[HealthCheck.too_slow])
UTC = timezone.utc
_HORIZON = timedelta(days=4)


def _field(low: int, high: int, *, star_weight: int = 1) -> st.SearchStrategy[str]:
    value = st.integers(low, high)
    step = st.integers(1, max(1, (high - low) // 2 + 1))

    @st.composite
    def ranged(draw: st.DrawFn) -> str:
        a, b = sorted((draw(value), draw(value)))
        return f"{a}-{b}"

    @st.composite
    def stepped(draw: st.DrawFn) -> str:
        a, b = sorted((draw(value), draw(value)))
        return f"{a}-{b}/{draw(step)}"

    item = st.one_of(value.map(str), ranged(), stepped(), step.map(lambda n: f"*/{n}"))
    listed = st.lists(item, min_size=1, max_size=3).map(",".join)
    return st.one_of(*([st.just("*")] * star_weight), item, listed)


_expressions = st.tuples(
    _field(0, 59),
    _field(0, 23),
    _field(1, 31, star_weight=4),
    _field(1, 12, star_weight=4),
    _field(0, 7, star_weight=4),
).map(" ".join)

_starts = st.datetimes(
    min_value=datetime(2020, 1, 1), max_value=datetime(2035, 12, 31), timezones=st.just(UTC)
).map(lambda d: d.replace(microsecond=0))


def _reference_matcher(expression: str):
    """An independent matcher for the generated subset (numbers, ranges, steps, lists, ``*``)."""
    bounds = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
    sets: list[set[int]] = []
    for text, (low, high) in zip(expression.split(), bounds):
        values: set[int] = set()
        for item in text.split(","):
            base, _, step = item.partition("/")
            if base == "*":
                a, b = low, high
            elif "-" in base:
                a, b = (int(x) for x in base.split("-"))
            else:
                a = b = int(base)
            values.update(range(a, b + 1, int(step) if step else 1))
        sets.append(values)
    minutes, hours, days, months, weekdays = sets
    weekdays = {0 if d == 7 else d for d in weekdays}
    dom_star, dow_star = expression.split()[2].startswith("*"), expression.split()[4].startswith("*")

    def matches(moment: datetime) -> bool:
        dom = moment.day in days
        dow = moment.isoweekday() % 7 in weekdays
        day = (dom and dow) if (dom_star or dow_star) else (dom or dow)
        return moment.minute in minutes and moment.hour in hours and moment.month in months and day

    return matches


def _parse(expression: str) -> CronExpression:
    try:
        return CronExpression(expression)
    except DecoratorUsageError:  # e.g. "31 in February only"
        hypothesis.assume(False)
        raise  # pragma: no cover


@_TOLERANT
@given(_expressions, _starts)
def test_next_after_is_strictly_later_and_matches(expression: str, start: datetime) -> None:
    cron = _parse(expression)

    result = cron.next_after(start)

    assert result > start
    assert result.second == 0
    assert cron.matches(result)


@_TOLERANT
@given(_expressions, _starts)
def test_iter_from_is_strictly_increasing(expression: str, start: datetime) -> None:
    times = list(islice(_parse(expression).iter_from(start), 5))

    assert all(earlier < later for earlier, later in zip(times, times[1:]))


@_TOLERANT
@given(_expressions, _starts)
def test_next_after_agrees_with_a_brute_force_search(expression: str, start: datetime) -> None:
    cron = _parse(expression)
    reference = _reference_matcher(expression)
    candidate = start.replace(second=0) + timedelta(minutes=1)
    end = start + _HORIZON
    brute = None
    while candidate <= end:
        if reference(candidate):
            brute = candidate
            break
        candidate += timedelta(minutes=1)

    result = cron.next_after(start)

    if brute is None:
        assert result > end
    else:
        assert result == brute
