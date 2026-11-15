"""Typed path converters and template / host-pattern parsing (private).

A route template segment is static text, ``{name}`` (any non-empty
segment), ``{name:converter}`` / ``{name:converter(args)}`` (a typed
segment) or ``{*name}`` / ``{name:path}`` (the remainder of the path).
Templates are parsed once — at decoration time for syntax and built-in
converter arguments, and again when the router registers the route, where
custom converters from :class:`~lauren.routing.RoutingConfig` resolve.

Converters decide *matching* (a mismatch is a routing miss, never a 422),
order sibling parameters by specificity, describe the parameter in OpenAPI
and format a value back into a segment (:meth:`PathConverter.to_url`, the
seam URL generation builds on). Extraction is unchanged: the handler
annotation still drives coercion of the raw segment string.
"""

from __future__ import annotations

import ast
import decimal
import math
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, ClassVar


class PathConverter:
    """Base class (and protocol) for typed path segments: ``{name:converter}``.

    Subclass it and register an instance by name with
    ``RoutingConfig(converters={"isbn": IsbnConverter()})``; any object with
    the same attributes works too. A converter decides whether a *whole*
    segment belongs to the route (:meth:`matches` — a mismatch makes the
    router try the next candidate, then answer 404), converts a matched
    segment (:meth:`to_python`), formats a value back into a segment
    (:meth:`to_url`, used by URL generation; it must produce a segment that
    :meth:`matches`) and documents the parameter (:meth:`openapi_schema`).
    ``python_type`` becomes the parameter's type when the handler leaves it
    unannotated. Custom converters take no arguments in templates and are
    tried before every built-in one at the same position.
    """

    #: Name used in templates (informational for custom converters: the
    #: ``RoutingConfig.converters`` key is what templates use).
    name: ClassVar[str] = ""
    #: Type an unannotated handler parameter receives.
    python_type: ClassVar[type] = str

    def matches(self, segment: str) -> bool:
        """Whether ``segment`` (already percent-decoded, never empty) belongs to this route."""
        return bool(segment)

    def to_python(self, segment: str) -> Any:
        """Convert a matched segment to ``python_type``."""
        return segment

    def to_url(self, value: Any) -> str:
        """Format ``value`` as a segment (before percent-encoding)."""
        return str(value)

    def openapi_schema(self) -> dict[str, Any]:
        """The JSON Schema of the parameter in the OpenAPI document."""
        return {"type": "string"}

    @property
    def spec(self) -> str:
        """Canonical template spelling (``int(min=1)``); identifies a tree position."""
        return self.name

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.spec or '?'}>"


# ---------------------------------------------------------------------------
# Built-in converters
# ---------------------------------------------------------------------------


class _StrConverter(PathConverter):
    """Any non-empty segment (the default for ``{name}``)."""

    name = "str"


class _RemainderConverter(PathConverter):
    """The rest of the path (``{*name}`` / ``{name:path}``); may be empty."""

    name = "path"

    def matches(self, segment: str) -> bool:
        return True


def _bound_spec(name: str, low: Any, high: Any) -> str:
    args = [f"{key}={value!r}" for key, value in (("min", low), ("max", high)) if value is not None]
    return f"{name}({', '.join(args)})" if args else name


class _BoundedNumber(PathConverter):
    """Shared ``min=`` / ``max=`` handling for the numeric converters."""

    _pattern: ClassVar[re.Pattern[str]]

    def __init__(self, min: Any = None, max: Any = None) -> None:  # noqa: A002 - template keywords
        for key, value in (("min", min), ("max", max)):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, self._bound_types())
                or (isinstance(value, float) and not math.isfinite(value))
            ):
                raise ValueError(f"{self.name}({key}=...) must be a finite number, got {value!r}")
        if min is not None and max is not None and min > max:
            raise ValueError(f"{self.name}(min={min!r}, max={max!r}): min is greater than max")
        self.min = min
        self.max = max

    def _bound_types(self) -> tuple[type, ...]:
        return (int,)

    def _convert(self, segment: str) -> Any:
        raise NotImplementedError

    def matches(self, segment: str) -> bool:
        if self._pattern.fullmatch(segment) is None:
            return False
        try:
            value = self._convert(segment)
        except (ValueError, OverflowError):  # e.g. more digits than int() accepts
            return False
        if isinstance(value, float) and not math.isfinite(value):
            return False
        return (self.min is None or value >= self.min) and (self.max is None or value <= self.max)

    def to_python(self, segment: str) -> Any:
        return self._convert(segment)

    def _schema_bounds(self, schema: dict[str, Any]) -> dict[str, Any]:
        if self.min is not None:
            schema["minimum"] = self.min
        if self.max is not None:
            schema["maximum"] = self.max
        return schema

    @property
    def spec(self) -> str:
        return _bound_spec(self.name, self.min, self.max)


