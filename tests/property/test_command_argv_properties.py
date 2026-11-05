"""Hypothesis property tests for the command argument parser (PRD F19 §10).

Rendering random values of every supported type to CLI tokens and parsing
them with the parser compiled at startup yields equal values. Skipped
automatically when hypothesis is not installed.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import uuid
from pathlib import Path
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren import LaurenFactory, injectable, module  # noqa: E402
from lauren.commands import command  # noqa: E402

_TOLERANT = settings(
    deadline=None, suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture]
)


class Color(enum.Enum):
    RED = "red"
    DARK_BLUE = "dark-blue"
    NUMBER = 7


@injectable()
class PropertyCommands:
    @command("all-types")
    def all_types(
        self,
        *,
        i: int = 0,
        f: float = 0.0,
        d: decimal.Decimal = decimal.Decimal(0),
        s: str = "",
        b: bool = False,
        c: Color = Color.RED,
        u: uuid.UUID | None = None,
        when: datetime.datetime | None = None,
        day: datetime.date | None = None,
        p: Path = Path("."),
        nums: list[int] | None = None,
        words: tuple[str, ...] = (),
    ) -> None: ...

    @command("positional")
    def positional(self, i: int, s: str, nums: list[float]) -> None: ...


@module(providers=[PropertyCommands])
class PropertyModule:
    pass


COMMANDS = LaurenFactory.create(PropertyModule)._commands
ALL_TYPES = COMMANDS.lookup("all-types")
POSITIONAL = COMMANDS.lookup("positional")
_ZONES = st.sampled_from(
    [None, datetime.timezone.utc, datetime.timezone(datetime.timedelta(hours=5, minutes=30))]
)


def _token(value: Any) -> str:
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, enum.Enum):
        return str(value.value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    return str(value)


def _option(name: str, value: Any) -> list[str]:
    if isinstance(value, bool):
        return [f"--{name}" if value else f"--no-{name}"]
    if isinstance(value, (list, tuple)):
        return [f"--{name}={_token(item)}" for item in value]
    return [f"--{name}={_token(value)}"]


@_TOLERANT
@given(
    i=st.integers(),
    f=st.floats(allow_nan=False),
    d=st.decimals(allow_nan=False, allow_infinity=False),
    s=st.text(),
    b=st.booleans(),
    c=st.sampled_from(list(Color)),
    u=st.uuids(),
    when=st.datetimes(timezones=_ZONES),
    day=st.dates(),
    p=st.sampled_from(["out.json", "a/b/c.txt", "with space", "-dash"]),
    nums=st.lists(st.integers(), min_size=1, max_size=5),
    words=st.lists(st.text(), max_size=5),
)
def test_rendered_options_parse_back_to_the_same_values(**values: Any) -> None:
    argv: list[str] = []
    for name, value in values.items():
        argv += _option(name, value)

    parsed = ALL_TYPES.parse(argv)

    expected = {**values, "p": Path(values["p"]), "words": tuple(values["words"])}
    assert parsed == expected


@_TOLERANT
@given(
    i=st.integers(),
    s=st.text().filter(lambda text: not text.startswith("-")),
    nums=st.lists(st.floats(allow_nan=False, allow_infinity=False, min_value=0), min_size=1, max_size=5),
)
def test_rendered_positionals_parse_back_to_the_same_values(i: int, s: str, nums: list[float]) -> None:
    argv = ["--", str(i), s, *(repr(n) for n in nums)] if i < 0 else [str(i), s, *(repr(n) for n in nums)]

    assert POSITIONAL.parse(argv) == {"i": i, "s": s, "nums": nums}
