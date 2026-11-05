"""The type compiler: annotations → validator nodes, once, at startup (private).

:class:`TypeCompiler` turns a type annotation into a :class:`CompiledType`
(``validate`` / ``project`` / ``json_schema``). Each compiler owns its cache,
keyed by ``(annotation, flavor)``: there is no module-level state, so several
apps (each with its own compiler) coexist in one process.

Flavors:

* ``LAX`` — JSON bodies, WebSocket frames and stream items in ``json_mode="lax"``
  (``"12"`` → ``12``);
* ``STRICT`` — ``json_mode="strict"``, ``Strict()`` fields, or
  ``@validation_config(strict=True)`` models;
* ``STRING`` — path, query, header, cookie and urlencoded-form values. Always
  lax, an empty value becomes ``None`` for ``X | None`` when ``X`` is not
  ``str``, and unions are tried left to right.

All annotations are resolved through :func:`lauren._di._safe_class_hints`
(which wraps :func:`lauren._typing.resolve_type_hints`).
"""

from __future__ import annotations

import collections.abc
import dataclasses
import datetime
import decimal
import enum
import pathlib
import re
import types
import typing
import uuid
from typing import Annotated, Any, ForwardRef, Literal, Union, get_args, get_origin

from ._issues import MISSING, ValidationFailed, _Invalid
from ._jsonschema import emit
from ._markers import Alias, Strict, ValidationPolicy, check_pattern, own_validation_config, to_camel
from ._nodes import (
    AnyNode,
    BoolNode,
    BoundCheck,
    BytesNode,
    Check,
    ComplexNode,
    ConstrainedNode,
    DataclassNode,
    DateNode,
    DatetimeNode,
    DecimalNode,
    DictNode,
    DiscriminatedNode,
    EnumNode,
    Field,
    FloatNode,
    InstanceNode,
    IntNode,
    LengthCheck,
    LiteralNode,
    MultipleOfCheck,
    NamedTupleNode,
    Node,
    NoneNode,
    OptionalNode,
    PathNode,
    PatternCheck,
    RefNode,
    SequenceNode,
    StrNode,
    TimedeltaNode,
    TimeNode,
    TupleNode,
    TypedDictNode,
    UnionNode,
    UUIDNode,
)

LAX, STRICT, STRING = 0, 1, 2

_NoneType = type(None)
_UNION_ORIGINS: tuple[Any, ...] = (Union, types.UnionType)
_SEQUENCE_ORIGINS: dict[Any, type] = {
    list: list,
    tuple: tuple,
    set: set,
    frozenset: frozenset,
    collections.abc.Sequence: list,
    collections.abc.MutableSequence: list,
    collections.abc.Set: frozenset,
    collections.abc.MutableSet: set,
}
_MAPPING_ORIGINS = (dict, collections.abc.Mapping, collections.abc.MutableMapping)
_NUMERIC_KINDS = frozenset({"int", "float", "decimal"})
_LENGTH_KINDS = frozenset({"str", "bytes", "list", "tuple", "set", "frozenset", "dict"})
_LEGACY_LENGTH_KINDS = frozenset({"str"})
_TYPED_DICT_WRAPPERS: tuple[Any, ...] = tuple(
    w
    for w in (
        getattr(typing, "Required", None),
        getattr(typing, "NotRequired", None),
        getattr(typing, "ReadOnly", None),
    )
    if w
)


def type_name(tp: Any) -> str:
    """A short, readable name for an annotation (``list[int]``, ``int | None``)."""
    origin = get_origin(tp)
    if origin is Annotated:
        return type_name(get_args(tp)[0])
    if tp is _NoneType or tp is None:
        return "None"
    if tp is Any:
        return "Any"
    if isinstance(tp, ForwardRef):
        return tp.__forward_arg__
    if origin in _UNION_ORIGINS:
        return " | ".join(type_name(a) for a in get_args(tp))
    if origin is Literal:
        return "Literal[" + ", ".join(repr(a) for a in get_args(tp)) + "]"
    if origin is not None:
        base = getattr(origin, "__name__", repr(origin))
        args = get_args(tp)
        if not args:
            return str(base)
        return f"{base}[" + ", ".join("..." if a is Ellipsis else type_name(a) for a in args) + "]"
    return str(getattr(tp, "__name__", repr(tp)))


