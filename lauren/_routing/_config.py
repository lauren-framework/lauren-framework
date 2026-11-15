"""``RoutingConfig`` — the router's HTTP semantics, validated when built (private)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from ..exceptions import StartupError
from ._converters import BUILTIN_CONVERTERS, PathConverter

#: ``RoutingConfig.trailing_slash`` values.
TRAILING_SLASH_POLICIES: tuple[str, ...] = ("ignore", "strict", "redirect")

_CONVERTER_METHODS = ("matches", "to_python", "to_url", "openapi_schema")


def _invalid(message: str, **detail: Any) -> StartupError:
    return StartupError(message, detail={"reason": "invalid_routing_config", **detail})


@dataclass(frozen=True)
class RoutingConfig:
    """How the router answers HTTP: implicit methods, path policies and converters.

    Pass it as ``LaurenFactory.create(..., routing=RoutingConfig(...))`` (or
    ``Lauren(routing=...)``). Every value is validated when the object is
    built; a bad one raises :class:`~lauren.exceptions.StartupError` with
    ``detail["reason"] == "invalid_routing_config"``.

    * ``auto_head`` (default ``True``) — a path with a ``GET`` handler and no
      explicit ``@head`` answers ``HEAD`` by running the ``GET`` handler
      (``request.method`` stays ``"HEAD"``); the body is never sent. ``False``
      restores 405 for such requests.
    * ``auto_options`` (default ``False``) — an ``OPTIONS`` request to a path
      that has routes but no ``@options`` handler gets ``204`` with ``Allow``.
      Global middlewares run first, so a CORS middleware still answers
      preflights.
    * ``trailing_slash`` — ``"ignore"`` (default: ``/x`` and ``/x/`` are the
      same route), ``"strict"`` (distinct templates; the other spelling is a
      404) or ``"redirect"`` (the declared spelling is canonical; the other
      one gets a 308 to it, query string and root path kept).
    * ``merge_slashes`` — collapse runs of ``/`` before matching (with
      ``"redirect"``: redirect to the merged path).
    * ``match_raw_path`` — match on the raw request path (``scope["raw_path"]``)
      and percent-decode each segment, so ``/files/a%2Fb`` reaches ``{name}``
      as ``"a/b"``; malformed encoding is a 400.
    * ``converters`` — custom :class:`~lauren.routing.PathConverter` instances
      by template name (``{"isbn": IsbnConverter()}``).
    * ``infer_converters`` — an untyped ``{name}`` whose handler parameter is
      annotated ``int`` / ``uuid.UUID`` routes as ``{name:sint}`` /
      ``{name:uuid}``, so a malformed value is a 404 instead of a 422.
    """

    auto_head: bool = True
    auto_options: bool = False
    trailing_slash: str = "ignore"
    merge_slashes: bool = False
    match_raw_path: bool = False
    converters: Mapping[str, PathConverter] = field(default_factory=dict)
    infer_converters: bool = False

    def __post_init__(self) -> None:
        for name in ("auto_head", "auto_options", "merge_slashes", "match_raw_path", "infer_converters"):
            value = getattr(self, name)
            if not isinstance(value, bool):
                raise _invalid(f"RoutingConfig.{name} must be a bool, got {value!r}", field=name)
        if self.trailing_slash not in TRAILING_SLASH_POLICIES:
            raise _invalid(
                f"RoutingConfig.trailing_slash must be one of "
                f"{', '.join(map(repr, TRAILING_SLASH_POLICIES))}, got {self.trailing_slash!r}",
                field="trailing_slash",
            )
        if not isinstance(self.converters, Mapping):
            raise _invalid(
                f"RoutingConfig.converters must map names to converters, got {type(self.converters).__name__}",
                field="converters",
            )
        for name, converter in self.converters.items():
            _check_converter(name, converter)
        object.__setattr__(self, "converters", MappingProxyType(dict(self.converters)))


def _check_converter(name: Any, converter: Any) -> None:
    if not isinstance(name, str) or not name.isidentifier():
        raise _invalid(
            f"RoutingConfig.converters: {name!r} is not a valid converter name", field="converters"
        )
    if name in BUILTIN_CONVERTERS or name == "path":
        raise _invalid(
            f"RoutingConfig.converters: {name!r} is a built-in converter name; pick another",
            field="converters",
            converter=name,
        )
    if isinstance(converter, type):
        raise _invalid(
            f"RoutingConfig.converters[{name!r}] is a class; register an instance ({converter.__name__}())",
            field="converters",
            converter=name,
        )
    missing = [attr for attr in _CONVERTER_METHODS if not callable(getattr(converter, attr, None))]
    python_type = getattr(converter, "python_type", None)
    if missing or not isinstance(python_type, type):
        what = f"methods {', '.join(missing)}" if missing else "a python_type class attribute"
        raise _invalid(
            f"RoutingConfig.converters[{name!r}] is not a path converter (missing {what}); "
            "subclass lauren.routing.PathConverter",
            field="converters",
            converter=name,
        )


__all__ = ["RoutingConfig", "TRAILING_SLASH_POLICIES"]