class _IntConverter(_BoundedNumber):
    """``[0-9]+`` (a non-negative integer), optionally bounded."""

    name = "int"
    python_type = int
    _pattern = re.compile(r"[0-9]+", re.ASCII)

    def _convert(self, segment: str) -> Any:
        return int(segment)

    def to_url(self, value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{{:{self.spec}}} expects an int, got {value!r}")
        text = str(value)
        if not self.matches(text):
            raise ValueError(f"{value!r} does not match {{:{self.spec}}}")
        return text

    def openapi_schema(self) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "integer", "minimum": 0}
        return self._schema_bounds(schema)


class _SignedIntConverter(_IntConverter):
    """``-?[0-9]+`` (any integer), optionally bounded."""

    name = "sint"
    _pattern = re.compile(r"-?[0-9]+", re.ASCII)

    def openapi_schema(self) -> dict[str, Any]:
        return self._schema_bounds({"type": "integer"})


class _FloatConverter(_BoundedNumber):
    """A decimal literal (``-?[0-9]+(.[0-9]+)?``), optionally bounded."""

    name = "float"
    python_type = float
    _pattern = re.compile(r"-?[0-9]+(?:\.[0-9]+)?", re.ASCII)

    def _bound_types(self) -> tuple[type, ...]:
        return (int, float)

    def _convert(self, segment: str) -> Any:
        return float(segment)

    def to_url(self, value: Any) -> str:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{{:{self.spec}}} expects a finite number, got {value!r}")
        # ``repr`` round-trips but may use an exponent; the decimal form does not.
        text = format(decimal.Decimal(repr(float(value))), "f")
        if not self.matches(text):
            raise ValueError(f"{value!r} does not match {{:{self.spec}}}")
        return text

    def openapi_schema(self) -> dict[str, Any]:
        return self._schema_bounds({"type": "number"})


class _UUIDConverter(PathConverter):
    """A canonical 8-4-4-4-12 hex UUID (case-insensitive)."""

    name = "uuid"
    python_type = uuid.UUID
    _pattern = re.compile(r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}", re.ASCII)

    def matches(self, segment: str) -> bool:
        return self._pattern.fullmatch(segment) is not None

    def to_python(self, segment: str) -> Any:
        return uuid.UUID(segment)

    def to_url(self, value: Any) -> str:
        return str(value if isinstance(value, uuid.UUID) else uuid.UUID(str(value)))

    def openapi_schema(self) -> dict[str, Any]:
        return {"type": "string", "format": "uuid"}


class _PatternConverter(PathConverter):
    """Shared ``fullmatch`` logic for ``slug`` and ``re(...)``."""

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self._compiled = re.compile(pattern)

    def matches(self, segment: str) -> bool:
        return self._compiled.fullmatch(segment) is not None

    def to_url(self, value: Any) -> str:
        text = str(value)
        if not self.matches(text):
            raise ValueError(f"{value!r} does not match {{:{self.spec}}}")
        return text

    def openapi_schema(self) -> dict[str, Any]:
        return {"type": "string", "pattern": f"^(?:{self.pattern})$"}


_SLUG = r"[a-z0-9]+(?:-[a-z0-9]+)*"


class _SlugConverter(_PatternConverter):
    """Lower-case ASCII words joined by single hyphens (``blue-mug``)."""

    name = "slug"

    def __init__(self) -> None:
        super().__init__(_SLUG)

    @property
    def spec(self) -> str:
        return "slug"


class _RegexConverter(_PatternConverter):
    """``re(<pattern>)``: the segment must ``re.fullmatch`` the pattern."""

    name = "re"

    @property
    def spec(self) -> str:
        return f"re({self.pattern})"


#: Built-in converter factories by template name.
BUILTIN_CONVERTERS: Mapping[str, type[PathConverter]] = MappingProxyType(
    {
        "str": _StrConverter,
        "int": _IntConverter,
        "sint": _SignedIntConverter,
        "float": _FloatConverter,
        "uuid": _UUIDConverter,
        "slug": _SlugConverter,
        "re": _RegexConverter,
    }
)

