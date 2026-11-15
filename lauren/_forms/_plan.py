"""Compile flat form bindings and multipart wire rules at startup (private)."""

from __future__ import annotations

import dataclasses
import types
import typing
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from .._di import _safe_class_hints
from .._schema._compiler import (
    STRING,
    CompileError,
    TypeCompiler,
    _strip_typed_dict_wrappers,
    _typed_dict_required,
    collect_meta,
    split_annotated,
    type_name,
)
from .._schema._issues import MISSING, ValidationIssue, _Invalid, make_issue
from .._schema._jsonschema import emit
from .._schema._markers import own_validation_config, to_camel
from .._schema._nodes import Node
from .._schema._params import ENFORCE, ParamPlan, ValidationRuntime, _model_kind
from ..exceptions import ExtractorError, UnresolvableParameterError
from ..extractors import FieldDescriptor, UploadField
from ..types import Request, UploadFile
from ._config import _PartSpec, _UploadSpec

_UNIONS = (Union, types.UnionType)
_SNIFF_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "application/pdf",
        "application/zip",
        "application/gzip",
    }
)


def _fail(where: Mapping[str, Any], reason: str, message: str, **extra: Any) -> typing.NoReturn:
    raise UnresolvableParameterError(
        f"{where.get('class', '?')}.{where.get('handler', '?')}: parameter {where.get('param')!r}: {message}",
        detail={**{k: v for k, v in where.items() if k != "route"}, "reason": reason, **extra},
    )


def _unwrap(tp: Any) -> tuple[Any, tuple[Any, ...], bool]:
    metadata: list[Any] = []
    optional = False
    while True:
        base, meta = split_annotated(tp)
        metadata.extend(meta)
        tp = _strip_typed_dict_wrappers(base)
        if get_origin(tp) in _UNIONS:
            args = get_args(tp)
            members = [arg for arg in args if arg is not type(None)]
            if len(members) == 1 and len(members) != len(args):
                optional = True
                tp = members[0]
                continue
        if tp is base:
            return tp, tuple(metadata), optional


def _is_upload(tp: Any) -> bool:
    return isinstance(tp, type) and issubclass(tp, UploadFile)


def _shape(tp: Any) -> tuple[Any, bool, tuple[Any, ...]]:
    base, metadata, _ = _unwrap(tp)
    sequence = get_origin(base) in (list, tuple) or base in (list, tuple)
    if sequence:
        args = get_args(base)
        base, inner_meta, _ = _unwrap(args[0] if args else str)
        metadata += inner_meta
    return base, sequence, metadata


def _fields(model: type, kind: str) -> tuple[tuple[str, str, Any, bool], ...]:
    """Snapshot (Python name, backend input key, annotation, required)."""
    if kind == "pydantic":
        out = []
        for name, info in getattr(model, "model_fields").items():
            annotation = info.annotation
            if info.metadata:
                annotation = Annotated[(annotation, *info.metadata)]
            alias = info.validation_alias or info.alias or name
            if not isinstance(alias, str):
                raise CompileError("unsupported_form_alias", f"field {name!r} needs a flat string alias")
            out.append((name, alias, annotation, info.is_required()))
        return tuple(out)
    if kind == "msgspec":
        import msgspec

        return tuple((f.name, f.encode_name, f.type, f.required) for f in msgspec.structs.fields(model))
    hints = _safe_class_hints(model, include_extras=True)
    config = own_validation_config(model)
    rename = config.rename if config else None

    def wire(name: str, annotation: Any) -> str:
        _, metadata, _ = _unwrap(annotation)
        alias = collect_meta(metadata).alias
        return alias or (to_camel(name) if rename == "camel" else name)

    if kind == "dataclass":
        return tuple(
            (
                f.name,
                wire(f.name, hints[f.name]),
                hints[f.name],
                f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING,
            )
            for f in dataclasses.fields(model)
            if f.init
        )
    if kind == "typeddict":
        total = bool(getattr(model, "__total__", True))
        return tuple(
            (name, wire(name, ann), _strip_typed_dict_wrappers(ann), _typed_dict_required(ann, total))
            for name, ann in hints.items()
        )
    defaults = getattr(model, "_field_defaults", {})
    return tuple(
        (name, wire(name, hints[name]), hints[name], name not in defaults)
        for name in getattr(model, "_fields")
    )


