"""Pagination parameters: the per-request value types and their compiled plans (private).

A handler parameter annotated ``OffsetPagination``, ``PageNumberPagination``
or ``CursorPagination[C]`` (optionally inside ``Annotated[..., PaginationConfig(...)]``)
is a built-in extraction source (``"pagination"``). ``LaurenFactory.create``
resolves its configuration and compiles a :class:`PaginationPlan` once —
validators for every integer (``Ge`` / ``Le`` markers through the app's type
compiler), the cursor codec and the parameter names — so a request performs
only query lookups, integer validation and, with a cursor, one HMAC check.
"""

from __future__ import annotations

from typing import Annotated, Any, Generic, Literal, TypeVar, get_args, get_origin

from .._schema._issues import ValidationFailed, ValidationIssue, make_issue
from .._schema._markers import Ge, Le
from .._signing import normalize_secrets
from ..exceptions import BadRequestError, ExtractorFieldError, StartupError, UnresolvableParameterError
from ._config import PaginationConfig, ResolvedPagination, resolve_parameter
from ._cursor import DIRECTIONS, CursorCodec, InvalidCursor
from ._links import RequestSnapshot, snapshot

C = TypeVar("C")


def _check_int(name: str, value: Any, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} must be an int >= {minimum}, got {value!r}")
    return value


class _Bound:
    """What an in-request paging value carries for rendering: config + request snapshot."""

    __slots__ = ("config", "snap")

    def __init__(self, config: ResolvedPagination, snap: RequestSnapshot) -> None:
        self.config = config
        self.snap = snap


class OffsetPagination:
    """Offset / limit pagination parameters (``?limit=20&offset=40``).

    Declare ``paging: OffsetPagination`` on a handler; the framework reads and
    validates ``limit`` (1..``max_limit``, default ``default_limit``) and
    ``offset`` (0..``max_offset``, default 0) — a bad value is a 422 with
    ``loc: ["query", "limit"]``. Build the response with
    ``Page.of(rows, paging, total=...)``. Construct one directly
    (``OffsetPagination(limit=10, offset=0)``) to call service code in tests;
    such an instance renders without links.
    """

    __slots__ = ("_limit", "_offset", "_bound")

    def __init__(self, limit: int = 20, offset: int = 0) -> None:
        self._limit = _check_int("limit", limit, 1)
        self._offset = _check_int("offset", offset, 0)
        self._bound: _Bound | None = None

    @classmethod
    def _create(cls, limit: int, offset: int, bound: _Bound) -> OffsetPagination:
        obj = cls.__new__(cls)
        obj._limit, obj._offset, obj._bound = limit, offset, bound
        return obj

    @property
    def limit(self) -> int:
        """Maximum number of items on this page."""
        return self._limit

    @property
    def offset(self) -> int:
        """Number of items skipped before this page."""
        return self._offset

    def __repr__(self) -> str:
        return f"OffsetPagination(limit={self._limit}, offset={self._offset})"

    def __eq__(self, other: object) -> bool:
        if type(other) is not OffsetPagination:
            return NotImplemented
        return (self._limit, self._offset) == (other._limit, other._offset)

    def __hash__(self) -> int:
        return hash((OffsetPagination, self._limit, self._offset))


class PageNumberPagination:
    """Page-number pagination parameters (``?page=3&per_page=20``, 1-based).

    ``page`` is at least 1; ``per_page`` follows the ``default_limit`` /
    ``max_limit`` rules. ``limit`` and ``offset`` give the equivalent window
    (``offset == (page - 1) * per_page``), so the same repository call and
    ``Page.of(...)`` work for both styles. A page beyond ``max_offset`` is a
    422 on ``page``.
    """

    __slots__ = ("_page", "_per_page", "_bound")

    def __init__(self, page: int = 1, per_page: int = 20) -> None:
        self._page = _check_int("page", page, 1)
        self._per_page = _check_int("per_page", per_page, 1)
        self._bound: _Bound | None = None

    @classmethod
    def _create(cls, page: int, per_page: int, bound: _Bound) -> PageNumberPagination:
        obj = cls.__new__(cls)
        obj._page, obj._per_page, obj._bound = page, per_page, bound
        return obj

    @property
    def page(self) -> int:
        """The 1-based page number."""
        return self._page

    @property
    def per_page(self) -> int:
        """Items per page."""
        return self._per_page

    @property
    def limit(self) -> int:
        """Same as :attr:`per_page`."""
        return self._per_page

    @property
    def offset(self) -> int:
        """``(page - 1) * per_page``."""
        return (self._page - 1) * self._per_page

    def __repr__(self) -> str:
        return f"PageNumberPagination(page={self._page}, per_page={self._per_page})"

    def __eq__(self, other: object) -> bool:
        if type(other) is not PageNumberPagination:
            return NotImplemented
        return (self._page, self._per_page) == (other._page, other._per_page)

    def __hash__(self) -> int:
        return hash((PageNumberPagination, self._page, self._per_page))


