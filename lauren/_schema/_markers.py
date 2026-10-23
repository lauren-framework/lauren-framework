"""Constraint markers, per-type configuration and the app validation policy (private).

The public names are re-exported by :mod:`lauren.validation`.

Markers are plain frozen values placed in ``Annotated[...]`` metadata. The
compiler also honours any metadata object exposing the ``annotated-types``
attribute names (``ge``, ``gt``, ``le``, ``lt``, ``multiple_of``,
``min_length``, ``max_length``, ``pattern``), so ``annotated_types.Ge(13)`` or
``annotated_types.Len(1, 10)`` work unchanged and one model definition behaves
the same under pydantic.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

from ..exceptions import DecoratorUsageError, StartupError

#: Attribute that ``@validation_config`` sets on a class (read from the class's
#: own ``__dict__``; subclasses do not inherit it).
VALIDATION_META = "__lauren_validation__"

Number = int | float


@dataclass(frozen=True, slots=True)
class Ge:
    """The value must be greater than or equal to ``ge``."""

    ge: Any


@dataclass(frozen=True, slots=True)
class Gt:
    """The value must be greater than ``gt``."""

    gt: Any


@dataclass(frozen=True, slots=True)
class Le:
    """The value must be less than or equal to ``le``."""

    le: Any


@dataclass(frozen=True, slots=True)
class Lt:
    """The value must be less than ``lt``."""

    lt: Any


@dataclass(frozen=True, slots=True)
class MultipleOf:
    """The value must be a multiple of ``multiple_of``."""

    multiple_of: Any


@dataclass(frozen=True, slots=True)
class MinLen:
    """A string, bytes, collection or mapping must have at least ``min_length`` items."""

    min_length: int


@dataclass(frozen=True, slots=True)
class MaxLen:
    """A string, bytes, collection or mapping must have at most ``max_length`` items."""

    max_length: int


@dataclass(frozen=True, slots=True)
class Pattern:
    """A string must contain a match of the regular expression ``pattern`` (``re.search``)."""

    pattern: str


@dataclass(frozen=True, slots=True)
class Strict:
    """Validate this field in strict mode: no string-to-number or number-to-bool coercion.

    Strict applies to JSON bodies, WebSocket frames and stream items. Values
    from the path, query string, headers, cookies and urlencoded forms always
    arrive as strings and are always parsed laxly.
    """

    strict: bool = True


@dataclass(frozen=True, slots=True)
class Alias:
    """Read and write this model field under ``name`` on the wire (``Alias("firstName")``)."""

    name: str


_EXTRA_VALUES = ("ignore", "forbid")
_RENAME_VALUES = ("camel",)


@dataclass(frozen=True, slots=True)
class ValidationConfig:
    """Per-type settings attached by :func:`validation_config`; ``None`` means "use the default"."""

    extra: Literal["ignore", "forbid"] | None = None
    strict: bool | None = None
    rename: Literal["camel"] | None = None

    def merged(self, other: ValidationConfig) -> ValidationConfig:
        """Combine with ``other``; values ``other`` sets win."""
        return ValidationConfig(
            extra=other.extra if other.extra is not None else self.extra,
            strict=other.strict if other.strict is not None else self.strict,
            rename=other.rename if other.rename is not None else self.rename,
        )


def validation_config(
    *args: Any,
    extra: Literal["ignore", "forbid"] | None = None,
    strict: bool | None = None,
    rename: Literal["camel"] | None = None,
) -> Any:
    """Attach validation settings to a dataclass, TypedDict or NamedTuple.

    ``extra="forbid"`` rejects unknown keys with an ``extra_forbidden`` issue
    (``"ignore"``, the default, drops them). ``strict=True`` validates every
    field strictly. ``rename="camel"`` reads and writes ``snake_case`` fields
    as ``camelCase`` on the wire (an :class:`Alias` on a field wins).

    The class is returned unchanged with a ``__lauren_validation__`` marker;
    subclasses do not inherit it. Stacking merges the settings. Bare usage
    (``@validation_config`` without parentheses) is rejected.
    """
    if args:
        from ..decorators import _reject_bare_usage

        _reject_bare_usage("validation_config", args[0])
        raise DecoratorUsageError(
            "@validation_config takes keyword arguments only, e.g. @validation_config(extra='forbid')",
            detail={"decorator": "validation_config", "args": [repr(a) for a in args]},
        )
    if extra is not None and extra not in _EXTRA_VALUES:
        raise DecoratorUsageError(
            f"@validation_config(extra={extra!r}) is invalid; use 'ignore' or 'forbid'",
            detail={"decorator": "validation_config", "extra": repr(extra)},
        )
    if strict is not None and not isinstance(strict, bool):
        raise DecoratorUsageError(
            f"@validation_config(strict={strict!r}) is invalid; use True or False",
            detail={"decorator": "validation_config", "strict": repr(strict)},
        )
    if rename is not None and rename not in _RENAME_VALUES:
        raise DecoratorUsageError(
            f"@validation_config(rename={rename!r}) is invalid; use 'camel'",
            detail={"decorator": "validation_config", "rename": repr(rename)},
        )
    config = ValidationConfig(extra=extra, strict=strict, rename=rename)

    def decorate(cls: Any) -> Any:
        if not isinstance(cls, type):
            raise DecoratorUsageError(
                "@validation_config decorates a class (a dataclass, TypedDict or NamedTuple), "
                f"got {type(cls).__name__}",
                detail={"decorator": "validation_config", "target": repr(cls)},
            )
        existing = cls.__dict__.get(VALIDATION_META)
        merged = existing.merged(config) if isinstance(existing, ValidationConfig) else config
        setattr(cls, VALIDATION_META, merged)
        return cls

    return decorate


def own_validation_config(cls: Any) -> ValidationConfig | None:
    """The ``@validation_config`` settings declared on ``cls`` itself (never inherited)."""
    meta = getattr(cls, "__dict__", {}).get(VALIDATION_META)
    return meta if isinstance(meta, ValidationConfig) else None


def to_camel(name: str) -> str:
    """``first_name`` → ``firstName``; leading underscores are kept."""
    stripped = name.lstrip("_")
    lead = name[: len(name) - len(stripped)]
    parts = [p for p in stripped.split("_") if p]
    if not parts:
        return name
    return lead + parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:])


_STDLIB_MODES = ("legacy", "warn", "enforce")
_JSON_MODES = ("lax", "strict")


@dataclass(frozen=True, slots=True)
class ValidationPolicy:
    """App-wide validation settings, passed as ``LaurenFactory.create(validation=...)``.

    ``stdlib_models`` picks how dataclass / TypedDict / NamedTuple bodies, query
    and form models and the stricter scalar rules (boolean tokens, empty
    optional values, typed scalars such as ``UUID``) behave:

    * ``"legacy"`` — the shallow pre-engine behaviour (escape hatch);
    * ``"warn"`` (the 1.x default) — the native validator runs and logs one
      warning per route and issue type for requests it would reject, but the
      request continues with the legacy value, so responses do not change;
    * ``"enforce"`` (the 2.0 default) — native results are used and failures
      answer 422 with ``detail.issues``.

    Pydantic models and ``msgspec.Struct`` types are validated by their own
    library in every mode, and a type that uses this module's markers or
    ``@validation_config`` is always enforced. ``json_mode`` sets coercion for
    JSON bodies, WebSocket frames and stream items (``"lax"`` accepts ``"12"``
    for an ``int``, like pydantic; ``"strict"`` does not). ``extra`` is the
    default for unknown keys in stdlib models. ``include_input`` echoes the
    rejected ``input`` in issues (``None``: only when ``debug`` is on).
    ``max_issues`` caps the issues reported for one request.
    """

    stdlib_models: Literal["legacy", "warn", "enforce"] = "warn"
    json_mode: Literal["lax", "strict"] = "lax"
    extra: Literal["ignore", "forbid"] = "ignore"
    include_input: bool | None = None
    max_issues: int = 50

    def __post_init__(self) -> None:
        problems: list[tuple[str, Any, str]] = []
        if self.stdlib_models not in _STDLIB_MODES:
            problems.append(("stdlib_models", self.stdlib_models, "'legacy', 'warn' or 'enforce'"))
        if self.json_mode not in _JSON_MODES:
            problems.append(("json_mode", self.json_mode, "'lax' or 'strict'"))
        if self.extra not in _EXTRA_VALUES:
            problems.append(("extra", self.extra, "'ignore' or 'forbid'"))
        if self.include_input is not None and not isinstance(self.include_input, bool):
            problems.append(("include_input", self.include_input, "True, False or None"))
        if isinstance(self.max_issues, bool) or not isinstance(self.max_issues, int) or self.max_issues < 1:
            problems.append(("max_issues", self.max_issues, "a positive int"))
        if problems:
            field, value, expected = problems[0]
            raise StartupError(
                f"ValidationPolicy({field}={value!r}) is invalid; use {expected}",
                detail={"reason": "invalid_validation_policy", "field": field, "got": repr(value)},
            )


def check_pattern(pattern: Any) -> re.Pattern[str]:
    """Compile a ``Pattern`` value, raising :class:`re.error` / :class:`TypeError` when invalid."""
    if isinstance(pattern, re.Pattern):
        return pattern
    if not isinstance(pattern, str):
        raise TypeError(f"pattern must be a string, got {type(pattern).__name__}")
    return re.compile(pattern)


__all__ = [
    "Alias",
    "Ge",
    "Gt",
    "Le",
    "Lt",
    "MaxLen",
    "MinLen",
    "MultipleOf",
    "Pattern",
    "Strict",
    "VALIDATION_META",
    "ValidationConfig",
    "ValidationPolicy",
    "check_pattern",
    "own_validation_config",
    "to_camel",
    "validation_config",
]
