"""URL templates compiled once per route at startup (private).

A template (``"/users/{id}/files/{*rest}"``, the router's canonical form) is
split into literal chunks and parameter slots; building a URL is a join over
them. Literal segments are percent-encoded once, here.

Parameter values are rendered by the slot's ``to_url`` and then encoded per
segment (``quote(text, safe="")``; ``.`` and ``..`` are escaped so they are
never dot-segments); a wildcard value keeps its ``/`` separators.
:func:`formatter_for` is the seam typed path converters plug into: it maps a
parameter's converter spec (``"int"`` in ``{id:int}``) to the function that
renders a value. Templates have no typed converters yet, so every parameter
renders with :func:`~lauren._urls._url.format_value`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import quote

from ._url import PATH_SAFE, format_value

#: Renders one path-parameter value as (unencoded) text.
PathFormatter = Callable[[Any], str]


def formatter_for(converter: str | None) -> PathFormatter:
    """The ``to_url`` of a template parameter's converter (``None``: untyped)."""
    return format_value


def _segment(text: str) -> str:
    if text in (".", ".."):
        return text.replace(".", "%2E")
    return quote(text, safe="")


class _Slot:
    __slots__ = ("name", "wildcard", "converter", "to_url")

    def __init__(self, name: str, wildcard: bool, converter: str | None) -> None:
        self.name = name
        self.wildcard = wildcard
        self.converter = converter
        self.to_url = formatter_for(converter)


class UrlTemplate:
    """A route template compiled for URL building."""

    __slots__ = ("template", "_chunks", "_slots", "param_names")

    def __init__(self, template: str) -> None:
        self.template = template
        chunks: list[str] = []
        slots: list[_Slot] = []
        literal = ""
        stripped = template.strip("/")
        for segment in stripped.split("/") if stripped else ():
            literal += "/"
            if segment.startswith("{") and segment.endswith("}"):
                inner = segment[1:-1]
                wildcard = inner.startswith("*")
                name, _, converter = inner.lstrip("*").partition(":")
                if converter == "path":
                    wildcard, converter = True, ""
                chunks.append(literal)
                slots.append(_Slot(name, wildcard, converter or None))
                literal = ""
            else:
                literal += quote(segment, safe=PATH_SAFE)
        chunks.append(literal or ("" if slots else "/"))
        self._chunks = tuple(chunks)
        self._slots = tuple(slots)
        self.param_names: frozenset[str] = frozenset(slot.name for slot in slots)

    def expand(self, params: Mapping[str, Any], *, label: str) -> str:
        """Build the encoded path; ``ValueError`` on missing / unexpected / empty parameters."""
        missing = sorted(self.param_names.difference(params))
        unexpected = sorted(set(params).difference(self.param_names))
        if missing or unexpected:
            problems = []
            if missing:
                problems.append(f"missing {', '.join(map(repr, missing))}")
            if unexpected:
                problems.append(f"unexpected {', '.join(map(repr, unexpected))}")
            expected = ", ".join(map(repr, sorted(self.param_names))) or "none"
            raise ValueError(
                f"route {label!r} ({self.template}): {'; '.join(problems)} path parameter(s) "
                f"(expected: {expected})"
            )
        out = [self._chunks[0]]
        empty_tail = False
        for slot, chunk in zip(self._slots, self._chunks[1:]):
            value = params[slot.name]
            if value is None:
                raise ValueError(f"route {label!r}: path parameter {slot.name!r} is None")
            text = slot.to_url(value)
            if slot.wildcard:
                rendered = "/".join(_segment(part) for part in text.split("/"))
                empty_tail = not rendered and not chunk
            else:
                if not text:
                    raise ValueError(f"route {label!r}: path parameter {slot.name!r} must not be empty")
                rendered = _segment(text)
            out.append(rendered)
            out.append(chunk)
        path = "".join(out)
        if empty_tail and len(path) > 1:
            # An empty trailing wildcard: ``/files/`` is ``/files``.
            path = path[:-1]
        return path


__all__ = ["PathFormatter", "UrlTemplate", "formatter_for"]
