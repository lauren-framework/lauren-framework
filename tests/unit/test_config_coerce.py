"""Unit tests for the configuration coercion table (``lauren._config._coerce``)."""

from __future__ import annotations

import collections.abc
import datetime
import decimal
import enum
import pathlib
import typing
import uuid
from typing import Annotated, Any, Literal, NewType, Optional

import pytest

from lauren._config._coerce import CoercionError, UnsupportedTypeError, coerce, type_name
from lauren.config import Secret


class Color(enum.Enum):
    RED = "red"
    BLUE = "blue"


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


UserId = NewType("UserId", int)


# ---------------------------------------------------------------------------
# Lax mode (strings from the environment / .env / secret files)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "tp", "expected"),
    [
        ("anything at all", str, "anything at all"),
        ("", str, ""),
        ("10", int, 10),
        (" 42 ", int, 42),
        ("1_000", int, 1000),
        ("-3", int, -3),
        ("1.5", float, 1.5),
        ("2", float, 2.0),
        ("1.10", decimal.Decimal, decimal.Decimal("1.10")),
        ("1", bool, True),
        ("TRUE", bool, True),
        ("yes", bool, True),
        ("On", bool, True),
        ("0", bool, False),
        ("false", bool, False),
        ("NO", bool, False),
        ("off", bool, False),
        ("red", Color, Color.RED),
        ("BLUE", Color, Color.BLUE),
        ("2", Level, Level.HIGH),
        ("LOW", Level, Level.LOW),
        ("INFO", Literal["DEBUG", "INFO"], "INFO"),
        ("2", Literal[1, 2], 2),
        ("/srv/app", pathlib.Path, pathlib.Path("/srv/app")),
        (
            "12345678-1234-5678-1234-567812345678",
            uuid.UUID,
            uuid.UUID("12345678-1234-5678-1234-567812345678"),
        ),
        ("2024-05-01T10:30:00", datetime.datetime, datetime.datetime(2024, 5, 1, 10, 30)),
        ("2024-05-01", datetime.date, datetime.date(2024, 5, 1)),
        ("10:30", datetime.time, datetime.time(10, 30)),
        ("90", datetime.timedelta, datetime.timedelta(seconds=90)),
        ("1.5", datetime.timedelta, datetime.timedelta(seconds=1.5)),
        ("PT5M", datetime.timedelta, datetime.timedelta(minutes=5)),
        ("P1DT2H", datetime.timedelta, datetime.timedelta(days=1, hours=2)),
        ("P2W", datetime.timedelta, datetime.timedelta(weeks=2)),
        ("-PT30S", datetime.timedelta, datetime.timedelta(seconds=-30)),
        ("a, b ,c", list[str], ["a", "b", "c"]),
        ("1,2,3", list[int], [1, 2, 3]),
        ("", list[int], []),
        ('["a", "b"]', list[str], ["a", "b"]),
        ("[1, 2]", tuple[int, ...], (1, 2)),
        ("1, x", tuple[int, str], (1, "x")),
        ("a,b,a", set[str], {"a", "b"}),
        ("a,b", frozenset[str], frozenset({"a", "b"})),
        ("a,b", collections.abc.Sequence[str], ("a", "b")),
        ("a,b", list, ["a", "b"]),
        ('{"a": 1, "b": 2}', dict[str, int], {"a": 1, "b": 2}),
        ('{"1": "x"}', dict[int, str], {1: "x"}),
        ("", Optional[int], None),
        ("", int | None, None),
        ("5", int | None, 5),
        ("7", int | str, 7),
        ("seven", int | str, "seven"),
        ("hello", bytes, b"hello"),
        ("5", UserId, 5),
        ("5", Annotated[int, "meta"], 5),
        ("raw", Any, "raw"),
        ("a", Literal[1, "a"], "a"),
        ("1.5", int | float, 1.5),
    ],
)
def test_lax_coercion_table(raw: str, tp: Any, expected: Any) -> None:
    assert coerce(raw, tp) == expected


@pytest.mark.parametrize(
    ("raw", "tp"),
    [
        ("ten", int),
        ("1.5", int),
        ("maybe", bool),
        ("", bool),
        ("x", float),
        ("abc", decimal.Decimal),
        ("green", Color),
        ("TRACE", Literal["DEBUG", "INFO"]),
        ("3", Literal[1, 2]),
        ("not-a-uuid", uuid.UUID),
        ("yesterday", datetime.datetime),
        ("2024-13-01", datetime.date),
        ("25:00", datetime.time),
        ("five minutes", datetime.timedelta),
        ("P", datetime.timedelta),
        ("PT", datetime.timedelta),
        ("1,x", list[int]),
        ("[1, 2", list[int]),
        ('{"a": 1}', list[int]),
        ('["1"]', list[int]),
        ("1", tuple[int, str]),
        ("a=1", dict[str, int]),
        ('{"a": "x"}', dict[str, int]),
        ("{bad json", dict[str, int]),
        ("[1]", dict[str, int]),
        ("x", int | None),
    ],
)
def test_lax_coercion_rejects_invalid_strings(raw: str, tp: Any) -> None:
    with pytest.raises(CoercionError):
        coerce(raw, tp)


def test_bool_garbage_is_rejected_instead_of_silently_false() -> None:
    with pytest.raises(CoercionError) as excinfo:
        coerce("maybe", bool)

    assert excinfo.value.expected == "bool"
    assert "true/false" in excinfo.value.reason


