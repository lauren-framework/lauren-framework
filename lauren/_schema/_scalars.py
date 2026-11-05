"""Scalar parsing tables shared by the validation engine and the config loader.

The boolean tokens and the ISO 8601 duration grammar live here once:
:mod:`lauren._config._coerce` (typed settings) imports them so a value like
``"on"`` or ``"PT5M"`` means the same thing in an environment variable, a query
string and a JSON body.
"""

from __future__ import annotations

import datetime
import re

#: Case-insensitive strings accepted as ``True`` by lax boolean coercion.
BOOL_TRUE: frozenset[str] = frozenset({"1", "true", "yes", "on"})
#: Case-insensitive strings accepted as ``False`` by lax boolean coercion.
BOOL_FALSE: frozenset[str] = frozenset({"0", "false", "no", "off"})

_NUM = r"(\d+(?:\.\d+)?)"
#: ISO 8601 duration, weeks to seconds (``P1W``, ``PT5M``, ``-P1DT2H``).
ISO_DURATION = re.compile(
    rf"^(?P<sign>[-+])?P(?:{_NUM}W)?(?:{_NUM}D)?(?:T(?:{_NUM}H)?(?:{_NUM}M)?(?:{_NUM}S)?)?$",
    re.IGNORECASE,
)


def parse_bool_token(text: str) -> bool | None:
    """Return the boolean a lax token names, or ``None`` when it names neither."""
    folded = text.strip().lower()
    if folded in BOOL_TRUE:
        return True
    if folded in BOOL_FALSE:
        return False
    return None


def parse_iso_duration(text: str) -> datetime.timedelta | None:
    """Parse an ISO 8601 duration (``P1DT2H30M``); ``None`` when ``text`` is not one."""
    match = ISO_DURATION.match(text.strip())
    if match is None or not any(match.groups()[1:]):
        return None
    weeks, days, hours, minutes, seconds = (float(g) if g else 0.0 for g in match.groups()[1:])
    delta = datetime.timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
    return -delta if match.group("sign") == "-" else delta


def format_iso_duration(delta: datetime.timedelta) -> str:
    """Render a :class:`~datetime.timedelta` as an ISO 8601 duration (``PT1H30M``).

    The inverse of :func:`parse_iso_duration` for every value it produces;
    microseconds become fractional seconds.
    """
    total = delta.total_seconds()
    sign = "-" if total < 0 else ""
    remaining = abs(delta)
    days = remaining.days
    seconds = remaining.seconds
    micros = remaining.microseconds
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    out = f"{sign}P"
    if days:
        out += f"{days}D"
    time_part = ""
    if hours:
        time_part += f"{hours}H"
    if minutes:
        time_part += f"{minutes}M"
    if seconds or micros or not (days or hours or minutes):
        if micros:
            time_part += f"{seconds}.{micros:06d}".rstrip("0") + "S"
        else:
            time_part += f"{seconds}S"
    if time_part:
        out += "T" + time_part
    return out


__all__ = [
    "BOOL_FALSE",
    "BOOL_TRUE",
    "ISO_DURATION",
    "format_iso_duration",
    "parse_bool_token",
    "parse_iso_duration",
]
