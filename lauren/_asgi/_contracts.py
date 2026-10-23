"""Response contracts: declared statuses, typed results and output enforcement (private).

Two layers, both built in Phase 5 of :meth:`LaurenFactory.create`:

* :func:`describe_responses` reads a route's declaration — ``status_code=``,
  ``response_model=``, the return annotation (typed results ``Ok`` /
  ``Created`` / ``Accepted`` / ``NoContent``, ``Annotated[T, Status(...)]``,
  unions of them, ``None``, ``Response``) — into a :class:`ResponseTable`
  (status → body type). Contradictions fail here with ``DecoratorUsageError``.
  The OpenAPI generator and the reflect readers consume the table.
* :func:`compile_response_plan` turns a table into a :class:`ResponsePlan`
  when the route needs runtime work: a declared status, typed results, or a
  ``response_validation`` mode of ``"filter"`` / ``"strict"`` (projectors from
  :mod:`lauren._schema._contract`). A route with none of these gets ``None``
  and keeps the plain coercion path, so it pays nothing.

At dispatch, :meth:`ResponsePlan.coerce` maps the handler's value to a
:class:`~lauren.types.Response`: it picks the status (tuple form, then typed
result, then the declared default), projects the body when the route enforces
a contract and serializes it with the route's encoder. A body that does not fit
raises :class:`ResponseContractViolation`, which the route boundary renders as a
500 ``response_validation_error`` (the body never carries the payload).
"""

from __future__ import annotations

import inspect
import types
import typing
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Callable, Union, get_args, get_origin

from .._schema._compiler import TypeCompiler, split_annotated, type_name
from .._schema._contract import Projector, build_projector
from .._schema._issues import ValidationIssue, _Invalid, make_issue
from ..decorators import RouteMeta
from ..exceptions import DecoratorUsageError, UnresolvableParameterError
from ..responses import RESULT_TYPES, Accepted, Created, NoContent, Status
from ..serialization import JSONEncoder
from ..types import Response

_NoneType = type(None)
_UNION_ORIGINS: tuple[Any, ...] = (Union, types.UnionType)

#: Statuses that may not carry content (RFC 9110 §15.3.5, §15.4.5).
_NO_CONTENT_STATUSES = frozenset({204, 304})


class _Marker:
    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return self.name


#: The response has no body (``None``, ``NoContent``).
NO_BODY: Any = _Marker("NO_BODY")
#: The handler returns a ``Response`` (subclass): passed through untouched.
OPAQUE: Any = _Marker("OPAQUE")
#: No usable annotation: the body is not described.
UNTYPED: Any = _Marker("UNTYPED")


@dataclass(frozen=True, slots=True)
class DeclaredResponse:
    """One documented success response of a route."""

    status: int
    #: A type annotation, or :data:`NO_BODY` / :data:`OPAQUE` / :data:`UNTYPED`.
    body: Any
    #: The typed-result class (``Created``…) or ``None`` for plain values.
    wrapper: type | None = None
    description: str | None = None

    @property
    def location(self) -> bool:
        """Whether the response documents a ``Location`` header (``Created`` / ``Accepted``)."""
        return self.wrapper in (Created, Accepted)


@dataclass(frozen=True, slots=True)
class ResponseTable:
    """A route's declared success responses, read once at startup."""

    #: Status for plain (non-wrapper) values.
    default_status: int
    #: Status when the handler returns ``None`` (``status_code=`` or 204).
    none_status: int
    #: Every documented success response, in declaration order.
    members: tuple[DeclaredResponse, ...]
    #: The member plain values map to (``None`` when only typed results are declared).
    bare: DeclaredResponse | None
    #: ``status_code=``, a typed result or ``Status(...)`` changes the runtime status.
    status_declared: bool
    #: Item type of a ``StreamingResponse[T]`` route.
    stream_item: Any = None

    @property
    def typed(self) -> bool:
        """Whether any member declares a body type the contract can enforce."""
        return any(m.body not in (NO_BODY, OPAQUE, UNTYPED) for m in self.members)


