"""Cron expressions: parsing and next-fire computation (stdlib only, private).

:class:`CronExpression` is public through :mod:`lauren.scheduling`. Five
fields (``minute hour day-of-month month day-of-week``) or six with a
leading seconds field; lists, ranges, steps, month and weekday names and the
``@daily``-style macros. Quartz extensions (``L``, ``W``, ``#``, ``?``) and
``@reboot`` are rejected. Invalid input raises
:class:`~lauren.exceptions.DecoratorUsageError` naming the field, its allowed
range and an example, so ``@cron("61 * * * *")`` fails at decoration time.

Time zones come from :mod:`zoneinfo`. Fire times are searched in local wall
time and then localised with a defined DST policy: a local time inside a
spring-forward gap fires once, at the first valid instant after the gap; an
ambiguous local time (fall-back) fires once, at its first occurrence.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..exceptions import DecoratorUsageError

_EXAMPLE = "'*/15 * * * *' (every 15 minutes) or '0 3 * * MON-FRI' (03:00 on weekdays)"

_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1
    )
}
_DOW_NAMES = {name: index for index, name in enumerate(("SUN", "MON", "TUE", "WED", "THU", "FRI", "SAT"))}

#: ``(name, lowest, highest, names)`` for the six fields, seconds first.
_FIELDS: tuple[tuple[str, int, int, dict[str, int] | None], ...] = (
    ("second", 0, 59, None),
    ("minute", 0, 59, None),
    ("hour", 0, 23, None),
    ("day-of-month", 1, 31, None),
    ("month", 1, 12, _MONTH_NAMES),
    ("day-of-week", 0, 7, _DOW_NAMES),
)

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

#: Longest month length per month (February counts its leap day).
_MAX_DAYS = {1: 31, 2: 29, 3: 31, 4: 30, 5: 31, 6: 30, 7: 31, 8: 31, 9: 30, 10: 31, 11: 30, 12: 31}

#: Upper bound on the search horizon. Validation guarantees a fire time
#: within 28 years (a 29 February on a given weekday), so this is never hit.
_MAX_YEARS = 50


def _error(expression: str, reason: str, message: str, **detail: Any) -> DecoratorUsageError:
    return DecoratorUsageError(
        f"invalid cron expression {expression!r}: {message}. Example: {_EXAMPLE}",
        detail={"decorator": "cron", "expression": expression, "reason": reason, **detail},
    )


def resolve_timezone(tz: str | tzinfo | None, *, where: str) -> tzinfo | None:
    """Turn ``tz=`` into a :class:`~datetime.tzinfo` (``None`` stays ``None``).

    ``"UTC"`` maps to :data:`datetime.timezone.utc`, so it works without the
    ``tzdata`` package on Windows. Unknown zones raise
    :class:`~lauren.exceptions.DecoratorUsageError` naming ``where``.
    """
    if tz is None or isinstance(tz, tzinfo):
        return tz
    if not isinstance(tz, str) or not tz:
        raise DecoratorUsageError(
            f"{where}: tz must be an IANA time-zone name such as 'Europe/Paris' or a tzinfo, got {tz!r}",
            detail={"where": where, "tz": repr(tz), "reason": "invalid_timezone"},
        )
    if tz.upper() == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(tz)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise DecoratorUsageError(
            f"{where}: unknown time zone {tz!r}; use an IANA name such as 'Europe/Paris' "
            "(on Windows, install the 'tzdata' package)",
            detail={"where": where, "tz": tz, "reason": "unknown_timezone"},
        ) from exc


def timezone_name(tz: tzinfo | None) -> str:
    """A readable name for ``tz`` (the IANA key for a :class:`~zoneinfo.ZoneInfo`)."""
    if tz is None:
        return "local"
    if tz is timezone.utc:
        return "UTC"
    return str(getattr(tz, "key", None) or tz)


def _parse_value(token: str, name: str, names: dict[str, int] | None, expression: str) -> int:
    upper = token.upper()
    if names is not None and upper in names:
        return names[upper]
    if token.isdigit():
        return int(token)
    if any(ch in upper for ch in "LW#?"):
        raise _error(
            expression,
            "unsupported_token",
            f"{name} field uses {token!r}; the Quartz extensions L, W, # and ? are not supported",
            field=name,
            value=token,
        )
    kind = "a number or a name" if names else "a number"
    raise _error(
        expression, "invalid_value", f"{name} field {token!r} is not {kind}", field=name, value=token
    )


def _parse_field(text: str, index: int, expression: str) -> frozenset[int]:
    name, low, high, names = _FIELDS[index]
    is_dow = name == "day-of-week"
    allowed = f"{low}-{high}" + (" or names" if names else "")
    values: set[int] = set()
    for item in text.split(","):
        if not item:
            raise _error(
                expression, "empty_item", f"{name} field {text!r} has an empty list item", field=name
            )
        base, slash, step_text = item.partition("/")
        step = 1
        if slash:
            if not step_text.isdigit() or int(step_text) == 0:
                raise _error(
                    expression,
                    "invalid_step",
                    f"{name} field {item!r} needs a positive step after '/'",
                    field=name,
                    value=item,
                )
            step = int(step_text)
        if base == "*":
            start, end = low, (6 if is_dow else high)
        elif "-" in base:
            first, _, last = base.partition("-")
            start = _parse_value(first, name, names, expression)
            end = _parse_value(last, name, names, expression)
            if is_dow and end == 0 and start > 0:
                end = 7  # FRI-SUN
        else:
            start = _parse_value(base, name, names, expression)
            end = (6 if is_dow else high) if slash else start
        for value in (start, end):
            if not low <= value <= high:
                raise _error(
                    expression,
                    "out_of_range",
                    f"{name} field value {value} is out of range; allowed {allowed}",
                    field=name,
                    value=item,
                    allowed=allowed,
                )
        if start > end:
            raise _error(
                expression,
                "reversed_range",
                f"{name} field range {item!r} runs backwards; write the smaller bound first",
                field=name,
                value=item,
                allowed=allowed,
            )
        values.update(range(start, end + 1, step))
    if is_dow and 7 in values:
        values.discard(7)
        values.add(0)  # 0 and 7 both mean Sunday
    return frozenset(values)


class CronExpression:
    """A parsed cron expression whose next fire times can be previewed.

    Five fields (``minute hour day-of-month month day-of-week``) or six with a
    leading seconds field; ``*``, lists ``a,b``, ranges ``a-b``, steps ``*/n``
    and ``a-b/n``, ``JAN``-``DEC`` and ``SUN``-``SAT`` (``0`` and ``7`` are
    Sunday), and the macros ``@yearly`` / ``@annually``, ``@monthly``,
    ``@weekly``, ``@daily`` / ``@midnight`` and ``@hourly``. When both
    day-of-month and day-of-week are restricted, a day matching either fires
    (POSIX cron semantics).

    ``tz`` (an IANA name or a :class:`~datetime.tzinfo`) is the zone the fields
    are read in. Without it, an aware datetime passed to :meth:`next_after`
    keeps its own zone and a naive one is treated as naive wall time.
    """

    __slots__ = (
        "_expression",
        "_tz",
        "_seconds",
        "_minutes",
        "_hours",
        "_days",
        "_months",
        "_weekdays",
        "_dom_star",
        "_dow_star",
        "_has_seconds",
    )

    def __init__(self, expression: str, *, tz: str | tzinfo | None = None) -> None:
        if not isinstance(expression, str) or not expression.strip():
            raise _error(str(expression), "empty", "expected five or six space-separated fields")
        self._expression = " ".join(expression.split())
        self._tz = resolve_timezone(tz, where="CronExpression")
        text = self._expression
        if text.startswith("@"):
            macro = _MACROS.get(text.lower())
            if macro is None:
                raise _error(
                    expression,
                    "unsupported_macro",
                    f"unknown macro {text!r}; use one of {', '.join(sorted(_MACROS))}",
                    value=text,
                )
            text = macro
        fields = text.split(" ")
        if len(fields) not in (5, 6):
            raise _error(
                expression,
                "field_count",
                f"expected five fields (minute hour day-of-month month day-of-week) or six with a "
                f"leading seconds field, got {len(fields)}",
            )
        self._has_seconds = len(fields) == 6
        if not self._has_seconds:
            fields = ["0", *fields]
        parsed = [_parse_field(field, index, expression) for index, field in enumerate(fields)]
        self._seconds, self._minutes, self._hours, self._days, self._months, self._weekdays = parsed
        self._dom_star = fields[3].startswith("*")
        self._dow_star = fields[5].startswith("*")
        if (self._dom_star or self._dow_star) and not any(
            day <= _MAX_DAYS[month] for month in self._months for day in self._days
        ):
            raise _error(
                expression,
                "never_fires",
                "the day-of-month and month fields never meet (for example 30 February)",
            )

    # -- Introspection --------------------------------------------------

    @property
    def expression(self) -> str:
        """The expression as written (whitespace normalised)."""
        return self._expression

    @property
    def tz(self) -> tzinfo | None:
        """The zone the fields are read in, or ``None``."""
        return self._tz

    @property
    def has_seconds(self) -> bool:
        """``True`` for the six-field form with a leading seconds field."""
        return self._has_seconds

    def with_tz(self, tz: str | tzinfo | None) -> CronExpression:
        """A copy of this expression read in ``tz``."""
        return CronExpression(self._expression, tz=tz)

    # -- Matching -------------------------------------------------------

    def _day_matches(self, moment: datetime) -> bool:
        dom = moment.day in self._days
        dow = moment.isoweekday() % 7 in self._weekdays
        if self._dom_star or self._dow_star:
            return dom and dow
        return dom or dow

    def _wall_matches(self, moment: datetime) -> bool:
        return (
            moment.month in self._months
            and self._day_matches(moment)
            and moment.hour in self._hours
            and moment.minute in self._minutes
            and moment.second in self._seconds
        )

    def matches(self, moment: datetime) -> bool:
        """``True`` when ``moment`` (read in :attr:`tz` when aware) satisfies every field."""
        if moment.tzinfo is not None and self._tz is not None:
            moment = moment.astimezone(self._tz)
        return self._wall_matches(moment)

    def _next_wall(self, moment: datetime) -> datetime:
        """The first naive wall time strictly after ``moment`` matching every field."""
        if self._has_seconds:
            current = moment.replace(microsecond=0) + timedelta(seconds=1)
        else:
            current = moment.replace(second=0, microsecond=0) + timedelta(minutes=1)
        horizon = current.year + _MAX_YEARS
        while current.year <= horizon:
            if current.month not in self._months:
                year, month = (
                    (current.year + 1, 1) if current.month == 12 else (current.year, current.month + 1)
                )
                current = current.replace(year=year, month=month, day=1, hour=0, minute=0, second=0)
            elif not self._day_matches(current):
                current = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0)
            elif current.hour not in self._hours:
                current = (current + timedelta(hours=1)).replace(minute=0, second=0)
            elif current.minute not in self._minutes:
                current = (current + timedelta(minutes=1)).replace(second=0)
            elif current.second not in self._seconds:
                current += timedelta(seconds=1)
            else:
                return current
        raise RuntimeError(f"cron expression {self._expression!r} has no fire time")  # pragma: no cover

    def next_after(self, moment: datetime) -> datetime:
        """The first fire time strictly after ``moment``.

        An aware ``moment`` returns an aware datetime in :attr:`tz` (or in
        ``moment``'s own zone when the expression has none); a naive one is
        read as wall time in :attr:`tz` (or stays naive without one).
        """
        zone = self._tz
        if moment.tzinfo is None:
            if zone is None:
                return self._next_wall(moment)
            anchor = moment.replace(tzinfo=zone)
        else:
            anchor = moment if zone is None else moment.astimezone(zone)
            zone = anchor.tzinfo
        assert zone is not None
        # Compare in UTC: datetimes sharing a tzinfo compare by wall time and
        # ignore ``fold``, which is wrong inside a repeated (fall-back) hour.
        anchor_utc = anchor.astimezone(timezone.utc)
        candidate = anchor.replace(tzinfo=None)
        while True:
            candidate = self._next_wall(candidate)
            instant = _localize(candidate, zone)
            if instant.astimezone(timezone.utc) > anchor_utc:
                return instant

    def iter_from(self, moment: datetime) -> Iterator[datetime]:
        """Yield fire times strictly after ``moment``, in increasing order (endless)."""
        while True:
            moment = self.next_after(moment)
            yield moment

    # -- Dunder ---------------------------------------------------------

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CronExpression):
            return NotImplemented
        return (self._expression, self._tz) == (other._expression, other._tz)

    def __hash__(self) -> int:
        return hash((self._expression, self._tz))

    def __repr__(self) -> str:
        if self._tz is None:
            return f"CronExpression({self._expression!r})"
        return f"CronExpression({self._expression!r}, tz={timezone_name(self._tz)!r})"

    def __str__(self) -> str:
        return self._expression


def _localize(wall: datetime, zone: tzinfo) -> datetime:
    """Attach ``zone`` to a naive wall time, applying the DST policy.

    An ambiguous time resolves to its first occurrence (``fold=0``). A time in
    a spring-forward gap does not exist; it maps to the first valid instant
    after the gap, found by bisecting the transition in UTC.
    """
    first = wall.replace(tzinfo=zone, fold=0)
    utc_first = first.astimezone(timezone.utc)
    if utc_first.astimezone(zone).replace(tzinfo=None) == wall:
        return first
    utc_second = wall.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    low, high = sorted((int(utc_first.timestamp()), int(utc_second.timestamp())))
    before = datetime.fromtimestamp(low, zone).utcoffset()
    while high - low > 1:
        middle = (low + high) // 2
        if datetime.fromtimestamp(middle, zone).utcoffset() == before:
            low = middle
        else:
            high = middle
    return datetime.fromtimestamp(high, zone)


__all__ = ["CronExpression", "resolve_timezone", "timezone_name"]