class CompileError(Exception):
    """A type cannot be compiled. ``reason`` is ``unsupported_type`` / ``bad_constraint`` / ``bad_pattern``."""

    def __init__(self, reason: str, message: str, *, field: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        self.field = field


class _Constraint:
    """A constraint read from ``Annotated`` metadata (``legacy`` = from a ``FieldDescriptor``)."""

    __slots__ = ("op", "value", "legacy")

    def __init__(self, op: str, value: Any, *, legacy: bool) -> None:
        self.op = op
        self.value = value
        self.legacy = legacy


_CONSTRAINT_ATTRS = ("ge", "gt", "le", "lt", "multiple_of", "min_length", "max_length", "pattern")


class _Meta:
    """Validation metadata collected from one ``Annotated[...]`` layer."""

    __slots__ = ("constraints", "strict", "alias", "discriminator", "pydantic", "explicit")

    def __init__(self) -> None:
        self.constraints: list[_Constraint] = []
        self.strict: bool | None = None
        self.alias: str | None = None
        self.discriminator: Any = None
        self.pydantic = False
        self.explicit = False


def _is_field_descriptor(obj: Any) -> bool:
    cls = type(obj)
    return cls.__name__ == "FieldDescriptor" and cls.__module__ == "lauren.extractors"


def collect_meta(metadata: tuple[Any, ...] | list[Any]) -> _Meta:
    """Classify ``Annotated`` metadata into constraints, ``Strict``, ``Alias`` and discriminators."""
    from .._discriminated import _DiscriminatorMarker

    meta = _Meta()
    for item in metadata:
        if isinstance(item, _DiscriminatorMarker):
            meta.discriminator = item
            continue
        if isinstance(item, Strict):
            meta.strict = item.strict
            meta.explicit = True
            continue
        if isinstance(item, Alias):
            meta.alias = item.name
            meta.explicit = True
            continue
        cls_name = type(item).__name__
        if cls_name == "FieldInfo" and hasattr(item, "metadata"):
            if isinstance(getattr(item, "discriminator", None), str):
                meta.pydantic = True
                continue
            inner = collect_meta(list(getattr(item, "metadata", ()) or ()))
            meta.constraints.extend(inner.constraints)
            meta.strict = inner.strict if inner.strict is not None else meta.strict
            alias = getattr(item, "alias", None)
            if isinstance(alias, str) and alias:
                meta.alias = alias
            meta.explicit = (
                meta.explicit or bool(inner.constraints) or inner.strict is not None or bool(alias)
            )
            continue
        if cls_name == "Strict" and isinstance(getattr(item, "strict", None), bool):
            meta.strict = item.strict
            meta.explicit = True
            continue
        legacy = _is_field_descriptor(item)
        found = False
        for attr in _CONSTRAINT_ATTRS:
            value = getattr(item, attr, None)
            if value is None:
                continue
            meta.constraints.append(_Constraint(attr, value, legacy=legacy))
            found = True
        if found and not legacy:
            meta.explicit = True
    return meta


def split_annotated(tp: Any) -> tuple[Any, tuple[Any, ...]]:
    """``Annotated[T, a, b]`` → ``(T, (a, b))``; anything else → ``(tp, ())``."""
    if get_origin(tp) is Annotated:
        args = get_args(tp)
        return args[0], tuple(args[1:])
    return tp, ()


def _hashable_key(tp: Any, flavor: int) -> tuple[Any, int] | None:
    try:
        hash(tp)
    except TypeError:
        return None
    return (tp, flavor)


class CompiledType:
    """A type compiled for validation, projection and JSON Schema emission.

    Built by :meth:`TypeCompiler.compile` (inside an app, the framework
    compiles every extractor, WebSocket payload and stream item type once at
    startup). Compiled types are immutable and safe to share across threads.
    """

    __slots__ = ("type", "node", "_limit")

    def __init__(self, tp: Any, node: Node, limit: int) -> None:
        #: The annotation this was compiled from.
        self.type = tp
        self.node = node
        self._limit = limit

    def validate(self, value: Any, *, loc: tuple[str | int, ...] = ()) -> Any:
        """Return ``value`` validated (and coerced) or raise :class:`ValidationFailed`.

        Every failing field is reported, up to the compiler's ``max_issues``.
        ``loc`` is prepended to each issue's location.
        """
        try:
            return self.node.validate(value)
        except _Invalid as exc:
            issues = exc.issues[: self._limit]
            if loc:
                issues = [issue.prefixed(*loc) for issue in issues]
            raise ValidationFailed(issues) from None

    def project(self, value: Any) -> Any:
        """Return ``value`` as JSON-compatible builtins holding only the declared shape.

        Models emit their declared fields under their wire names (extra
        attributes are dropped); enums, dates, UUIDs and decimals become their
        JSON representation. ``validate(project(x))`` round-trips.
        """
        return self.node.project(value)

    def json_schema(
        self, *, dialect: str = "2020-12", components: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Return the JSON Schema (``"2020-12"``) or OpenAPI 3.1 schema (``"openapi-3.1"``).

        With ``"openapi-3.1"`` named definitions go into ``components`` (the
        caller's ``components.schemas`` dict) and are referenced as
        ``#/components/schemas/<Name>``.
        """
        return emit(self.node, dialect=dialect, components=components)

    @property
    def explicit(self) -> bool:
        """True when the type uses validation markers, ``Alias`` or ``@validation_config``."""
        return self.node.explicit

    def __repr__(self) -> str:
        return f"CompiledType({type_name(self.type)})"


class TypeCompiler:
    """Compiles annotations into :class:`CompiledType` validators and caches them.

    Hold one compiler for hot paths: compiling walks the type once, validating
    then runs prebuilt closures with no reflection. Inside an app,
    ``app.type_compiler`` is the compiler ``LaurenFactory.create`` used, so
    services see exactly the semantics the HTTP layer applies. ``policy``
    supplies ``json_mode``, the default ``extra`` and ``max_issues``.
    """

    def __init__(self, policy: ValidationPolicy | None = None) -> None:
        if policy is not None and not isinstance(policy, ValidationPolicy):
            raise TypeError(f"policy must be a ValidationPolicy, got {type(policy).__name__}")
        self._policy = policy or ValidationPolicy()
        self._cache: dict[tuple[Any, int], Node] = {}
        self._building: dict[tuple[Any, int], RefNode] = {}
        #: When true, ``FieldDescriptor`` constraints that do not apply to the
        #: parameter type are dropped (the pre-engine behaviour) with a warning.
        self.soft_legacy = self._policy.stdlib_models != "enforce"
        #: Startup warnings collected while compiling (logged by the factory).
        self.warnings: list[str] = []

    @property
    def policy(self) -> ValidationPolicy:
        return self._policy

    @property
    def max_issues(self) -> int:
        return self._policy.max_issues

    def compile(self, tp: Any, *, mode: str | None = None) -> CompiledType:
        """Compile ``tp``; ``mode`` (``"lax"`` / ``"strict"``) defaults to the policy's ``json_mode``.

        Raises :class:`~lauren.exceptions.UnresolvableParameterError` when the
        type is unsupported or a constraint is invalid.
        """
        if mode is None:
            mode = self._policy.json_mode
        if mode not in ("lax", "strict"):
            raise ValueError(f"mode must be 'lax' or 'strict', got {mode!r}")
        flavor = STRICT if mode == "strict" else LAX
        try:
            node = self.node(tp, flavor)
        except CompileError as exc:
            from ..exceptions import UnresolvableParameterError

            detail: dict[str, Any] = {"type": type_name(tp), "reason": exc.reason}
            if exc.field:
                detail["field"] = exc.field
            raise UnresolvableParameterError(
                f"cannot compile {type_name(tp)}: {exc.message}", detail=detail
            ) from None
        return CompiledType(tp, node, self.max_issues)

    def wrap(self, tp: Any, node: Node) -> CompiledType:
        """A :class:`CompiledType` around an already-built node (framework use)."""
        return CompiledType(tp, node, self.max_issues)

    # ------------------------------------------------------------------
    # Node construction
    # ------------------------------------------------------------------

    def node(self, tp: Any, flavor: int) -> Node:
        """Return the (cached) node for ``tp``; raises :class:`CompileError`."""
        key = _hashable_key(tp, flavor)
        if key is not None:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            building = self._building.get(key)
            if building is not None:
                return building
        built = self._build(tp, flavor)
        if key is not None:
            self._cache[key] = built
        return built

    def _build(self, tp: Any, flavor: int) -> Node:  # noqa: PLR0911, PLR0912
        base, metadata = split_annotated(tp)
        if metadata:
            return self._build_annotated(tp, base, metadata, flavor)
        if isinstance(tp, (str, ForwardRef)):
            name = tp if isinstance(tp, str) else tp.__forward_arg__
            raise CompileError("unsupported_type", f"cannot resolve the annotation {name!r}")
        if tp is Any or tp is object:
            return AnyNode()
        if tp is None or tp is _NoneType:
            return NoneNode()
        supertype = getattr(tp, "__supertype__", None)
        if supertype is not None:  # typing.NewType
            return self.node(supertype, flavor)
        alias_value = getattr(tp, "__value__", None)
        if alias_value is not None and type(tp).__name__ == "TypeAliasType":
            return self.node(alias_value, flavor)
        origin = get_origin(tp)
        if origin in _UNION_ORIGINS:
            return self._build_union(get_args(tp), flavor)
        if origin is Literal:
            return LiteralNode(get_args(tp), strict=flavor == STRICT)
        if tp in _SEQUENCE_ORIGINS or origin in _SEQUENCE_ORIGINS:
            return self._build_sequence(tp, origin, flavor)
        if tp in _MAPPING_ORIGINS or origin in _MAPPING_ORIGINS:
            args = get_args(tp)
            key_node = self.node(args[0], flavor) if len(args) == 2 else AnyNode()
            value_node = self.node(args[1], flavor) if len(args) == 2 else AnyNode()
            return DictNode(key_node, value_node, limit=self.max_issues)
        if isinstance(tp, dataclasses.InitVar):
            return self.node(tp.type, flavor)
        if not isinstance(tp, type):
            raise CompileError("unsupported_type", f"unsupported type {type_name(tp)}")
        return self._build_class(tp, flavor)

    def _build_class(self, tp: type, flavor: int) -> Node:  # noqa: PLR0911, PLR0912
        from .._validation import is_dataclass, is_msgspec_struct, is_pydantic_model, is_typeddict

        strict = flavor == STRICT
        if is_pydantic_model(tp):
            from ._backends import PydanticModelNode

            return PydanticModelNode(tp)
        if is_msgspec_struct(tp):
            from ._backends import MsgspecStructNode

            return MsgspecStructNode(tp, strict=flavor != STRING)
        if is_dataclass(tp):
            if hasattr(tp, "__pydantic_validator__"):
                from ._backends import PydanticAdapterNode

                return PydanticAdapterNode(tp)
            return self._build_model(tp, flavor, "dataclass")
        if is_typeddict(tp):
            return self._build_model(tp, flavor, "typeddict")
        if issubclass(tp, tuple) and hasattr(tp, "_fields"):
            return self._build_model(tp, flavor, "namedtuple")
        if issubclass(tp, enum.Enum):
            return EnumNode(tp, strict=strict)
        if tp is bool:
            return BoolNode(strict)
        if tp is int:
            return IntNode(strict)
        if tp is float:
            return FloatNode(strict)
        if tp is str:
            return StrNode()
        if tp in (bytes, bytearray):
            return BytesNode()
        if tp is complex:
            return ComplexNode(strict)
        if issubclass(tp, decimal.Decimal):
            return DecimalNode(strict)
        if issubclass(tp, uuid.UUID):
            return UUIDNode(strict)
        if issubclass(tp, datetime.datetime):
            return DatetimeNode()
        if issubclass(tp, datetime.date):
            return DateNode()
        if issubclass(tp, datetime.time):
            return TimeNode()
        if issubclass(tp, datetime.timedelta):
            return TimedeltaNode(strict)
        if issubclass(tp, pathlib.PurePath):
            return PathNode(tp)
        from ..types import UploadFile

        if issubclass(tp, UploadFile):
            return InstanceNode(tp)
        raise CompileError("unsupported_type", f"unsupported type {type_name(tp)}")

    def _build_annotated(self, tp: Any, base: Any, metadata: tuple[Any, ...], flavor: int) -> Node:
        meta = collect_meta(metadata)
        if meta.pydantic:
            from ._backends import PydanticAdapterNode

            return PydanticAdapterNode(tp)
        if meta.discriminator is not None:
            node = self._build_discriminated(meta.discriminator, flavor)
        else:
            inner_flavor = flavor
            if meta.strict is not None and flavor != STRING:
                inner_flavor = STRICT if meta.strict else LAX
            node = self.node(base, inner_flavor)
        if meta.constraints:
            node = self._constrain(node, meta.constraints, base)
        if meta.explicit and not node.explicit:
            node = _mark_explicit(node)
        return node

    def _build_union(self, args: tuple[Any, ...], flavor: int) -> Node:
        members = [a for a in args if a is not _NoneType and a is not None]
        optional = len(members) != len(args)
        if not members:
            return NoneNode()
        if len(members) == 1:
            inner = self.node(members[0], flavor)
        else:
            nodes = [self.node(m, flavor) for m in members]
            strict_nodes = nodes if flavor != LAX else [self.node(m, STRICT) for m in members]
            inner = UnionNode(nodes, strict_nodes, left_to_right=flavor == STRING, strict=flavor == STRICT)
        if not optional:
            return inner
        blank = flavor == STRING and not _accepts_blank(inner)
        return OptionalNode(inner, blank_none=blank)

    def _build_sequence(self, tp: Any, origin: Any, flavor: int) -> Node:
        container = _SEQUENCE_ORIGINS[origin if origin is not None else tp]
        args = get_args(tp)
        if container is tuple and args and not (len(args) == 2 and args[1] is Ellipsis):
            if args == ((),):
                return TupleNode([])
            return TupleNode([self.node(a, flavor) for a in args])
        item = self.node(args[0], flavor) if args else AnyNode()
        return SequenceNode(item, container, strict=flavor == STRICT, limit=self.max_issues)

    def _build_discriminated(self, marker: Any, flavor: int) -> Node:
        variants: dict[str, Node] = {}
        classes: dict[str, type] = {}
        strip: set[str] = set()
        for tag, variant in marker.mapping.items():
            node = self.node(variant, flavor)
            variants[tag] = node
            classes[tag] = variant
            fields = getattr(node, "fields", None)
            if fields is not None and all(f.wire != marker.key for f in fields):
                strip.add(tag)
        return DiscriminatedNode(marker.key, variants, classes, frozenset(strip))

    # ------------------------------------------------------------------
    # Models
    # ------------------------------------------------------------------

    def _build_model(self, cls: type, flavor: int, kind: str) -> Node:
        key = (cls, flavor)
        ref = RefNode(cls.__name__)
        self._building[key] = ref
        try:
            node = self._build_model_inner(cls, flavor, kind)
        finally:
            self._building.pop(key, None)
        ref.target = node
        return node

    def _build_model_inner(self, cls: type, flavor: int, kind: str) -> Node:
        from .._di import _safe_class_hints

        config = own_validation_config(cls)
        extra = (config.extra if config and config.extra else None) or self._policy.extra
        forbid = extra == "forbid"
        rename = config.rename if config else None
        field_flavor = flavor
        if config is not None and config.strict is not None and flavor != STRING:
            field_flavor = STRICT if config.strict else LAX
        hints = _safe_class_hints(cls, include_extras=True)
        explicit = config is not None
        fields: list[Field] = []
        output: list[Field] = []

        def make(name: str, ann: Any, *, required: bool, default: Any, init: bool) -> Field:
            nonlocal explicit
            if ann is None:
                raise CompileError(
                    "unsupported_type", f"field {name!r} has no annotation", field=f"{cls.__name__}.{name}"
                )
            _, field_meta = split_annotated(ann)
            alias = collect_meta(field_meta).alias if field_meta else None
            wire = alias or (to_camel(name) if rename == "camel" else name)
            try:
                node = self.node(ann, field_flavor)
            except CompileError as exc:
                if not init:
                    node = AnyNode()
                else:
                    if exc.field is None:
                        exc.field = f"{cls.__name__}.{name}"
                    raise
            explicit = explicit or node.explicit or alias is not None
            return Field(name, wire, node, required=required, default=default, init=init)

        if kind == "dataclass":
            initvar_type = getattr(dataclasses, "_FIELD_INITVAR", None)
            regular = {f.name for f in dataclasses.fields(cls)}
            for f in getattr(cls, "__dataclass_fields__", {}).values():
                is_initvar = initvar_type is not None and getattr(f, "_field_type", None) is initvar_type
                if not is_initvar and f.name not in regular:
                    continue  # ClassVar pseudo-fields
                ann = hints.get(f.name)
                if isinstance(ann, dataclasses.InitVar):
                    ann = ann.type
                has_default = f.default is not dataclasses.MISSING
                has_factory = f.default_factory is not dataclasses.MISSING
                default = f.default if has_default else MISSING
                built = make(
                    f.name, ann, required=not (has_default or has_factory), default=default, init=f.init
                )
                if f.init:
                    fields.append(built)
                if not is_initvar:
                    output.append(built)
            node: Node = DataclassNode(cls, fields, forbid=forbid, limit=self.max_issues, output=output)
        elif kind == "typeddict":
            total = bool(getattr(cls, "__total__", True))
            for name, ann in hints.items():
                required = _typed_dict_required(ann, total)
                td_field = make(
                    name, _strip_typed_dict_wrappers(ann), required=required, default=MISSING, init=True
                )
                fields.append(td_field)
            node = TypedDictNode(cls, fields, forbid=forbid, limit=self.max_issues)
        else:
            defaults = getattr(cls, "_field_defaults", {})
            for name in cls._fields:  # type: ignore[attr-defined]
                has_default = name in defaults
                default = defaults[name] if has_default else MISSING
                fields.append(
                    make(name, hints.get(name, Any), required=not has_default, default=default, init=True)
                )
            node = NamedTupleNode(cls, fields, forbid=forbid, limit=self.max_issues)
        node.explicit = explicit
        return node

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------

    def _constrain(self, node: Node, constraints: list[_Constraint], base: Any) -> Node:
        if isinstance(node, OptionalNode):
            return OptionalNode(self._constrain(node.inner, constraints, base), blank_none=node.blank_none)
        checks: list[Check] = []
        kind = node.kind
        min_length: int | None = None
        max_length: int | None = None
        lower: Any = None
        upper: Any = None
        any_explicit = False
        for c in constraints:
            # ``FieldDescriptor`` constraints keep their pre-engine reach: length
            # and pattern checks only ever applied to strings.
            length_kinds = _LEGACY_LENGTH_KINDS if c.legacy else _LENGTH_KINDS
            applicable = (
                (c.op in ("ge", "gt", "le", "lt", "multiple_of") and kind in _NUMERIC_KINDS)
                or (c.op in ("min_length", "max_length") and kind in length_kinds)
                or (c.op == "pattern" and kind == "str")
            )
            if not applicable:
                self._bad(c, f"{_marker_name(c.op)} does not apply to {type_name(base)}")
                continue
            if c.op in ("min_length", "max_length"):
                if isinstance(c.value, bool) or not isinstance(c.value, int) or c.value < 0:
                    self._bad(c, f"{_marker_name(c.op)} must be a non-negative int, got {c.value!r}")
                    continue
                if c.op == "min_length":
                    min_length = c.value
                else:
                    max_length = c.value
            elif c.op == "pattern":
                try:
                    regex = check_pattern(c.value)
                except (re.error, TypeError) as exc:
                    if c.legacy and self.soft_legacy:
                        self.warnings.append(f"invalid FieldDescriptor pattern {c.value!r} ignored: {exc}")
                        continue
                    raise CompileError("bad_pattern", f"invalid pattern {c.value!r}: {exc}") from None
                checks.append(PatternCheck(regex, full=c.legacy))
            else:
                limit = c.value
                if isinstance(limit, bool) or not isinstance(limit, (int, float, decimal.Decimal)):
                    self._bad(c, f"{_marker_name(c.op)} needs a number, got {limit!r}")
                    continue
                if kind == "decimal":
                    limit = decimal.Decimal(str(limit))
                if c.op == "multiple_of":
                    if limit == 0:
                        self._bad(c, "MultipleOf(0) is not allowed")
                        continue
                    checks.append(MultipleOfCheck(limit))
                else:
                    if c.op in ("ge", "gt"):
                        lower = limit if lower is None else max(lower, limit)
                    else:
                        upper = limit if upper is None else min(upper, limit)
                    checks.append(BoundCheck(c.op, limit))
            any_explicit = any_explicit or not c.legacy
        if min_length is not None and max_length is not None and min_length > max_length:
            raise CompileError("bad_constraint", f"MinLen({min_length}) is greater than MaxLen({max_length})")
        if lower is not None and upper is not None and lower > upper:
            raise CompileError(
                "bad_constraint", f"the lower bound {lower} is greater than the upper bound {upper}"
            )
        if min_length is not None or max_length is not None:
            checks.insert(0, LengthCheck(kind, min_length, max_length))
        if not checks:
            return node
        constrained = ConstrainedNode(node, checks)
        constrained.explicit = any_explicit or node.explicit
        return constrained

    def _bad(self, c: _Constraint, message: str) -> None:
        if c.legacy and self.soft_legacy:
            self.warnings.append(f"FieldDescriptor constraint ignored: {message}")
            return
        raise CompileError("bad_constraint", message)


_MARKER_NAMES = {
    "ge": "Ge",
    "gt": "Gt",
    "le": "Le",
    "lt": "Lt",
    "multiple_of": "MultipleOf",
    "min_length": "MinLen",
    "max_length": "MaxLen",
    "pattern": "Pattern",
}


def _marker_name(op: str) -> str:
    return _MARKER_NAMES.get(op, op)


def _accepts_blank(node: Node) -> bool:
    """Whether ``""`` is a meaningful value for ``node`` (``str`` / ``Any`` members)."""
    if node.kind in ("str", "any"):
        return True
    if isinstance(node, UnionNode):
        return any(_accepts_blank(m) for m in node.members)
    if isinstance(node, ConstrainedNode):
        return _accepts_blank(node.inner)
    return False


def _typed_dict_required(ann: Any, total: bool) -> bool:
    """Whether a TypedDict key is required, read from the *resolved* annotation.

    ``__required_keys__`` cannot be trusted under ``from __future__ import
    annotations`` on Python 3.11 (``Required[...]`` is still a string when the
    class is created), so ``Required`` / ``NotRequired`` are read here.
    """
    while True:
        origin = get_origin(ann)
        if origin is None:
            return total
        if origin is Annotated or origin is getattr(typing, "ReadOnly", Annotated):
            ann = get_args(ann)[0]
            continue
        if origin is typing.Required:
            return True
        if origin is typing.NotRequired:
            return False
        return total


def _strip_typed_dict_wrappers(ann: Any) -> Any:
    while get_origin(ann) in _TYPED_DICT_WRAPPERS:
        ann = get_args(ann)[0]
    base, metadata = split_annotated(ann)
    if metadata and get_origin(base) in _TYPED_DICT_WRAPPERS:
        return Annotated[(_strip_typed_dict_wrappers(base), *metadata)]  # type: ignore[valid-type]
    return ann


class _Explicit(Node):
    """Marks a node as built from explicit metadata (``Annotated[int, Strict()]``)."""

    __slots__ = ("inner",)

    def __init__(self, inner: Node) -> None:
        super().__init__()
        self.inner = inner
        self.explicit = True
        self.kind = inner.kind
        self.exact_types = inner.exact_types

    def validate(self, value: Any) -> Any:
        return self.inner.validate(value)

    def project(self, value: Any) -> Any:
        return self.inner.project(value)

    def schema(self, ctx: Any) -> dict[str, Any]:
        return self.inner.schema(ctx)

    def is_instance(self, value: Any) -> bool:
        return self.inner.is_instance(value)

    @property
    def label(self) -> str:
        return self.inner.label


def _mark_explicit(node: Node) -> Node:
    return _Explicit(node)


__all__ = [
    "LAX",
    "STRICT",
    "STRING",
    "CompileError",
    "CompiledType",
    "TypeCompiler",
    "collect_meta",
    "split_annotated",
    "type_name",
]
