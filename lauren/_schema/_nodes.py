"""Validator nodes — one small object per type kind (private).

Every node exposes three operations, all built once at startup by the
:class:`~lauren._schema._compiler.TypeCompiler`:

* ``validate(value)`` returns the validated (possibly coerced) value or raises
  :class:`~lauren._schema._issues._Invalid` whose issues carry ``loc`` paths
  relative to the node. Containers validate every item and report every
  failure (up to the compiler's ``max_issues``) before raising.
* ``project(value)`` returns JSON-compatible builtins holding only the declared
  shape (models emit their declared fields under their wire names). It is the
  building block response contracts use to filter output.
* ``schema(ctx)`` returns a JSON Schema fragment; models register themselves
  in ``ctx`` and return a ``$ref``.

There is no reflection here: all type inspection happened in the compiler.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import math
import pathlib
import re
import uuid
from collections.abc import Mapping
from typing import Any

from ._issues import (
    MISSING,
    ValidationFailed,
    ValidationIssue,
    _Invalid,
    format_expected,
    invalid,
    make_issue,
)
from ._scalars import format_iso_duration, parse_bool_token, parse_iso_duration

_NoneType = type(None)


class Node:
    """Base class; subclasses override :meth:`validate`, :meth:`project` and :meth:`schema`."""

    __slots__ = ("explicit", "kind", "exact_types")

    #: Short kind name used for constraint applicability and union labels.
    KIND = "any"
    #: Python types that count as an *exact* match in smart unions.
    EXACT: tuple[type, ...] = ()

    def __init__(self) -> None:
        #: True when the node (or a child) was built from explicit validation
        #: metadata (markers, ``Strict``, ``Alias``, ``@validation_config``).
        self.explicit = False
        self.kind: str = self.KIND
        self.exact_types: tuple[type, ...] = self.EXACT

    def validate(self, value: Any) -> Any:
        return value

    def project(self, value: Any) -> Any:
        return to_builtins(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {}

    def is_instance(self, value: Any) -> bool:
        """Cheap structural check used to pick a union member when projecting."""
        return bool(self.exact_types) and isinstance(value, self.exact_types)

    @property
    def label(self) -> str:
        return self.kind


def to_builtins(value: Any, _depth: int = 0) -> Any:
    """Best-effort conversion of an untyped value (``Any`` fields) to JSON builtins."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if _depth > 32:
        return str(value)
    if isinstance(value, enum.Enum):
        return to_builtins(value.value, _depth + 1)
    if isinstance(value, Mapping):
        return {
            k if isinstance(k, (str, int, float, bool)) else str(k): to_builtins(v, _depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_builtins(v, _depth + 1) for v in value]
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return format_iso_duration(value)
    if isinstance(value, (uuid.UUID, decimal.Decimal, pathlib.PurePath)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_builtins(getattr(value, f.name), _depth + 1) for f in dataclasses.fields(value)}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if hasattr(value, "__struct_fields__"):
        return {name: to_builtins(getattr(value, name), _depth + 1) for name in value.__struct_fields__}
    return str(value)


# ---------------------------------------------------------------------------
# Trivial nodes
# ---------------------------------------------------------------------------


class AnyNode(Node):
    """``Any`` / ``object``: everything is accepted unchanged."""

    __slots__ = ()
    KIND = "any"


class NoneNode(Node):
    """``None``: only ``None`` is accepted."""

    __slots__ = ()
    KIND = "none"
    EXACT = (_NoneType,)

    def validate(self, value: Any) -> Any:
        if value is None:
            return None
        raise invalid("none_required", value)

    def project(self, value: Any) -> Any:
        return None

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "null"}