def _target(fn: Any) -> str:
    return str(getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn))


def _usage(fn: Any, reason: str, message: str) -> DecoratorUsageError:
    target = _target(fn)
    return DecoratorUsageError(
        f"{target}: {message}", detail={"decorator": "route", "target": target, "reason": reason}
    )


def _flatten_union(tp: Any) -> list[Any]:
    if get_origin(tp) in _UNION_ORIGINS:
        out: list[Any] = []
        for arg in get_args(tp):
            out.extend(_flatten_union(arg))
        return out
    return [tp]


def _check_status(fn: Any, code: Any) -> int:
    if isinstance(code, bool) or not isinstance(code, int) or not 100 <= code <= 599:
        raise _usage(fn, "invalid_status_code", f"Status({code!r}) must be an int between 100 and 599")
    return code


def _status_marker(fn: Any, metadata: tuple[Any, ...]) -> Status | None:
    markers = [m for m in metadata if isinstance(m, Status)]
    if len(markers) > 1:
        raise _usage(fn, "multiple_status", "an annotation may carry only one Status(...)")
    if markers:
        _check_status(fn, markers[0].code)
        return markers[0]
    return None


def _status_tuple_body(tp: Any) -> Any:
    """``tuple[T, int]`` / ``tuple[T, int, H]`` (the ``return body, 201`` form) → ``T``."""
    if get_origin(tp) is tuple:
        args = get_args(tp)
        if len(args) in (2, 3) and args[1] is int:
            return args[0]
    return None


