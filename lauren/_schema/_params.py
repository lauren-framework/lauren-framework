"""Startup-compiled validation plans for request parameters, frames and stream items (private).

``LaurenFactory.create`` attaches a plan to every path / query / header /
cookie / JSON-body / form-model :class:`~lauren.extractors.Extraction`
(``extraction.compiled``), to every typed WebSocket payload and to every
``Stream[T]`` item type. A plan owns the compiled validator, the parameter's
``loc`` root and its *strategy*:

* ``native`` — the native result equals the pre-engine result (pydantic and
  msgspec types, ``str`` / ``int`` / ``float`` scalars, ``Any``): one path in
  every mode. On failure the pre-engine code runs once more to produce the
  1.x ``message`` / ``detail`` the response keeps next to ``issues``;
* ``enforce`` — native validation decides (``stdlib_models="enforce"``, or the
  type uses explicit validation metadata);
* ``warn`` — the pre-engine code decides; the native validator shadows it and
  logs one warning per (route, issue type) it would have rejected;
* ``legacy`` — the pre-engine code only.

Every failure raises an error carrying ``issues``; the dispatcher merges the
failures of all parameters into one 422.
"""

from __future__ import annotations

import json as _jsonlib
import logging
import types
from collections.abc import Callable, Mapping
from typing import Annotated, Any, Union, get_args, get_origin

from ..exceptions import ExtractorError, ExtractorFieldError, HTTPError
from ._backends import (
    MsgspecDecodeError,
    MsgspecStructNode,
    PydanticAdapterNode,
    PydanticModelNode,
    translate_msgspec,
    translate_pydantic,
)
from ._compiler import STRING, CompileError, TypeCompiler, split_annotated, type_name
from ._issues import ValidationIssue, _Invalid, make_issue
from ._nodes import AnyNode, DiscriminatedNode, Node, SequenceNode, TupleNode
from ._nodes import OptionalNode as _OptionalNode

NATIVE, LEGACY, WARN, ENFORCE = "native", "legacy", "warn", "enforce"

#: ``ParamPlan.run(native_raw=...)`` default: the native validator sees ``raw``.
_SAME: Any = object()

_stdlib_logger = logging.getLogger("lauren")

#: Cap on remembered (route, issue type) pairs in warn mode — bounded memory.
_WARN_LIMIT = 4096

_EQUIVALENT_SCALARS: frozenset[Any] = frozenset({str, int, float, bytes, complex, Any, object})
_UNION_ORIGINS: tuple[Any, ...] = (Union, types.UnionType)
_SEQUENCE_TYPES: tuple[type, ...] = (list, tuple, set, frozenset)
_BACKEND_NODES = (PydanticModelNode, PydanticAdapterNode, MsgspecStructNode)


class ValidationRuntime:
    """App-owned validation state: the compiler, the policy and the warn-once memory."""

    def __init__(self, compiler: TypeCompiler, *, include_input: bool, logger: Any = None) -> None:
        self.compiler = compiler
        self.policy = compiler.policy
        self.include_input = include_input
        self.max_issues = compiler.max_issues
        self._logger = logger
        self._warned: set[tuple[str, str]] = set()

    def warn(self, route: str, issues: list[ValidationIssue]) -> None:
        """Log issues the native validator would have rejected (warn mode), once per route and type."""
        fresh = [i for i in issues if (route, i.type) not in self._warned]
        if not fresh or len(self._warned) >= _WARN_LIMIT:
            return
        for issue in fresh:
            self._warned.add((route, issue.type))
        shown = "; ".join(str(i) for i in issues[:5])
        message = (
            f"{route}: ValidationPolicy(stdlib_models='enforce') would reject this request "
            f"({len(issues)} issue{'' if len(issues) == 1 else 's'}: {shown}); it was accepted "
            "with the legacy value"
        )
        _stdlib_logger.warning(message)
        if self._logger is not None:
            self._logger.warn(
                message,
                context="Validation",
                route=route,
                issues=[{"loc": list(i.loc), "type": i.type} for i in issues[:20]],
            )

    def strategy(self, node: Node | None, *, equivalent: bool) -> str:
        if node is None:
            return LEGACY
        if node.explicit:
            return ENFORCE
        if equivalent:
            return NATIVE
        return self.policy.stdlib_models


# ---------------------------------------------------------------------------
# Issues ↔ errors
# ---------------------------------------------------------------------------


