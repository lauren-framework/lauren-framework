"""Private pagination runtime.

Public symbols are re-exported through :mod:`lauren.pagination` (and the
top-level ``lauren`` namespace). Layout:

* ``_config`` — :class:`PaginationConfig` and its startup resolution;
* ``_params`` — the per-request paging values and their compiled plans
  (the ``"pagination"`` extraction source);
* ``_cursor`` — the signed, versioned cursor codec (shared signer:
  :mod:`lauren._signing`);
* ``_links`` — request snapshots and RFC 8288 link URLs;
* ``_page`` — ``Page[T]`` / ``CursorPage[T]``, the row helpers and rendering;
* ``_openapi`` — the OpenAPI contribution (parameters, page components).
"""

from __future__ import annotations

from ._config import PaginationConfig, ResolvedPagination, resolve_app
from ._page import CursorPage, Page, PageResponse, paginate_sequence
from ._params import (
    CursorPagination,
    OffsetPagination,
    PageNumberPagination,
    PaginationPlan,
    build_plan,
    is_paging_annotation,
)

__all__ = [
    "CursorPage",
    "CursorPagination",
    "OffsetPagination",
    "Page",
    "PageNumberPagination",
    "PageResponse",
    "PaginationConfig",
    "PaginationPlan",
    "ResolvedPagination",
    "build_plan",
    "is_paging_annotation",
    "paginate_sequence",
    "resolve_app",
]