def _forbid_extra(model: type, kind: str, runtime: ValidationRuntime) -> bool:
    """Snapshot the final backend's unknown-field policy without invoking it."""
    if kind == "pydantic":
        return getattr(model, "model_config").get("extra") == "forbid"
    if kind == "msgspec":
        return bool(getattr(model, "__struct_config__").forbid_unknown_fields)
    pydantic_config = getattr(model, "__pydantic_config__", None)
    if pydantic_config is not None:
        return pydantic_config.get("extra") == "forbid"
    config = own_validation_config(model)
    return ((config.extra if config else None) or runtime.policy.extra) == "forbid"


def contains_upload(tp: Any, seen: set[Any] | None = None) -> bool:
    """Find file values through containers and every supported model backend, cycle-safely."""
    seen = set() if seen is None else seen
    base, _, _ = _unwrap(tp)
    if _is_upload(base):
        return True
    origin = get_origin(base)
    if origin is not None:
        return any(contains_upload(arg, seen) for arg in get_args(base) if arg is not Ellipsis)
    if not isinstance(base, type) or base in seen:
        return False
    seen.add(base)
    kind = _model_kind(base)
    if kind == "pydantic":
        annotations = (info.annotation for info in getattr(base, "model_fields").values())
    elif kind is not None:
        annotations = (ann for _, _, ann, _ in _fields(base, kind))
    else:
        return False
    return any(contains_upload(ann, seen) for ann in annotations)


def _contains_upload_descriptor(tp: Any, seen: set[type] | None = None) -> bool:
    seen = set() if seen is None else seen
    base, metadata, _ = _unwrap(tp)
    if any(isinstance(item, UploadField) for item in metadata):
        return True
    if get_origin(base) is not None:
        return any(_contains_upload_descriptor(arg, seen) for arg in get_args(base) if arg is not Ellipsis)
    if not isinstance(base, type) or base in seen:
        return False
    seen.add(base)
    kind = _model_kind(base)
    if kind == "pydantic":
        return any(
            any(isinstance(meta, UploadField) for meta in info.metadata)
            or _contains_upload_descriptor(info.annotation, seen)
            for info in getattr(base, "model_fields").values()
        )
    return kind is not None and any(
        _contains_upload_descriptor(ann, seen) for _, _, ann, _ in _fields(base, kind)
    )


def validate_form_placement(ext: Any, where: Mapping[str, Any]) -> None:
    """Reject file bodies in JSON and upload descriptors on non-form sources before fallback."""
    if ext.source not in ("json", "path", "query", "header", "cookie", "host_param", "form", "upload_file"):
        if isinstance(ext.field_descriptor, UploadField):
            _fail(where, "upload_field_placement", "UploadField requires a file form parameter")
        return
    if ext.source not in ("form", "upload_file"):
        if contains_upload(ext.inner_type):
            _fail(where, "upload_requires_form", "UploadFile values require Form[...] or an upload parameter")
        if isinstance(ext.field_descriptor, UploadField) or _contains_upload_descriptor(ext.inner_type):
            _fail(where, "upload_field_placement", "UploadField requires UploadFile or list[UploadFile]")


def _compile(compiler: TypeCompiler, tp: Any, where: Mapping[str, Any]) -> Node:
    try:
        return compiler.node(tp, STRING)
    except CompileError as exc:
        return _fail(
            where, exc.reason, exc.message, type=type_name(tp), **({"field": exc.field} if exc.field else {})
        )
    except Exception as exc:
        return _fail(where, "unsupported_type", str(exc), type=type_name(tp))


def _keeps_blank(tp: Any) -> bool:
    base, _, _ = _unwrap(tp)
    if base in (str, Any, object):
        return True
    if get_origin(base) is Literal:
        return "" in get_args(base)
    if get_origin(base) in _UNIONS:
        return any(_keeps_blank(arg) for arg in get_args(base))
    return False