def attach_issues(err: Any, issues: list[ValidationIssue], *, include_input: bool) -> Any:
    """Store ``issues`` on ``err`` (``err.issues`` and ``detail["issues"]``) and return it."""
    err.issues = tuple(issues)
    err.detail = {**(err.detail or {}), "issues": [i.to_dict(include_input=include_input) for i in issues]}
    return err


def issues_from_exception(exc: BaseException | None) -> list[ValidationIssue] | None:
    """Translate a backend exception (pydantic / msgspec) found on ``exc`` or its cause."""
    seen = 0
    while exc is not None and seen < 4:
        errors = getattr(exc, "errors", None)
        if (
            callable(errors)
            and type(exc).__name__ == "ValidationError"
            and "pydantic" in type(exc).__module__
        ):
            return translate_pydantic(exc)
        if type(exc).__name__ == "ValidationError" and type(exc).__module__.startswith("msgspec"):
            return translate_msgspec(str(exc))
        exc = exc.__cause__ or exc.__context__
        seen += 1
    return None


def synthesize_issues(err: ExtractorError, root: tuple[str | int, ...]) -> list[ValidationIssue]:
    """Issues for an error raised without them (a custom extractor, a pipe, pre-engine code)."""
    translated = issues_from_exception(err.__cause__ or err.__context__)
    if translated:
        return [i.prefixed(*root) for i in translated]
    raw = err.detail.get("errors") if isinstance(err.detail, Mapping) else None
    if isinstance(raw, list) and raw and all(isinstance(e, Mapping) and "loc" in e for e in raw):
        out = []
        for e in raw:
            loc = tuple(e.get("loc") or ())
            out.append(ValidationIssue(root + loc, str(e.get("type", "value_error")), str(e.get("msg", ""))))
        return out
    message = err.message or "validation error"
    if isinstance(err, ExtractorFieldError) and message.startswith("missing"):
        return [make_issue("missing", loc=root)]
    return [make_issue("value_error", ctx={"error": message}, loc=root, msg=message)]


def param_loc_root(ext: Any) -> tuple[str | int, ...]:
    """The ``loc`` root of an extraction: ``("query", "page")``, ``("body",)``, …"""
    source = ext.source
    fd = ext.field_descriptor
    alias = fd.alias if fd is not None and getattr(fd, "alias", None) else None
    if source == "path":
        return ("path", ext.name)
    if source == "query":
        return ("query", alias or ext.name)
    if source == "header":
        return ("header", alias or ext.name.replace("_", "-"))
    if source == "cookie":
        return ("cookie", alias or ext.name)
    if source in ("json", "stream", "bytes", "byte_stream"):
        return ("body",)
    if source == "upload_file":
        return ("form", alias or ext.name)
    if source == "form":
        return ("form",)
    return (source, ext.name)


def ensure_issues(err: ExtractorError, ext: Any, runtime: ValidationRuntime | None) -> ExtractorError:
    """Give ``err`` issues when it has none (errors from custom extractors, pipes, …)."""
    if not getattr(err, "issues", ()):
        include = runtime.include_input if runtime is not None else False
        attach_issues(err, synthesize_issues(err, param_loc_root(ext)), include_input=include)
    return err


def merge_failures(
    failures: list[tuple[Any, ExtractorError]], runtime: ValidationRuntime | None
) -> ExtractorError:
    """One 422 for every failing parameter of a request.

    A single failing parameter keeps its own error (code, message and the 1.x
    ``detail`` keys) with ``issues`` added; several are combined into one
    ``ExtractorError`` whose ``issues`` list every failure.
    """
    limit = runtime.max_issues if runtime is not None else 50
    for ext, err in failures:
        ensure_issues(err, ext, runtime)
    if len(failures) == 1:
        err = failures[0][1]
        if len(err.issues) > limit:
            attach_issues(
                err, list(err.issues)[:limit], include_input=runtime.include_input if runtime else False
            )
        return err
    issues: list[ValidationIssue] = []
    for _, err in failures:
        issues.extend(err.issues)
    first_ext, first_err = failures[0]
    field = (
        first_err.detail.get("field", first_ext.name)
        if isinstance(first_err.detail, Mapping)
        else first_ext.name
    )
    return ExtractorError.from_issues(
        issues[:limit],
        field=field,
        include_input=runtime.include_input if runtime is not None else False,
    )


def failure_weight(err: ExtractorError) -> int:
    """How many issues a failure contributes towards ``max_issues``."""
    return len(getattr(err, "issues", ()) or ()) or 1