class CursorPagination(Generic[C]):
    """Keyset (cursor) pagination parameters (``?cursor=<opaque>&limit=20``).

    ``CursorPagination[C]`` decodes the opaque, signed ``cursor`` back into a
    ``C`` keyset (a dataclass, ``TypedDict``, ``NamedTuple``, pydantic model,
    msgspec struct or scalar, validated by the native engine) — ``cursor`` is
    ``None`` on the first page. ``direction`` is ``"next"`` (fetch rows after
    the keyset, ascending) or ``"prev"`` (rows before it, descending). A
    tampered, malformed, expired or outdated cursor is a 400 with
    ``code="invalid_cursor"``. Fetch ``limit + 1`` rows and build the page with
    ``CursorPage.from_rows(rows, paging, key=...)``.

    Outside a request, ``CursorPagination(limit=10, secret="test-key")`` gives
    an instance that can sign cursors (for unit tests of service code).
    """

    __slots__ = ("_cursor", "_direction", "_limit", "_codec", "_bound")

    def __init__(
        self,
        cursor: C | None = None,
        *,
        limit: int = 20,
        direction: Literal["next", "prev"] = "next",
        secret: Any = None,
    ) -> None:
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be 'next' or 'prev', got {direction!r}")
        self._cursor = cursor
        self._direction: str = direction
        self._limit = _check_int("limit", limit, 1)
        secrets = normalize_secrets(secret)
        self._codec: CursorCodec | None = CursorCodec(secrets) if secrets else None
        self._bound: _Bound | None = None

    @classmethod
    def _create(
        cls, cursor: Any, direction: str, limit: int, codec: CursorCodec, bound: _Bound
    ) -> CursorPagination[Any]:
        obj = cls.__new__(cls)
        obj._cursor, obj._direction, obj._limit = cursor, direction, limit
        obj._codec, obj._bound = codec, bound
        return obj

    @property
    def cursor(self) -> C | None:
        """The decoded keyset, or ``None`` on the first page."""
        return self._cursor

    @property
    def direction(self) -> str:
        """``"next"`` or ``"prev"`` — which way the client is paging."""
        return self._direction

    @property
    def limit(self) -> int:
        """Maximum number of items on this page."""
        return self._limit

    def encode_cursor(self, key: C, *, direction: Literal["next", "prev"] = "next") -> str:
        """Sign ``key`` into an opaque cursor (what ``CursorPage.from_rows`` emits)."""
        if self._codec is None:
            raise ValueError(
                "this CursorPagination cannot sign cursors: it was built without a secret "
                "(pass secret=... outside a request)"
            )
        return self._codec.encode(key, direction)

    def __repr__(self) -> str:
        return (
            f"CursorPagination(cursor={self._cursor!r}, direction={self._direction!r}, limit={self._limit})"
        )


#: The value types a page can be rendered from.
OffsetLike = OffsetPagination | PageNumberPagination


# ---------------------------------------------------------------------------
# Startup: detection and plan compilation
# ---------------------------------------------------------------------------


def detect(annotation: Any) -> tuple[str, Any, tuple[PaginationConfig, ...]] | None:
    """Return ``(kind, cursor_type, config_layers)`` when ``annotation`` is a paging type.

    ``kind`` is ``"offset"``, ``"page"`` or ``"cursor"``; ``cursor_type`` is
    ``C`` of ``CursorPagination[C]`` (``Any`` when bare).
    """
    layers: list[PaginationConfig] = []
    base = annotation
    while get_origin(base) is Annotated:
        args = get_args(base)
        base = args[0]
        layers.extend(m for m in args[1:] if isinstance(m, PaginationConfig))
    if base is OffsetPagination:
        return "offset", None, tuple(layers)
    if base is PageNumberPagination:
        return "page", None, tuple(layers)
    if base is CursorPagination:
        return "cursor", Any, tuple(layers)
    if get_origin(base) is CursorPagination:
        return "cursor", get_args(base)[0], tuple(layers)
    return None


