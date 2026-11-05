"""The validation-issue model shared by every backend and transport (private).

A :class:`ValidationIssue` is one failure: ``loc`` (where, as a path of keys
and indexes), ``type`` (a stable machine code, using pydantic-core's
vocabulary where the two overlap), ``msg`` (English text) and optionally the
offending ``input`` and a ``ctx`` mapping with the constraint values.

Inside the engine, nodes raise :class:`_Invalid` with issues whose ``loc`` is
relative to the node; containers prefix their own key as the exception
travels up. :class:`ValidationFailed` is the public exception a compiled type
raises, with absolute locations.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: Longest ``msg`` kept on an issue; longer texts (a huge ``__post_init__``
#: message, a backend error quoting the whole input) are cut.
MAX_MESSAGE_LENGTH = 512

#: Message templates by issue ``type``. ``{name}`` placeholders read ``ctx``.
MESSAGES: dict[str, str] = {
    "missing": "Field required",
    "extra_forbidden": "Extra inputs are not permitted",
    "string_type": "Input should be a valid string",
    "string_too_short": "String should have at least {min_length} character{min_length_s}",
    "string_too_long": "String should have at most {max_length} character{max_length_s}",
    "string_pattern_mismatch": "String should match pattern '{pattern}'",
    "int_type": "Input should be a valid integer",
    "int_parsing": "Input should be a valid integer, unable to parse string as an integer",
    "int_from_float": "Input should be a valid integer, got a number with a fractional part",
    "float_type": "Input should be a valid number",
    "float_parsing": "Input should be a valid number, unable to parse string as a number",
    "bool_type": "Input should be a valid boolean",
    "bool_parsing": "Input should be a valid boolean, unable to interpret input",
    "greater_than": "Input should be greater than {gt}",
    "greater_than_equal": "Input should be greater than or equal to {ge}",
    "less_than": "Input should be less than {lt}",
    "less_than_equal": "Input should be less than or equal to {le}",
    "multiple_of": "Input should be a multiple of {multiple_of}",
    "finite_number": "Input should be a finite number",
    "decimal_type": "Decimal input should be an integer, float, string or Decimal object",
    "decimal_parsing": "Input should be a valid decimal",
    "complex_type": "Input should be a valid complex number",
    "complex_str_parsing": "Input should be a valid complex string",
    "uuid_type": "UUID input should be a string or UUID object",
    "uuid_parsing": "Input should be a valid UUID",
    "datetime_type": "Input should be a valid datetime",
    "datetime_parsing": "Input should be a valid datetime in ISO 8601 format",
    "date_type": "Input should be a valid date",
    "date_parsing": "Input should be a valid date in ISO 8601 format (YYYY-MM-DD)",
    "time_type": "Input should be a valid time",
    "time_parsing": "Input should be a valid time in ISO 8601 format",
    "time_delta_type": "Input should be a valid timedelta",
    "time_delta_parsing": "Input should be a valid timedelta: seconds or an ISO 8601 duration such as PT5M",
    "bytes_type": "Input should be a valid bytes",
    "bytes_too_short": "Data should have at least {min_length} byte{min_length_s}",
    "bytes_too_long": "Data should have at most {max_length} byte{max_length_s}",
    "path_type": "Input should be a valid path",
    "enum": "Input should be {expected}",
    "literal_error": "Input should be {expected}",
    "list_type": "Input should be a valid list",
    "tuple_type": "Input should be a valid tuple",
    "set_type": "Input should be a valid set",
    "frozen_set_type": "Input should be a valid frozenset",
    "set_item_not_hashable": "Set items should be hashable",
    "too_short": "{field_type} should have at least {min_length} item{min_length_s} after validation, not {actual_length}",
    "too_long": "{field_type} should have at most {max_length} item{max_length_s} after validation, not {actual_length}",
    "dict_type": "Input should be a valid dictionary",
    "model_type": "Input should be a valid dictionary or instance of {class_name}",
    "is_instance_of": "Input should be an instance of {class}",
    "none_required": "Input should be None",
    "union_tag_not_found": "Unable to extract tag using discriminator {discriminator}",
    "union_tag_invalid": (
        "Input tag '{tag}' found using {discriminator} does not match any of the expected tags: {expected_tags}"
    ),
    "union_no_match": "Input does not match any member of the union",
    "json_invalid": "Invalid JSON: {error}",
    "value_error": "{error}",
}


class _Missing:
    """Sentinel for "no value" (an absent ``input``, a key not in a mapping)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "MISSING"

    def __bool__(self) -> bool:
        return False


#: The "no value" sentinel. ``ValidationIssue.input is MISSING`` means the
#: issue carries no input (a missing field, or an input the engine dropped).
MISSING: Any = _Missing()


def format_loc(loc: Sequence[str | int]) -> str:
    """Render a ``loc`` path as dotted text (``body.items.0.name``)."""
    return ".".join(str(part) for part in loc) if loc else "<root>"


