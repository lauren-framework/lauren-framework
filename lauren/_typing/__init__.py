"""Internal typing utilities — private to :mod:`lauren`.

This sub-package concentrates every piece of type-hint introspection the
framework performs at startup. It exists so that every other module of
lauren speaks a single, well-tested dialect of
``typing.get_type_hints`` — one that can actually deal with

* PEP 563 / ``from __future__ import annotations`` stringified hints,
* :class:`typing.ForwardRef` instances (e.g. ``"UserRepo"``) that refer to
  names defined in a different module, a function body, a class body, or
  in ``if TYPE_CHECKING`` blocks,
* recursive / self-referential annotations (``"Node"`` inside
  ``class Node``),
* names living behind ``TYPE_CHECKING`` imports that were already
  imported eagerly at runtime by other lauren modules.

The public surface is intentionally tiny — only
:func:`resolve_type_hints` and :func:`resolve_forwardref` are re-exported.
See :mod:`lauren._typing.forwardref` for the heavy lifting.
"""

from __future__ import annotations

from .forwardref import (
    ForwardRefError,
    ResolutionStrategy,
    resolve_forwardref,
    resolve_type_hints,
)

__all__ = [
    "ForwardRefError",
    "ResolutionStrategy",
    "resolve_forwardref",
    "resolve_type_hints",
]