class PaginationPlan:
    """The compiled extraction of one pagination parameter (startup → request)."""

    __slots__ = (
        "kind",
        "config",
        "limit_validator",
        "offset_validator",
        "page_validator",
        "codec",
        "include_input",
        "cursor_type",
    )

    def __init__(
        self,
        kind: str,
        config: ResolvedPagination,
        *,
        limit_validator: Any,
        offset_validator: Any = None,
        page_validator: Any = None,
        codec: CursorCodec | None = None,
        include_input: bool = False,
        cursor_type: Any = None,
    ) -> None:
        self.kind = kind
        self.config = config
        self.limit_validator = limit_validator
        self.offset_validator = offset_validator
        self.page_validator = page_validator
        self.codec = codec
        self.include_input = include_input
        self.cursor_type = cursor_type

    @staticmethod
    def _read(
        query: dict[str, list[str]], name: str, validator: Any, default: int, issues: list[ValidationIssue]
    ) -> int:
        values = query.get(name)
        if not values:
            return default
        try:
            return int(validator.validate(values[0], loc=("query", name)))
        except ValidationFailed as exc:
            issues.extend(exc.issues)
            return default

    def _fail(self, issues: list[ValidationIssue]) -> ExtractorFieldError:
        return ExtractorFieldError.from_issues(issues, include_input=self.include_input)

    async def extract(self, request: Any, extraction: Any = None) -> Any:
        """Build the paging value for ``request`` (422 / 400 on bad input)."""
        cfg = self.config
        query = request.query_params
        issues: list[ValidationIssue] = []
        if self.kind == "cursor":
            assert self.codec is not None
            cursor: Any = None
            direction = "next"
            raw = query.get(cfg.cursor_param)
            if raw and raw[0]:
                try:
                    cursor, direction = self.codec.decode(raw[0])
                except InvalidCursor as exc:
                    raise BadRequestError(
                        f"invalid cursor in query parameter {cfg.cursor_param!r}",
                        code="invalid_cursor",
                        detail={"param": cfg.cursor_param, "reason": exc.reason},
                    ) from None
            limit = self._read(query, cfg.limit_param, self.limit_validator, cfg.default_limit, issues)
            if issues:
                raise self._fail(issues)
            bound = _Bound(cfg, snapshot(request))
            return CursorPagination._create(cursor, direction, limit, self.codec, bound)
        if self.kind == "offset":
            limit = self._read(query, cfg.limit_param, self.limit_validator, cfg.default_limit, issues)
            offset = self._read(query, cfg.offset_param, self.offset_validator, 0, issues)
            if issues:
                raise self._fail(issues)
            return OffsetPagination._create(limit, offset, _Bound(cfg, snapshot(request)))
        page = self._read(query, cfg.page_param, self.page_validator, 1, issues)
        per_page = self._read(query, cfg.per_page_param, self.limit_validator, cfg.default_limit, issues)
        if not issues and cfg.max_offset is not None and (page - 1) * per_page > cfg.max_offset:
            max_page = cfg.max_offset // per_page + 1
            issues.append(
                make_issue("less_than_equal", page, {"le": max_page}, loc=("query", cfg.page_param))
            )
        if issues:
            raise self._fail(issues)
        return PageNumberPagination._create(page, per_page, _Bound(cfg, snapshot(request)))


def build_plan(
    annotation: Any,
    app_config: ResolvedPagination,
    compiler: Any,
    *,
    include_input: bool,
    where: dict[str, Any],
) -> PaginationPlan | None:
    """Compile the plan for a paging parameter, or ``None`` when ``annotation`` is not one.

    Raises ``StartupError`` (``reason="invalid_pagination_config"`` or
    ``"pagination_secret_missing"``) and ``UnresolvableParameterError`` (a
    cursor type the validation engine cannot compile).
    """
    detected = detect(annotation)
    if detected is None:
        return None
    kind, cursor_type, layers = detected
    config = resolve_parameter(app_config, layers, where)
    limit_validator = compiler.compile(Annotated[int, Ge(1), Le(config.max_limit)], mode="lax")
    if kind == "offset":
        offset_type: Any = (
            Annotated[int, Ge(0)]
            if config.max_offset is None
            else Annotated[int, Ge(0), Le(config.max_offset)]
        )
        return PaginationPlan(
            kind,
            config,
            limit_validator=limit_validator,
            offset_validator=compiler.compile(offset_type, mode="lax"),
            include_input=include_input,
        )
    if kind == "page":
        return PaginationPlan(
            kind,
            config,
            limit_validator=limit_validator,
            page_validator=compiler.compile(Annotated[int, Ge(1)], mode="lax"),
            include_input=include_input,
        )
    if not config.secrets:
        raise StartupError(
            f"{where.get('route', 'a route')} uses CursorPagination but no cursor secret is configured; "
            "pass LaurenFactory.create(..., pagination=PaginationConfig(cursor_secret=...)) — "
            "a dedicated random key, never the session secret",
            detail={"reason": "pagination_secret_missing", **where},
        )
    try:
        compiled = compiler.compile(cursor_type, mode="lax")
    except UnresolvableParameterError as exc:
        raise UnresolvableParameterError(
            f"CursorPagination[{getattr(cursor_type, '__name__', cursor_type)}] on parameter "
            f"{where.get('param')!r} of {where.get('handler')}: the cursor type is not supported "
            f"({exc.message})",
            detail={**where, "type": str(exc.detail.get("type")), "reason": "unsupported_cursor_type"},
        ) from None
    codec = CursorCodec(
        config.secrets,
        max_age=config.cursor_max_age,
        dump=compiled.project,
        load=compiled.validate,
    )
    return PaginationPlan(
        kind,
        config,
        limit_validator=limit_validator,
        codec=codec,
        include_input=include_input,
        cursor_type=cursor_type,
    )


def is_paging_annotation(annotation: Any) -> bool:
    """``True`` for ``OffsetPagination`` / ``PageNumberPagination`` / ``CursorPagination[...]``."""
    return detect(annotation) is not None


__all__ = [
    "CursorPagination",
    "OffsetPagination",
    "PageNumberPagination",
    "PaginationPlan",
    "build_plan",
    "detect",
    "is_paging_annotation",
]
