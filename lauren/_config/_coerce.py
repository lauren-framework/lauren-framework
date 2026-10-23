"""String-to-type coercion for configuration values — the one place it lives.

:func:`coerce` turns a raw configuration value into the type a settings
field declares. It has two modes:

* **lax** (``strict=False``) for strings read from the environment,
  ``.env`` files and secret files. ``"10"`` becomes ``10``, ``"yes"``
  becomes ``True``, ``"a, b"`` becomes ``["a", "b"]``.
* **strict** (``strict=True``) for values that are already typed — TOML or
  YAML values, JSON array items, Python override values. Numbers, booleans
  and collections must already have the right type; only types that have
  no native file representation (dates, durations, paths, UUIDs,
  decimals, enums, bytes) are still parsed from strings.

The boolean tokens and the ISO 8601 duration grammar come from the native
validation engine (:mod:`lauren._schema._scalars`), so ``"on"`` or ``"PT5M"``
mean the same thing in a setting, a query string and a JSON body. The rest of
the table stays here because configuration has needs request validation does
not: comma-separated lists in one environment variable, JSON-array strings,
``Secret``, paths, enum member names and the strict mode for already-typed
file values, each with the error reasons the loader reports. Nothing here
imports pydantic or msgspec.
"""

from __future__ import annotations

import collections.abc
import datetime
import decimal
import enum
import json
import pathlib
import types
import uuid
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from .._schema._scalars import BOOL_FALSE as _FALSE
from .._schema._scalars import BOOL_TRUE as _TRUE
from .._schema._scalars import parse_iso_duration
from ._secret import Secret


class CoercionError(ValueError):
    """A raw value could not be converted to the declared type."""

    def __init__(self, expected: str, reason: str = "") -> None:
        super().__init__(f"invalid {expected}" + (f": {reason}" if reason else ""))
        self.expected = expected
        self.reason = reason


class UnsupportedTypeError(CoercionError):
    """The declared type is not in the coercion table."""


_UNION_TYPES: tuple[Any, ...] = (Union, types.UnionType)
_SEQUENCE_ORIGINS: dict[Any, type] = {
    list: list,
    tuple: tuple,
    set: set,
    frozenset: frozenset,
    collections.abc.Sequence: tuple,
    collections.abc.MutableSequence: list,
    collections.abc.Set: frozenset,
    collections.abc.MutableSet: set,
}
_MAPPING_ORIGINS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)


def type_name(tp: Any) -> str:
    """Return a short, readable name for an annotation (``list[int]``, ``int | None``)."""
    origin = get_origin(tp)
    if origin is Annotated:
        return type_name(get_args(tp)[0])
    if tp is type(None):
        return "None"
    if tp is Any:
        return "Any"
    if origin in _UNION_TYPES:
        return " | ".join(type_name(a) for a in get_args(tp))
    if origin is Literal:
        return "Literal[" + ", ".join(repr(a) for a in get_args(tp)) + "]"
    if origin is not None:
        base = getattr(origin, "__name__", repr(origin))
        args = get_args(tp)
        if not args:
            return str(base)
        return f"{base}[" + ", ".join("..." if a is Ellipsis else type_name(a) for a in args) + "]"
    return str(getattr(tp, "__name__", repr(tp)))


