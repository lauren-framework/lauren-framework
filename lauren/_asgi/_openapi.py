"""OpenAPI 3.1 generator.

Walks :class:`CompiledHandler` extractions to produce rich, faithful
documentation: path / query / header / cookie parameters with their Python
types, request bodies from ``Json[Model]``, response bodies from
``response_model``, and constraints lifted from :class:`FieldDescriptor`
(``ge`` / ``le`` / ``min_length`` / ``max_length`` / ``pattern`` / ``example``
/ ``description`` / ``alias``).

The generator also surfaces a top-level ``tags`` array (deduplicated across
controllers and routes) and supports caller-supplied ``info`` / ``servers`` /
``security_schemes`` overrides.
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING, get_args, get_origin

from ..decorators import OPENAPI_SECURITY_META, ControllerMeta, RouteMeta
from ..extractors import FieldDescriptor, _Extraction
from ..streaming import (
    FORMAT_TO_MEDIA_TYPE,
    discriminator_key,
    discriminator_variants,
    is_discriminated_union,
)

if TYPE_CHECKING:
    from . import LaurenApp

try:
    import pydantic

    _PYDANTIC = True
    _BaseModel = pydantic.BaseModel
except ImportError:  # pragma: no cover
    _PYDANTIC = False
    _BaseModel = None  # type: ignore[assignment,misc]


DEFAULT_INFO: dict[str, Any] = {"title": "lauren application", "version": "1.0.0"}


# ---------------------------------------------------------------------------
# Schema translation
# ---------------------------------------------------------------------------


def _python_type_to_schema(tp: Any) -> dict[str, Any]:
    """Translate a Python type hint into a JSON Schema snippet.

    Handles primitives, ``list[T]``, ``dict[str, T]``, ``Optional[T]``, enums
    (via their value type), and Pydantic models (the caller is responsible
    for registering the model in ``components`` and returning a ``$ref``).
    """
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is str or tp is None or tp is type(None):
        return {"type": "string"}
    if tp is bytes:
        return {"type": "string", "format": "binary"}
    origin = get_origin(tp)
    if origin in (list, tuple):
        args = get_args(tp)
        item_type = args[0] if args else str
        return {"type": "array", "items": _python_type_to_schema(item_type)}
    if origin is dict:
        args = get_args(tp)
        value_t = args[1] if len(args) == 2 else str
        return {
            "type": "object",
            "additionalProperties": _python_type_to_schema(value_t),
        }
    # Optional[T] / T | None / Union[...]
    import types as _types

    if origin is getattr(_types, "UnionType", None) or origin is type(int | str):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return _python_type_to_schema(args[0])
        return {"oneOf": [_python_type_to_schema(a) for a in args]}
    try:
        import enum

        if isinstance(tp, type) and issubclass(tp, enum.Enum):
            values = [e.value for e in tp]
            base = type(values[0]) if values else str
            schema = _python_type_to_schema(base)
            schema["enum"] = values
            return schema
    except Exception:
        pass
    return {}  # unknown -> permissive


def _apply_field_descriptor(
    schema: dict[str, Any], fd: FieldDescriptor
) -> dict[str, Any]:
    """Annotate ``schema`` with constraints from ``fd``."""
    if fd.ge is not None:
        schema["minimum"] = fd.ge
    if fd.le is not None:
        schema["maximum"] = fd.le
    if fd.gt is not None:
        schema["exclusiveMinimum"] = fd.gt
    if fd.lt is not None:
        schema["exclusiveMaximum"] = fd.lt
    if fd.min_length is not None:
        schema["minLength"] = fd.min_length
    if fd.max_length is not None:
        schema["maxLength"] = fd.max_length
    if fd.pattern is not None:
        schema["pattern"] = fd.pattern
    if fd.example is not None:
        schema["example"] = fd.example
    return schema


def _ensure_component(components: dict[str, Any], model: type) -> str:
    """Register a Pydantic model in ``components`` and return its ``$ref``."""
    name = model.__name__
    if name not in components["schemas"]:
        try:
            schema = model.model_json_schema(  # type: ignore[attr-defined]
                ref_template="#/components/schemas/{model}"
            )
        except Exception:
            schema = {"type": "object"}
        # Hoist Pydantic's ``$defs`` into top-level components so cross-model
        # references resolve.
        defs = schema.pop("$defs", None) or schema.pop("definitions", None)
        if defs:
            for dname, dschema in defs.items():
                components["schemas"].setdefault(dname, dschema)
        components["schemas"][name] = schema
    return f"#/components/schemas/{name}"


def _schema_for_discriminated_union(
    target: Any, components: dict[str, Any]
) -> dict[str, Any]:
    """Build a ``oneOf`` + ``discriminator`` schema for a tagged union.

    Emits the canonical OpenAPI 3.1 shape (feature 6)::

        {
          "oneOf": [{"$ref": "#/components/schemas/ImageEvent"}, ...],
          "discriminator": {
            "propertyName": "kind",
            "mapping": {"image": "#/components/schemas/ImageEvent", ...}
          }
        }

    The mapping keys are the ``Literal`` values observed on each variant's
    discriminator field; we read them out of the variant's JSON Schema
    (which Pydantic already populates with the enum constraint) to stay
    independent of how users type the discriminator.
    """
    key = discriminator_key(target)
    variants = discriminator_variants(target)
    one_of: list[dict[str, Any]] = []
    mapping: dict[str, str] = {}
    for variant in variants:
        ref = _ensure_component(components, variant)
        one_of.append({"$ref": ref})
        tag = _variant_tag_value(variant, key)
        if tag is not None:
            mapping[str(tag)] = ref
    schema: dict[str, Any] = {"oneOf": one_of}
    if key is not None:
        schema["discriminator"] = {"propertyName": key}
        if mapping:
            schema["discriminator"]["mapping"] = mapping
    return schema


def _variant_tag_value(variant: type, key: str | None) -> Any:
    """Read the ``Literal`` tag value declared on ``variant.<key>``.

    We consult the variant's JSON Schema so a ``Literal["image"]`` field
    surfaces as ``{"const": "image"}`` or ``{"enum": ["image"]}`` and we
    pick it up uniformly. Returning ``None`` means Pydantic didn't pin the
    field to a constant, in which case the discriminator mapping simply
    omits that variant — OpenAPI still validates it via ``oneOf``.
    """
    if key is None:
        return None
    try:
        schema = variant.model_json_schema()  # type: ignore[attr-defined]
    except Exception:
        return None
    props = schema.get("properties") or {}
    field_schema = props.get(key) or {}
    if "const" in field_schema:
        return field_schema["const"]
    enum = field_schema.get("enum")
    if isinstance(enum, list) and len(enum) == 1:
        return enum[0]
    return None


def _schema_for_extraction(
    ext: _Extraction, components: dict[str, Any]
) -> dict[str, Any]:
    """Build a JSON-Schema fragment describing an extraction's input shape.

    Pipes are intentionally not reflected in the schema: they transform the
    value server-side after it has crossed the wire, so documenting the
    original wire type is the correct client contract.
    """
    inner = ext.inner_type
    # Feature 6 — discriminated unions surface as oneOf + discriminator
    # rather than a bare ``$ref``.
    if _PYDANTIC and is_discriminated_union(inner):
        return _schema_for_discriminated_union(inner, components)
    if (
        _PYDANTIC
        and isinstance(inner, type)
        and _BaseModel is not None
        and issubclass(inner, _BaseModel)
    ):
        return {"$ref": _ensure_component(components, inner)}
    schema = _python_type_to_schema(inner)
    if ext.field_descriptor is not None:
        _apply_field_descriptor(schema, ext.field_descriptor)
    return schema


# ---------------------------------------------------------------------------
# Parameter & operation builders
# ---------------------------------------------------------------------------


_PARAM_LOCATIONS = {
    "path": "path",
    "query": "query",
    "header": "header",
    "cookie": "cookie",
}


def _build_parameter(
    ext: _Extraction, components: dict[str, Any]
) -> dict[str, Any] | None:
    loc = _PARAM_LOCATIONS.get(ext.source)
    if loc is None:
        return None
    fd = ext.field_descriptor
    name = fd.alias if fd and fd.alias else ext.name
    if loc == "header":
        # Header names are case-insensitive; OpenAPI convention uses kebab-case.
        name = name.replace("_", "-")
    schema = _schema_for_extraction(ext, components)
    param: dict[str, Any] = {
        "name": name,
        "in": loc,
        "required": loc == "path" or not ext.has_default,
        "schema": schema,
    }
    if fd and fd.description:
        param["description"] = fd.description
    if ext.has_default and ext.default is not ... and ext.default is not None:
        schema.setdefault("default", ext.default)
    return param


def _build_request_body(
    ext: _Extraction, components: dict[str, Any]
) -> dict[str, Any] | None:
    """Build a ``requestBody`` fragment from a body-reading extraction."""
    if ext.source == "json":
        return {
            "required": not ext.has_default,
            "content": {
                "application/json": {"schema": _schema_for_extraction(ext, components)}
            },
        }
    if ext.source == "form":
        return {
            "required": not ext.has_default,
            "content": {
                "application/x-www-form-urlencoded": {
                    "schema": _schema_for_extraction(ext, components)
                }
            },
        }
    if ext.source == "bytes":
        return {
            "required": not ext.has_default,
            "content": {
                "application/octet-stream": {
                    "schema": {"type": "string", "format": "binary"}
                }
            },
        }
    return None


def _build_responses(
    rmeta: RouteMeta,
    components: dict[str, Any],
    *,
    streaming_item_type: Any = None,
) -> dict[str, Any]:
    responses: dict[str, Any] = {}
    for code, v in (rmeta.responses or {}).items():
        responses[str(code)] = (
            dict(v) if isinstance(v, dict) else {"description": str(v)}
        )
    if not responses:
        responses["200"] = {"description": "Success"}
    if rmeta.response_model is not None and _PYDANTIC:
        # Feature 6 — response may be a discriminated union.
        if is_discriminated_union(rmeta.response_model):
            schema = _schema_for_discriminated_union(rmeta.response_model, components)
        else:
            ref = _ensure_component(components, rmeta.response_model)
            schema = {"$ref": ref}
        ok = responses.setdefault("200", {"description": "Success"})
        ok.setdefault("content", {})
        # Feature 7 — StreamingResponse[T] declares its wire negotiation
        # up front: clients can ask for any of the three media types.
        if streaming_item_type is not None:
            item_schema = _resolve_item_schema(streaming_item_type, components)
            for media in FORMAT_TO_MEDIA_TYPE.values():
                ok["content"][media] = {"schema": item_schema}
        else:
            ok["content"]["application/json"] = {"schema": schema}
    elif streaming_item_type is not None and _PYDANTIC:
        # Streaming-only route (no response_model declared) — still document
        # the three negotiable content types so clients know the contract.
        item_schema = _resolve_item_schema(streaming_item_type, components)
        ok = responses.setdefault("200", {"description": "Success"})
        ok.setdefault("content", {})
        for media in FORMAT_TO_MEDIA_TYPE.values():
            ok["content"][media] = {"schema": item_schema}
    return responses


def _resolve_item_schema(item_type: Any, components: dict[str, Any]) -> dict[str, Any]:
    """Schema fragment for a single streamed item (model / union / primitive)."""
    if _PYDANTIC and is_discriminated_union(item_type):
        return _schema_for_discriminated_union(item_type, components)
    if (
        _PYDANTIC
        and isinstance(item_type, type)
        and _BaseModel is not None
        and issubclass(item_type, _BaseModel)
    ):
        return {"$ref": _ensure_component(components, item_type)}
    return _python_type_to_schema(item_type)


# ---------------------------------------------------------------------------
# Guard-derived security
# ---------------------------------------------------------------------------


def _collect_guard_security(
    guards: tuple[type, ...],
) -> list[dict[str, list[str]]] | None:
    """Derive an OpenAPI ``security`` array from a compiled handler's guards.

    Resolution rules:

    * Guards without ``@openapi_security`` metadata are ignored.
    * A **single** decorated guard → its requirements are returned verbatim
      (preserving OR semantics: ``[{"BearerAuth": []}, {"ApiKey": []}]``).
    * **Multiple** decorated guards → their requirements are AND-merged into
      a single requirement object so that *all* schemes must be present:
      ``[{"BearerAuth": [], "TenantHeader": []}]``.
    * Returns ``None`` when no guard carries security metadata.
    """
    per_guard: list[list[dict[str, list[str]]]] = []
    for guard_cls in guards:
        meta = getattr(guard_cls, OPENAPI_SECURITY_META, None)
        if meta is None or not meta.requirements:
            continue
        per_guard.append(list(meta.requirements))

    if not per_guard:
        return None

    if len(per_guard) == 1:
        # Single guard — return its requirements verbatim (OR semantics).
        return [dict(r) for r in per_guard[0]]

    # Multiple guards — AND semantics: merge all into one requirement object.
    merged: dict[str, list[str]] = {}
    for reqs in per_guard:
        for req in reqs:
            merged.update(req)
    return [merged]


# ---------------------------------------------------------------------------
# Top-level generator
# ---------------------------------------------------------------------------


def generate_openapi(app: "LaurenApp") -> dict[str, Any]:
    info = getattr(app, "_openapi_info", None) or DEFAULT_INFO
    servers = getattr(app, "_openapi_servers", None)
    root_path = getattr(app, "_root_path", "")
    if not servers and root_path:
        servers = [{"url": root_path}]
    security_schemes = getattr(app, "_openapi_security_schemes", None)

    paths: dict[str, Any] = {}
    components: dict[str, Any] = {"schemas": {}}
    if security_schemes:
        components["securitySchemes"] = dict(security_schemes)
    tag_set: dict[str, dict[str, Any]] = {}
    
    app_global_guards = getattr(app, "_global_guards", ())

    for entry in app.router.routes():
        rmeta: RouteMeta = entry.metadata["route_meta"]
        ctrl_meta: ControllerMeta = entry.metadata["controller_meta"]
        if not rmeta.include_in_schema:
            continue
        compiled = app._handlers.get((entry.method, entry.path_template))  # type: ignore[attr-defined]
        path_item = paths.setdefault(entry.path_template, {})
        op: dict[str, Any] = {}
        if rmeta.summary or ctrl_meta.summary:
            op["summary"] = rmeta.summary or ctrl_meta.summary
        if rmeta.description or ctrl_meta.description:
            op["description"] = rmeta.description or ctrl_meta.description
        tags = list(rmeta.tags or ctrl_meta.tags)
        if tags:
            op["tags"] = tags
            for t in tags:
                tag_set.setdefault(t, {"name": t})
        if rmeta.deprecated or ctrl_meta.deprecated:
            op["deprecated"] = True
        if rmeta.operation_id:
            op["operationId"] = rmeta.operation_id

        params: list[dict[str, Any]] = []
        request_body: dict[str, Any] | None = None
        if compiled is not None:
            seen_param_keys: set[tuple[str, str]] = set()
            for ext in compiled.extractions:
                p = _build_parameter(ext, components)
                if p is not None:
                    key = (p["in"], p["name"])
                    if key in seen_param_keys:
                        continue
                    seen_param_keys.add(key)
                    params.append(p)
                    continue
                body = _build_request_body(ext, components)
                if body is not None and request_body is None:
                    request_body = body
        else:  # pragma: no cover - routes registered outside Phase 5
            for pname in entry.param_names:
                params.append(
                    {
                        "name": pname,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                )
        if params:
            op["parameters"] = params
        if request_body is not None:
            op["requestBody"] = request_body
        streaming_item = (
            getattr(compiled, "streaming_item_type", None) if compiled else None
        )
        op["responses"] = _build_responses(
            rmeta, components, streaming_item_type=streaming_item
        )
        if streaming_item is not None:
            # Feature 7 — vendor extension telling OpenAPI tooling this
            # operation yields a structured, typed stream.
            op["x-streaming"] = True
        if app_global_guards:
            guard_sec = _collect_guard_security(app_global_guards)
            if guard_sec is not None:
                op["security"] = guard_sec
        elif ctrl_meta.security:
            # Explicit @controller(security=[...]) always takes precedence.
            op["security"] = [dict(s) for s in ctrl_meta.security]
        elif compiled is not None and compiled.guards:
            guard_sec = _collect_guard_security(compiled.guards)
            if guard_sec is not None:
                op["security"] = guard_sec

        path_item[entry.method.lower()] = op

    doc: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": dict(info),
        "paths": paths,
        "components": components,
    }
    if servers:
        doc["servers"] = [dict(s) for s in servers]
    if tag_set:
        doc["tags"] = list(tag_set.values())
    return doc
