"""Pydantic and msgspec backends plus their error translation (private).

Backend selection is fixed per type at compile time: a pydantic ``BaseModel``
(or pydantic dataclass, or a pydantic ``Field(discriminator=...)`` union) is
validated by pydantic, a ``msgspec.Struct`` by msgspec, everything else by the
native nodes. Both libraries are imported lazily, only for their own types, so
the engine works with neither installed.

Their errors are translated to :class:`~lauren._schema._issues.ValidationIssue`:
pydantic already uses the same vocabulary (its ``url`` is dropped and ``ctx``
is made JSON-safe); msgspec messages such as ``Expected `int`, got `str` - at
`$.age``` are parsed for the path and the expected type. Unrecognised msgspec
messages become ``value_error`` issues that keep the original text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ._issues import MISSING, ValidationIssue, _Invalid, jsonable, make_issue
from ._nodes import Node


# ---------------------------------------------------------------------------
# pydantic
# ---------------------------------------------------------------------------


def translate_pydantic(exc: Any) -> list[ValidationIssue]:
    """Translate a ``pydantic.ValidationError`` into issues (``url`` dropped)."""
    try:
        errors = exc.errors(include_url=False)
    except TypeError:  # pragma: no cover - very old pydantic 2.x
        errors = exc.errors()
    issues: list[ValidationIssue] = []
    for err in errors:
        loc = tuple(part if isinstance(part, (str, int)) else str(part) for part in err.get("loc", ()))
        ctx = err.get("ctx")
        safe_ctx = {str(k): jsonable(v) for k, v in ctx.items()} if isinstance(ctx, Mapping) and ctx else None
        issues.append(
            ValidationIssue(
                loc=loc,
                type=str(err.get("type", "value_error")),
                msg=str(err.get("msg", "")),
                input=err.get("input", MISSING),
                ctx=safe_ctx,
            )
        )
    return issues


class PydanticModelNode(Node):
    """A pydantic ``BaseModel``: ``model_validate`` / ``model_dump(mode="json")``."""

    __slots__ = ("model", "_error")
    KIND = "model"

    def __init__(self, model: type) -> None:
        super().__init__()
        import pydantic

        self.model = model
        self._error = pydantic.ValidationError
        self.exact_types = (model,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.model):
            return value
        try:
            return self.model.model_validate(value)  # type: ignore[attr-defined]
        except self._error as exc:
            raise _Invalid(translate_pydantic(exc)) from None

    def project(self, value: Any) -> Any:
        if not isinstance(value, self.model):
            value = self.model.model_validate(value)  # type: ignore[attr-defined]
        # The declared model's serializer, not ``value.model_dump()``: a subclass
        # instance (``UserInDB`` for ``UserOut``) must not emit its extra fields.
        return self.model.__pydantic_serializer__.to_python(  # type: ignore[attr-defined]
            value, mode="json", by_alias=True
        )

    def is_instance(self, value: Any) -> bool:
        return isinstance(value, self.model)

    def schema(self, ctx: Any) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            raw = self.model.model_json_schema(ref_template=ctx.ref_template)  # type: ignore[attr-defined]
            defs = raw.pop("$defs", {})
            own = defs.pop(self.model.__name__, None) if "$ref" in raw else None
            for name, sub in defs.items():
                ctx.add_raw(name, sub)
            return own if own is not None else raw

        return ctx.define(self.model, build)

    @property
    def label(self) -> str:
        return self.model.__name__


class PydanticAdapterNode(Node):
    """Anything pydantic owns that is not a model (tagged unions, pydantic dataclasses)."""

    __slots__ = ("annotation", "_adapter", "_error", "_cls")
    KIND = "pydantic"

    def __init__(self, annotation: Any) -> None:
        super().__init__()
        import pydantic

        self.annotation = annotation
        self._adapter: pydantic.TypeAdapter[Any] = pydantic.TypeAdapter(annotation)
        self._error = pydantic.ValidationError
        self._cls = annotation if isinstance(annotation, type) else None
        if self._cls is not None:
            self.exact_types = (self._cls,)

    def validate(self, value: Any) -> Any:
        try:
            return self._adapter.validate_python(value)
        except self._error as exc:
            raise _Invalid(translate_pydantic(exc)) from None

    def project(self, value: Any) -> Any:
        return self._adapter.dump_python(value, mode="json", by_alias=True)

    def is_instance(self, value: Any) -> bool:
        return self._cls is not None and isinstance(value, self._cls)

    def schema(self, ctx: Any) -> dict[str, Any]:
        raw = self._adapter.json_schema(ref_template=ctx.ref_template)
        for name, sub in raw.pop("$defs", {}).items():
            ctx.add_raw(name, sub)
        return raw


# ---------------------------------------------------------------------------
# msgspec
# ---------------------------------------------------------------------------

_MSGSPEC_LOC = re.compile(r"\.([^.\[]+)|\[(\d+)\]|\[(\.\.\.)\]")
_EXPECTED_GOT = re.compile(r"^Expected `([^`]+)`, got `([^`]+)`$")
_EXPECTED_BOUND = re.compile(r"^Expected `([^`]+)` (>=|>|<=|<) (\S+)$")
_EXPECTED_LENGTH = re.compile(r"^Expected `([^`]+)` of length (>=|<=) (\d+)$")
_EXPECTED_MULTIPLE = re.compile(r"^Expected `([^`]+)` that's a multiple of (\S+)$")
_EXPECTED_PATTERN = re.compile(r"^Expected `str` matching regex (.+)$")
_MISSING_FIELD = re.compile(r"^Object missing required field `([^`]+)`$")
_UNKNOWN_FIELD = re.compile(r"^Object contains unknown field `([^`]+)`$")
_INVALID_ENUM = re.compile(r"^Invalid enum value (.+)$")

_TYPE_CODES = {
    "int": "int_type",
    "float": "float_type",
    "str": "string_type",
    "bool": "bool_type",
    "array": "list_type",
    "object": "dict_type",
    "null": "none_required",
    "bytes": "bytes_type",
}
_BOUND_TYPES = {
    ">=": ("greater_than_equal", "ge"),
    ">": ("greater_than", "gt"),
    "<=": ("less_than_equal", "le"),
    "<": ("less_than", "lt"),
}
_INVALID_MESSAGES = {
    "Invalid UUID": "uuid_parsing",
    "Invalid RFC3339 encoded datetime": "datetime_parsing",
    "Invalid RFC3339 encoded date": "date_parsing",
    "Invalid RFC3339 encoded time": "time_parsing",
    "Invalid ISO8601 duration": "time_delta_parsing",
    "Invalid decimal string": "decimal_parsing",
    "Invalid base64 encoded string": "bytes_type",
}


def _number(text: str) -> Any:
    try:
        return int(text)
    except ValueError:
        try:
            return float(text)
        except ValueError:
            return text


def msgspec_loc(path: str) -> tuple[str | int, ...]:
    """``$.a[0].b`` → ``("a", 0, "b")``; a hidden dict key (``[...]``) becomes ``"..."``."""
    out: list[str | int] = []
    for name, index, hidden in _MSGSPEC_LOC.findall(path.lstrip("$")):
        if name:
            out.append(name)
        elif index:
            out.append(int(index))
        elif hidden:
            out.append("...")
    return tuple(out)


def translate_msgspec(message: str) -> list[ValidationIssue]:
    """Translate one ``msgspec.ValidationError`` message into issues."""
    text, _, where = message.partition(" - at `")
    loc = msgspec_loc(where.rstrip("`")) if where else ()
    text = text.strip()
    match = _MISSING_FIELD.match(text)
    if match:
        return [make_issue("missing", loc=(*loc, match.group(1)))]
    match = _UNKNOWN_FIELD.match(text)
    if match:
        return [make_issue("extra_forbidden", loc=(*loc, match.group(1)))]
    match = _EXPECTED_GOT.match(text)
    if match:
        expected, got = match.groups()
        if expected == "int" and got == "float":
            return [make_issue("int_from_float", loc=loc)]
        code = _TYPE_CODES.get(expected.split(" ")[0].split("[")[0])
        if code is not None:
            return [make_issue(code, loc=loc)]
        return [make_issue("value_error", ctx={"error": text}, loc=loc, msg=text)]
    match = _EXPECTED_BOUND.match(text)
    if match:
        code, key = _BOUND_TYPES[match.group(2)]
        return [make_issue(code, ctx={key: _number(match.group(3))}, loc=loc)]
    match = _EXPECTED_LENGTH.match(text)
    if match:
        kind, op, size = match.group(1), match.group(2), int(match.group(3))
        key = "min_length" if op == ">=" else "max_length"
        if kind == "str":
            code = "string_too_short" if op == ">=" else "string_too_long"
            return [make_issue(code, ctx={key: size}, loc=loc)]
        if kind == "bytes":
            code = "bytes_too_short" if op == ">=" else "bytes_too_long"
            return [make_issue(code, ctx={key: size}, loc=loc)]
        code = "too_short" if op == ">=" else "too_long"
        field_type = "Dictionary" if kind == "object" else "List"
        bound = "at least" if op == ">=" else "at most"
        msg = f"{field_type} should have {bound} {size} item{'' if size == 1 else 's'}"
        return [make_issue(code, ctx={"field_type": field_type, key: size}, loc=loc, msg=msg)]
    match = _EXPECTED_MULTIPLE.match(text)
    if match:
        return [make_issue("multiple_of", ctx={"multiple_of": _number(match.group(2))}, loc=loc)]
    match = _EXPECTED_PATTERN.match(text)
    if match:
        pattern = match.group(1).strip()
        if len(pattern) >= 2 and pattern[0] == pattern[-1] and pattern[0] in "'\"":
            pattern = pattern[1:-1]
        return [make_issue("string_pattern_mismatch", ctx={"pattern": pattern}, loc=loc)]
    match = _INVALID_ENUM.match(text)
    if match:
        return [make_issue("enum", ctx={"expected": "one of the declared values"}, loc=loc, msg=text)]
    code = _INVALID_MESSAGES.get(text)
    if code is not None:
        return [make_issue(code, loc=loc)]
    return [make_issue("value_error", ctx={"error": text}, loc=loc, msg=text)]


class MsgspecDecodeError(Exception):
    """The body is not valid JSON (raised by the msgspec single-decode path)."""


class MsgspecStructNode(Node):
    """A ``msgspec.Struct``: ``msgspec.convert`` (or one ``msgspec.json.decode`` for bodies)."""

    __slots__ = ("struct", "strict", "_msgspec")
    KIND = "model"

    def __init__(self, struct: type, *, strict: bool) -> None:
        super().__init__()
        import msgspec

        self.struct = struct
        self.strict = strict
        self._msgspec = msgspec
        self.exact_types = (struct,)

    def validate(self, value: Any) -> Any:
        if isinstance(value, self.struct):
            return value
        msgspec = self._msgspec
        try:
            return msgspec.convert(value, type=self.struct, strict=self.strict)
        except msgspec.ValidationError as exc:
            raise _Invalid(translate_msgspec(str(exc)), str(exc)) from None

    def decode_json(self, body: bytes) -> Any:
        """Decode and validate a JSON body in one pass (``msgspec.json.decode``)."""
        msgspec = self._msgspec
        try:
            return msgspec.json.decode(body, type=self.struct)
        except msgspec.ValidationError as exc:
            raise _Invalid(translate_msgspec(str(exc)), str(exc)) from None
        except msgspec.DecodeError as exc:
            raise MsgspecDecodeError(str(exc)) from None

    def project(self, value: Any) -> Any:
        return self._msgspec.to_builtins(value)

    def is_instance(self, value: Any) -> bool:
        return isinstance(value, self.struct)

    def schema(self, ctx: Any) -> dict[str, Any]:
        def build() -> dict[str, Any]:
            (root,), components = self._msgspec.json.schema_components(
                [self.struct], ref_template=ctx.ref_template.replace("{model}", "{name}")
            )
            own = components.pop(self.struct.__name__, None)
            for name, sub in components.items():
                ctx.add_raw(name, sub)
            return own if own is not None else root

        return ctx.define(self.struct, build)

    @property
    def label(self) -> str:
        return self.struct.__name__


__all__ = [
    "MsgspecDecodeError",
    "MsgspecStructNode",
    "PydanticAdapterNode",
    "PydanticModelNode",
    "msgspec_loc",
    "translate_msgspec",
    "translate_pydantic",
]
