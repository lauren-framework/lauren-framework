"""Multipart resource budgets and immutable compiled part rules."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from ..exceptions import StartupError


@dataclass(frozen=True)
class MultipartConfig:
    """Bound multipart parsing, in-memory fields and per-file spooling.

    Supply this through ``ContentConfig(multipart=...)``. Files exceeding
    ``spool_threshold`` move to the system temporary directory or ``temp_dir``.
    The request's total body limit remains in force.
    """

    max_parts: int = 1000
    max_fields: int = 1000
    max_files: int = 100
    max_field_size: int = 1024 * 1024
    max_part_header_bytes: int = 16 * 1024
    spool_threshold: int = 1024 * 1024
    temp_dir: str | os.PathLike[str] | None = None
    undeclared_parts: Literal["ignore", "reject"] = "ignore"


@dataclass(frozen=True)
class _UploadSpec:
    """Snapshot a field's upload constraints without retaining mutable metadata."""

    max_size: int | None = None
    content_types: tuple[str, ...] = ()
    sniff: bool | Callable[[bytes], str | None] = False
    max_files: int | None = None
    min_files: int = 0


@dataclass(frozen=True)
class _PartSpec:
    """Describe the wire kind and every binding's constraints for one part name."""

    kind: Literal["text", "file"]
    uploads: tuple[_UploadSpec, ...] = ()


def resolve_multipart(value: MultipartConfig | None) -> MultipartConfig:
    """Validate the app budget once, before any request reads a body."""
    if value is None:
        value = MultipartConfig()
    if not isinstance(value, MultipartConfig):
        raise StartupError(
            "ContentConfig.multipart must be a MultipartConfig instance.",
            detail={"reason": "invalid_multipart_config", "field": "multipart"},
        )
    for field in (
        "max_parts",
        "max_fields",
        "max_files",
        "max_field_size",
        "max_part_header_bytes",
        "spool_threshold",
    ):
        limit = getattr(value, field)
        if type(limit) is not int or limit <= 0:
            raise StartupError(
                f"MultipartConfig.{field} must be a positive integer.",
                detail={"reason": "invalid_multipart_config", "field": field},
            )
    if value.undeclared_parts not in ("ignore", "reject"):
        raise StartupError(
            "MultipartConfig.undeclared_parts must be 'ignore' or 'reject'.",
            detail={"reason": "invalid_multipart_config", "field": "undeclared_parts"},
        )
    if value.temp_dir is not None:
        try:
            directory = os.fspath(value.temp_dir)
            if not isinstance(directory, str):
                raise TypeError("expected a text path")
            directory = os.path.abspath(directory)
            valid = os.path.isdir(directory) and os.access(directory, os.W_OK | os.X_OK)
        except (TypeError, ValueError, OSError):
            valid = False
        if not valid:
            raise StartupError(
                "MultipartConfig.temp_dir must name an existing writable directory.",
                detail={"reason": "invalid_multipart_config", "field": "temp_dir"},
            )
        value = replace(value, temp_dir=directory)
    return value
