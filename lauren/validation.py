"""Native validation: constraint markers, issues, policy and the type compiler.

Lauren validates stdlib types itself — dataclasses (nested, defaults,
``__post_init__``), ``TypedDict``, ``NamedTuple``, enums, literals, numbers,
dates, UUIDs, decimals, containers, ``X | None`` and unions — so a pydantic-free
app gets real type checks. Pydantic models and ``msgspec.Struct`` types keep
their own validators. Every failure becomes a :class:`ValidationIssue`
(``{loc, type, msg}``), the single error format of HTTP 422 responses,
WebSocket error frames and stream errors.

Constraints ride in ``Annotated`` metadata::

    from dataclasses import dataclass, field
    from typing import Annotated

    from lauren.validation import Ge, Le, MaxLen, Pattern, validation_config

    @validation_config(extra="forbid")
    @dataclass
    class CreateUser:
        email: Annotated[str, Pattern(r"^[^@\\s]+@[^@\\s]+$"), MaxLen(254)]
        age: Annotated[int, Ge(13), Le(130)]
        tags: Annotated[list[str], MaxLen(10)] = field(default_factory=list)

App-wide behaviour is set with ``LaurenFactory.create(validation=ValidationPolicy(...))``.
Outside a request, compile a type once and validate with it::

    from lauren.validation import TypeCompiler

    create_user = TypeCompiler().compile(CreateUser)
    user = create_user.validate({"email": "a@b.co", "age": "30"})
"""

from __future__ import annotations

from typing import Any

from ._schema._compiler import CompiledType, TypeCompiler
from ._schema._issues import MISSING, ValidationFailed, ValidationIssue
from ._schema._markers import (
    Alias,
    Ge,
    Gt,
    Le,
    Lt,
    MaxLen,
    MinLen,
    MultipleOf,
    Pattern,
    Strict,
    ValidationPolicy,
    validation_config,
)


def validate(tp: Any, value: Any, *, mode: str = "lax", policy: ValidationPolicy | None = None) -> Any:
    """Validate ``value`` against ``tp`` and return the result (``ValidationFailed`` on failure).

    A convenience that compiles ``tp`` on every call; hold a
    :class:`TypeCompiler` (or use ``app.type_compiler``) for repeated use.
    ``mode`` is ``"lax"`` (``"12"`` → ``12``, like pydantic) or ``"strict"``.
    """
    return TypeCompiler(policy).compile(tp, mode=mode).validate(value)


__all__ = [
    "Alias",
    "CompiledType",
    "Ge",
    "Gt",
    "Le",
    "Lt",
    "MISSING",
    "MaxLen",
    "MinLen",
    "MultipleOf",
    "Pattern",
    "Strict",
    "TypeCompiler",
    "ValidationFailed",
    "ValidationIssue",
    "ValidationPolicy",
    "validate",
    "validation_config",
]