# ---------------------------------------------------------------------------
# Parameter plans
# ---------------------------------------------------------------------------


def _legacy_call(fn: Callable[[], Any], name: str, source: str) -> Any:
    """Run pre-engine code with the error wrapping ``_extract_raw`` always applied."""
    try:
        return fn()
    except ExtractorError:
        raise
    except HTTPError:
        raise
    except Exception as exc:
        raise ExtractorError(
            f"failed to extract {name!r}: {exc}",
            detail={"field": name, "source": source},
        ) from exc


class ParamPlan:
    """Base plan: strategy dispatch shared by every parameter source."""

    __slots__ = ("runtime", "node", "strategy", "loc", "route", "field", "error_cls", "source", "name")

    def __init__(
        self,
        runtime: ValidationRuntime,
        node: Node | None,
        strategy: str,
        *,
        loc: tuple[str | int, ...],
        route: str,
        field: str,
        error_cls: type[ExtractorError],
        source: str,
        name: str,
    ) -> None:
        self.runtime = runtime
        self.node = node
        self.strategy = strategy
        self.loc = loc
        self.route = route
        self.field = field
        self.error_cls = error_cls
        self.source = source
        self.name = name

    def native(self, raw: Any) -> Any:
        assert self.node is not None
        return self.node.validate(raw)

    def _absolute(self, issues: list[ValidationIssue]) -> list[ValidationIssue]:
        loc = self.loc
        return [i.prefixed(*loc) for i in issues[: self.runtime.max_issues]]

    def _native_issues(self, raw: Any) -> list[ValidationIssue] | None:
        try:
            self.native(raw)
        except _Invalid as exc:
            return self._absolute(exc.issues)
        except Exception:  # noqa: BLE001 — the shadow run must never break a request
            return None
        return None

    def _error(self, legacy: ExtractorError | None, issues: list[ValidationIssue]) -> ExtractorError:
        include = self.runtime.include_input
        if legacy is not None:
            return attach_issues(legacy, issues, include_input=include)
        return self.error_cls.from_issues(issues, field=self.field, include_input=include)

    def run(self, raw: Any, legacy: Callable[[Any], Any], native_raw: Any = _SAME) -> Any:
        """Validate ``raw`` with the plan's strategy.

        ``native_raw`` is what the native validator sees when it differs from
        the pre-engine input (``Query[list[int] | None]`` reads every value
        natively but only the first one before the engine).
        """
        nraw = raw if native_raw is _SAME else native_raw
        strategy = self.strategy
        if strategy == NATIVE or strategy == ENFORCE:
            try:
                return self.native(nraw)
            except _Invalid as exc:
                issues = self._absolute(exc.issues)
            legacy_err: ExtractorError | None = None
            if strategy == NATIVE:
                try:
                    _legacy_call(lambda: legacy(raw), self.name, self.source)
                except ExtractorError as err:
                    legacy_err = err
            raise self._error(legacy_err, issues)
        try:
            value = _legacy_call(lambda: legacy(raw), self.name, self.source)
        except ExtractorError as err:
            native_issues = self._native_issues(nraw) if self.node is not None else None
            raise self._error(err, native_issues or synthesize_issues(err, self.loc)) from err.__cause__
        if strategy == WARN and self.node is not None:
            shadow = self._native_issues(nraw)
            if shadow:
                self.runtime.warn(self.route, shadow)
        return value

    def _missing(self, ext: Any, message: str, field: str) -> Any:
        if ext.has_default:
            return ext.default
        err = ExtractorFieldError(message, detail={"field": field})
        raise attach_issues(
            err, [make_issue("missing", loc=self.loc)], include_input=self.runtime.include_input
        )

    async def extract(self, request: Any, ext: Any) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError


class ScalarPlan(ParamPlan):
    """``Path[T]`` / ``Query[T]`` / ``Header[T]`` / ``Cookie[T]`` (and the implicit forms)."""

    __slots__ = ("key", "is_list", "native_list", "split", "inner", "elem_type")

    def __init__(
        self, *args: Any, key: str, is_list: bool, native_list: bool, split: bool, inner: Any, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self.key = key
        #: The pre-engine code reads every value of the key (``Query[list[T]]``).
        self.is_list = is_list
        #: The native validator reads every value although the pre-engine code
        #: read the first one (``Query[list]``, ``Query[list[T] | None]``).
        self.native_list = native_list
        self.split = split
        self.inner = inner
        args_ = get_args(inner)
        self.elem_type = args_[0] if args_ else str

    def native(self, raw: Any) -> Any:
        if self.split and isinstance(raw, str):
            raw = raw.split(",")
        assert self.node is not None
        return self.node.validate(raw)

    def _legacy(self, raw: Any, ext: Any) -> Any:
        from ..extractors import _coerce_scalar

        fd = ext.field_descriptor
        if self.is_list:
            values = [_coerce_scalar(v, self.elem_type) for v in raw]
            return fd.validate(ext.name, values) if fd else values
        value = _coerce_scalar(raw, self.inner)
        return fd.validate(ext.name, value) if fd else value

    async def extract(self, request: Any, ext: Any) -> Any:
        source = self.source
        if source == "query":
            raw_list = request.query_params.get(self.key, [])
            if self.is_list:
                return self.run(raw_list, lambda raw: self._legacy(raw, ext))
            if not raw_list:
                return self._missing(ext, f"missing query parameter {self.key!r}", self.key)
            if self.native_list:
                return self.run(
                    raw_list[0], lambda value: self._legacy(value, ext), native_raw=list(raw_list)
                )
            raw: Any = raw_list[0]
        elif source == "path":
            raw = request.path_params.get(ext.name)
            if raw is None:
                return self._missing(ext, f"missing path parameter {ext.name!r}", ext.name)
        elif source == "header":
            raw = request.headers.get(self.key)
            if raw is None:
                return self._missing(ext, f"missing header {self.key!r}", self.key)
        else:
            raw = request.cookies.get(self.key)
            if raw is None:
                return self._missing(ext, f"missing cookie {self.key!r}", self.key)
        return self.run(raw, lambda value: self._legacy(value, ext))


class JsonPlan(ParamPlan):
    """``Json[T]`` and implicit model bodies."""

    __slots__ = ("inner", "single_decode")

    def __init__(self, *args: Any, inner: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.inner = inner
        #: msgspec ``Struct`` bodies decode and validate in one ``msgspec.json.decode``.
        self.single_decode = isinstance(self.node, MsgspecStructNode) and self.strategy == NATIVE

    def _invalid_json(self, ext: Any, exc: Exception) -> ExtractorError:
        if isinstance(exc, (_jsonlib.JSONDecodeError, MsgspecDecodeError)):
            err = ExtractorError(f"invalid JSON: {exc}", detail={"field": ext.name})
        else:
            err = ExtractorError(
                f"failed to extract {ext.name!r}: {exc}", detail={"field": ext.name, "source": "json"}
            )
        issue = make_issue("json_invalid", ctx={"error": str(exc)}, loc=self.loc)
        return attach_issues(err, [issue], include_input=False)

    async def extract(self, request: Any, ext: Any) -> Any:
        body = await request.body()
        if not body:
            if ext.has_default:
                return ext.default
            err = ExtractorFieldError("missing JSON body", detail={"field": ext.name})
            raise attach_issues(err, [make_issue("missing", loc=self.loc)], include_input=False)
        return self.validate_body(body, ext)

    def validate_body(self, body: bytes, ext: Any) -> Any:
        """Decode and validate one JSON body (the single decode step F03's codecs extend)."""
        if self.single_decode:
            node = self.node
            assert isinstance(node, MsgspecStructNode)
            try:
                return node.decode_json(body)
            except MsgspecDecodeError as exc:
                raise self._invalid_json(ext, exc) from None
            except _Invalid as exc:
                err = ExtractorError(
                    f"Validation failed for field '{ext.name}'",
                    detail={"field": ext.name, "errors": [exc.text or "validation error"]},
                )
                raise attach_issues(
                    err, self._absolute(exc.issues), include_input=self.runtime.include_input
                ) from None
        try:
            data = _jsonlib.loads(body)
        except ValueError as exc:
            raise self._invalid_json(ext, exc) from exc

        def legacy(value: Any) -> Any:
            from ..extractors import _validate_json

            return _validate_json(value, self.inner, ext.name)

        return self.run(data, legacy)


class ModelPlan(ParamPlan):
    """``Query[Model]`` / ``Form[Model]``: one key per field from a multi-dict."""

    __slots__ = ("wires", "optional", "model", "hints", "legacy_kind")

    def __init__(
        self,
        *args: Any,
        wires: tuple[tuple[str, bool], ...],
        optional: bool,
        model: Any,
        hints: dict[str, Any],
        legacy_kind: str,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.wires = wires
        self.optional = optional
        self.model = model
        self.hints = hints
        self.legacy_kind = legacy_kind

    def collect(self, md: Mapping[str, list[Any]]) -> dict[str, Any]:
        data: dict[str, Any] = {}
        for wire, as_list in self.wires:
            values = md.get(wire)
            if values:
                data[wire] = list(values) if as_list else values[0]
        return data

    def native(self, raw: Any) -> Any:
        assert self.node is not None
        return self.node.validate(self.collect(raw))

    def _legacy_query(self, md: Mapping[str, list[Any]], ext: Any) -> Any:
        from ..extractors import _convert_struct, _validate_as

        model = self.model
        if self.legacy_kind == "pydantic":
            fields_dict: dict[str, object] = {}
            for f_name, f_info in model.model_fields.items():
                f_alias = f_info.alias if getattr(f_info, "alias", None) else f_name
                raw = md.get(f_alias, md.get(f_name, []))
                if raw:
                    fields_dict[f_name] = raw[0] if len(raw) == 1 else raw
            return _validate_as(model, fields_dict, field=ext.name)
        if self.legacy_kind == "msgspec":
            import msgspec as _msgspec

            data: dict[str, object] = {}
            for sf in _msgspec.structs.fields(model):
                for key_candidate in (sf.encode_name, sf.name):
                    raw = md.get(key_candidate, [])
                    if raw:
                        data[sf.name] = raw[0] if len(raw) == 1 else raw
                        break
            return _convert_struct(data, model, ext.name, hints=self.hints)
        if self.legacy_kind == "dataclass":
            import dataclasses

            data = {}
            for sf in dataclasses.fields(model):
                raw = md.get(sf.name, [])
                if raw:
                    data[sf.name] = raw[0] if len(raw) == 1 else raw
            return _convert_struct(data, model, ext.name, hints=self.hints)
        # TypedDict / NamedTuple: the pre-engine code read one scalar key.
        from ..extractors import _coerce_scalar

        key = ext.field_descriptor.alias if ext.field_descriptor and ext.field_descriptor.alias else ext.name
        raw_list = md.get(key, [])
        if not raw_list:
            if ext.has_default:
                return ext.default
            raise ExtractorFieldError(f"missing query parameter {key!r}", detail={"field": key})
        value = _coerce_scalar(raw_list[0], ext.inner_type)
        return ext.field_descriptor.validate(ext.name, value) if ext.field_descriptor else value

    def _legacy_form(self, md: Mapping[str, list[Any]], ext: Any) -> Any:
        if self.legacy_kind == "pydantic":
            from ..extractors import _validate_as

            flat = {k: v[0] if len(v) == 1 else v for k, v in md.items()}
            return _validate_as(self.model, flat, field=ext.name)
        return md

    async def extract(self, request: Any, ext: Any) -> Any:
        if self.source == "form":
            md = await request.form()
            legacy: Callable[[Any], Any] = lambda raw: self._legacy_form(raw, ext)  # noqa: E731
        else:
            md = request.query_params
            legacy = lambda raw: self._legacy_query(raw, ext)  # noqa: E731
            if self.optional and not self.collect(md):
                return ext.default if ext.has_default else None
        return self.run(md, legacy)


# ---------------------------------------------------------------------------
# WebSocket frames and stream items
# ---------------------------------------------------------------------------


class ItemFailure(Exception):
    """A WebSocket payload or stream item failed; ``issues`` are relative to the item."""

    def __init__(
        self, issues: list[ValidationIssue], errors: list[Any] | None, cause: BaseException | None
    ) -> None:
        super().__init__("validation error")
        self.issues = issues
        #: The 1.x ``detail.errors`` content when the pre-engine validator ran.
        self.errors = errors
        self.cause = cause


def _legacy_errors(exc: BaseException) -> list[Any]:
    errors = getattr(exc, "errors", None)
    return errors() if callable(errors) else [str(exc)]  # type: ignore[no-any-return]


class ItemPlan:
    """Validates WebSocket payloads and inbound ``Stream[T]`` items (same strategies as HTTP)."""

    __slots__ = ("runtime", "node", "strategy", "legacy_adapter", "route", "type")

    def __init__(
        self,
        runtime: ValidationRuntime,
        node: Node | None,
        strategy: str,
        legacy_adapter: Any,
        route: str,
        tp: Any,
    ) -> None:
        self.runtime = runtime
        self.node = node
        self.strategy = strategy
        self.legacy_adapter = legacy_adapter
        self.route = route
        self.type = tp

    def _legacy(self, data: Any) -> Any:
        adapter = self.legacy_adapter
        return data if adapter is None else adapter.validate_python(data)

    def _native_issues(self, data: Any) -> list[ValidationIssue] | None:
        assert self.node is not None
        try:
            self.node.validate(data)
        except _Invalid as exc:
            return exc.issues[: self.runtime.max_issues]
        except Exception:  # noqa: BLE001 — shadow validation never breaks a frame
            return None
        return None

    def validate(self, data: Any) -> Any:
        """Return the validated item or raise :class:`ItemFailure`."""
        strategy = self.strategy
        if strategy == NATIVE or strategy == ENFORCE:
            assert self.node is not None
            try:
                return self.node.validate(data)
            except _Invalid as exc:
                issues = exc.issues[: self.runtime.max_issues]
            errors: list[Any] | None = None
            if strategy == NATIVE:
                try:
                    self._legacy(data)
                except Exception as exc:  # noqa: BLE001 — collecting the 1.x error list
                    errors = _legacy_errors(exc)
            raise ItemFailure(issues, errors, None)
        try:
            value = self._legacy(data)
        except Exception as exc:
            found = (self._native_issues(data) if self.node is not None else None) or issues_from_exception(
                exc
            )
            if not found:
                found = [
                    make_issue("value_error", ctx={"error": str(exc)}, msg=str(exc) or type(exc).__name__)
                ]
            raise ItemFailure(found, _legacy_errors(exc), exc) from exc
        if strategy == WARN and self.node is not None:
            shadow = self._native_issues(data)
            if shadow:
                self.runtime.warn(self.route, shadow)
        return value


# ---------------------------------------------------------------------------
# Builders (startup)
# ---------------------------------------------------------------------------


def validation_metadata(annotation: Any) -> tuple[Any, ...]:
    """Validation markers found on a parameter annotation (``Query[int, Le(100)]`` → ``(Le(100),)``).

    Extractor markers, field descriptors, pipes and the metadata the extractor
    parser already keeps on the inner type are skipped.
    """
    from ..extractors import ExtractionMarker, FieldDescriptor, _ParamSpec, _peel_optional, is_pipe
    from ._compiler import collect_meta

    peeled, _ = _peel_optional(annotation)
    _, metadata = split_annotated(peeled)
    found: list[Any] = []
    for item in metadata:
        candidate = item if isinstance(item, type) else type(item)
        if isinstance(candidate, type) and issubclass(candidate, ExtractionMarker):
            continue
        if isinstance(item, (FieldDescriptor, _ParamSpec)) or is_pipe(item):
            continue
        meta = collect_meta([item])
        if meta.explicit and meta.discriminator is None and not meta.pydantic:
            found.append(item)
    return tuple(found)


def _target_with(inner: Any, extra: tuple[Any, ...]) -> Any:
    if not extra:
        return inner
    return Annotated[(inner, *extra)]  # type: ignore[valid-type]


def _alias_of(extra: tuple[Any, ...]) -> str | None:
    from ._markers import Alias

    for item in extra:
        if isinstance(item, Alias):
            return item.name
    return None


def _is_backend_node(node: Node) -> bool:
    if isinstance(node, _BACKEND_NODES) or isinstance(node, AnyNode):
        return True
    if isinstance(node, DiscriminatedNode):
        return all(isinstance(v, _BACKEND_NODES) for v in node.variants.values())
    return False


def _is_equivalent_scalar(inner: Any) -> bool:
    base, _ = split_annotated(inner)
    if base in _EQUIVALENT_SCALARS:
        return True
    if get_origin(base) in (list, tuple):
        args = get_args(base)
        if not args:
            return True
        if get_origin(base) is tuple and not (len(args) == 2 and args[1] is Ellipsis) and len(args) != 1:
            return False
        return args[0] in _EQUIVALENT_SCALARS
    return False


def _compile_node(runtime: ValidationRuntime, tp: Any, flavor: int, where: dict[str, Any]) -> Node | None:
    """Compile ``tp``; ``None`` when it is unsupported and the mode tolerates that."""
    from ..exceptions import UnresolvableParameterError

    try:
        return runtime.compiler.node(tp, flavor)
    except CompileError as exc:
        reason = exc.reason
        if reason == "unsupported_type" and runtime.policy.stdlib_models != "enforce":
            message = (
                f"{where.get('route', '?')}: parameter {where.get('param')!r} is not validated natively "
                f"({exc.message}); it keeps the legacy behaviour (ValidationPolicy(stdlib_models='enforce') "
                "would reject this at startup)"
            )
            runtime.compiler.warnings.append(message)
            return None
        detail = {**{k: v for k, v in where.items() if k != "route"}, "type": type_name(tp), "reason": reason}
        if exc.field:
            detail["field"] = exc.field
        raise UnresolvableParameterError(
            f"cannot validate parameter {where.get('param')!r} of {where.get('class')}.{where.get('handler')}: "
            f"{exc.message}",
            detail=detail,
        ) from None
    except Exception as exc:  # noqa: BLE001 — e.g. pydantic refusing to build a TypeAdapter
        if runtime.policy.stdlib_models == "enforce":
            raise UnresolvableParameterError(
                f"cannot validate parameter {where.get('param')!r}: {exc}",
                detail={
                    **{k: v for k, v in where.items() if k != "route"},
                    "type": type_name(tp),
                    "reason": "unsupported_type",
                },
            ) from exc
        runtime.compiler.warnings.append(
            f"{where.get('route', '?')}: {where.get('param')!r} not validated natively: {exc}"
        )
        return None


_SCALAR_SOURCES = ("path", "query", "header", "cookie")


def build_param_plan(
    ext: Any,
    annotation: Any,
    runtime: ValidationRuntime,
    *,
    route: str,
    owner: str,
    handler: str,
) -> ParamPlan | ItemPlan | None:
    """Build and return the plan for one extraction (``None`` for sources without validation)."""
    from ..extractors import _peel_optional

    source = ext.source
    where = {"class": owner, "handler": handler, "param": ext.name, "route": route}
    extra = validation_metadata(annotation)
    inner = ext.inner_type
    fd = ext.field_descriptor
    common: dict[str, Any] = {"route": route, "source": source, "name": ext.name}

    if source in _SCALAR_SOURCES:
        peeled, _ = _peel_optional(inner)
        if source == "query" and _model_kind(peeled) is not None:
            return _build_model_plan(ext, runtime, peeled, inner, extra, where, common)
        is_list = source == "query" and get_origin(inner) in (list, tuple)
        sequence = get_origin(peeled) in _SEQUENCE_TYPES or peeled in _SEQUENCE_TYPES
        native_list = source == "query" and sequence and not is_list
        split = source != "query" and sequence
        target = _target_with(inner, (*extra, fd) if fd is not None else extra)
        node = _compile_node(runtime, target, STRING, where)
        alias = _alias_of(extra)
        if source == "path":
            key = ext.name
        elif source == "header":
            key = alias or (fd.alias if fd and fd.alias else ext.name.replace("_", "-"))
        else:
            key = alias or (fd.alias if fd and fd.alias else ext.name)
        equivalent = (
            _is_equivalent_scalar(inner) and not split and (source == "query" or get_origin(inner) is None)
        )
        strategy = runtime.strategy(node, equivalent=equivalent)
        return ScalarPlan(
            runtime,
            node,
            strategy,
            loc=(source, key),
            field=key,
            error_cls=ExtractorFieldError,
            key=key,
            is_list=is_list,
            native_list=native_list,
            split=split,
            inner=inner,
            **common,
        )

    if source == "json":
        node = _compile_node(runtime, _target_with(inner, extra), _json_flavor(runtime), where)
        equivalent = node is not None and _is_backend_node(node)
        strategy = runtime.strategy(node, equivalent=equivalent)
        return JsonPlan(
            runtime,
            node,
            strategy,
            loc=("body",),
            field=ext.name,
            error_cls=ExtractorError,
            inner=inner,
            **common,
        )

    if source == "form":
        peeled, _ = _peel_optional(inner)
        if _model_kind(peeled) is None:
            return None
        return _build_model_plan(ext, runtime, peeled, inner, extra, where, common)

    if source == "stream":
        return build_item_plan(inner, runtime, route=route, where=where)
    return None


def _json_flavor(runtime: ValidationRuntime) -> int:
    from ._compiler import LAX, STRICT

    return STRICT if runtime.policy.json_mode == "strict" else LAX


def _model_kind(tp: Any) -> str | None:
    from .._validation import is_dataclass, is_msgspec_struct, is_pydantic_model, is_typeddict

    if not isinstance(tp, type):
        return None
    if is_pydantic_model(tp):
        return "pydantic"
    if is_msgspec_struct(tp):
        return "msgspec"
    if is_dataclass(tp):
        return "dataclass"
    if is_typeddict(tp):
        return "typeddict"
    if issubclass(tp, tuple) and hasattr(tp, "_fields"):
        return "namedtuple"
    return None


def _sequence_field(node: Node) -> bool:
    inner = node.inner if isinstance(node, _OptionalNode) else node
    inner = getattr(inner, "inner", inner)  # constrained / explicit wrappers
    return isinstance(inner, (SequenceNode, TupleNode))


def _is_sequence_annotation(tp: Any) -> bool:
    """``list[int]`` / ``tuple[str, ...]`` / ``set[int] | None`` → ``True``."""
    origin = get_origin(tp)
    if origin in _UNION_ORIGINS:
        members = [a for a in get_args(tp) if a is not type(None)]
        return len(members) == 1 and _is_sequence_annotation(members[0])
    if origin is Annotated:
        return _is_sequence_annotation(get_args(tp)[0])
    return origin in (list, tuple, set, frozenset) or tp in (list, tuple, set, frozenset)


def _model_wires(node: Node, model: Any, kind: str) -> tuple[tuple[str, bool], ...]:
    """``(wire name, reads every value)`` per field of a query / form model."""
    fields = getattr(node, "fields", None)
    if fields is not None:
        return tuple((f.wire, _sequence_field(f.node)) for f in fields)
    if kind == "pydantic":
        return tuple(
            (info.alias or name, _is_sequence_annotation(info.annotation))
            for name, info in model.model_fields.items()
        )
    if kind == "msgspec":
        import msgspec

        return tuple(
            (sf.encode_name, _is_sequence_annotation(sf.type)) for sf in msgspec.structs.fields(model)
        )
    return ()  # pragma: no cover - every model kind is handled above


def _build_model_plan(
    ext: Any,
    runtime: ValidationRuntime,
    peeled: Any,
    inner: Any,
    extra: tuple[Any, ...],
    where: dict[str, Any],
    common: dict[str, Any],
) -> ParamPlan | None:
    from .._di import _safe_class_hints

    kind = _model_kind(peeled)
    assert kind is not None
    node = _compile_node(runtime, _target_with(peeled, extra), STRING, where)
    if node is None:
        return None
    base = getattr(node, "inner", node) if not hasattr(node, "fields") else node
    wires = _model_wires(base, peeled, kind)
    hints: dict[str, Any] = {}
    if kind in ("dataclass", "msgspec"):
        try:
            hints = _safe_class_hints(peeled, include_extras=False)
        except Exception:  # noqa: BLE001 — hints only feed the legacy coercer
            hints = {}
    optional = inner is not peeled
    source = ext.source
    if source == "form":
        equivalent = kind == "pydantic" and not optional
    else:
        equivalent = kind in ("pydantic", "msgspec")
    strategy = runtime.strategy(node, equivalent=equivalent)
    return ModelPlan(
        runtime,
        node,
        strategy,
        loc=(source,),
        field=ext.name,
        error_cls=ExtractorError,
        wires=wires,
        optional=optional,
        model=peeled,
        hints=hints,
        legacy_kind=kind,
        **common,
    )


def build_item_plan(
    inner: Any, runtime: ValidationRuntime, *, route: str, where: dict[str, Any] | None = None
) -> ItemPlan:
    """Plan for a WebSocket payload type or a ``Stream[T]`` item type."""
    from ..streaming import _build_adapter

    where = where or {"route": route, "param": "payload"}
    node = _compile_node(runtime, inner, _json_flavor(runtime), where)
    equivalent = node is not None and _is_backend_node(node)
    strategy = runtime.strategy(node, equivalent=equivalent)
    legacy_adapter = None if strategy == ENFORCE else _build_adapter(inner)
    return ItemPlan(runtime, node, strategy, legacy_adapter, route, inner)


__all__ = [
    "ENFORCE",
    "LEGACY",
    "NATIVE",
    "WARN",
    "ItemFailure",
    "ItemPlan",
    "JsonPlan",
    "ModelPlan",
    "ParamPlan",
    "ScalarPlan",
    "ValidationRuntime",
    "attach_issues",
    "build_item_plan",
    "build_param_plan",
    "ensure_issues",
    "failure_weight",
    "merge_failures",
    "param_loc_root",
    "synthesize_issues",
    "validation_metadata",
]
