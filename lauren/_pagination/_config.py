"""``PaginationConfig`` and its startup resolution (private).

A :class:`PaginationConfig` is a *partial* declaration: every field defaults
to ``None``, meaning "inherit". ``LaurenFactory.create(pagination=...)`` sets
the app layer and ``Annotated[OffsetPagination, PaginationConfig(...)]`` adds
a per-parameter layer on top. :func:`resolve` merges the layers over the
built-in defaults and validates the result — at startup, once per parameter —
into an immutable :class:`ResolvedPagination` the request path only reads.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import Any, Literal

from .._config._secret import Secret
from .._signing import normalize_secrets
from ..exceptions import StartupError

#: One cursor signing secret: plain, or wrapped in :class:`lauren.config.Secret`.
_SecretValue = str | bytes | Secret[str] | Secret[bytes]

ENVELOPES: tuple[str, ...] = ("items", "data", "bare")
META_KEYS: tuple[str, ...] = ("snake", "camel")

#: Fields only the app layer may set: one envelope shape per app.
APP_ONLY_FIELDS: tuple[str, ...] = ("envelope", "meta_keys")


@dataclass(frozen=True, slots=True)
class PaginationConfig:
    """Pagination defaults, limits, parameter names and response shape.

    Pass it app-wide as ``LaurenFactory.create(..., pagination=PaginationConfig(...))``
    (or ``Lauren(pagination=...)``) and per parameter as
    ``Annotated[OffsetPagination, PaginationConfig(max_limit=500)]``. Every
    field left at ``None`` inherits: parameter → app → built-in default
    (``default_limit=20``, ``max_limit=100``, ``max_offset`` unbounded,
    ``limit`` / ``offset`` / ``page`` / ``per_page`` / ``cursor`` parameters,
    ``envelope="items"``, ``meta_keys="snake"``). ``envelope`` and
    ``meta_keys`` are app-wide only. ``default_limit > max_limit`` is an error
    unless the maximum comes from a more specific level, which lowers the
    inherited default with it. ``total_header`` defaults to on for the
    ``"bare"`` envelope and off otherwise. ``base_url`` (a trusted
    ``https://host`` origin) makes ``Link`` URLs absolute; they are relative
    otherwise. ``cursor_secret`` signs cursors (first key signs, all verify);
    ``cursor_max_age`` (seconds) makes them expire. Everything is validated
    by ``LaurenFactory.create``.
    """

    default_limit: int | None = None
    max_limit: int | None = None
    max_offset: int | None = None
    limit_param: str | None = None
    offset_param: str | None = None
    page_param: str | None = None
    per_page_param: str | None = None
    cursor_param: str | None = None
    envelope: Literal["items", "data", "bare"] | None = None
    meta_keys: Literal["snake", "camel"] | None = None
    total_header: bool | None = None
    base_url: str | None = None
    #: str / bytes, a :class:`lauren.config.Secret` of either, or a sequence of
    #: those (the first signs, all verify — key rotation). Never shown in repr.
    cursor_secret: _SecretValue | Sequence[_SecretValue] | None = field(default=None, repr=False)
    cursor_max_age: int | float | None = None


_BUILTIN: dict[str, Any] = {
    "default_limit": 20,
    "max_limit": 100,
    "max_offset": None,
    "limit_param": "limit",
    "offset_param": "offset",
    "page_param": "page",
    "per_page_param": "per_page",
    "cursor_param": "cursor",
    "envelope": "items",
    "meta_keys": "snake",
    "total_header": None,
    "base_url": None,
    "cursor_secret": None,
    "cursor_max_age": None,
}

_FIELD_NAMES: tuple[str, ...] = tuple(f.name for f in fields(PaginationConfig))


@dataclass(frozen=True, slots=True)
class ResolvedPagination:
    """The effective, validated configuration of one app or one parameter."""

    default_limit: int
    max_limit: int
    max_offset: int | None
    limit_param: str
    offset_param: str
    page_param: str
    per_page_param: str
    cursor_param: str
    envelope: str
    meta_keys: str
    total_header: bool
    base_url: str
    secrets: tuple[bytes, ...] = field(repr=False)
    cursor_max_age: float | None
    #: The merged ``PaginationConfig`` layers this was resolved from, kept so a
    #: parameter layer can be applied on top of the app layer.
    layers: tuple[PaginationConfig, ...] = field(repr=False, compare=False)


def _fail(name: str, value: Any, why: str, where: dict[str, Any]) -> StartupError:
    shown = "<secret>" if name == "cursor_secret" else repr(value)
    owner = ""
    if where.get("handler"):
        owner = f" on parameter {where.get('param')!r} of {where['handler']}"
    return StartupError(
        f"PaginationConfig({name}={shown}){owner} is invalid: {why}",
        detail={"reason": "invalid_pagination_config", "field": name, **where},
    )


def _positive_int(name: str, value: Any, where: dict[str, Any], *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _fail(name, value, f"expected an int >= {minimum}", where)
    return value


def resolve(layers: Sequence[PaginationConfig], *, where: dict[str, Any] | None = None) -> ResolvedPagination:
    """Merge ``layers`` (app first, then parameter) over the defaults and validate.

    Raises :class:`~lauren.exceptions.StartupError` with
    ``detail["reason"] == "invalid_pagination_config"`` and ``detail["field"]``
    naming the offending field.
    """
    where = dict(where or {})
    merged = dict(_BUILTIN)
    # Which layer set each field (-1: the built-in default).
    origin = dict.fromkeys(_FIELD_NAMES, -1)
    for index, layer in enumerate(layers):
        for name in _FIELD_NAMES:
            value = getattr(layer, name)
            if value is not None:
                merged[name] = value
                origin[name] = index

    default_limit = _positive_int("default_limit", merged["default_limit"], where, minimum=1)
    max_limit = _positive_int("max_limit", merged["max_limit"], where, minimum=1)
    if default_limit > max_limit:
        if origin["default_limit"] >= origin["max_limit"]:
            raise _fail(
                "default_limit",
                default_limit,
                f"default_limit must not exceed max_limit ({max_limit})",
                where,
            )
        # A more specific layer lowered the maximum below an inherited
        # default: the default follows the maximum.
        default_limit = max_limit
    max_offset = merged["max_offset"]
    if max_offset is not None:
        max_offset = _positive_int("max_offset", max_offset, where, minimum=0)

    names: dict[str, str] = {}
    for name in ("limit_param", "offset_param", "page_param", "per_page_param", "cursor_param"):
        value = merged[name]
        if not isinstance(value, str) or not value or value != value.strip():
            raise _fail(name, value, "expected a non-empty query parameter name", where)
        names[name] = value
    for first, second in (
        ("limit_param", "offset_param"),
        ("page_param", "per_page_param"),
        ("cursor_param", "limit_param"),
    ):
        if names[first] == names[second]:
            raise _fail(second, names[second], f"{first} and {second} must differ", where)

    envelope = merged["envelope"]
    if envelope not in ENVELOPES:
        raise _fail("envelope", envelope, f"expected one of {', '.join(map(repr, ENVELOPES))}", where)
    meta_keys = merged["meta_keys"]
    if meta_keys not in META_KEYS:
        raise _fail("meta_keys", meta_keys, f"expected one of {', '.join(map(repr, META_KEYS))}", where)
    total_header = merged["total_header"]
    if total_header is None:
        total_header = envelope == "bare"
    elif not isinstance(total_header, bool):
        raise _fail("total_header", total_header, "expected a bool or None", where)

    base_url = merged["base_url"]
    if base_url is None:
        base_url = ""
    elif (
        not isinstance(base_url, str)
        or not base_url.startswith(("http://", "https://"))
        or any(ch in base_url for ch in "?#<> \t\r\n")
    ):
        raise _fail("base_url", base_url, "expected an absolute http(s) URL without query or fragment", where)
    else:
        base_url = base_url.rstrip("/")

    try:
        secrets = normalize_secrets(merged["cursor_secret"])
    except TypeError:
        raise _fail(
            "cursor_secret", None, "expected str / bytes / Secret or a sequence of them", where
        ) from None
    max_age = merged["cursor_max_age"]
    if max_age is not None and (
        isinstance(max_age, bool)
        or not isinstance(max_age, (int, float))
        or not math.isfinite(max_age)
        or max_age <= 0
    ):
        raise _fail("cursor_max_age", max_age, "expected a positive number of seconds or None", where)

    return ResolvedPagination(
        default_limit=default_limit,
        max_limit=max_limit,
        max_offset=max_offset,
        limit_param=names["limit_param"],
        offset_param=names["offset_param"],
        page_param=names["page_param"],
        per_page_param=names["per_page_param"],
        cursor_param=names["cursor_param"],
        envelope=envelope,
        meta_keys=meta_keys,
        total_header=total_header,
        base_url=base_url,
        secrets=secrets,
        cursor_max_age=float(max_age) if max_age is not None else None,
        layers=tuple(layers),
    )


def resolve_app(config: Any) -> ResolvedPagination:
    """Resolve ``LaurenFactory.create(pagination=...)`` (``None`` → built-in defaults)."""
    if config is None:
        return resolve(())
    if not isinstance(config, PaginationConfig):
        raise StartupError(
            f"pagination= expects a PaginationConfig, got {type(config).__name__}",
            detail={"reason": "invalid_pagination_config", "got": type(config).__name__},
        )
    return resolve((config,), where={"layer": "app"})


def resolve_parameter(
    app: ResolvedPagination, extra: Sequence[PaginationConfig], where: dict[str, Any]
) -> ResolvedPagination:
    """The effective config of one parameter: its ``Annotated`` layers on top of the app's."""
    if not extra:
        return app
    for layer in extra:
        for name in APP_ONLY_FIELDS:
            if getattr(layer, name) is not None:
                raise _fail(
                    name,
                    getattr(layer, name),
                    "the response shape is app-wide; set it on LaurenFactory.create(pagination=...)",
                    where,
                )
    return resolve((*app.layers, *extra), where=where)


BUILTIN_DEFAULTS: ResolvedPagination = resolve(())
