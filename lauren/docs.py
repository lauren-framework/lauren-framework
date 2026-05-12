"""AI-ingestible documentation access.

This module exposes the bundled ``llms.txt`` and ``llms-full.txt`` files so
AI agents (and humans) can discover the framework's API without external
network access.

Usage::

    from lauren import docs
    summary = docs.llms_txt()              # short overview
    reference = docs.llms_full_txt()       # complete reference

The same files are also available on the package root so tooling that
understands the ``llms.txt`` convention (https://llmstxt.org) can locate
them via standard filesystem discovery.
"""

from __future__ import annotations

from importlib import resources


def llms_txt() -> str:
    """Return the short, high-level ``llms.txt`` overview."""
    return resources.files(__package__).joinpath("llms.txt").read_text(encoding="utf-8")


def llms_full_txt() -> str:
    """Return the complete ``llms-full.txt`` reference.

    This is the preferred document for AI coding assistants to ingest: it
    covers every public API, idiomatic usage patterns, and common
    anti-patterns.
    """
    return resources.files(__package__).joinpath("llms-full.txt").read_text(encoding="utf-8")


__all__ = ["llms_txt", "llms_full_txt"]