@dataclass(frozen=True)
class _FieldBinding:
    wire: str
    key: str
    node: Node
    sequence: bool
    file: bool
    blank: bool
    required: bool
    uploads: tuple[_UploadSpec, ...]
    description: str | None = None

    def collect(self, data: Any) -> Any:
        values = data.getall(self.wire)
        if self.file:
            for spec in self.uploads:
                count = len(values)
                if count < spec.min_files or (spec.max_files is not None and count > spec.max_files):
                    raise _Invalid(
                        [
                            make_issue(
                                "value_error",
                                loc=(self.wire,),
                                msg="upload count outside allowed range",
                                ctx={
                                    "reason": "file_count",
                                    "min_files": spec.min_files,
                                    "max_files": spec.max_files,
                                },
                            )
                        ]
                    )
        if not values:
            return MISSING
        if any(isinstance(value, UploadFile) != self.file for value in values):
            reason = "expected_file" if self.file else "expected_text"
            message = "expected a file" if self.file else "expected a text field"
            raise _Invalid([make_issue("value_error", loc=(self.wire,), msg=message, ctx={"reason": reason})])
        if not self.file and not self.blank:
            values = [value for value in values if value != ""] if self.sequence else values[:1]
            if not values or values[0] == "":
                return MISSING
        raw = values if self.sequence else values[0]
        try:
            return self.node.validate(raw)
        except _Invalid as exc:
            raise _Invalid([issue.prefixed(self.wire) for issue in exc.issues]) from None

    def schema(self, components: dict[str, Any] | None) -> dict[str, Any]:
        result = emit(self.node, dialect="openapi-3.1", components=components)
        if self.file:
            target = result
            if "anyOf" in target:
                target = next(v for v in target["anyOf"] if v.get("type") != "null")
            if self.sequence:
                minimum = max((spec.min_files for spec in self.uploads), default=0)
                maximums = [spec.max_files for spec in self.uploads if spec.max_files is not None]
                if minimum:
                    target["minItems"] = minimum
                if maximums:
                    target["maxItems"] = min(maximums)
            allowed = next((spec.content_types for spec in self.uploads if spec.content_types), ())
            # A media range or several accepted types cannot truthfully be
            # represented as one concrete JSON-Schema contentMediaType.
            if len(allowed) == 1 and "*" not in allowed[0]:
                while "anyOf" in target or "items" in target:
                    if "anyOf" in target:
                        target = next(v for v in target["anyOf"] if v.get("type") != "null")
                    else:
                        target = target["items"]
                target["contentMediaType"] = allowed[0]
        if self.description:
            result["description"] = self.description
        return result


def _binding(
    wire: str,
    key: str,
    tp: Any,
    required: bool,
    runtime: ValidationRuntime,
    where: Mapping[str, Any],
    descriptor: FieldDescriptor | None = None,
) -> _FieldBinding:
    base, sequence, metadata = _shape(tp)
    _, outer_meta, _ = _unwrap(tp)
    descriptors = [item for item in (*outer_meta, *metadata) if isinstance(item, FieldDescriptor)]
    # _shape includes outer metadata; preserve identity rather than duplicating it.
    descriptors = list({id(item): item for item in descriptors}.values())
    if descriptor is not None and all(item is not descriptor for item in descriptors):
        descriptors.append(descriptor)
    uploads = [item for item in descriptors if isinstance(item, UploadField)]
    file = _is_upload(base)
    if uploads and not file:
        _fail(
            where, "upload_field_placement", "UploadField requires UploadFile or list[UploadFile]", field=wire
        )
    if not file and contains_upload(tp):
        _fail(where, "nested_upload", "file fields must be UploadFile or list[UploadFile]", field=wire)
    specs = []
    for upload in uploads:
        # Revalidate mutable user metadata, then copy only immutable values.
        upload = dataclasses.replace(upload)
        if not sequence and (upload.max_files is not None or upload.min_files != 0):
            _fail(
                where,
                "upload_count_requires_list",
                "max_files/min_files require list[UploadFile]",
                field=wire,
            )
        if upload.sniff is True and any(media not in _SNIFF_TYPES for media in upload.content_types):
            _fail(
                where,
                "unsupported_sniff_type",
                "sniff=True requires supported exact content_types or a custom sniffer",
                field=wire,
            )
        specs.append(
            _UploadSpec(
                upload.max_size, tuple(upload.content_types), upload.sniff, upload.max_files, upload.min_files
            )
        )
    if file and not specs:
        specs.append(_UploadSpec())
    alias = next((item.alias for item in reversed(descriptors) if item.alias), None)
    description = next((item.description for item in reversed(descriptors) if item.description), None)
    target = tp
    if descriptor is not None:
        target = Annotated[(tp, dataclasses.replace(descriptor))]
    node = _compile(runtime.compiler, target, where)
    return _FieldBinding(
        alias or wire, key, node, sequence, file, _keeps_blank(base), required, tuple(specs), description
    )