class RefNode(Node):
    """Late-bound placeholder for a recursive type (``children: list[Node]``)."""

    __slots__ = ("target", "name")

    def __init__(self, name: str) -> None:
        super().__init__()
        self.target: Node | None = None
        self.name = name

    def _node(self) -> Node:
        if self.target is None:  # pragma: no cover - the compiler always binds it
            raise RuntimeError(f"recursive type {self.name} was not bound")
        return self.target

    def validate(self, value: Any) -> Any:
        return self._node().validate(value)

    def project(self, value: Any) -> Any:
        return self._node().project(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return self._node().schema(ctx)

    def is_instance(self, value: Any) -> bool:
        return self._node().is_instance(value)

    @property
    def label(self) -> str:
        return self.name


# ---------------------------------------------------------------------------
# Scalars
# ---------------------------------------------------------------------------


class StrNode(Node):
    __slots__ = ()
    KIND = "str"
    EXACT = (str,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, str):
            return value
        raise invalid("string_type", value)

    def project(self, value: Any) -> Any:
        return value if isinstance(value, str) else str(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string"}


class IntNode(Node):
    __slots__ = ("strict",)
    KIND = "int"
    EXACT = (int,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if type(value) is int:
            return value
        if isinstance(value, bool):
            raise invalid("int_type", value)
        if isinstance(value, int):
            return int(value)
        if not self.strict:
            if isinstance(value, str):
                try:
                    return int(value)
                except ValueError:
                    raise invalid("int_parsing", value) from None
            if isinstance(value, float):
                if not math.isfinite(value):
                    raise invalid("finite_number", value)
                if value.is_integer():
                    return int(value)
                raise invalid("int_from_float", value)
        raise invalid("int_type", value)

    def is_instance(self, value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool)

    def project(self, value: Any) -> Any:
        return int(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "integer"}


class FloatNode(Node):
    __slots__ = ("strict",)
    KIND = "float"
    EXACT = (float,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if type(value) is float:
            return value
        if isinstance(value, bool):
            raise invalid("float_type", value)
        if isinstance(value, (int, float)):
            try:
                return float(value)
            except OverflowError:
                raise invalid("float_parsing", value) from None
        if not self.strict and isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                raise invalid("float_parsing", value) from None
        raise invalid("float_type", value)

    def is_instance(self, value: Any) -> bool:
        return isinstance(value, float)

    def project(self, value: Any) -> Any:
        return float(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "number"}


class BoolNode(Node):
    __slots__ = ("strict",)
    KIND = "bool"
    EXACT = (bool,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if value is True or value is False:
            return value
        if not self.strict:
            if isinstance(value, str):
                parsed = parse_bool_token(value)
                if parsed is not None:
                    return parsed
                raise invalid("bool_parsing", value)
            if isinstance(value, int) and not isinstance(value, bool):
                if value in (0, 1):
                    return bool(value)
                raise invalid("bool_parsing", value)
        raise invalid("bool_type", value)

    def project(self, value: Any) -> Any:
        return bool(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "boolean"}


class DecimalNode(Node):
    __slots__ = ("strict",)
    KIND = "decimal"
    EXACT = (decimal.Decimal,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if isinstance(value, decimal.Decimal):
            result = value
        elif isinstance(value, bool):
            raise invalid("decimal_type", value)
        elif isinstance(value, int):
            result = decimal.Decimal(value)
        elif isinstance(value, str):
            try:
                result = decimal.Decimal(value.strip())
            except decimal.InvalidOperation:
                raise invalid("decimal_parsing", value) from None
        elif isinstance(value, float) and not self.strict:
            result = decimal.Decimal(str(value))
        else:
            raise invalid("decimal_type", value)
        if not result.is_finite():
            raise invalid("finite_number", value)
        return result

    def project(self, value: Any) -> Any:
        return str(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {
            "anyOf": [{"type": "number"}, {"type": "string", "pattern": r"^-?\d+(\.\d+)?([eE][+-]?\d+)?$"}]
        }


class ComplexNode(Node):
    __slots__ = ("strict",)
    KIND = "complex"
    EXACT = (complex,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if isinstance(value, complex):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not self.strict:
            return complex(value)
        if isinstance(value, str):
            try:
                return complex(value)
            except ValueError:
                raise invalid("complex_str_parsing", value) from None
        raise invalid("complex_type", value)

    def project(self, value: Any) -> Any:
        return str(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string"}


class BytesNode(Node):
    __slots__ = ()
    KIND = "bytes"
    EXACT = (bytes,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        raise invalid("bytes_type", value)

    def project(self, value: Any) -> Any:
        return bytes(value).decode("utf-8", errors="replace")

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string", "format": "binary"}


class UUIDNode(Node):
    __slots__ = ("strict",)
    KIND = "uuid"
    EXACT = (uuid.UUID,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if isinstance(value, uuid.UUID):
            return value
        if isinstance(value, str):
            try:
                parsed = uuid.UUID(value.strip())
            except ValueError:
                raise invalid("uuid_parsing", value) from None
            if self.strict and str(parsed) != value:
                raise invalid("uuid_parsing", value)
            return parsed
        raise invalid("uuid_type", value)

    def project(self, value: Any) -> Any:
        return str(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string", "format": "uuid"}


class _TemporalNode(Node):
    __slots__ = ()
    target: type = datetime.datetime
    type_code = "datetime_type"
    parsing_code = "datetime_parsing"
    json_format = "date-time"

    def _accepts(self, value: Any) -> bool:
        return isinstance(value, self.target)

    def validate(self, value: Any) -> Any:
        if self._accepts(value):
            return value
        if isinstance(value, str):
            try:
                return self.target.fromisoformat(value.strip())  # type: ignore[attr-defined]
            except ValueError:
                raise invalid(self.parsing_code, value) from None
        raise invalid(self.type_code, value)

    def project(self, value: Any) -> Any:
        return value.isoformat()

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string", "format": self.json_format}


class DatetimeNode(_TemporalNode):
    __slots__ = ()
    KIND = "datetime"
    EXACT = (datetime.datetime,)


class DateNode(_TemporalNode):
    __slots__ = ()
    KIND = "date"
    EXACT = (datetime.date,)
    target = datetime.date
    type_code = "date_type"
    parsing_code = "date_parsing"
    json_format = "date"

    def _accepts(self, value: Any) -> bool:
        return isinstance(value, datetime.date) and not isinstance(value, datetime.datetime)


class TimeNode(_TemporalNode):
    __slots__ = ()
    KIND = "time"
    EXACT = (datetime.time,)
    target = datetime.time
    type_code = "time_type"
    parsing_code = "time_parsing"
    json_format = "time"


class TimedeltaNode(Node):
    __slots__ = ("strict",)
    KIND = "timedelta"
    EXACT = (datetime.timedelta,)

    def __init__(self, strict: bool) -> None:
        super().__init__()
        self.strict = strict

    def validate(self, value: Any) -> Any:
        if isinstance(value, datetime.timedelta):
            return value
        if isinstance(value, str):
            parsed = parse_iso_duration(value)
            if parsed is not None:
                return parsed
            if not self.strict:
                try:
                    return datetime.timedelta(seconds=float(value))
                except (ValueError, OverflowError):
                    pass
            raise invalid("time_delta_parsing", value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and not self.strict:
            try:
                return datetime.timedelta(seconds=value)
            except OverflowError:
                raise invalid("time_delta_parsing", value) from None
        raise invalid("time_delta_type", value)

    def project(self, value: Any) -> Any:
        return format_iso_duration(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string", "format": "duration"}


class PathNode(Node):
    __slots__ = ("cls",)
    KIND = "path"

    def __init__(self, cls: type) -> None:
        super().__init__()
        self.cls = cls
        self.exact_types = (cls,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.cls):
            return value
        if isinstance(value, (str, pathlib.PurePath)):
            return self.cls(value)
        raise invalid("path_type", value)

    def project(self, value: Any) -> Any:
        return str(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string", "format": "path"}


class InstanceNode(Node):
    """Accepts instances of one framework type (``UploadFile``) as they are."""

    __slots__ = ("cls",)
    KIND = "instance"

    def __init__(self, cls: type) -> None:
        super().__init__()
        self.cls = cls
        self.exact_types = (cls,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.cls):
            return value
        raise invalid("is_instance_of", value, {"class": self.cls.__name__})

    def project(self, value: Any) -> Any:
        return to_builtins(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"type": "string", "format": "binary"}

    @property
    def label(self) -> str:
        return self.cls.__name__


class EnumNode(Node):
    __slots__ = ("cls", "strict", "_by_value", "_by_text", "_expected")
    KIND = "enum"

    def __init__(self, cls: type[enum.Enum], strict: bool) -> None:
        super().__init__()
        self.cls = cls
        self.strict = strict
        self.exact_types = (cls,)
        self._by_value: dict[tuple[type, Any], enum.Enum] = {}
        self._by_text: dict[str, enum.Enum] = {}
        for member in cls:
            try:
                self._by_value.setdefault((type(member.value), member.value), member)
            except TypeError:  # unhashable value — matched by the linear scan
                pass
            self._by_text.setdefault(str(member.value), member)
        self._expected = format_expected(m.value for m in cls)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.cls):
            return value
        try:
            member = self._by_value.get((type(value), value))
        except TypeError:
            member = next((m for m in self.cls if m.value == value), None)
        if member is not None:
            return member
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            for (vtype, vvalue), candidate in self._by_value.items():
                if vtype in (int, float) and vvalue == value:
                    return candidate
        if not self.strict and isinstance(value, str) and value in self._by_text:
            return self._by_text[value]
        raise invalid("enum", value, {"expected": self._expected})

    def project(self, value: Any) -> Any:
        return to_builtins(value.value if isinstance(value, enum.Enum) else value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        values = [to_builtins(m.value) for m in self.cls]
        out: dict[str, Any] = {"title": self.cls.__name__, "enum": values}
        json_type = _json_type_of(values)
        if json_type:
            out["type"] = json_type
        return ctx.define(self.cls, out)

    @property
    def label(self) -> str:
        return self.cls.__name__


class LiteralNode(Node):
    __slots__ = ("values", "strict", "_expected", "_by_text")
    KIND = "literal"

    def __init__(self, values: tuple[Any, ...], strict: bool) -> None:
        super().__init__()
        self.values = values
        self.strict = strict
        self._expected = format_expected(values)
        self._by_text: dict[str, Any] = {}
        for lit in values:
            if isinstance(lit, bool):
                for token in ("1", "true", "yes", "on") if lit else ("0", "false", "no", "off"):
                    self._by_text.setdefault(token, lit)
            elif isinstance(lit, int):
                self._by_text.setdefault(str(lit), lit)

    def validate(self, value: Any) -> Any:
        for lit in self.values:
            if value == lit and type(value) is type(lit):
                return lit
        if not self.strict and isinstance(value, str):
            hit = self._by_text.get(value.strip().lower(), MISSING)
            if hit is not MISSING:
                return hit
        raise invalid("literal_error", value, {"expected": self._expected})

    def is_instance(self, value: Any) -> bool:
        return any(value == lit and type(value) is type(lit) for lit in self.values)

    def project(self, value: Any) -> Any:
        return to_builtins(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        values = [to_builtins(v) for v in self.values]
        out: dict[str, Any] = {"const": values[0]} if len(values) == 1 else {"enum": values}
        json_type = _json_type_of(values)
        if json_type:
            out["type"] = json_type
        return out


def _json_type_of(values: list[Any]) -> str | None:
    kinds = set()
    for v in values:
        if isinstance(v, bool):
            kinds.add("boolean")
        elif isinstance(v, int):
            kinds.add("integer")
        elif isinstance(v, float):
            kinds.add("number")
        elif isinstance(v, str):
            kinds.add("string")
        elif v is None:
            kinds.add("null")
        else:
            return None
    return kinds.pop() if len(kinds) == 1 else None


# ---------------------------------------------------------------------------
# Constraints (``Annotated[int, Ge(1)]`` …)
# ---------------------------------------------------------------------------


class Check:
    """One constraint applied after the inner node validated the value."""

    __slots__ = ()

    def check(self, value: Any) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    def keywords(self) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError


_BOUND_CODES = {
    "ge": ("greater_than_equal", "minimum"),
    "gt": ("greater_than", "exclusiveMinimum"),
    "le": ("less_than_equal", "maximum"),
    "lt": ("less_than", "exclusiveMaximum"),
}


class BoundCheck(Check):
    __slots__ = ("op", "limit")

    def __init__(self, op: str, limit: Any) -> None:
        self.op = op
        self.limit = limit

    def check(self, value: Any) -> None:
        limit = self.limit
        op = self.op
        if op == "ge":
            ok = value >= limit
        elif op == "gt":
            ok = value > limit
        elif op == "le":
            ok = value <= limit
        else:
            ok = value < limit
        if not ok:
            raise invalid(_BOUND_CODES[op][0], value, {op: _ctx_number(limit)})

    def keywords(self) -> dict[str, Any]:
        return {_BOUND_CODES[self.op][1]: _ctx_number(self.limit)}


def _ctx_number(limit: Any) -> Any:
    if isinstance(limit, decimal.Decimal):
        return int(limit) if limit == limit.to_integral_value() else float(limit)
    return limit


class MultipleOfCheck(Check):
    __slots__ = ("divisor",)

    def __init__(self, divisor: Any) -> None:
        self.divisor = divisor

    def check(self, value: Any) -> None:
        divisor = self.divisor
        if isinstance(value, int) and isinstance(divisor, int):
            ok = value % divisor == 0
        elif isinstance(value, decimal.Decimal):
            ok = value % decimal.Decimal(str(divisor)) == 0
        else:
            quotient = value / divisor
            ok = abs(quotient - round(quotient)) <= 1e-9 * max(1.0, abs(quotient))
        if not ok:
            raise invalid("multiple_of", value, {"multiple_of": _ctx_number(divisor)})

    def keywords(self) -> dict[str, Any]:
        return {"multipleOf": _ctx_number(self.divisor)}


_LENGTH_CODES = {
    "str": ("string_too_short", "string_too_long", "minLength", "maxLength"),
    "bytes": ("bytes_too_short", "bytes_too_long", "minLength", "maxLength"),
    "list": ("too_short", "too_long", "minItems", "maxItems"),
    "tuple": ("too_short", "too_long", "minItems", "maxItems"),
    "set": ("too_short", "too_long", "minItems", "maxItems"),
    "frozenset": ("too_short", "too_long", "minItems", "maxItems"),
    "dict": ("too_short", "too_long", "minProperties", "maxProperties"),
}
_FIELD_TYPES = {
    "list": "List",
    "tuple": "Tuple",
    "set": "Set",
    "frozenset": "Frozenset",
    "dict": "Dictionary",
}


class LengthCheck(Check):
    __slots__ = ("min_length", "max_length", "kind")

    def __init__(self, kind: str, min_length: int | None, max_length: int | None) -> None:
        self.kind = kind
        self.min_length = min_length
        self.max_length = max_length

    def check(self, value: Any) -> None:
        size = len(value)
        short_code, long_code, _, _ = _LENGTH_CODES[self.kind]
        ctx: dict[str, Any]
        if self.min_length is not None and size < self.min_length:
            ctx = {"min_length": self.min_length}
            if self.kind in _FIELD_TYPES:
                ctx = {
                    "field_type": _FIELD_TYPES[self.kind],
                    "min_length": self.min_length,
                    "actual_length": size,
                }
            raise invalid(short_code, value, ctx)
        if self.max_length is not None and size > self.max_length:
            ctx = {"max_length": self.max_length}
            if self.kind in _FIELD_TYPES:
                ctx = {
                    "field_type": _FIELD_TYPES[self.kind],
                    "max_length": self.max_length,
                    "actual_length": size,
                }
            raise invalid(long_code, value, ctx)

    def keywords(self) -> dict[str, Any]:
        _, _, min_kw, max_kw = _LENGTH_CODES[self.kind]
        out: dict[str, Any] = {}
        if self.min_length is not None:
            out[min_kw] = self.min_length
        if self.max_length is not None:
            out[max_kw] = self.max_length
        return out


class PatternCheck(Check):
    """``Pattern`` (search semantics) or a legacy ``FieldDescriptor.pattern`` (full match)."""

    __slots__ = ("regex", "full")

    def __init__(self, regex: re.Pattern[str], *, full: bool) -> None:
        self.regex = regex
        self.full = full

    def check(self, value: Any) -> None:
        matched = self.regex.fullmatch(value) if self.full else self.regex.search(value)
        if matched is None:
            raise invalid("string_pattern_mismatch", value, {"pattern": self.regex.pattern})

    def keywords(self) -> dict[str, Any]:
        pattern = self.regex.pattern
        return {"pattern": f"^(?:{pattern})$" if self.full else pattern}


class ConstrainedNode(Node):
    __slots__ = ("inner", "checks")

    def __init__(self, inner: Node, checks: list[Check]) -> None:
        super().__init__()
        self.inner = inner
        self.checks = tuple(checks)
        self.kind = inner.kind
        self.exact_types = inner.exact_types

    def validate(self, value: Any) -> Any:
        result = self.inner.validate(value)
        for check in self.checks:
            check.check(result)
        return result

    def project(self, value: Any) -> Any:
        return self.inner.project(value)

    def is_instance(self, value: Any) -> bool:
        return self.inner.is_instance(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        out = dict(self.inner.schema(ctx))
        for check in self.checks:
            out.update(check.keywords())
        return out

    @property
    def label(self) -> str:
        return self.inner.label


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

_SEQUENCE_CODES: dict[type, str] = {
    list: "list_type",
    tuple: "tuple_type",
    set: "set_type",
    frozenset: "frozen_set_type",
}
_SEQUENCE_KINDS: dict[type, str] = {list: "list", tuple: "tuple", set: "set", frozenset: "frozenset"}


class SequenceNode(Node):
    """``list[T]`` / ``tuple[T, ...]`` / ``set[T]`` / ``frozenset[T]``."""

    __slots__ = ("item", "container", "limit", "_accepted", "_code")

    def __init__(self, item: Node, container: type, *, strict: bool, limit: int) -> None:
        super().__init__()
        self.item = item
        self.container = container
        self.limit = limit
        self.kind = _SEQUENCE_KINDS[container]
        self.exact_types = (container,)
        accepted: tuple[type, ...] = (list, tuple)
        if container in (set, frozenset) or not strict:
            accepted = (list, tuple, set, frozenset)
        self._accepted = accepted
        self._code = _SEQUENCE_CODES[container]
        self.explicit = item.explicit

    def validate(self, value: Any) -> Any:
        if not isinstance(value, self._accepted):
            raise invalid(self._code, value)
        validate = self.item.validate
        out: list[Any] = []
        append = out.append
        issues: list[ValidationIssue] | None = None
        elements: Any = value
        for index, element in enumerate(elements):
            try:
                append(validate(element))
            except _Invalid as exc:
                if issues is None:
                    issues = []
                issues.extend(issue.prefixed(index) for issue in exc.issues)
                if len(issues) >= self.limit:
                    break
        if issues:
            raise _Invalid(issues)
        if self.container is list:
            return out
        try:
            return self.container(out)
        except TypeError:
            raise invalid("set_item_not_hashable", value) from None

    def project(self, value: Any) -> Any:
        item = self.item
        return [item.project(v) for v in value]

    def schema(self, ctx: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "array", "items": self.item.schema(ctx)}
        if self.container in (set, frozenset):
            out["uniqueItems"] = True
        return out

    @property
    def label(self) -> str:
        return f"{self.kind}[{self.item.label}]"


class TupleNode(Node):
    """Fixed-length ``tuple[A, B, C]``."""

    __slots__ = ("items",)
    KIND = "tuple"
    EXACT = (tuple,)

    def __init__(self, items: list[Node]) -> None:
        super().__init__()
        self.items = tuple(items)
        self.explicit = any(n.explicit for n in items)

    def validate(self, value: Any) -> Any:
        if not isinstance(value, (list, tuple)):
            raise invalid("tuple_type", value)
        issues: list[ValidationIssue] = []
        out: list[Any] = []
        for index, node in enumerate(self.items):
            if index >= len(value):
                issues.append(make_issue("missing", loc=(index,)))
                continue
            try:
                out.append(node.validate(value[index]))
            except _Invalid as exc:
                issues.extend(issue.prefixed(index) for issue in exc.issues)
        if len(value) > len(self.items):
            ctx = {"field_type": "Tuple", "max_length": len(self.items), "actual_length": len(value)}
            issues.append(make_issue("too_long", value, ctx))
        if issues:
            raise _Invalid(issues)
        return tuple(out)

    def project(self, value: Any) -> Any:
        return [node.project(v) for node, v in zip(self.items, value)]

    def schema(self, ctx: Any) -> dict[str, Any]:
        n = len(self.items)
        return {
            "type": "array",
            "prefixItems": [node.schema(ctx) for node in self.items],
            "minItems": n,
            "maxItems": n,
        }


class DictNode(Node):
    __slots__ = ("key", "value", "limit")
    KIND = "dict"
    EXACT = (dict,)

    def __init__(self, key: Node, value: Node, *, limit: int) -> None:
        super().__init__()
        self.key = key
        self.value = value
        self.limit = limit
        self.explicit = key.explicit or value.explicit

    def validate(self, value: Any) -> Any:
        if not isinstance(value, Mapping):
            raise invalid("dict_type", value)
        out: dict[Any, Any] = {}
        issues: list[ValidationIssue] | None = None
        key_node, value_node = self.key, self.value
        for k, v in value.items():
            try:
                new_key = key_node.validate(k)
            except _Invalid as exc:
                issues = issues or []
                issues.extend(issue.prefixed(k, "[key]") for issue in exc.issues)
                new_key = MISSING
            try:
                new_value = value_node.validate(v)
            except _Invalid as exc:
                issues = issues or []
                issues.extend(issue.prefixed(k) for issue in exc.issues)
                new_value = MISSING
            if issues:
                if len(issues) >= self.limit:
                    break
                continue
            out[new_key] = new_value
        if issues:
            raise _Invalid(issues)
        return out

    def project(self, value: Any) -> Any:
        return {self.key.project(k): self.value.project(v) for k, v in value.items()}

    def schema(self, ctx: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"type": "object"}
        value_schema = self.value.schema(ctx)
        out["additionalProperties"] = value_schema if value_schema else True
        return out


# ---------------------------------------------------------------------------
# Optional and unions
# ---------------------------------------------------------------------------


class OptionalNode(Node):
    """``X | None``. ``blank_none`` turns an empty string into ``None`` (query/form values)."""

    __slots__ = ("inner", "blank_none")
    KIND = "optional"

    def __init__(self, inner: Node, *, blank_none: bool) -> None:
        super().__init__()
        self.inner = inner
        self.blank_none = blank_none
        self.exact_types = (*inner.exact_types, _NoneType)
        self.explicit = inner.explicit

    def validate(self, value: Any) -> Any:
        if value is None:
            return None
        if self.blank_none and value == "":
            return None
        return self.inner.validate(value)

    def project(self, value: Any) -> Any:
        return None if value is None else self.inner.project(value)

    def is_instance(self, value: Any) -> bool:
        return value is None or self.inner.is_instance(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        inner = self.inner.schema(ctx)
        return {"anyOf": [inner, {"type": "null"}]}

    @property
    def label(self) -> str:
        return f"{self.inner.label} | None"


class UnionNode(Node):
    """``A | B``.

    JSON values use *smart* matching: an exact type match first, then the
    members strictly left to right, then (lax mode) laxly left to right.
    String sources (path, query, header, cookie, form) try the members laxly
    left to right, so ``int | str`` reads ``"5"`` as ``5``.
    """

    __slots__ = ("members", "strict_members", "left_to_right", "strict")
    KIND = "union"

    def __init__(
        self, members: list[Node], strict_members: list[Node], *, left_to_right: bool, strict: bool
    ) -> None:
        super().__init__()
        self.members = tuple(members)
        self.strict_members = tuple(strict_members)
        self.left_to_right = left_to_right
        self.strict = strict
        self.explicit = any(m.explicit for m in members)
        exact: tuple[type, ...] = ()
        for m in members:
            exact += m.exact_types
        self.exact_types = exact

    def validate(self, value: Any) -> Any:
        if self.left_to_right:
            return self._first_success(self.members, value)
        vtype = type(value)
        for member, strict_member in zip(self.members, self.strict_members):
            if vtype in member.exact_types:
                try:
                    return strict_member.validate(value)
                except _Invalid:
                    break
        if self.strict:
            return self._first_success(self.strict_members, value)
        for strict_member in self.strict_members:
            try:
                return strict_member.validate(value)
            except _Invalid:
                continue
        return self._first_success(self.members, value)

    def _first_success(self, members: tuple[Node, ...], value: Any) -> Any:
        issues: list[ValidationIssue] = []
        for member in members:
            try:
                return member.validate(value)
            except _Invalid as exc:
                label = member.label
                issues.extend(issue.prefixed(label) for issue in exc.issues)
        raise _Invalid(issues or [make_issue("union_no_match", value)])

    def project(self, value: Any) -> Any:
        for member in self.members:
            if member.is_instance(value):
                return member.project(value)
        return to_builtins(value)

    def is_instance(self, value: Any) -> bool:
        return any(m.is_instance(value) for m in self.members)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return {"anyOf": [m.schema(ctx) for m in self.members]}

    @property
    def label(self) -> str:
        return " | ".join(m.label for m in self.members)


class DiscriminatedNode(Node):
    """``Discriminated[A | B, "kind"]``: the tag picks the variant, variant issues nest under the tag."""

    __slots__ = ("key", "variants", "classes", "strip_key", "_expected")
    KIND = "discriminated"

    def __init__(
        self, key: str, variants: dict[str, Node], classes: dict[str, type], strip_key: frozenset[str]
    ) -> None:
        super().__init__()
        self.key = key
        self.variants = variants
        self.classes = classes
        #: Tags whose variant does not declare the discriminator field — the
        #: key is removed before validating them so ``extra="forbid"`` holds.
        self.strip_key = strip_key
        self._expected = ", ".join(repr(t) for t in variants)
        self.explicit = any(n.explicit for n in variants.values())

    def validate(self, value: Any) -> Any:
        key = self.key
        if not isinstance(value, Mapping):
            raise invalid(
                "model_type", value, {"class_name": " | ".join(c.__name__ for c in self.classes.values())}
            )
        raw_tag = value.get(key)
        if raw_tag is None:
            raise invalid("union_tag_not_found", value, {"discriminator": repr(key)})
        tag = str(raw_tag)
        node = self.variants.get(tag)
        if node is None:
            raise invalid(
                "union_tag_invalid",
                value,
                {"discriminator": repr(key), "tag": tag, "expected_tags": self._expected},
            )
        payload = {k: v for k, v in value.items() if k != key} if tag in self.strip_key else value
        try:
            return node.validate(payload)
        except _Invalid as exc:
            raise _Invalid([issue.prefixed(tag) for issue in exc.issues]) from None

    def project(self, value: Any) -> Any:
        for tag, cls in self.classes.items():
            if isinstance(value, cls):
                out = self.variants[tag].project(value)
                if isinstance(out, dict) and tag in self.strip_key:
                    out = {self.key: tag, **out}
                return out
        return to_builtins(value)

    def is_instance(self, value: Any) -> bool:
        return any(isinstance(value, cls) for cls in self.classes.values())

    def schema(self, ctx: Any) -> dict[str, Any]:
        one_of = []
        mapping: dict[str, str] = {}
        for tag, node in self.variants.items():
            ref = node.schema(ctx)
            one_of.append(ref)
            if "$ref" in ref:
                mapping[tag] = ref["$ref"]
        out: dict[str, Any] = {"oneOf": one_of, "discriminator": {"propertyName": self.key}}
        if mapping:
            out["discriminator"]["mapping"] = mapping
        return out


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Field:
    """One model field: python ``name``, ``wire`` name, validator and default info."""

    __slots__ = ("name", "wire", "node", "required", "default", "init")

    def __init__(
        self, name: str, wire: str, node: Node, *, required: bool, default: Any = MISSING, init: bool = True
    ):
        self.name = name
        self.wire = wire
        self.node = node
        self.required = required
        self.default = default
        self.init = init


class ModelNode(Node):
    """Shared logic for dataclass / TypedDict / NamedTuple validators."""

    __slots__ = ("cls", "fields", "forbid", "limit", "_wires", "_known", "_output", "_plan")
    KIND = "model"

    def __init__(
        self, cls: type, fields: list[Field], *, forbid: bool, limit: int, output: list[Field] | None = None
    ):
        super().__init__()
        self.cls = cls
        self.fields = tuple(f for f in fields if f.init)
        self.forbid = forbid
        self.limit = limit
        self._wires = frozenset(f.wire for f in self.fields)
        #: Keys tolerated under ``extra="forbid"``: every declared wire name
        #: (output-only fields included) — they are not "extra".
        self._known = frozenset(f.wire for f in (output or fields)) | self._wires
        #: Fields emitted by :meth:`project` (includes ``field(init=False)``).
        self._output = tuple(output or fields)
        #: Hot-path view of the fields: ``(name, wire, validate, required)``.
        self._plan = tuple((f.name, f.wire, f.node.validate, f.required) for f in self.fields)

    def _collect(self, value: Any) -> dict[str, Any]:
        if type(value) is not dict and not isinstance(value, Mapping):
            raise invalid("model_type", value, {"class_name": self.cls.__name__})
        kwargs: dict[str, Any] = {}
        issues: list[ValidationIssue] | None = None
        get = value.get
        for name, wire, validate, required in self._plan:
            raw = get(wire, MISSING)
            if raw is MISSING:
                if required:
                    issues = issues or []
                    issues.append(make_issue("missing", loc=(wire,)))
                continue
            try:
                kwargs[name] = validate(raw)
            except _Invalid as exc:
                issues = issues or []
                issues.extend(issue.prefixed(wire) for issue in exc.issues)
                if len(issues) >= self.limit:
                    raise _Invalid(issues) from None
        if self.forbid:
            for key in value:
                if key not in self._known:
                    issues = issues or []
                    issues.append(make_issue("extra_forbidden", value[key], loc=(key,)))
        if issues:
            raise _Invalid(issues)
        return kwargs

    def _construct(self, kwargs: dict[str, Any]) -> Any:
        try:
            return self.cls(**kwargs)
        except ValidationFailed as exc:
            raise _Invalid(list(exc.issues)) from None
        except (ValueError, TypeError, AssertionError) as exc:
            text = str(exc) or type(exc).__name__
            raise invalid("value_error", MISSING, {"error": text}) from None

    def project(self, value: Any) -> Any:
        out: dict[str, Any] = {}
        if isinstance(value, Mapping):
            for f in self._output:
                if f.name in value:
                    out[f.wire] = f.node.project(value[f.name])
                elif f.wire in value:
                    out[f.wire] = f.node.project(value[f.wire])
            return out
        for f in self._output:
            attr = getattr(value, f.name, MISSING)
            if attr is not MISSING:
                out[f.wire] = f.node.project(attr)
        return out

    def is_instance(self, value: Any) -> bool:
        return isinstance(value, self.cls)

    def schema(self, ctx: Any) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            properties: dict[str, Any] = {}
            required: list[str] = []
            for f in self.fields:
                prop = f.node.schema(ctx)
                if not f.required and f.default is not MISSING and _is_json_literal(f.default):
                    prop = {**prop, "default": f.default}
                properties[f.wire] = prop
                if f.required:
                    required.append(f.wire)
            out: dict[str, Any] = {"type": "object", "title": self.cls.__name__, "properties": properties}
            if required:
                out["required"] = required
            if self.forbid:
                out["additionalProperties"] = False
            return out

        return ctx.define(self.cls, build)

    @property
    def label(self) -> str:
        return self.cls.__name__


def _is_json_literal(value: Any) -> bool:
    return value is None or isinstance(value, (str, bool, int, float))


class DataclassNode(ModelNode):
    __slots__ = ()

    def __init__(self, cls: type, fields: list[Field], *, forbid: bool, limit: int, output: list[Field]):
        super().__init__(cls, fields, forbid=forbid, limit=limit, output=output)
        self.exact_types = (cls,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.cls):
            return value
        return self._construct(self._collect(value))


class TypedDictNode(ModelNode):
    __slots__ = ()

    def validate(self, value: Any) -> Any:
        return self._collect(value)

    def is_instance(self, value: Any) -> bool:
        return isinstance(value, Mapping)


class NamedTupleNode(ModelNode):
    __slots__ = ()

    def __init__(self, cls: type, fields: list[Field], *, forbid: bool, limit: int):
        super().__init__(cls, fields, forbid=forbid, limit=limit)
        self.exact_types = (cls,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.cls):
            return value
        if isinstance(value, (list, tuple)):
            if len(value) > len(self.fields):
                ctx = {"field_type": "Tuple", "max_length": len(self.fields), "actual_length": len(value)}
                raise invalid("too_long", value, ctx)
            value = {f.wire: item for f, item in zip(self.fields, value)}
        return self._construct(self._collect(value))


__all__ = [
    "AnyNode",
    "BoolNode",
    "BoundCheck",
    "BytesNode",
    "Check",
    "ComplexNode",
    "ConstrainedNode",
    "DataclassNode",
    "DateNode",
    "DatetimeNode",
    "DecimalNode",
    "DictNode",
    "DiscriminatedNode",
    "EnumNode",
    "Field",
    "FloatNode",
    "InstanceNode",
    "IntNode",
    "LengthCheck",
    "LiteralNode",
    "ModelNode",
    "MultipleOfCheck",
    "NamedTupleNode",
    "Node",
    "NoneNode",
    "OptionalNode",
    "PathNode",
    "PatternCheck",
    "RefNode",
    "SequenceNode",
    "StrNode",
    "TimeNode",
    "TimedeltaNode",
    "TupleNode",
    "TypedDictNode",
    "UUIDNode",
    "UnionNode",
    "to_builtins",
]
