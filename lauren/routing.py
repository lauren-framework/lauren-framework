"""Router HTTP semantics: :class:`RoutingConfig` and typed path converters.

``RoutingConfig`` groups how the router answers HTTP — implicit ``HEAD`` /
``OPTIONS``, the trailing-slash policy, slash merging, raw-path matching and
custom converters — and is passed as ``LaurenFactory.create(...,
routing=RoutingConfig(...))``. Templates use ``{name:converter}`` segments::

    @get("/items/{id:int}")          # /items/42 → here; /items/abc → 404
    @get("/items/{slug:slug}")       # /items/blue-mug → here
    @get("/codes/{code:re([A-Z]{3})}")
    @get("/files/{rest:path}")       # same as {*rest}

Subclass :class:`PathConverter` for a custom converter and register an
instance with ``RoutingConfig(converters={"isbn": IsbnConverter()})``.
"""

from __future__ import annotations

from ._routing._config import TRAILING_SLASH_POLICIES, RoutingConfig
from ._routing._converters import BUILTIN_CONVERTERS, PathConverter

__all__ = ["BUILTIN_CONVERTERS", "PathConverter", "RoutingConfig", "TRAILING_SLASH_POLICIES"]