class FormBinding(ParamPlan):
    """Read the shared form cache, run precompiled coercers, then the final backend validator."""

    __slots__ = ("fields", "mode", "known", "route_plan", "forbid_extra")

    def __init__(
        self,
        runtime: ValidationRuntime,
        node: Node | None,
        *,
        fields: tuple[_FieldBinding, ...],
        mode: str,
        ext: Any,
        route: str,
    ) -> None:
        scalar = mode == "field"
        super().__init__(
            runtime,
            node,
            ENFORCE,
            loc=("form",),
            route=route,
            field=fields[0].wire if scalar else ext.name,
            error_cls=ExtractorError,
            source=ext.source,
            name=ext.name,
        )
        self.fields = fields
        self.mode = mode
        self.known = frozenset(field.wire for field in fields)
        self.route_plan: RouteFormPlan | None = None
        self.forbid_extra = False

    async def extract(self, request: Any, ext: Any) -> Any:
        from ._runtime import read_form_data

        form = await read_form_data(request)
        if isinstance(request, Request) and self.route_plan is not None:
            await self.route_plan.validate_cached(request, form)
        if self.mode == "raw":
            return form
        if self.mode == "dict":
            out: dict[str, list[str]] = {}
            for key, value in form.multi_items():
                if isinstance(value, str):
                    out.setdefault(key, []).append(value)
            return out
        data: dict[str, Any] = {}
        issues: list[ValidationIssue] = []
        transport_failed = False
        for field in self.fields:
            try:
                value = field.collect(form)
            except _Invalid as exc:
                transport_failed = True
                issues.extend(exc.issues)
                continue
            if value is not MISSING:
                data[field.key] = value
            elif field.required:
                issues.append(make_issue("missing", loc=(field.wire,)))
        # Unknown text keys must reach the model's extra="forbid" policy,
        # including when another field failed transport coercion. Snapshotting
        # that policy avoids invoking arbitrary model validators on invalid data.
        if self.mode == "model":
            for key, value in form.multi_items():
                if key not in self.known and isinstance(value, str) and key not in data:
                    data[key] = value
                    if transport_failed and self.forbid_extra:
                        issues.append(make_issue("extra_forbidden", loc=(key,), value=value))
        if transport_failed:
            raise self._error(None, self._absolute(issues))
        if self.mode == "field":
            field = self.fields[0]
            if field.key not in data:
                if ext.has_default:
                    return ext.default
                raise self._error(None, [make_issue("missing", loc=("form", field.wire))])
            return data[field.key]
        if not data and ext.has_default:
            return ext.default
        return self.run(data, lambda value: value)


@dataclass(frozen=True)
class RouteFormPlan:
    """Immutable route wire rules plus its already-compiled parameter bindings."""

    rules: Mapping[str, _PartSpec]
    takes_all: bool
    requires_multipart: bool
    bindings: tuple[FormBinding, ...]
    legacy_urlencoded: bool = False

    async def validate_cached(self, request: Request, data: Any) -> None:
        """Check all route rules once when middleware parsed before routing.

        Normal streamed parsing already checked these policies and records this
        plan's identity. A cached early parse has no route rules, so recheck the
        completed files before returning even a raw FormData binding.
        """
        if request.__dict__.get("__lauren_form_validated_plan__") is self:
            return
        failure = request.__dict__.get("__lauren_form_rule_error__")
        if failure is not None and failure[0] is self:
            raise failure[1]
        try:
            await self._validate_cached(request, data)
        except ExtractorError as exc:
            request.__dict__["__lauren_form_rule_error__"] = (self, exc)
            raise
        request.__dict__["__lauren_form_validated_plan__"] = self

    async def _validate_cached(self, request: Request, data: Any) -> None:
        from ._runtime import _SNIFF_BYTES, _check_sniff, _check_type, _field_error, _limit

        content = request.__dict__.get("__lauren_content__")
        reject = content is not None and content.multipart.undeclared_parts == "reject"
        if reject and not self.takes_all:
            for name in data:
                if name not in self.rules:
                    raise _field_error(name, "undeclared_part", "multipart part is not declared")
        for name, rule in self.rules.items():
            values = data.getall(name)
            file = rule.kind == "file"
            if any(isinstance(value, UploadFile) != file for value in values):
                reason = "expected_file" if file else "expected_text"
                raise _field_error(name, reason, "multipart part has the wrong kind")
            if not file:
                continue
            for spec in rule.uploads:
                if len(values) < spec.min_files:
                    raise _field_error(
                        name, "too_few_files", "too few files for this field", min_files=spec.min_files
                    )
                if spec.max_files is not None and len(values) > spec.max_files:
                    raise _field_error(
                        name, "too_many_files", "too many files for this field", max_files=spec.max_files
                    )
            for upload in values:
                _check_type(upload, rule.uploads)
                for spec in rule.uploads:
                    if spec.max_size is not None and upload.size > spec.max_size:
                        raise _limit("file_too_large", field=name, max_size=spec.max_size)
                if any(spec.sniff for spec in rule.uploads):
                    position = await upload.seek(0, 1)
                    try:
                        await upload.seek(0)
                        prefix = await upload.read(_SNIFF_BYTES)
                    finally:
                        await upload.seek(position)
                    _check_sniff(upload, rule.uploads, prefix)

    def schema(self, components: dict[str, Any] | None = None) -> dict[str, Any]:
        properties: dict[str, Any] = {}
        required: list[str] = []
        for binding in self.bindings:
            for field in binding.fields:
                item = field.schema(components)
                if field.wire in properties and properties[field.wire] != item:
                    item = {"allOf": [properties[field.wire], item]}
                properties[field.wire] = item
                if field.required and field.wire not in required:
                    required.append(field.wire)
        schema: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            schema["required"] = required
        return schema

    def encoding(self) -> dict[str, Any]:
        out = {}
        for name, rule in self.rules.items():
            allowed = next((spec.content_types for spec in rule.uploads if spec.content_types), ())
            if allowed:
                out[name] = {"contentType": ", ".join(allowed)}
        return out