def _phrase(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Response"


def describe_responses(
    rmeta: RouteMeta,
    return_hint: Any,
    *,
    fn: Any = None,
    streaming_item: Any = None,
) -> ResponseTable:
    """Read the success responses a route declares (raises ``DecoratorUsageError`` on contradictions)."""
    declared_status = rmeta.status_code
    if streaming_item is not None:
        status = declared_status or 200
        member = DeclaredResponse(status, streaming_item)
        return ResponseTable(
            status, declared_status or 204, (member,), member, declared_status is not None, streaming_item
        )

    untyped = return_hint is inspect.Parameter.empty
    wrappers: list[DeclaredResponse] = []
    bare_types: list[Any] = []
    bare_markers: list[Status | None] = []
    has_none = False
    opaque = False
    if not untyped:
        for arg in _flatten_union(return_hint):
            base, metadata = split_annotated(arg)
            marker = _status_marker(fn, metadata) if metadata else None
            other = tuple(m for m in metadata if not isinstance(m, Status))
            if base is None or base is _NoneType:
                has_none = True
                continue
            origin = get_origin(base) or base
            if isinstance(origin, type) and origin in RESULT_TYPES:
                wrapper_status = int(origin.status)  # type: ignore[attr-defined]
                if marker is not None and marker.code != wrapper_status:
                    raise _usage(
                        fn,
                        "status_conflict",
                        f"Status({marker.code}) contradicts {origin.__name__}, which sends {wrapper_status}",
                    )
                args = get_args(base)
                if origin is NoContent:
                    body: Any = NO_BODY
                else:
                    body = args[0] if args else UNTYPED
                wrappers.append(
                    DeclaredResponse(wrapper_status, body, origin, marker.description if marker else None)
                )
                continue
            if isinstance(origin, type) and issubclass(origin, Response):
                opaque = True
                continue
            if base is Any or base is object:
                bare_types.append(UNTYPED)
                bare_markers.append(marker)
                continue
            inner = _status_tuple_body(base)
            if inner is not None:
                base = inner
            bare_types.append(typing.Annotated[(base, *other)] if other else base)  # type: ignore[valid-type]
            bare_markers.append(marker)

    # ---- the plain-value member ---------------------------------------
    marker_codes = {m.code for m in bare_markers if m is not None}
    if declared_status is not None and marker_codes and declared_status not in marker_codes:
        raise _usage(
            fn,
            "status_conflict",
            f"status_code={declared_status} contradicts Status({sorted(marker_codes)[0]}) in the return annotation",
        )
    codes = set(marker_codes)
    if any(m is None for m in bare_markers):
        codes.add(declared_status or 200)
    if len(codes) > 1:
        raise _usage(
            fn,
            "ambiguous_status",
            f"plain return types declare several statuses ({', '.join(map(str, sorted(codes)))}); "
            "return typed results (Ok / Created / Accepted / NoContent) to send different statuses",
        )
    bare_status = codes.pop() if codes else (declared_status or 200)
    bare_description = next((m.description for m in bare_markers if m is not None and m.description), None)

    if rmeta.response_model is not None:
        model = rmeta.response_model
        wrappers = [
            DeclaredResponse(w.status, model, w.wrapper, w.description) if w.body is not NO_BODY else w
            for w in wrappers
        ]
        # ``response_model`` documents (and, under a contract, enforces) the
        # body of plain values — also for ``-> Response`` routes, whose
        # ``Response`` instances still pass through untouched.
        if bare_types or untyped or opaque or not (wrappers or has_none):
            bare_types = [model]

    members: list[DeclaredResponse] = []
    bare: DeclaredResponse | None = None
    if bare_types:
        if UNTYPED in bare_types:
            bare_body: Any = UNTYPED
        elif len(bare_types) == 1:
            bare_body = bare_types[0]
        else:
            bare_body = Union[tuple(bare_types)]  # type: ignore[valid-type]
        bare = DeclaredResponse(bare_status, bare_body, None, bare_description)
        members.append(bare)
    elif untyped:
        bare = DeclaredResponse(bare_status, UNTYPED)
        members.append(bare)
    elif opaque:
        bare = DeclaredResponse(bare_status, OPAQUE)
        members.append(bare)
    members.extend(wrappers)
    none_status = declared_status if declared_status is not None else 204
    if has_none and not any(m.status == none_status for m in members):
        members.append(DeclaredResponse(none_status, NO_BODY))

    # ---- consistency ---------------------------------------------------
    if declared_status is not None and wrappers and not bare_types and not untyped and not opaque:
        statuses = {w.status for w in wrappers}
        if not has_none and declared_status not in statuses:
            names = " | ".join(w.wrapper.__name__ for w in wrappers if w.wrapper is not None)
            raise _usage(
                fn,
                "status_conflict",
                f"status_code={declared_status} contradicts the return annotation {names}",
            )
    seen: dict[int, DeclaredResponse] = {}
    for member in members:
        previous = seen.get(member.status)
        if previous is not None:
            raise _usage(
                fn,
                "duplicate_status",
                f"the return annotation declares status {member.status} twice; "
                "use one member with a union body (Ok[A | B]) instead",
            )
        seen[member.status] = member
    for member in members:
        if member.status in _NO_CONTENT_STATUSES and member.body not in (NO_BODY, OPAQUE, UNTYPED):
            raise _usage(
                fn,
                "body_not_allowed",
                f"status {member.status} cannot carry a body, but the route declares "
                f"{type_name(member.body)}; annotate '-> None' or '-> NoContent'",
            )
    status_declared = declared_status is not None or bool(wrappers) or bool(marker_codes)
    return ResponseTable(
        default_status=bare_status if bare is not None else (declared_status or 200),
        none_status=none_status,
        members=tuple(members),
        bare=bare,
        status_declared=status_declared,
    )


# ---------------------------------------------------------------------------
# Runtime plan
# ---------------------------------------------------------------------------


class ResponseContractViolation(Exception):
    """A handler's value does not fit its declared response type (rendered as a 500)."""

    def __init__(self, issues: list[ValidationIssue], *, route: str, handler: str, mode: str) -> None:
        self.issues: tuple[ValidationIssue, ...] = tuple(issues)
        self.route = route
        self.handler = handler
        self.mode = mode
        head = "; ".join(str(issue) for issue in self.issues[:3])
        more = f" (+{len(self.issues) - 3} more)" if len(self.issues) > 3 else ""
        super().__init__(f"response of {handler} ({route}) violates its contract: {head}{more}")


Coerce = Callable[..., Response]


class ResponsePlan:
    """A route's compiled response contract (see the module docstring)."""

    __slots__ = (
        "table",
        "mode",
        "route",
        "handler",
        "default_status",
        "none_status",
        "_bare",
        "_reject_bare",
        "_wrappers",
        "_limit",
        "_coerce",
        "stream_projector",
    )

    def __init__(
        self,
        table: ResponseTable,
        *,
        mode: str,
        route: str,
        handler: str,
        bare: Projector | None,
        wrappers: dict[type, tuple[int, Projector | None]],
        limit: int,
        coerce: Coerce,
        stream_projector: Projector | None = None,
    ) -> None:
        self.table = table
        self.mode = mode
        self.route = route
        self.handler = handler
        self.default_status = table.default_status
        self.none_status = table.none_status
        self._bare = bare
        # Under a contract, a route that declares only typed results has no
        # type to project a plain value (or a ``(body, status)`` tuple) onto.
        self._reject_bare = mode != "off" and table.bare is None and table.stream_item is None
        self._wrappers = wrappers
        self._limit = limit
        self._coerce = coerce
        #: Projector applied to each item of a ``StreamingResponse[T]`` (``None``: off).
        self.stream_projector = stream_projector

    def _violation(self, issues: list[ValidationIssue], *loc: str | int) -> ResponseContractViolation:
        prefixed = [issue.prefixed("response", *loc) for issue in issues[: self._limit]]
        return ResponseContractViolation(prefixed, route=self.route, handler=self.handler, mode=self.mode)

    def project(self, projector: Projector, value: Any) -> Any:
        try:
            return projector(value)
        except _Invalid as exc:
            raise self._violation(exc.issues) from None

    def project_item(self, value: Any, index: int) -> Any:
        """Project one streamed item (only called when :attr:`stream_projector` is set)."""
        try:
            return self.stream_projector(value)  # type: ignore[misc]
        except _Invalid as exc:
            raise self._violation(exc.issues, "item", index) from None

    def coerce(self, raw: Any, encoder: JSONEncoder) -> Response:
        """Turn the handler's return value into the route's response."""
        if isinstance(raw, Response):
            return self._coerce(raw, encoder=encoder)
        headers: Any = None
        location: str | None = None
        projector: Projector | None
        if type(raw) is tuple and len(raw) in (2, 3) and isinstance(raw[1], int):
            body, status = raw[0], raw[1]
            headers = raw[2] if len(raw) == 3 else None
            projector = self._bare
        elif isinstance(raw, RESULT_TYPES):
            entry = self._wrappers.get(type(raw))
            if entry is None:
                if self.mode != "off":
                    issue = make_issue(
                        "value_error",
                        ctx={"error": f"the route does not declare {type(raw).__name__} results"},
                    )
                    raise self._violation([issue])
                return _coerce_result(raw, encoder=encoder, coerce=self._coerce)
            status, projector = entry
            body = None if isinstance(raw, NoContent) else raw.value  # type: ignore[attr-defined]
            headers = raw.headers  # type: ignore[attr-defined]
            location = getattr(raw, "location", None)
        elif raw is None:
            return Response(b"", status=self.none_status)
        else:
            body, status, projector = raw, self.default_status, self._bare
        if body is None:
            response = Response(b"", status=status)
        else:
            if projector is not None:
                body = self.project(projector, body)
            elif self._reject_bare and not isinstance(raw, RESULT_TYPES):
                issue = make_issue(
                    "value_error",
                    ctx={"error": "the route declares only typed results; return one of them"},
                )
                raise self._violation([issue])
            response = _body_response(body, status, encoder, self._coerce)
        if headers:
            response = response.with_headers(dict(headers))
        if location:
            response = response.with_header("location", location)
        return response


def _body_response(body: Any, status: int, encoder: JSONEncoder | None, coerce: Coerce) -> Response:
    if isinstance(body, (dict, list)):
        return Response.json(body, status=status, encoder=encoder)
    response = coerce(body, encoder=encoder)
    return response if response.status == status else response.with_status(status)


def _coerce_result(value: Any, *, encoder: JSONEncoder | None, coerce: Coerce) -> Response:
    """A typed result on a route without a contract: status + headers, body coerced as usual."""
    status = int(type(value).status)
    body = None if isinstance(value, NoContent) else value.value
    response = Response(b"", status=status) if body is None else _body_response(body, status, encoder, coerce)
    if value.headers:
        response = response.with_headers(dict(value.headers))
    location = getattr(value, "location", None)
    if location:
        response = response.with_header("location", location)
    return response


def _compile_projector(
    body: Any,
    *,
    strict: bool,
    compiler: TypeCompiler,
    controller_cls: type,
    fn: Any,
    mode: str,
) -> Projector:
    handler = getattr(fn, "__name__", repr(fn))
    try:
        compiled = compiler.compile(body, mode="strict" if strict else "lax")
    except UnresolvableParameterError as exc:
        reason = (exc.detail or {}).get("reason")
        raise UnresolvableParameterError(
            f"{controller_cls.__name__}.{handler}: cannot enforce response_validation={mode!r} on the "
            f"declared response type {type_name(body)}: {exc.message}",
            detail={
                "class": controller_cls.__name__,
                "handler": handler,
                "param": "<return>",
                "reason": "unsupported_response_type",
                "type": type_name(body),
                "cause": reason,
            },
        ) from None
    return build_projector(compiled.node, strict=strict, limit=compiler.max_issues)


def compile_response_plan(
    table: ResponseTable,
    *,
    mode: str,
    compiler: TypeCompiler,
    coerce: Coerce,
    controller_cls: type,
    fn: Any,
    route: str,
) -> ResponsePlan | None:
    """The runtime plan for ``table`` under ``mode`` (``None`` when the plain path is equivalent)."""
    if not table.typed:
        mode = "off"
    if mode == "off" and not table.status_declared:
        return None
    strict = mode == "strict"
    cache: dict[Any, Projector] = {}

    def projector_for(body: Any) -> Projector | None:
        if mode == "off" or body in (NO_BODY, OPAQUE, UNTYPED):
            return None
        key = id(body)
        built = cache.get(key)
        if built is None:
            built = cache[key] = _compile_projector(
                body, strict=strict, compiler=compiler, controller_cls=controller_cls, fn=fn, mode=mode
            )
        return built

    handler = f"{controller_cls.__name__}.{getattr(fn, '__name__', repr(fn))}"
    if table.stream_item is not None:
        return ResponsePlan(
            table,
            mode=mode,
            route=route,
            handler=handler,
            bare=None,
            wrappers={},
            limit=compiler.max_issues,
            coerce=coerce,
            stream_projector=projector_for(table.stream_item),
        )
    wrappers = {m.wrapper: (m.status, projector_for(m.body)) for m in table.members if m.wrapper is not None}
    bare = projector_for(table.bare.body) if table.bare is not None else None
    return ResponsePlan(
        table,
        mode=mode,
        route=route,
        handler=handler,
        bare=bare,
        wrappers=wrappers,
        limit=compiler.max_issues,
        coerce=coerce,
    )


def describe_status(status: int) -> str:
    """OpenAPI description for a success status without one (``"Success"`` for 200)."""
    return "Success" if status == 200 else _phrase(status)


__all__ = [
    "NO_BODY",
    "OPAQUE",
    "UNTYPED",
    "DeclaredResponse",
    "ResponseContractViolation",
    "ResponsePlan",
    "ResponseTable",
    "compile_response_plan",
    "describe_responses",
    "describe_status",
]
