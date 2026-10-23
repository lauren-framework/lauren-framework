"""The error catalog as data — :func:`error_catalog` (startup / tooling only)."""

from __future__ import annotations

import inspect

from ..exceptions import HTTPError, LaurenError, LifecycleError, StartupError
from ._types import ReflectedError

__all__ = ["error_catalog"]


def _is_framework(cls: type) -> bool:
    module = cls.__module__
    return (module == "lauren" or module.startswith("lauren.")) and not cls.__name__.startswith("_")


def _category(cls: type) -> str:
    from ..socketio import SocketIOError  # noqa: PLC0415 — avoid an import cycle at package load
    from ..websockets import WebSocketError  # noqa: PLC0415

    if issubclass(cls, HTTPError):
        return "http"
    if issubclass(cls, StartupError):
        return "startup"
    if issubclass(cls, LifecycleError):
        return "lifecycle"
    if issubclass(cls, (WebSocketError, SocketIOError)):
        return "websocket"
    return "other"


def _summary(cls: type) -> str:
    doc = inspect.cleandoc(cls.__dict__.get("__doc__") or "")
    return doc.split("\n\n", 1)[0].replace("\n", " ").strip()


def error_catalog(*, include_user: bool = True) -> tuple[ReflectedError, ...]:
    """Every :class:`~lauren.exceptions.LaurenError` class, as :class:`ReflectedError` entries.

    Walks ``LaurenError`` and its subclasses recursively **at call time**
    (depth-first, in definition order), so application errors defined so far
    are included; ``include_user=False`` keeps only the public classes defined
    by lauren itself. Meant for documentation, OpenAPI generation, tooling and
    tests — never call it on the request path.
    """
    entries: list[ReflectedError] = []
    seen: set[type] = set()

    def visit(cls: type) -> None:
        if cls in seen:
            return
        seen.add(cls)
        if include_user or _is_framework(cls):
            status = getattr(cls, "status_code", None)
            entries.append(
                ReflectedError(
                    cls=cls,
                    code=str(getattr(cls, "code", "")),
                    status_code=status if issubclass(cls, HTTPError) else None,
                    category=_category(cls),  # type: ignore[arg-type]
                    problem_type=getattr(cls, "problem_type", None),
                    problem_title=getattr(cls, "problem_title", None),
                    summary=_summary(cls),
                )
            )
        subclasses: list[type] = cls.__subclasses__()
        for sub in subclasses:
            visit(sub)

    visit(LaurenError)
    return tuple(entries)