def build_form_plan(
    extractions: Sequence[Any],
    runtime: ValidationRuntime,
    *,
    route: str = "?",
    owner: str = "?",
    handler: str = "?",
) -> RouteFormPlan | None:
    """Build every form binding and merge part rules after compiling the handler signature."""
    bindings: list[FormBinding] = []
    rules: dict[str, _PartSpec] = {}
    takes_all = False
    for ext in extractions:
        if ext.source not in ("form", "upload_file"):
            continue
        where = {"class": owner, "handler": handler, "param": ext.name, "route": route}
        base, _, _ = _unwrap(ext.inner_type)
        if isinstance(ext.field_descriptor, UploadField) and not _is_upload(_shape(ext.inner_type)[0]):
            _fail(
                where,
                "upload_field_placement",
                "UploadField requires UploadFile or list[UploadFile]",
                field=ext.name,
            )
        kind = _model_kind(base)
        fields: tuple[_FieldBinding, ...] = ()
        node = None
        if isinstance(base, type) and base.__name__ == "FormData" and base.__module__ == "lauren.types":
            mode = "raw"
            takes_all = True
        elif base is dict or get_origin(base) is dict:
            if get_args(base) not in ((), (str, list[str])):
                _fail(where, "unsupported_form_type", "raw forms require dict or dict[str, list[str]]")
            mode = "dict"
        elif kind is not None:
            mode = "model"
            try:
                model_fields = _fields(base, kind)
            except CompileError as exc:
                _fail(where, exc.reason, exc.message)
            fields = tuple(
                _binding(wire, key, tp, required, runtime, where) for key, wire, tp, required in model_fields
            )
            # The native model node's wire keys match the model's aliases;
            # Pydantic/msgspec retain their own final validation and hooks.
            fields = tuple(
                dataclasses.replace(field, key=model_fields[index][1]) for index, field in enumerate(fields)
            )
            node = _compile(runtime.compiler, base, where)
        else:
            mode = "field"
            fd = ext.field_descriptor
            fields = (_binding(ext.name, ext.name, ext.inner_type, not ext.has_default, runtime, where, fd),)
        binding = FormBinding(runtime, node, fields=fields, mode=mode, ext=ext, route=route)
        if mode == "model":
            assert kind is not None
            binding.forbid_extra = _forbid_extra(base, kind, runtime)
        ext.compiled = binding
        bindings.append(binding)
        for field in fields:
            rule = _PartSpec("file" if field.file else "text", field.uploads)
            previous = rules.get(field.wire)
            if previous is not None:
                if previous.kind != rule.kind:
                    _fail(
                        where,
                        "form_kind_collision",
                        "a form part cannot be both text and file",
                        field=field.wire,
                    )
                rule = _PartSpec(rule.kind, previous.uploads + rule.uploads)
            rules[field.wire] = rule
    if not bindings:
        return None
    plan = RouteFormPlan(
        MappingProxyType(rules),
        takes_all,
        any(rule.kind == "file" for rule in rules.values()),
        tuple(bindings),
        legacy_urlencoded=all(binding.mode == "dict" for binding in bindings),
    )
    for binding in bindings:
        binding.route_plan = plan
    return plan