def coerce(value: Any, tp: Any, *, strict: bool = False) -> Any:
    """Convert ``value`` to ``tp`` or raise :class:`CoercionError`.

    Lax mode only applies to ``str`` input; any other value is checked
    strictly, so ``coerce(3, int)`` and ``coerce("3", int)`` both work in
    lax mode while ``coerce("3", int, strict=True)`` fails.
    """
    origin = get_origin(tp)
    if origin is Annotated:
        return coerce(value, get_args(tp)[0], strict=strict)
    if tp is Any or tp is object:
        return value
    if tp is Secret or origin is Secret:
        args = get_args(tp)
        raw = value.get_secret_value() if isinstance(value, Secret) else value
        return Secret(coerce(raw, args[0] if args else Any, strict=strict))
    if not isinstance(value, str):
        strict = True
    if origin in _UNION_TYPES:
        return _coerce_union(value, tp, strict)
    if origin is Literal:
        return _coerce_literal(value, tp, strict)
    if tp in _SEQUENCE_ORIGINS or origin in _SEQUENCE_ORIGINS:
        return _coerce_sequence(value, tp, strict)
    if tp in _MAPPING_ORIGINS or origin in _MAPPING_ORIGINS:
        return _coerce_mapping(value, tp, strict)
    supertype = getattr(tp, "__supertype__", None)
    if supertype is not None:  # typing.NewType
        return coerce(value, supertype, strict=strict)
    if isinstance(tp, type):
        return _coerce_scalar(value, tp, strict)
    raise UnsupportedTypeError(type_name(tp), "unsupported type")


def _coerce_union(value: Any, tp: Any, strict: bool) -> Any:
    args = get_args(tp)
    if type(None) in args:
        if value is None or (not strict and value == ""):
            return None
    for arg in args:
        if arg is type(None):
            continue
        try:
            return coerce(value, arg, strict=strict)
        except CoercionError:
            continue
    raise CoercionError(type_name(tp))


def _coerce_literal(value: Any, tp: Any, strict: bool) -> Any:
    for choice in get_args(tp):
        if strict or isinstance(choice, str):
            if type(value) is type(choice) and value == choice:
                return choice
            continue
        try:
            if coerce(value, type(choice)) == choice:
                return choice
        except CoercionError:
            continue
    raise CoercionError(type_name(tp), "expected one of " + ", ".join(repr(c) for c in get_args(tp)))


def _coerce_sequence(value: Any, tp: Any, strict: bool) -> Any:
    origin = get_origin(tp) or tp
    container = _SEQUENCE_ORIGINS[origin]
    args = get_args(tp)
    name = type_name(tp)
    if not strict:
        text = value.strip()
        if text.startswith("["):
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError as exc:
                raise CoercionError(name, f"malformed JSON array ({exc.msg})") from None
            items, item_strict = decoded, True  # text starts with "[", so a list
        else:
            items = [] if text == "" else [part.strip() for part in text.split(",")]
            item_strict = False
    else:
        if not isinstance(value, (list, tuple, set, frozenset)):
            raise CoercionError(name, f"expected an array, got {type(value).__name__}")
        items, item_strict = list(value), True
    if origin is tuple and args and not (len(args) == 2 and args[1] is Ellipsis):
        if len(items) != len(args):
            raise CoercionError(name, f"expected {len(args)} item(s), got {len(items)}")
        item_types = list(args)
    else:
        item_types = [args[0] if args else Any] * len(items)
    out = []
    for index, (item, item_tp) in enumerate(zip(items, item_types)):
        try:
            out.append(coerce(item, item_tp, strict=item_strict))
        except CoercionError as exc:
            raise CoercionError(name, f"item {index}: {exc}") from None
    try:
        return container(out)
    except TypeError as exc:
        raise CoercionError(name, str(exc)) from None


def _coerce_mapping(value: Any, tp: Any, strict: bool) -> Any:
    args = get_args(tp)
    key_tp, value_tp = (args[0], args[1]) if len(args) == 2 else (Any, Any)
    name = type_name(tp)
    if not strict:
        text = value.strip()
        if not text.startswith("{"):
            raise CoercionError(name, "expected a JSON object")
        try:
            decoded = json.loads(text)
        except json.JSONDecodeError as exc:
            raise CoercionError(name, f"malformed JSON object ({exc.msg})") from None
        value = decoded  # text starts with "{", so a dict
    elif not isinstance(value, collections.abc.Mapping):
        raise CoercionError(name, f"expected a table, got {type(value).__name__}")
    out: dict[Any, Any] = {}
    for key, item in value.items():
        try:
            out[coerce(key, key_tp)] = coerce(item, value_tp, strict=True)
        except CoercionError as exc:
            raise CoercionError(name, f"key {key!r}: {exc}") from None
    return out


