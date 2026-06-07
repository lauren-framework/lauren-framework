"""Pydantic-free discriminated union support.

Public API (re-exported from lauren.types):
    Discriminated[Union[A, B], "kind"]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

__all__ = [
    "_DiscriminatorMarker",
    "is_native_discriminated_union",
    "get_discriminator_marker",
    "validate_native_discriminated",
    "openapi_schema_for_discriminated",
]


# ---------------------------------------------------------------------------
# Marker — embedded in Annotated metadata
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DiscriminatorMarker:
    """Embedded in Annotated[Union[...], _DiscriminatorMarker(...)] by Discriminated."""

    key: str
    mapping: dict[str, type]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def is_native_discriminated_union(target: Any) -> bool:
    """Return True iff *target* was created by ``Discriminated[T, key]``."""
    return get_discriminator_marker(target) is not None


def get_discriminator_marker(target: Any) -> _DiscriminatorMarker | None:
    """Extract the _DiscriminatorMarker from an Annotated type, or return None."""
    if get_origin(target) is not Annotated:
        return None
    for meta in get_args(target)[1:]:
        if isinstance(meta, _DiscriminatorMarker):
            return meta
    return None


# ---------------------------------------------------------------------------
# Validation dispatcher
# ---------------------------------------------------------------------------


def validate_native_discriminated(
    data: object,
    target: Any,
    field_name: str,
) -> object:
    """Validate *data* against a ``Discriminated[Union[A, B], 'key']`` type.

    Raises ExtractorError for missing discriminator key, unknown tag value,
    or variant validation failure.
    """
    from .exceptions import ExtractorError  # noqa: PLC0415
    from ._validation import validate_as  # noqa: PLC0415

    marker = get_discriminator_marker(target)
    if marker is None:
        raise ExtractorError(
            "discriminated union error",
            detail={"field": field_name, "errors": ["not a Discriminated type"]},
        )

    if not isinstance(data, dict):
        raise ExtractorError(
            "discriminated union error",
            detail={
                "field": field_name,
                "errors": [f"expected a JSON object with '{marker.key}' field, got {type(data).__name__}"],
            },
        )

    tag_value = data.get(marker.key)
    if tag_value is None:
        raise ExtractorError(
            "discriminated union error",
            detail={
                "field": field_name,
                "errors": [
                    f"missing discriminator field '{marker.key}'. "
                    f"Expected one of: {sorted(marker.mapping.keys())}"
                ],
            },
        )

    variant = marker.mapping.get(str(tag_value))
    if variant is None:
        raise ExtractorError(
            "discriminated union error",
            detail={
                "field": field_name,
                "errors": [
                    f"unknown discriminator value '{tag_value}' for field '{marker.key}'. "
                    f"Expected one of: {sorted(marker.mapping.keys())}"
                ],
            },
        )

    return validate_as(variant, data, field=field_name)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# OpenAPI schema builder
# ---------------------------------------------------------------------------


def openapi_schema_for_discriminated(target: Any, components: dict[str, Any]) -> dict[str, Any]:
    """Build a JSON Schema oneOf + discriminator block for a Discriminated type.

    Mutates *components* to add each variant's schema under its class name
    and returns the oneOf reference block.
    """
    from ._validation import json_schema_for  # noqa: PLC0415

    marker = get_discriminator_marker(target)
    if marker is None:
        return {}

    one_of: list[dict[str, Any]] = []
    discriminator_mapping: dict[str, str] = {}

    for tag_value, variant in marker.mapping.items():
        schema_name = variant.__name__
        if schema_name not in components:
            raw = json_schema_for(variant)
            if "$defs" in raw:
                for def_name, def_schema in raw.pop("$defs").items():
                    components.setdefault(def_name, def_schema)
            components[schema_name] = raw
        ref = f"#/components/schemas/{schema_name}"
        one_of.append({"$ref": ref})
        discriminator_mapping[tag_value] = ref

    return {
        "oneOf": one_of,
        "discriminator": {
            "propertyName": marker.key,
            "mapping": discriminator_mapping,
        },
    }