#: Specificity at one tree position: lower is tried first. Custom
#: converters (and anything not listed) share rank 0 with ``re(...)``.
_RANKS: Mapping[type, int] = MappingProxyType(
    {
        _RegexConverter: 0,
        _UUIDConverter: 1,
        _IntConverter: 2,
        _SignedIntConverter: 2,
        _FloatConverter: 3,
        _SlugConverter: 4,
        _StrConverter: 5,
    }
)

#: Shared instances for the argument-free defaults.
STR_CONVERTER = _StrConverter()
REMAINDER_CONVERTER = _RemainderConverter()


def converter_rank(converter: Any) -> int:
    """Specificity rank of ``converter`` (custom converters are tried first)."""
    return _RANKS.get(type(converter), 0)


def is_builtin_converter(converter: Any) -> bool:
    return type(converter) in _RANKS or type(converter) is _RemainderConverter


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------


class TemplateError(ValueError):
    """A route template or host pattern is malformed (``reason`` names the problem)."""

    def __init__(self, message: str, *, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


_CONVERTER_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@dataclass(frozen=True, slots=True)
class Segment:
    """One parsed template segment."""

    kind: str  # "static" | "param" | "wildcard"
    #: Static text, or the parameter name.
    text: str
    #: ``None`` for static segments; the converter otherwise (untyped
    #: parameters get :data:`STR_CONVERTER`).
    converter: PathConverter | None = None
    #: Canonical converter spelling (``int(min=1)``, a custom name); ``""``
    #: when the template spelled none (``{id}``).
    spec: str = ""

    @property
    def typed(self) -> bool:
        return bool(self.spec)

    @property
    def node_key(self) -> str:
        """Identity of the tree position this segment occupies."""
        if self.kind == "static":
            return self.text
        if self.kind == "wildcard":
            return "*"
        return self.spec or "str"

    def template(self) -> str:
        if self.kind == "static":
            return self.text
        if self.kind == "wildcard":
            return "{*" + self.text + "}"
        if not self.spec:
            return "{" + self.text + "}"
        return "{" + self.text + ":" + self.spec + "}"

    def format(self) -> str:
        """OpenAPI-style spelling: ``{name}`` for every parameter."""
        return self.text if self.kind == "static" else "{" + self.text + "}"


def _parse_args(name: str, text: str) -> dict[str, Any]:
    try:
        call = ast.parse(f"f({text})", mode="eval").body
    except SyntaxError:
        raise TemplateError(
            f"cannot parse the arguments of {name}({text})", reason="bad_converter_args"
        ) from None
    assert isinstance(call, ast.Call)
    if call.args:
        raise TemplateError(
            f"{name}(...) takes keyword arguments only (e.g. {name}(min=1)), got {name}({text})",
            reason="bad_converter_args",
        )
    out: dict[str, Any] = {}
    for kw in call.keywords:
        if kw.arg is None:
            raise TemplateError(f"{name}(...) does not accept **kwargs", reason="bad_converter_args")
        try:
            out[kw.arg] = ast.literal_eval(kw.value)
        except ValueError:
            raise TemplateError(
                f"{name}({kw.arg}=...) must be a literal value", reason="bad_converter_args"
            ) from None
    return out


def build_converter(spec: str, custom: Mapping[str, PathConverter] | None) -> PathConverter | None:
    """Return the converter for ``spec`` (``int(min=1)``, ``re(...)``, ``isbn``).

    ``None`` when the name is neither built in nor in ``custom`` — the caller
    decides whether that is an error yet (decoration time does not know the
    app's custom converters).
    """
    return _build(spec, custom)[0]


def _build(spec: str, custom: Mapping[str, PathConverter] | None) -> tuple[PathConverter | None, str]:
    """``(converter, canonical spec)``; the spec of a custom converter is its registered name."""
    paren = spec.find("(")
    if paren == -1:
        name, args_text = spec.strip(), None
    else:
        if not spec.endswith(")"):
            raise TemplateError(
                f"converter {spec!r} is missing its closing parenthesis", reason="bad_converter_args"
            )
        name, args_text = spec[:paren].strip(), spec[paren + 1 : -1]
    if not _CONVERTER_NAME.fullmatch(name):
        raise TemplateError(f"invalid converter name {name!r}", reason="bad_converter_name")
    if custom and name in custom:
        if args_text is not None:
            raise TemplateError(
                f"custom converter {name!r} takes no arguments ({spec!r})", reason="bad_converter_args"
            )
        return custom[name], name
    factory = BUILTIN_CONVERTERS.get(name)
    if factory is None:
        return None, name
    if name == "re":
        if not args_text:
            raise TemplateError("re(...) needs a pattern: {code:re([A-Z]{3})}", reason="bad_converter_args")
        try:
            regex = _RegexConverter(args_text)
            return regex, regex.spec
        except re.error as exc:
            raise TemplateError(
                f"re({args_text}) is not a valid regular expression: {exc}", reason="bad_pattern"
            ) from None
    kwargs = _parse_args(name, args_text) if args_text is not None else {}
    if kwargs and factory not in (_IntConverter, _SignedIntConverter, _FloatConverter):
        raise TemplateError(f"converter {name!r} takes no arguments ({spec!r})", reason="bad_converter_args")
    unknown = set(kwargs) - {"min", "max"}
    if unknown:
        raise TemplateError(
            f"{name}(...) accepts min= and max= only, got {', '.join(sorted(unknown))}",
            reason="bad_converter_args",
        )
    try:
        converter = factory(**kwargs)
    except ValueError as exc:
        raise TemplateError(str(exc), reason="bad_converter_args") from None
    return converter, converter.spec


def parse_segment(
    seg: str,
    custom: Mapping[str, PathConverter] | None = None,
    *,
    strict_names: bool = True,
) -> Segment:
    """Classify and parse one template segment.

    Unknown converter names raise :class:`TemplateError` (``reason=
    "unknown_converter"``) unless ``strict_names`` is false, in which case
    the segment parses with a placeholder (decoration-time syntax check).
    """
    if not (seg.startswith("{") and seg.endswith("}")):
        if "{" in seg or "}" in seg:
            raise TemplateError(f"malformed parameter segment {seg!r}", reason="bad_template")
        return Segment("static", seg)
    inner = seg[1:-1]
    if inner.startswith("*"):
        name = inner[1:]
        if ":" in name:
            raise TemplateError(f"wildcard {seg!r} takes no converter", reason="bad_template")
        if not _CONVERTER_NAME.fullmatch(name):
            raise TemplateError(f"invalid wildcard name {name!r} in {seg!r}", reason="bad_template")
        return Segment("wildcard", name, REMAINDER_CONVERTER)
    name, sep, spec = inner.partition(":")
    if not _CONVERTER_NAME.fullmatch(name):
        raise TemplateError(f"invalid parameter name {name!r} in {seg!r}", reason="bad_template")
    if not sep:
        return Segment("param", name, STR_CONVERTER)
    spec = spec.strip()
    if not spec:
        raise TemplateError(f"empty converter in {seg!r}", reason="bad_template")
    if spec == "path":
        return Segment("wildcard", name, REMAINDER_CONVERTER)
    converter, canonical = _build(spec, custom)
    if converter is None:
        if strict_names:
            known = sorted({*BUILTIN_CONVERTERS, "path", *(custom or {})})
            raise TemplateError(
                f"unknown path converter {canonical!r} in {seg!r}; known converters are "
                f"{', '.join(known)} (register custom ones with RoutingConfig(converters=...))",
                reason="unknown_converter",
            )
        return Segment("param", name, STR_CONVERTER, canonical)
    return Segment("param", name, converter, canonical)


def split_template(path: str, *, keep_trailing_slash: bool) -> list[str]:
    """Split a route template (or a request path) into raw segments."""
    path = path.strip()
    if not path.startswith("/"):
        path = "/" + path
    if not keep_trailing_slash and len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    if path == "/":
        return []
    return path[1:].split("/")


def parse_template(
    path: str,
    custom: Mapping[str, PathConverter] | None = None,
    *,
    keep_trailing_slash: bool = False,
    strict_names: bool = True,
) -> list[Segment]:
    """Parse a whole template; wildcard placement and duplicate names are checked."""
    segments = [
        parse_segment(raw, custom, strict_names=strict_names)
        for raw in split_template(path, keep_trailing_slash=keep_trailing_slash)
    ]
    seen: set[str] = set()
    for i, seg in enumerate(segments):
        if seg.kind == "static":
            continue
        if seg.kind == "wildcard" and i != len(segments) - 1:
            raise TemplateError(f"wildcard must be the last segment of {path!r}", reason="wildcard_not_last")
        if seg.text in seen:
            raise TemplateError(f"parameter {seg.text!r} appears twice in {path!r}", reason="duplicate_param")
        seen.add(seg.text)
    return segments


def check_template(path: str) -> None:
    """Decoration-time check: syntax and built-in converter arguments (custom names resolve at startup)."""
    parse_template(path, None, keep_trailing_slash=True, strict_names=False)


# ---------------------------------------------------------------------------
# Host patterns
# ---------------------------------------------------------------------------

_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9_-]*[a-z0-9])?", re.IGNORECASE | re.ASCII)


