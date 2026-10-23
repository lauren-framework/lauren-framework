"""The OpenAPI contribution of pagination — one function, kept separate for F06 (private).

:func:`contribute_openapi` documents a route's pagination query parameters
(names, ``minimum`` / ``maximum`` / ``default`` from the effective config, the
opaque ``cursor``) and, when the response type is ``Page[T]`` /
``CursorPage[T]``, the envelope-specific body schema as a named component
(``UserOutPage``, ``UserOutNumberedPage``, ``EventOutCursorPage``) plus the
``Link`` / ``X-Total-Count`` response headers. F06's generic-schema naming can
take over the component naming without touching the callers.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, get_args, get_origin

from ._config import ResolvedPagination
from ._page import LINK_RELATIONS, CursorPage, Page, schema_name

_INT: dict[str, Any] = {"type": "integer"}
_NULLABLE_INT: dict[str, Any] = {"type": ["integer", "null"]}
_NULLABLE_STR: dict[str, Any] = {"type": ["string", "null"]}
_BOOL: dict[str, Any] = {"type": "boolean"}

_CAMEL: dict[str, str] = {
    "has_next": "hasNext",
    "per_page": "perPage",
    "total_pages": "totalPages",
    "next_cursor": "nextCursor",
    "prev_cursor": "prevCursor",
}


def page_type(annotation: Any) -> tuple[type, Any] | None:
    """``Page[T]`` → ``(Page, T)``; ``CursorPage`` → ``(CursorPage, None)``; else ``None``."""
    origin = get_origin(annotation)
    if origin in (Page, CursorPage):
        args = get_args(annotation)
        return origin, args[0] if args else None
    if annotation in (Page, CursorPage):
        return annotation, None
    return None


def parameters(plan: Any) -> list[dict[str, Any]]:
    """The query parameters a pagination plan reads, with their constraints."""
    cfg: ResolvedPagination = plan.config
    limit: dict[str, Any] = {
        "type": "integer",
        "minimum": 1,
        "maximum": cfg.max_limit,
        "default": cfg.default_limit,
    }
    if plan.kind == "page":
        return [
            _query(cfg.page_param, {"type": "integer", "minimum": 1, "default": 1}, "1-based page number."),
            _query(cfg.per_page_param, limit, "Items per page."),
        ]
    if plan.kind == "cursor":
        return [
            _query(
                cfg.cursor_param,
                {"type": "string"},
                "Opaque cursor from a previous page (next_cursor / prev_cursor); omit it for the first page.",
            ),
            _query(cfg.limit_param, limit, "Maximum number of items to return."),
        ]
    offset: dict[str, Any] = {"type": "integer", "minimum": 0, "default": 0}
    if cfg.max_offset is not None:
        offset["maximum"] = cfg.max_offset
    return [
        _query(cfg.limit_param, limit, "Maximum number of items to return."),
        _query(cfg.offset_param, offset, "Number of items to skip."),
    ]


def _query(name: str, schema: dict[str, Any], description: str) -> dict[str, Any]:
    return {"name": name, "in": "query", "required": False, "schema": schema, "description": description}


def _meta_schema(kind: str, style: str) -> dict[str, Any]:
    if kind == "cursor":
        fields: list[tuple[str, dict[str, Any]]] = [
            ("limit", _INT),
            ("has_next", _BOOL),
            ("next_cursor", _NULLABLE_STR),
            ("prev_cursor", _NULLABLE_STR),
        ]
    elif kind == "page":
        fields = [
            ("page", _INT),
            ("per_page", _INT),
            ("total", _NULLABLE_INT),
            ("total_pages", _NULLABLE_INT),
            ("has_next", _BOOL),
        ]
    else:
        fields = [("limit", _INT), ("offset", _INT), ("total", _NULLABLE_INT), ("has_next", _BOOL)]
    props = {(_CAMEL.get(n, n) if style == "camel" else n): dict(s) for n, s in fields}
    return {"type": "object", "properties": props, "required": list(props)}


def page_schema(
    cls: type,
    item_type: Any,
    kind: str,
    cfg: ResolvedPagination,
    components: dict[str, Any],
    item_schema: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """The response body schema of ``cls[item_type]`` under ``cfg``'s envelope."""
    items = {"type": "array", "items": item_schema(item_type, components) if item_type is not None else {}}
    if cfg.envelope == "bare":
        return items
    if kind == "page":
        name = schema_name(item_type, "NumberedPage")
    else:
        name = getattr(cls, "__lauren_schema_name__")((item_type,) if item_type is not None else ())
    # The envelope and meta keys are app-wide, so one name is always one shape:
    # (re)writing it is idempotent and replaces any generic dataclass schema
    # registered for a bare ``-> Page`` annotation.
    key = "data" if cfg.envelope == "data" else "items"
    props: dict[str, Any] = {key: items, "meta": _meta_schema(kind, cfg.meta_keys)}
    if cfg.envelope == "data":
        props["links"] = {
            "type": "object",
            "properties": {rel: dict(_NULLABLE_STR) for rel in LINK_RELATIONS},
            "required": list(LINK_RELATIONS),
        }
    components["schemas"][name] = {
        "type": "object",
        "title": name,
        "properties": props,
        "required": list(props),
    }
    return {"$ref": f"#/components/schemas/{name}"}


def contribute_openapi(
    op: dict[str, Any],
    *,
    extractions: Iterable[Any],
    response_type: Any,
    components: dict[str, Any],
    app_config: ResolvedPagination,
    item_schema: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> None:
    """Add a route's pagination parameters and page response schema to ``op``."""
    plan = next((ext.compiled for ext in extractions if ext.source == "pagination"), None)
    if plan is not None:
        params = op.setdefault("parameters", [])
        seen = {(p.get("in"), p.get("name")) for p in params}
        for param in parameters(plan):
            if ("query", param["name"]) not in seen:
                params.append(param)
    detected = page_type(response_type)
    if detected is None:
        return
    cls, item_type = detected
    cfg = plan.config if plan is not None else app_config
    if cls is CursorPage:
        kind = "cursor"
    else:
        kind = plan.kind if plan is not None and plan.kind in ("offset", "page") else "offset"
    schema = page_schema(cls, item_type, kind, cfg, components, item_schema)
    ok = op.setdefault("responses", {}).setdefault("200", {"description": "Success"})
    ok.setdefault("content", {})["application/json"] = {"schema": schema}
    headers = ok.setdefault("headers", {})
    headers["Link"] = {
        "description": 'RFC 8288 navigation links (rel="first", "prev", "next", "last").',
        "schema": {"type": "string"},
    }
    if cfg.total_header and kind != "cursor":
        headers["X-Total-Count"] = {
            "description": "Total number of items, when the handler knows it.",
            "schema": {"type": "integer"},
        }