def _coerce_scalar(value: Any, tp: type, strict: bool) -> Any:  # noqa: PLR0911, PLR0912
    name = type_name(tp)
    if tp is bool:
        if isinstance(value, bool):
            return value
        if not strict:
            folded = value.strip().lower()
            if folded in _TRUE:
                return True
            if folded in _FALSE:
                return False
            raise CoercionError(name, "use one of 1/0, true/false, yes/no, on/off")
        raise CoercionError(name)
    if tp is int:
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if not strict:
            try:
                return int(value)
            except ValueError:
                raise CoercionError(name) from None
        raise CoercionError(name)
    if tp is float:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
        if not strict:
            try:
                return float(value)
            except ValueError:
                raise CoercionError(name) from None
        raise CoercionError(name)
    if tp is str:
        if isinstance(value, str):
            return value
        raise CoercionError(name)
    if tp is bytes:
        if isinstance(value, bytes):
            return value
        if isinstance(value, str):
            return value.encode("utf-8")
        raise CoercionError(name)
    if issubclass(tp, decimal.Decimal):
        if isinstance(value, decimal.Decimal):
            return value
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                return tp(str(value).strip())
            except decimal.InvalidOperation:
                raise CoercionError(name) from None
        raise CoercionError(name)
    if issubclass(tp, enum.Enum):
        return _coerce_enum(value, tp)
    if issubclass(tp, datetime.datetime):
        return _coerce_iso(value, tp, datetime.datetime)
    if issubclass(tp, datetime.date):
        if isinstance(value, datetime.datetime):
            raise CoercionError(name, "got a date-time")
        return _coerce_iso(value, tp, datetime.date)
    if issubclass(tp, datetime.time):
        return _coerce_iso(value, tp, datetime.time)
    if issubclass(tp, datetime.timedelta):
        return _coerce_timedelta(value)
    if issubclass(tp, pathlib.PurePath):
        if isinstance(value, pathlib.PurePath):
            return tp(value)
        if isinstance(value, str):
            return tp(value)
        raise CoercionError(name)
    if issubclass(tp, uuid.UUID):
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                return tp(value.strip())
            except ValueError:
                raise CoercionError(name) from None
        raise CoercionError(name)
    if isinstance(value, tp):
        return value
    raise UnsupportedTypeError(name, "unsupported type")


def _coerce_enum(value: Any, tp: type[enum.Enum]) -> Any:
    if isinstance(value, tp):
        return value
    for member in tp:
        if member.value == value or (isinstance(value, str) and str(member.value) == value):
            return member
    if isinstance(value, str) and value in tp.__members__:
        return tp.__members__[value]
    choices = ", ".join(repr(m.value) for m in tp)
    raise CoercionError(type_name(tp), f"expected one of {choices}")


def _coerce_iso(value: Any, tp: type, base: type) -> Any:
    if isinstance(value, base):
        return value
    if isinstance(value, str):
        try:
            return tp.fromisoformat(value.strip())  # type: ignore[attr-defined]
        except ValueError:
            raise CoercionError(type_name(tp), "expected ISO 8601") from None
    raise CoercionError(type_name(tp))


def _coerce_timedelta(value: Any) -> datetime.timedelta:
    if isinstance(value, datetime.timedelta):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.timedelta(seconds=value)
    if isinstance(value, str):
        text = value.strip()
        try:
            return datetime.timedelta(seconds=float(text))
        except ValueError:
            pass
        delta = parse_iso_duration(text)
        if delta is not None:
            return delta
        raise CoercionError("timedelta", "expected seconds or an ISO 8601 duration such as PT5M")
    raise CoercionError("timedelta")


__all__ = ["CoercionError", "UnsupportedTypeError", "coerce", "type_name"]
