"""Unit tests for ``CronExpression``: parsing, matching, next-fire search and DST policy."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from itertools import islice
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from lauren.exceptions import DecoratorUsageError
from lauren.scheduling import CronExpression

UTC = timezone.utc


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:  # pragma: no cover - Windows without tzdata
        pytest.skip(f"time-zone data for {name} is not installed")


def _next(expr: str, start: datetime, count: int = 1) -> list[datetime]:
    return list(islice(CronExpression(expr).iter_from(start), count))


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "start", "expected"),
    [
        ("* * * * *", datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC), datetime(2026, 1, 1, 0, 1, tzinfo=UTC)),
        ("*/15 * * * *", datetime(2026, 1, 1, 0, 1, tzinfo=UTC), datetime(2026, 1, 1, 0, 15, tzinfo=UTC)),
        ("5,35 * * * *", datetime(2026, 1, 1, 0, 6, tzinfo=UTC), datetime(2026, 1, 1, 0, 35, tzinfo=UTC)),
        ("10-12 * * * *", datetime(2026, 1, 1, 0, 11, tzinfo=UTC), datetime(2026, 1, 1, 0, 12, tzinfo=UTC)),
        ("0-30/10 * * * *", datetime(2026, 1, 1, 0, 21, tzinfo=UTC), datetime(2026, 1, 1, 0, 30, tzinfo=UTC)),
        ("5/20 * * * *", datetime(2026, 1, 1, 0, 6, tzinfo=UTC), datetime(2026, 1, 1, 0, 25, tzinfo=UTC)),
        ("0 3 * * *", datetime(2026, 1, 1, 4, 0, tzinfo=UTC), datetime(2026, 1, 2, 3, 0, tzinfo=UTC)),
        ("0 0 1 JAN *", datetime(2026, 1, 1, 0, 0, tzinfo=UTC), datetime(2027, 1, 1, 0, 0, tzinfo=UTC)),
        ("0 9 * * MON-FRI", datetime(2026, 1, 2, 10, 0, tzinfo=UTC), datetime(2026, 1, 5, 9, 0, tzinfo=UTC)),
        ("0 0 * * sun", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC)),
        ("0 0 * * 7", datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC)),
        ("0 0 * * FRI-SUN", datetime(2026, 1, 3, 1, tzinfo=UTC), datetime(2026, 1, 4, tzinfo=UTC)),
        ("0 0 29 2 *", datetime(2026, 3, 1, tzinfo=UTC), datetime(2028, 2, 29, tzinfo=UTC)),
        ("0 0 31 * *", datetime(2026, 4, 1, tzinfo=UTC), datetime(2026, 5, 31, tzinfo=UTC)),
        ("59 23 31 12 *", datetime(2026, 6, 1, tzinfo=UTC), datetime(2026, 12, 31, 23, 59, tzinfo=UTC)),
    ],
)
def test_next_after_finds_the_first_matching_minute(expr: str, start: datetime, expected: datetime) -> None:
    assert CronExpression(expr).next_after(start) == expected


@pytest.mark.parametrize(
    ("macro", "expected"),
    [
        ("@yearly", datetime(2027, 1, 1, tzinfo=UTC)),
        ("@annually", datetime(2027, 1, 1, tzinfo=UTC)),
        ("@monthly", datetime(2026, 2, 1, tzinfo=UTC)),
        ("@weekly", datetime(2026, 1, 4, tzinfo=UTC)),
        ("@daily", datetime(2026, 1, 2, tzinfo=UTC)),
        ("@midnight", datetime(2026, 1, 2, tzinfo=UTC)),
        ("@hourly", datetime(2026, 1, 1, 13, tzinfo=UTC)),
    ],
)
def test_macros_expand_to_their_standard_schedule(macro: str, expected: datetime) -> None:
    assert CronExpression(macro).next_after(datetime(2026, 1, 1, 12, 30, tzinfo=UTC)) == expected


def test_six_fields_add_a_leading_seconds_field() -> None:
    expr = CronExpression("*/10 * * * * *")

    times = list(islice(expr.iter_from(datetime(2026, 1, 1, 0, 0, 5, tzinfo=UTC)), 3))

    assert expr.has_seconds
    assert [t.second for t in times] == [10, 20, 30]


def test_both_day_fields_restricted_match_either_posix_style() -> None:
    # The 1st and 15th of the month AND every Sunday.
    times = _next("0 0 1,15 * SUN", datetime(2026, 1, 1, 0, 0, tzinfo=UTC), 4)

    assert [t.day for t in times] == [4, 11, 15, 18]


def test_star_step_day_of_month_with_weekday_requires_both() -> None:
    # Vixie cron: a field starting with "*" counts as unrestricted, so the AND applies.
    times = _next("0 0 */2 * MON", datetime(2026, 1, 1, tzinfo=UTC), 2)

    assert all(t.isoweekday() == 1 and t.day % 2 == 1 for t in times)


def test_iter_from_is_strictly_increasing_across_a_year_boundary() -> None:
    times = _next("0 0 28-31 * *", datetime(2026, 12, 27, tzinfo=UTC), 6)

    assert times == sorted(times)
    assert len(set(times)) == len(times)
    assert times[4] == datetime(2027, 1, 28, tzinfo=UTC)


def test_whitespace_is_normalised_and_used_for_equality() -> None:
    a = CronExpression("  0   3 * *  * ")
    b = CronExpression("0 3 * * *")

    assert a == b
    assert hash(a) == hash(b)
    assert a.expression == "0 3 * * *"
    assert str(a) == "0 3 * * *"
    assert repr(a) == "CronExpression('0 3 * * *')"
    assert a != "0 3 * * *"


def test_matches_reads_the_fields_in_the_expression_zone() -> None:
    paris = _zone("Europe/Paris")
    expr = CronExpression("0 3 * * *", tz=paris)

    assert expr.matches(datetime(2026, 1, 1, 2, 0, tzinfo=UTC))  # 03:00 in Paris
    assert not expr.matches(datetime(2026, 1, 1, 3, 0, tzinfo=UTC))
    assert repr(expr) == "CronExpression('0 3 * * *', tz='Europe/Paris')"


def test_naive_moment_without_zone_returns_naive_wall_time() -> None:
    assert CronExpression("30 * * * *").next_after(datetime(2026, 1, 1, 10, 0)) == datetime(
        2026, 1, 1, 10, 30
    )


def test_naive_moment_with_zone_is_read_as_wall_time_in_that_zone() -> None:
    paris = _zone("Europe/Paris")

    result = CronExpression("0 12 * * *", tz=paris).next_after(datetime(2026, 1, 1, 9, 0))

    assert result == datetime(2026, 1, 1, 12, 0, tzinfo=paris)


def test_aware_moment_keeps_its_own_zone_without_an_expression_zone() -> None:
    tz = timezone(timedelta(hours=5))

    result = CronExpression("0 12 * * *").next_after(datetime(2026, 1, 1, 9, 0, tzinfo=tz))

    assert result == datetime(2026, 1, 1, 12, 0, tzinfo=tz)


def test_utc_name_needs_no_tzdata() -> None:
    assert CronExpression("0 0 * * *", tz="UTC").tz is timezone.utc


def test_with_tz_returns_a_copy_in_the_new_zone() -> None:
    base = CronExpression("0 0 * * *")

    assert base.with_tz("UTC").tz is timezone.utc
    assert base.tz is None


# ---------------------------------------------------------------------------
# Invalid input — each error names the field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expr", "field", "reason"),
    [
        ("61 * * * *", "minute", "out_of_range"),
        ("* 24 * * *", "hour", "out_of_range"),
        ("* * 0 * *", "day-of-month", "out_of_range"),
        ("* * * 13 *", "month", "out_of_range"),
        ("* * * * 8", "day-of-week", "out_of_range"),
        ("60 * * * * *", "second", "out_of_range"),
        ("*/0 * * * *", "minute", "invalid_step"),
        ("*/x * * * *", "minute", "invalid_step"),
        ("30-10 * * * *", "minute", "reversed_range"),
        ("a * * * *", "minute", "invalid_value"),
        ("* * * FOO *", "month", "invalid_value"),
        ("1,,2 * * * *", "minute", "empty_item"),
        ("* * L * *", "day-of-month", "unsupported_token"),
        ("* * 15W * *", "day-of-month", "unsupported_token"),
        ("* * * * 1#3", "day-of-week", "unsupported_token"),
        ("* * ? * *", "day-of-month", "unsupported_token"),
    ],
)
def test_invalid_field_names_the_field(expr: str, field: str, reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        CronExpression(expr)

    assert info.value.detail["field"] == field
    assert info.value.detail["reason"] == reason
    assert info.value.detail["expression"] == expr
    assert "Example:" in info.value.message


def test_out_of_range_reports_the_allowed_range() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        CronExpression("61 * * * *")

    assert info.value.detail["allowed"] == "0-59"
    assert "0-59" in info.value.message


@pytest.mark.parametrize(
    ("expr", "reason"),
    [
        ("", "empty"),
        ("* * * *", "field_count"),
        ("* * * * * * *", "field_count"),
        ("@reboot", "unsupported_macro"),
        ("@sometimes", "unsupported_macro"),
        ("0 0 30 2 *", "never_fires"),
        ("0 0 31 4,6,9,11 *", "never_fires"),
    ],
)
def test_structurally_invalid_expressions_are_rejected(expr: str, reason: str) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        CronExpression(expr)

    assert info.value.detail["reason"] == reason


def test_unknown_time_zone_is_rejected() -> None:
    with pytest.raises(DecoratorUsageError) as info:
        CronExpression("0 0 * * *", tz="Mars/Olympus_Mons")

    assert info.value.detail["reason"] == "unknown_timezone"
    assert "tzdata" in info.value.message


@pytest.mark.parametrize("tz", ["", 42])
def test_non_string_time_zone_is_rejected(tz: object) -> None:
    with pytest.raises(DecoratorUsageError) as info:
        CronExpression("0 0 * * *", tz=tz)  # type: ignore[arg-type]

    assert info.value.detail["reason"] == "invalid_timezone"


def test_restricted_weekday_rescues_an_impossible_day_of_month() -> None:
    # POSIX OR: "30 February OR any Monday" fires on Mondays.
    result = CronExpression("0 0 30 2 MON").next_after(datetime(2026, 1, 1, tzinfo=UTC))

    assert result.isoweekday() == 1


# ---------------------------------------------------------------------------
# DST policy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("zone", "start", "expected_utc"),
    [
        # Spring forward: 02:30 does not exist; fire once at the first valid instant (03:00 local).
        ("America/New_York", datetime(2026, 3, 8, 0, 0), datetime(2026, 3, 8, 7, 0, tzinfo=UTC)),
        ("Europe/Paris", datetime(2026, 3, 29, 0, 0), datetime(2026, 3, 29, 1, 0, tzinfo=UTC)),
    ],
)
def test_time_in_a_spring_forward_gap_fires_once_after_the_gap(
    zone: str, start: datetime, expected_utc: datetime
) -> None:
    tz = _zone(zone)
    expr = CronExpression("30 2 * * *", tz=tz)

    first = expr.next_after(start.replace(tzinfo=tz))
    second = expr.next_after(first)

    assert first.astimezone(UTC) == expected_utc
    assert first.astimezone(tz).hour == 3 and first.astimezone(tz).minute == 0
    assert second.astimezone(tz).replace(tzinfo=None) == start.replace(hour=2, minute=30) + timedelta(days=1)


def test_every_minute_in_a_gap_collapses_to_one_fire() -> None:
    tz = _zone("America/New_York")
    expr = CronExpression("* 2 * * *", tz=tz)

    first = expr.next_after(datetime(2026, 3, 8, 1, 59, tzinfo=tz))
    second = expr.next_after(first)

    assert first.astimezone(tz).replace(tzinfo=None) == datetime(2026, 3, 8, 3, 0)
    assert second.astimezone(tz).replace(tzinfo=None) == datetime(2026, 3, 9, 2, 0)


@pytest.mark.parametrize(
    ("zone", "day", "first_utc"),
    [
        # Fall back: 01:30 happens twice; fire at the first occurrence only.
        ("America/New_York", datetime(2026, 11, 1), datetime(2026, 11, 1, 5, 30, tzinfo=UTC)),
        ("Europe/Paris", datetime(2026, 10, 25), datetime(2026, 10, 25, 0, 30, tzinfo=UTC)),
    ],
)
def test_ambiguous_time_fires_once_at_its_first_occurrence(
    zone: str, day: datetime, first_utc: datetime
) -> None:
    tz = _zone(zone)
    hour = 1 if zone == "America/New_York" else 2
    expr = CronExpression(f"30 {hour} * * *", tz=tz)

    first = expr.next_after(day.replace(tzinfo=tz))
    second = expr.next_after(first)

    assert first.astimezone(UTC) == first_utc
    # The repeated hour does not fire again: the next fire is a day later.
    assert second.astimezone(UTC) - first.astimezone(UTC) > timedelta(hours=23)


def test_search_from_inside_the_repeated_hour_never_goes_back_in_time() -> None:
    tz = _zone("America/New_York")
    # 01:10 EST, the *second* 01:10 of the fall-back night.
    inside = datetime(2026, 11, 1, 1, 10, tzinfo=tz, fold=1)

    result = CronExpression("*/15 * * * *", tz=tz).next_after(inside)

    assert result.astimezone(UTC) > inside.astimezone(UTC)
    assert result.astimezone(tz).replace(tzinfo=None) == datetime(2026, 11, 1, 2, 0)