def test_secret_coerces_inner_type_then_wraps() -> None:
    value = coerce("8080", Secret[int])

    assert isinstance(value, Secret)
    assert value.get_secret_value() == 8080


def test_secret_accepts_an_existing_secret() -> None:
    value = coerce(Secret("5"), Secret[int])

    assert value.get_secret_value() == 5


def test_bare_secret_keeps_raw_value() -> None:
    assert coerce("x", Secret).get_secret_value() == "x"


def test_optional_secret_empty_string_is_none() -> None:
    assert coerce("", Secret[str] | None) is None


def test_unsupported_type_raises_unsupported_type_error() -> None:
    class Custom:
        pass

    with pytest.raises(UnsupportedTypeError):
        coerce("x", Custom)


def test_unsupported_non_class_annotation_raises() -> None:
    with pytest.raises(UnsupportedTypeError):
        coerce("x", collections.abc.Callable[[], int])


def test_list_item_error_names_the_index() -> None:
    with pytest.raises(CoercionError) as excinfo:
        coerce("1,2,x", list[int])

    assert "item 2" in str(excinfo.value)


def test_unhashable_items_for_a_set_are_rejected() -> None:
    with pytest.raises(CoercionError):
        coerce("[[1], [2]]", set[Any])


# ---------------------------------------------------------------------------
# Strict mode (typed values from TOML / YAML / overrides)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "tp", "expected"),
    [
        (10, int, 10),
        (1, float, 1.0),
        (1.5, float, 1.5),
        (True, bool, True),
        ("text", str, "text"),
        (3, decimal.Decimal, decimal.Decimal(3)),
        ("1.10", decimal.Decimal, decimal.Decimal("1.10")),
        (decimal.Decimal("2"), decimal.Decimal, decimal.Decimal("2")),
        (b"x", bytes, b"x"),
        (Color.RED, Color, Color.RED),
        ("blue", Color, Color.BLUE),
        (2, Level, Level.HIGH),
        ("INFO", Literal["DEBUG", "INFO"], "INFO"),
        (datetime.date(2024, 1, 2), datetime.date, datetime.date(2024, 1, 2)),
        (datetime.datetime(2024, 1, 2, 3), datetime.datetime, datetime.datetime(2024, 1, 2, 3)),
        (datetime.time(3, 4), datetime.time, datetime.time(3, 4)),
        (30, datetime.timedelta, datetime.timedelta(seconds=30)),
        (datetime.timedelta(1), datetime.timedelta, datetime.timedelta(1)),
        (pathlib.PurePosixPath("/a"), pathlib.PurePosixPath, pathlib.PurePosixPath("/a")),
        (uuid.UUID(int=1), uuid.UUID, uuid.UUID(int=1)),
        ([1, 2], list[int], [1, 2]),
        ([1, 2], tuple[int, ...], (1, 2)),
        ({"a": 1}, dict[str, int], {"a": 1}),
        ({"a": 1}, dict, {"a": 1}),
        (None, int | None, None),
    ],
)
def test_strict_coercion_accepts_typed_values(value: Any, tp: Any, expected: Any) -> None:
    assert coerce(value, tp, strict=True) == expected


@pytest.mark.parametrize(
    ("value", "tp"),
    [
        ("10", int),
        (True, int),
        ("1.5", float),
        (True, float),
        ("true", bool),
        (1, bool),
        (1, str),
        (1, bytes),
        (True, decimal.Decimal),
        ([1], decimal.Decimal),
        ("1,2", list[int]),
        ("x", dict[str, int]),
        (datetime.datetime(2024, 1, 1), datetime.date),
        (5, datetime.date),
        (True, datetime.timedelta),
        ([], datetime.timedelta),
        (5, pathlib.Path),
        (5, uuid.UUID),
        (1, Literal[True]),
        (9, Color),
    ],
)
def test_strict_coercion_rejects_untyped_values(value: Any, tp: Any) -> None:
    with pytest.raises(CoercionError):
        coerce(value, tp, strict=True)


def test_strict_mode_parses_iso_strings_for_types_without_a_file_representation() -> None:
    assert coerce("PT1H", datetime.timedelta, strict=True) == datetime.timedelta(hours=1)
    assert coerce("2024-01-02", datetime.date, strict=True) == datetime.date(2024, 1, 2)


def test_non_string_input_is_always_checked_strictly() -> None:
    assert coerce(3, int) == 3
    with pytest.raises(CoercionError):
        coerce(3.0, int)


def test_instance_of_a_custom_class_is_accepted_as_is() -> None:
    class Custom:
        pass

    obj = Custom()

    assert coerce(obj, Custom, strict=True) is obj


# ---------------------------------------------------------------------------
# type_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("tp", "expected"),
    [
        (int, "int"),
        (list[int], "list[int]"),
        (tuple[int, ...], "tuple[int, ...]"),
        (int | None, "int | None"),
        (Literal["a", "b"], "Literal['a', 'b']"),
        (Annotated[str, "x"], "str"),
        (Secret[str], "Secret[str]"),
        (Any, "Any"),
        (type(None), "None"),
        (list, "list"),
        (typing.Sequence, "Sequence"),
    ],
)
def test_type_name(tp: Any, expected: str) -> None:
    assert type_name(tp) == expected