@dataclass(frozen=True, slots=True)
class HostPattern:
    """A parsed ``host=`` pattern: dot-separated literal and ``{param}`` labels."""

    source: str
    labels: tuple[Segment, ...]

    @property
    def is_exact(self) -> bool:
        return all(label.kind == "static" for label in self.labels)

    @property
    def canonical(self) -> str:
        return ".".join(label.template() for label in self.labels)

    @property
    def param_names(self) -> tuple[str, ...]:
        return tuple(label.text for label in self.labels if label.kind != "static")

    def match(self, labels: list[str]) -> dict[str, str] | None:
        """Host parameters when ``labels`` (a lower-cased host split on ``.``) match, else ``None``."""
        if len(labels) != len(self.labels):
            return None
        params: dict[str, str] = {}
        for want, got in zip(self.labels, labels):
            if want.kind == "static":
                if want.text != got:
                    return None
            else:
                assert want.converter is not None
                if _HOST_LABEL.fullmatch(got) is None or not want.converter.matches(got):
                    return None
                params[want.text] = got
        return params

    def openapi_url(self) -> str:
        return "//" + ".".join(label.format() for label in self.labels)

    def build(self, params: Mapping[str, Any]) -> str:
        """Format the host for ``params`` (URL generation seam)."""
        out: list[str] = []
        for label in self.labels:
            if label.kind == "static":
                out.append(label.text)
                continue
            assert label.converter is not None
            if label.text not in params:
                raise KeyError(f"missing host parameter {label.text!r}")
            text = label.converter.to_url(params[label.text])
            if _HOST_LABEL.fullmatch(text) is None or not label.converter.matches(text):
                raise ValueError(f"{params[label.text]!r} is not a valid {{{label.text}}} host label")
            out.append(text.lower())
        return ".".join(out)