def _bounded(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_LENGTH else text[: MAX_MESSAGE_LENGTH - 3] + "..."


def jsonable(value: Any, _depth: int = 0) -> Any:
    """Return ``value`` as JSON-compatible builtins, best effort.

    Used to echo ``input`` and to keep ``ctx`` serialisable: primitives pass
    through, containers are converted recursively, anything else becomes its
    ``str()``.
    """
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if _depth > 16:
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): jsonable(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [jsonable(v, _depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("utf-8", errors="replace")
    return str(value)


@dataclass(frozen=True, slots=True)
class ValidationIssue:
    """One validation failure: where it happened, a stable code and a message.

    ``loc`` starts with the parameter source and wire name when the issue comes
    from a request (``("query", "page")``, ``("body", "address", "zip")``,
    ``("frame", "data", "age")``, ``("item", 0, "text")``); a compiled type
    used directly reports locations relative to the validated value. ``type``
    is the machine-readable code (``"int_parsing"``, ``"missing"``,
    ``"less_than_equal"``, …), ``msg`` the English text, ``ctx`` the constraint
    values (``{"le": 100}``) and ``input`` the rejected value when kept.
    """

    loc: tuple[str | int, ...]
    type: str
    msg: str
    input: Any = MISSING
    ctx: Mapping[str, Any] | None = None

    def to_dict(self, *, include_input: bool = False) -> dict[str, Any]:
        """Return the JSON shape ``{"loc", "type", "msg", "input"?, "ctx"?}``.

        ``input`` is included only when ``include_input`` is true and the issue
        kept one; it is converted to JSON-compatible builtins.
        """
        out: dict[str, Any] = {"loc": list(self.loc), "type": self.type, "msg": self.msg}
        if include_input and self.input is not MISSING:
            out["input"] = jsonable(self.input)
        if self.ctx:
            out["ctx"] = {str(k): jsonable(v) for k, v in self.ctx.items()}
        return out

    def prefixed(self, *prefix: str | int) -> ValidationIssue:
        """Return a copy whose ``loc`` starts with ``prefix``."""
        return ValidationIssue(prefix + self.loc, self.type, self.msg, self.input, self.ctx)

    def __str__(self) -> str:
        return f"{format_loc(self.loc)}: {self.msg}"


def make_issue(
    issue_type: str,
    value: Any = MISSING,
    ctx: Mapping[str, Any] | None = None,
    *,
    loc: tuple[str | int, ...] = (),
    msg: str | None = None,
) -> ValidationIssue:
    """Build an issue from the message catalogue (``msg`` overrides the template)."""
    if msg is None:
        template = MESSAGES.get(issue_type, issue_type)
        fields: dict[str, Any] = dict(ctx or {})
        for key in ("min_length", "max_length"):
            if key in fields:
                fields[f"{key}_s"] = "" if fields[key] == 1 else "s"
        try:
            msg = template.format(**fields)
        except (KeyError, IndexError, ValueError):
            msg = template
    return ValidationIssue(loc, issue_type, _bounded(msg), value, dict(ctx) if ctx else None)


class _Invalid(Exception):
    """Internal: a node rejected its input. ``issues`` carry node-relative ``loc``s.

    ``text`` keeps a backend's original message (msgspec) for the legacy
    ``detail.errors`` strings.
    """

    __slots__ = ("issues", "text")

    def __init__(self, issues: list[ValidationIssue], text: str | None = None) -> None:
        super().__init__()
        self.issues = issues
        self.text = text


def invalid(
    issue_type: str, value: Any = MISSING, ctx: Mapping[str, Any] | None = None, **kw: Any
) -> _Invalid:
    """Shorthand: an :class:`_Invalid` carrying one catalogue issue."""
    return _Invalid([make_issue(issue_type, value, ctx, **kw)])


def prefix_all(key: str | int, issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    """Prefix every issue's ``loc`` with ``key``."""
    return [issue.prefixed(key) for issue in issues]


def format_expected(values: Iterable[Any]) -> str:
    """``'a', 'b' or 'c'`` — the ``expected`` text of ``enum`` / ``literal_error``."""
    rendered = [repr(v) for v in values]
    if not rendered:
        return "nothing"
    if len(rendered) == 1:
        return rendered[0]
    return ", ".join(rendered[:-1]) + " or " + rendered[-1]


class ValidationFailed(ValueError):
    """A value did not match its compiled type; ``issues`` lists every failure.

    Raised by :meth:`CompiledType.validate` and :func:`lauren.validation.validate`.
    It is a plain :class:`ValueError` (not an HTTP error); inside a request the
    framework turns validation failures into a 422 ``ExtractorError``.
    """

    def __init__(self, issues: Sequence[ValidationIssue]) -> None:
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        count = len(self.issues)
        head = f"{count} validation error{'' if count == 1 else 's'}"
        body = "; ".join(str(issue) for issue in self.issues[:5])
        more = f" (+{count - 5} more)" if count > 5 else ""
        super().__init__(f"{head}: {body}{more}" if body else head)

    def errors(self, *, include_input: bool = False) -> list[dict[str, Any]]:
        """Return the issues as JSON dicts (see :meth:`ValidationIssue.to_dict`)."""
        return [issue.to_dict(include_input=include_input) for issue in self.issues]


__all__ = [
    "MESSAGES",
    "MISSING",
    "MAX_MESSAGE_LENGTH",
    "ValidationFailed",
    "ValidationIssue",
    "format_expected",
    "format_loc",
    "invalid",
    "jsonable",
    "make_issue",
    "prefix_all",
]
