"""Route-name syntax, checked when ``name=`` is given to a decorator (private)."""

from __future__ import annotations

import re
from typing import Any

from ..exceptions import DecoratorUsageError

_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(?:\.[A-Za-z_][A-Za-z0-9_-]*)*")

#: ``detail["reason"]`` of a malformed ``name=``.
INVALID_NAME_REASON = "invalid_route_name"


def name_problem(value: Any) -> str | None:
    """Why ``value`` is not a valid route / controller / module name, or ``None``."""
    if not isinstance(value, str):
        return f"name must be a str, got {type(value).__name__}"
    if not _NAME_RE.fullmatch(value):
        return (
            f"name {value!r} must be dot-separated identifiers "
            "([A-Za-z_][A-Za-z0-9_-]*, e.g. 'users' or 'users.show')"
        )
    return None


def check_name(value: Any, *, decorator: str, target: Any) -> str | None:
    """Return ``value`` (``None`` = not named) or raise :class:`DecoratorUsageError`."""
    if value is None:
        return None
    problem = name_problem(value)
    if problem is None:
        return str(value)
    target_name = getattr(target, "__qualname__", None) or repr(target)
    raise DecoratorUsageError(
        f"@{decorator}(..., name={value!r}) on {target_name}: {problem}",
        detail={
            "decorator": decorator,
            "target": target_name,
            "option": "name",
            "reason": INVALID_NAME_REASON,
        },
    )


__all__ = ["INVALID_NAME_REASON", "check_name", "name_problem"]
