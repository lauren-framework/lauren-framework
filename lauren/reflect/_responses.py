"""Turn a route's response table and ``@raises`` declarations into reflect entries (private)."""

from __future__ import annotations

import inspect
from typing import Any

from ._types import ReflectedResponse

_RAISES_META = "__lauren_raises__"


def route_responses(
    table: Any, raises: Any, manual: dict[int, Any], *, status_code: int | None
) -> tuple[int, tuple[ReflectedResponse, ...], tuple[type, ...]]:
    """``(status_code, responses_typed, raises)`` for a :class:`ReflectedRoute`."""
    from .._asgi._contracts import NO_BODY, OPAQUE, UNTYPED  # noqa: PLC0415
    from ..types import Response  # noqa: PLC0415

    items: list[ReflectedResponse] = []
    effective = status_code or 200
    if table is not None:
        # The primary success: plain values' status, else the first declared
        # response (``-> None`` → 204, ``-> Created[T]`` → 201).
        primary = table.bare or (table.members[0] if table.members else None)
        effective = primary.status if primary is not None else table.default_status
        for member in table.members:
            body: Any = member.body
            if body is NO_BODY:
                body = None
            elif body is OPAQUE:
                body = Response
            elif body is UNTYPED:
                body = Any
            items.append(ReflectedResponse(member.status, body, member.description, "declared"))
    specs = tuple(raises or ())
    for spec in specs:
        items.append(ReflectedResponse(spec.status_code, spec.exc_type, spec.description, "raises"))
    for code, value in (manual or {}).items():
        description = value.get("description") if isinstance(value, dict) else str(value)
        items.append(ReflectedResponse(int(code), None, description, "manual"))
    return effective, tuple(items), tuple(spec.exc_type for spec in specs)


def static_route_responses(
    owner: type, member: Any, rmeta: Any
) -> tuple[int, tuple[ReflectedResponse, ...], tuple[type, ...]]:
    """Best-effort response info for a route read from its class (no app compiled).

    The return annotation is resolved here; a declaration the app build would
    reject (or one that cannot be resolved) reports only ``status_code=``.
    """
    from .._asgi._contracts import describe_responses  # noqa: PLC0415
    from .._typing import resolve_type_hints  # noqa: PLC0415
    from ..exceptions import LaurenError  # noqa: PLC0415
    from ..streaming import extract_streaming_item_type  # noqa: PLC0415

    fn = getattr(member, "__func__", member)
    table = None
    try:
        hint = resolve_type_hints(fn, include_extras=True).get("return", inspect.Parameter.empty)
        table = describe_responses(rmeta, hint, fn=fn, streaming_item=extract_streaming_item_type(hint))
    except (LaurenError, NameError, TypeError, AttributeError):
        table = None
    specs: list[Any] = []
    for spec in [*getattr(fn, _RAISES_META, []), *owner.__dict__.get(_RAISES_META, [])]:
        if all(spec.exc_type is not known.exc_type for known in specs):
            specs.append(spec)
    return route_responses(table, specs, rmeta.responses, status_code=rmeta.status_code)