def parse_host(
    pattern: str,
    custom: Mapping[str, PathConverter] | None = None,
    *,
    strict_names: bool = True,
) -> HostPattern:
    """Parse ``{tenant}.example.com``-style host patterns (case-insensitive, no port)."""
    if not isinstance(pattern, str) or not pattern.strip():
        raise TemplateError(f"host must be a non-empty string, got {pattern!r}", reason="bad_host")
    text = pattern.strip()
    labels: list[Segment] = []
    for raw in _split_host_labels(text):
        if not raw:
            raise TemplateError(f"host {pattern!r} has an empty label", reason="bad_host")
        if raw.startswith("{") and raw.endswith("}"):
            seg = parse_segment(raw, custom, strict_names=strict_names)
            if seg.kind == "wildcard":
                raise TemplateError(f"host {pattern!r}: wildcard labels are not supported", reason="bad_host")
            if not seg.text:
                raise TemplateError(f"host {pattern!r} has a parameter without a name", reason="bad_host")
            labels.append(seg)
        elif "{" in raw or "}" in raw or _HOST_LABEL.fullmatch(raw) is None:
            raise TemplateError(
                f"host {pattern!r}: label {raw!r} must be literal text or a whole {{param}} "
                "(a bare host name: no scheme, port, path or user info)",
                reason="bad_host",
            )
        else:
            labels.append(Segment("static", raw.lower()))
    names = [label.text for label in labels if label.kind != "static"]
    if len(set(names)) != len(names):
        raise TemplateError(f"host {pattern!r} repeats a parameter name", reason="bad_host")
    return HostPattern(text, tuple(labels))


def _split_host_labels(text: str) -> list[str]:
    """Split on dots outside ``{...}`` (a ``re(...)`` pattern may contain dots)."""
    out: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth = max(depth - 1, 0)
        if ch == "." and depth == 0:
            out.append("".join(current))
            current = []
        else:
            current.append(ch)
    out.append("".join(current))
    return out


def normalize_host(value: str) -> str:
    """Lower-case a ``Host`` header value and drop the port and a trailing dot."""
    host = value.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[: end + 1] if end != -1 else host
    colon = host.rfind(":")
    if colon != -1:
        host = host[:colon]
    return host.rstrip(".")


__all__ = [
    "BUILTIN_CONVERTERS",
    "HostPattern",
    "PathConverter",
    "REMAINDER_CONVERTER",
    "STR_CONVERTER",
    "Segment",
    "TemplateError",
    "build_converter",
    "check_template",
    "converter_rank",
    "is_builtin_converter",
    "normalize_host",
    "parse_host",
    "parse_segment",
    "parse_template",
    "split_template",
]
