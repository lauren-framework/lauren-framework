"""Provider-agnostic type detection and validation helpers.

All third-party imports are deferred to function bodies so that neither
pydantic nor msgspec is required at module import time.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Union, get_args, get_origin, get_type_hints

__all__ = [
    "is_pydantic_model",
    "is_msgspec_struct",
    "is_dataclass",
    "is_typeddict",
    "is_json_body_type",
    "validate_as",
    "json_schema_for",
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _peel_to_inner(tp: Any) -> Any:
    """Strip Optional[T] / Annotated[T, ...] wrappers, returning the inner type."""
    import types as _types
    import typing

    origin = get_origin(tp)

    # Annotated[T, ...] -> T
    if origin is typing.Annotated:
        return _peel_to_inner(get_args(tp)[0])

    # Optional[T] == Union[T, None] (typing.Union or PEP 604 types.UnionType)
    _union_types = {Union}
    _union_type = getattr(_types, "UnionType", None)
    if _union_type is not None:
        _union_types.add(_union_type)
    if origin in _union_types:
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return _peel_to_inner(args[0])

    return tp


def _raise_extractor_error(field: str, errors: list[Any]) -> None:
    from .exceptions import ExtractorError

    raise ExtractorError(
        f"Validation failed for field '{field}'",
        detail={"field": field, "errors": [str(e) for e in errors]},
    )


# ---------------------------------------------------------------------------
# Type detection
# ---------------------------------------------------------------------------


def is_pydantic_model(tp: Any) -> bool:
    """Return True if *tp* is a Pydantic v2 BaseModel subclass (no import needed at call site)."""
    tp = _peel_to_inner(tp)
    try:
        import pydantic

        return isinstance(tp, type) and issubclass(tp, pydantic.BaseModel)
    except ImportError:
        return False


def is_msgspec_struct(tp: Any) -> bool:
    """Return True if *tp* is a msgspec Struct subclass, detected via attribute probe."""
    tp = _peel_to_inner(tp)
    return isinstance(tp, type) and hasattr(tp, "__struct_fields__") and hasattr(tp, "__struct_config__")


def is_dataclass(tp: Any) -> bool:
    """Return True if *tp* is a dataclass (not an instance of one)."""
    tp = _peel_to_inner(tp)
    return isinstance(tp, type) and dataclasses.is_dataclass(tp)


def is_typeddict(tp: Any) -> bool:
    """Return True if *tp* is a TypedDict class."""
    tp = _peel_to_inner(tp)
    return (
        isinstance(tp, type)
        and issubclass(tp, dict)
        and hasattr(tp, "__required_keys__")
        and hasattr(tp, "__optional_keys__")
    )


def is_json_body_type(tp: Any) -> bool:
    """Return True if *tp* is any supported structured-body type."""
    return is_pydantic_model(tp) or is_msgspec_struct(tp) or is_dataclass(tp) or is_typeddict(tp)


# ---------------------------------------------------------------------------
# Validation backends
# ---------------------------------------------------------------------------


def _validate_pydantic(tp: Any, data: dict[str, Any], field: str) -> Any:
    try:
        return tp.model_validate(data)
    except Exception as exc:
        try:
            errors = exc.errors()  # type: ignore[attr-defined]
        except AttributeError:
            errors = [str(exc)]
        _raise_extractor_error(field, errors)


def _validate_msgspec(tp: Any, data: dict[str, Any], field: str) -> Any:
    import msgspec
    import msgspec.json

    try:
        import json

        raw = json.dumps(data).encode()
        return msgspec.json.decode(raw, type=tp)
    except msgspec.ValidationError as exc:
        _raise_extractor_error(field, [str(exc)])


def _validate_dataclass(tp: Any, data: dict[str, Any], field: str) -> Any:
    try:
        fields = {f.name for f in dataclasses.fields(tp)}
        filtered = {k: v for k, v in data.items() if k in fields}
        return tp(**filtered)
    except (TypeError, ValueError) as exc:
        _raise_extractor_error(field, [str(exc)])


def _validate_typeddict(tp: Any, data: dict[str, Any], field: str) -> Any:
    required: frozenset[str] = getattr(tp, "__required_keys__", frozenset())
    missing = required - data.keys()
    if missing:
        _raise_extractor_error(field, [f"Missing required keys: {sorted(missing)}"])
    return data


def validate_as(tp: Any, data: dict[str, Any], *, field: str = "body") -> Any:
    """Validate *data* as *tp*, raising :class:`~lauren.exceptions.ExtractorError` on failure."""
    tp = _peel_to_inner(tp)
    if is_pydantic_model(tp):
        return _validate_pydantic(tp, data, field)
    if is_msgspec_struct(tp):
        return _validate_msgspec(tp, data, field)
    if is_dataclass(tp):
        return _validate_dataclass(tp, data, field)
    if is_typeddict(tp):
        return _validate_typeddict(tp, data, field)
    raise TypeError(f"Unsupported type for validation: {tp!r}")


# ---------------------------------------------------------------------------
# JSON schema generation
# ---------------------------------------------------------------------------


def _annotation_to_json_schema(annotation: Any) -> dict[str, Any]:  # noqa: PLR0911
    """Recursively convert a type annotation to a JSON Schema fragment."""
    import typing

    if annotation is type(None):
        return {"type": "null"}

    # Annotated[T, ...] — strip metadata
    if get_origin(annotation) is typing.Annotated:
        return _annotation_to_json_schema(get_args(annotation)[0])

    # Optional[T] / Union[T, None]
    origin = get_origin(annotation)
    if origin is Union:
        args = get_args(annotation)
        schemas = [_annotation_to_json_schema(a) for a in args]
        if len(schemas) == 2 and {"type": "null"} in schemas:
            non_null = [s for s in schemas if s != {"type": "null"}][0]
            non_null = dict(non_null)
            non_null["nullable"] = True
            return non_null
        return {"anyOf": schemas}

    # Literal[...]
    try:
        if origin is typing.Literal:
            return {"enum": list(get_args(annotation))}
    except AttributeError:
        pass

    # list / List[T]
    if origin in (list, typing.List) or annotation is list:
        item_args = get_args(annotation)
        schema: dict[str, Any] = {"type": "array"}
        if item_args:
            schema["items"] = _annotation_to_json_schema(item_args[0])
        return schema

    # dict / Dict[K, V]
    if origin in (dict, typing.Dict) or annotation is dict:
        return {"type": "object"}

    # Scalars
    _scalar_map = {
        str: "string",
        int: "integer",
        float: "number",
        bool: "boolean",
        bytes: "string",
    }
    if annotation in _scalar_map:
        return {"type": _scalar_map[annotation]}

    # Nested structured type
    if is_json_body_type(annotation):
        return json_schema_for(annotation)

    return {}


def _schema_from_dataclass(tp: Any) -> dict[str, Any]:
    hints = get_type_hints(tp)
    required: list[str] = []
    properties: dict[str, Any] = {}
    for f in dataclasses.fields(tp):
        properties[f.name] = _annotation_to_json_schema(hints.get(f.name, Any))
        if f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING:  # type: ignore[misc]
            required.append(f.name)
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return schema


def _schema_from_typeddict(tp: Any) -> dict[str, Any]:
    hints = get_type_hints(tp)
    required_keys: frozenset[str] = getattr(tp, "__required_keys__", frozenset())
    properties = {k: _annotation_to_json_schema(v) for k, v in hints.items()}
    schema: dict[str, Any] = {"type": "object", "properties": properties}
    if required_keys:
        schema["required"] = sorted(required_keys)
    return schema


def json_schema_for(tp: Any) -> dict[str, Any]:
    """Generate a JSON Schema dict for *tp*.  Raises :exc:`TypeError` for unsupported types."""
    tp = _peel_to_inner(tp)
    if is_pydantic_model(tp):
        return tp.model_json_schema()
    if is_msgspec_struct(tp):
        import msgspec.json

        return msgspec.json.schema(tp)
    if is_dataclass(tp):
        return _schema_from_dataclass(tp)
    if is_typeddict(tp):
        return _schema_from_typeddict(tp)
    raise TypeError(f"Cannot generate JSON schema for: {tp!r}")
