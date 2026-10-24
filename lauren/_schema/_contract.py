"""Response projection: a handler's return value → its declared shape (private).

Response contracts (``response_validation="filter"`` / ``"strict"``) build one
projector per declared body type at startup, from the nodes the app's
:class:`~lauren._schema._compiler.TypeCompiler` produced. A projector returns
JSON-ready data holding only the declared shape, or raises
:class:`~lauren._schema._issues._Invalid` with node-relative issues:

* models emit only their declared fields (under their wire names), read from
  attributes — "return the entity, annotate the DTO" — or from mapping keys,
  recursively through containers, unions and discriminated unions;
* a missing required field, a scalar where a model or container is declared,
  or a value no union member can produce is a violation in both modes;
* ``strict`` also validates every leaf against its declared type and runs the
  constraint checks; ``filter`` passes leaves through unchanged so they encode
  exactly as with ``response_validation="off"``;
* pydantic models and ``msgspec.Struct`` types project through their library
  (the declared type's serializer drops a subclass's extra fields); values that
  are not instances are converted with ``from_attributes=True``, so a failed
  conversion is a violation in both modes.

Unlike :meth:`CompiledType.project` (best effort, never fails), a projector
refuses to produce output that does not fit the declared type. There is no
reflection here: the request path only calls the prebuilt closures.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from ._compiler import _Explicit
from ._issues import MISSING, ValidationIssue, _Invalid, invalid, make_issue
from ._nodes import (
    AnyNode,
    ConstrainedNode,
    DictNode,
    DiscriminatedNode,
    ModelNode,
    NamedTupleNode,
    Node,
    OptionalNode,
    RefNode,
    SequenceNode,
    TupleNode,
    UnionNode,
)

#: ``value -> projected value``; raises ``_Invalid``.
Projector = Callable[[Any], Any]

#: Values that can never stand for a model.
_NOT_A_MODEL: tuple[type, ...] = (str, bytes, bytearray, int, float, bool, list, tuple, set, frozenset)
#: Values that can never stand for a list.
_NOT_A_SEQUENCE: tuple[type, ...] = (str, bytes, bytearray, Mapping)


def _identity(value: Any) -> Any:
    return value


def build_projector(node: Node, *, strict: bool, limit: int) -> Projector:
    """Build the projector for ``node`` (``strict`` validates leaves too)."""
    return _Builder(strict, limit).build(node)


def _pydantic_version() -> tuple[int, int]:
    import pydantic

    major, minor = (int(part) for part in pydantic.VERSION.split(".")[:2])
    return major, minor


class _Builder:
    """Walks a node tree once and returns closures; recursion goes through a memo."""

    __slots__ = ("strict", "limit", "_memo")

    def __init__(self, strict: bool, limit: int) -> None:
        self.strict = strict
        self.limit = limit
        self._memo: dict[int, Projector] = {}

    def build(self, node: Node) -> Projector:
        key = id(node)
        done = self._memo.get(key)
        if done is not None:
            return done
        slot: list[Projector] = []

        def deferred(value: Any) -> Any:  # a recursive type refers to itself
            return slot[0](value)

        self._memo[key] = deferred
        built = self._build(node)
        slot.append(built)
        self._memo[key] = built
        return built

    def _build(self, node: Node) -> Projector:  # noqa: PLR0911
        from ._backends import MsgspecStructNode, PydanticAdapterNode, PydanticModelNode

        if isinstance(node, _Explicit):
            return self.build(node.inner)
        if isinstance(node, RefNode):
            return self.build(node._node())
        if isinstance(node, ConstrainedNode):
            return self._constrained(node)
        if isinstance(node, OptionalNode):
            return self._optional(node)
        if isinstance(node, ModelNode):
            return self._model(node)
        if isinstance(node, SequenceNode):
            return self._sequence(node)
        if isinstance(node, TupleNode):
            return self._tuple(node)
        if isinstance(node, DictNode):
            return self._dict(node)
        if isinstance(node, UnionNode):
            return self._union(node)
        if isinstance(node, DiscriminatedNode):
            return self._discriminated(node)
        if isinstance(node, (PydanticModelNode, PydanticAdapterNode)):
            return self._pydantic(node)
        if isinstance(node, MsgspecStructNode):
            return self._msgspec(node)
        if isinstance(node, AnyNode):
            return _identity
        return self._leaf(node)

    # -- leaves and wrappers ---------------------------------------------

    def _leaf(self, node: Node) -> Projector:
        if not self.strict:
            return _identity
        validate = node.validate

        def project(value: Any) -> Any:
            validate(value)
            return value

        return project

    def _constrained(self, node: ConstrainedNode) -> Projector:
        inner = self.build(node.inner)
        if not self.strict:
            return inner
        checks = node.checks

        def project(value: Any) -> Any:
            out = inner(value)
            for check in checks:
                check.check(out)
            return out

        return project

    def _optional(self, node: OptionalNode) -> Projector:
        inner = self.build(node.inner)
        if inner is _identity:
            return _identity

        def project(value: Any) -> Any:
            return None if value is None else inner(value)

        return project

    # -- models ----------------------------------------------------------

    def _model(self, node: ModelNode) -> Projector:
        fields = tuple(
            (f.name, f.wire, self.build(f.node), f.required and f.init, f.default) for f in node._output
        )
        cls = node.cls
        class_name = cls.__name__
        positional = isinstance(node, NamedTupleNode)
        limit = self.limit

        def project(value: Any) -> Any:
            from_mapping = isinstance(value, Mapping)
            if not from_mapping:
                if positional and isinstance(value, (list, tuple)) and not isinstance(value, cls):
                    value = {wire: item for (_, wire, _, _, _), item in zip(fields, value)}
                    from_mapping = True
                elif value is None or (isinstance(value, _NOT_A_MODEL) and not isinstance(value, cls)):
                    raise invalid("model_type", value, {"class_name": class_name})
            out: dict[str, Any] = {}
            issues: list[ValidationIssue] | None = None
            for name, wire, proj, required, default in fields:
                if from_mapping:
                    raw = value.get(name, MISSING)
                    if raw is MISSING and wire != name:
                        raw = value.get(wire, MISSING)
                else:
                    raw = getattr(value, name, MISSING)
                if raw is MISSING:
                    if not required:
                        if default is not MISSING:
                            out[wire] = default
                        continue
                    issues = issues or []
                    issues.append(make_issue("missing", loc=(wire,)))
                else:
                    try:
                        out[wire] = proj(raw)
                        continue
                    except _Invalid as exc:
                        issues = issues or []
                        issues.extend(issue.prefixed(wire) for issue in exc.issues)
                if len(issues) >= limit:
                    break
            if issues:
                raise _Invalid(issues)
            return out

        return project

    # -- containers ------------------------------------------------------

    def _sequence(self, node: SequenceNode) -> Projector:
        item = self.build(node.item)
        code = node._code
        limit = self.limit

        def project(value: Any) -> Any:
            if value is None or isinstance(value, _NOT_A_SEQUENCE) or not isinstance(value, Iterable):
                raise invalid(code, value)
            if item is _identity:
                return value if type(value) is list else list(value)
            out: list[Any] = []
            issues: list[ValidationIssue] | None = None
            for index, element in enumerate(value):
                try:
                    out.append(item(element))
                except _Invalid as exc:
                    issues = issues or []
                    issues.extend(issue.prefixed(index) for issue in exc.issues)
                    if len(issues) >= limit:
                        break
            if issues:
                raise _Invalid(issues)
            return out

        return project

    def _tuple(self, node: TupleNode) -> Projector:
        items = tuple(self.build(n) for n in node.items)
        size = len(items)
        strict = self.strict

        def project(value: Any) -> Any:
            if not isinstance(value, (list, tuple)):
                raise invalid("tuple_type", value)
            out: list[Any] = []
            issues: list[ValidationIssue] = []
            for index, proj in enumerate(items):
                if index >= len(value):
                    issues.append(make_issue("missing", loc=(index,)))
                    continue
                try:
                    out.append(proj(value[index]))
                except _Invalid as exc:
                    issues.extend(issue.prefixed(index) for issue in exc.issues)
            if strict and len(value) > size:
                ctx = {"field_type": "Tuple", "max_length": size, "actual_length": len(value)}
                issues.append(make_issue("too_long", value, ctx))
            if issues:
                raise _Invalid(issues)
            return out

        return project

    def _dict(self, node: DictNode) -> Projector:
        values = self.build(node.value)
        check_key = node.key.validate if self.strict and not isinstance(node.key, AnyNode) else None
        limit = self.limit

        def project(value: Any) -> Any:
            if not isinstance(value, Mapping):
                raise invalid("dict_type", value)
            if values is _identity and check_key is None:
                return value if type(value) is dict else dict(value)
            out: dict[Any, Any] = {}
            issues: list[ValidationIssue] | None = None
            for key, item in value.items():
                try:
                    if check_key is not None:
                        try:
                            check_key(key)
                        except _Invalid as exc:
                            raise _Invalid([issue.prefixed("[key]") for issue in exc.issues]) from None
                    out[key] = values(item)
                except _Invalid as exc:
                    issues = issues or []
                    issues.extend(issue.prefixed(key) for issue in exc.issues)
                    if len(issues) >= limit:
                        break
            if issues:
                raise _Invalid(issues)
            return out

        return project

    # -- unions ----------------------------------------------------------

    def _union(self, node: UnionNode) -> Projector:
        members = tuple((m, self.build(m)) for m in node.members)
        if self.strict:
            fallbacks = members
            passthrough = False
        else:
            # In filter mode a leaf member accepts anything, so it must not
            # swallow a value a structured member (model, container) could not
            # produce: only exact matches and structured members are tried, and
            # unmatched values pass through only for all-leaf unions (or ``Any``).
            fallbacks = tuple((m, p) for m, p in members if p is not _identity)
            passthrough = len(fallbacks) < len(members) and (
                not fallbacks or any(isinstance(m, AnyNode) for m, _ in members)
            )

        def project(value: Any) -> Any:
            issues: list[ValidationIssue] = []
            for member, proj in members:
                if member.is_instance(value):
                    try:
                        return proj(value)
                    except _Invalid as exc:
                        issues.extend(issue.prefixed(member.label) for issue in exc.issues)
            for member, proj in fallbacks:
                if member.is_instance(value):
                    continue
                try:
                    return proj(value)
                except _Invalid as exc:
                    issues.extend(issue.prefixed(member.label) for issue in exc.issues)
            if passthrough:
                return value
            raise _Invalid(issues or [make_issue("union_no_match", value)])

        return project

    def _discriminated(self, node: DiscriminatedNode) -> Projector:
        variants = {tag: (cls, self.build(node.variants[tag])) for tag, cls in node.classes.items()}
        key = node.key
        strip = node.strip_key
        names = " | ".join(cls.__name__ for cls in node.classes.values())
        expected = ", ".join(repr(t) for t in variants)

        def project(value: Any) -> Any:
            chosen: str | None = None
            for tag, (cls, _) in variants.items():
                if isinstance(value, cls):
                    chosen = tag
                    break
            if chosen is None:
                if not isinstance(value, Mapping):
                    raise invalid("model_type", value, {"class_name": names})
                raw_tag = value.get(key)
                if raw_tag is None:
                    raise invalid("union_tag_not_found", value, {"discriminator": repr(key)})
                chosen = str(raw_tag)
                if chosen not in variants:
                    raise invalid(
                        "union_tag_invalid",
                        value,
                        {"discriminator": repr(key), "tag": chosen, "expected_tags": expected},
                    )
            try:
                out = variants[chosen][1](value)
            except _Invalid as exc:
                raise _Invalid([issue.prefixed(chosen) for issue in exc.issues]) from None
            if isinstance(out, dict) and chosen in strip:
                out = {key: chosen, **out}
            return out

        return project

    # -- library-owned types ---------------------------------------------

    def _pydantic(self, node: Node) -> Projector:
        import pydantic
        from pydantic_core import PydanticSerializationError

        from ._backends import PydanticModelNode, translate_pydantic

        adapter: Any
        if isinstance(node, PydanticModelNode):
            adapter = pydantic.TypeAdapter(node.model)
            cls: type | None = node.model
        else:
            adapter = node._adapter  # type: ignore[attr-defined]
            cls = node._cls  # type: ignore[attr-defined]
        dump = adapter.dump_python
        validate = adapter.validate_python
        error = pydantic.ValidationError
        version = _pydantic_version()
        convert_kwargs: dict[str, Any] = {"from_attributes": True}
        if version >= (2, 11):
            convert_kwargs["by_name"] = True  # ORM attributes carry field names, not aliases
        strict = self.strict
        # pydantic >= 2.7 can turn "value does not fit the declared type" into an
        # error while serializing; older versions are checked by re-validation.
        type_checked_dump = strict and version >= (2, 7)

        def check_instance(value: Any) -> Any:
            if type_checked_dump:
                try:
                    return dump(value, mode="json", by_alias=True, warnings="error")
                except PydanticSerializationError as exc:
                    out = dump(value, mode="json", by_alias=True, warnings=False)
                    try:
                        validate(out)
                    except error as invalid_exc:
                        raise _Invalid(translate_pydantic(invalid_exc)) from None
                    raise _Invalid([make_issue("value_error", ctx={"error": str(exc)})]) from None
            out = dump(value, mode="json", by_alias=True, warnings=False)  # pragma: no cover - pydantic < 2.7
            try:  # pragma: no cover - pydantic < 2.7
                validate(out)
            except error as invalid_exc:  # pragma: no cover - pydantic < 2.7
                raise _Invalid(translate_pydantic(invalid_exc)) from None
            return out  # pragma: no cover - pydantic < 2.7

        def project(value: Any) -> Any:
            if cls is not None and isinstance(value, cls):
                if strict:
                    return check_instance(value)
                return dump(value, mode="json", by_alias=True, warnings=False)
            try:
                converted = validate(value, **convert_kwargs)
            except error as exc:
                raise _Invalid(translate_pydantic(exc)) from None
            return dump(converted, mode="json", by_alias=True, warnings=False)

        return project

    def _msgspec(self, node: Node) -> Projector:
        from ._backends import translate_msgspec

        msgspec = node._msgspec  # type: ignore[attr-defined]
        struct = node.struct  # type: ignore[attr-defined]
        to_builtins = msgspec.to_builtins
        convert = msgspec.convert
        error = msgspec.ValidationError
        strict = self.strict

        def project(value: Any) -> Any:
            if type(value) is struct:
                out = to_builtins(value)
                if strict:
                    try:
                        convert(out, struct)
                    except error as exc:
                        raise _Invalid(translate_msgspec(str(exc)), str(exc)) from None
                return out
            try:
                converted = convert(value, struct, from_attributes=True, strict=False)
            except error as exc:
                raise _Invalid(translate_msgspec(str(exc)), str(exc)) from None
            return to_builtins(converted)

        return project


__all__ = ["Projector", "build_projector"]
