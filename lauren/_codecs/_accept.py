"""The ``Accept`` header: RFC 9110 §12.5.1 parsing and server-driven negotiation (private).

One parser serves both response negotiation (``produces=``) and typed-stream
format selection (:func:`lauren.streaming.negotiate_stream_format`).

* :func:`parse_accept` never raises: malformed members (no ``/``, an invalid
  token, ``*/subtype``, a bad ``q``) are skipped.
* An offer's quality is the ``q`` of the **most specific** range that matches
  it (``type/subtype`` › ``type/*`` › ``*/*``; equally specific ranges take
  the highest ``q``). Media-type parameters other than ``q`` are ignored, so
  ``application/json; charset=utf-8`` matches ``application/json``.
* ``q=0`` means "not acceptable"; such an offer is never chosen.
* Ties are broken by specificity, then by the server's order (``produces``),
  or — for stream formats — by the client's order first.
* A missing, empty or entirely malformed header accepts anything: the first
  offer wins.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

#: Media type name characters (RFC 9110 ``token`` without ``*``).
_NAME = re.compile(r"^[!#$%&'+.^_`|~0-9a-z-]+$")
#: RFC 9110 ``qvalue``: ``0`` / ``0.xyz`` / ``1`` / ``1.000``.
_QVALUE = re.compile(r"^(?:0(?:\.[0-9]{0,3})?|1(?:\.0{0,3})?)$")


def _split_quoted(value: str, delimiter: str) -> list[str]:
    """Split a header on a separator outside quoted parameter values.

    RFC 9110 permits quoted parameters containing commas or semicolons; a
    simple ``str.split`` would move the member's ``q`` to the wrong range.
    """
    parts: list[str] = []
    start = 0
    quoted = False
    escaped = False
    for index, char in enumerate(value):
        if escaped:
            escaped = False
        elif quoted and char == "\\":
            escaped = True
        elif char == '"':
            quoted = not quoted
        elif char == delimiter and not quoted:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


@dataclass(frozen=True, slots=True)
class MediaRange:
    """One member of an ``Accept`` header."""

    type: str
    subtype: str
    q: float
    #: Position in the header (client order).
    index: int

    @property
    def specificity(self) -> int:
        """``2`` for ``type/subtype``, ``1`` for ``type/*``, ``0`` for ``*/*``."""
        if self.type == "*":
            return 0
        return 1 if self.subtype == "*" else 2

    def matches(self, media: str) -> bool:
        """Whether this range covers the concrete, lower-cased ``media``."""
        if self.type == "*":
            return True
        mtype, _, msub = media.partition("/")
        return mtype == self.type and (self.subtype == "*" or msub == self.subtype)


def _valid_range(type_: str, subtype: str) -> bool:
    if type_ == "*":
        return subtype == "*"
    return bool(_NAME.match(type_)) and (subtype == "*" or bool(_NAME.match(subtype)))


def parse_accept(header: str | None) -> tuple[MediaRange, ...]:
    """Parse an ``Accept`` header into its valid media ranges, in header order."""
    if not header:
        return ()
    ranges: list[MediaRange] = []
    for index, member in enumerate(_split_quoted(header, ",")):
        parts = _split_quoted(member, ";")
        type_, sep, subtype = parts[0].strip().lower().partition("/")
        if not sep or not _valid_range(type_, subtype):
            continue
        q = 1.0
        valid = True
        for param in parts[1:]:
            name, _, value = param.partition("=")
            if name.strip().lower() == "q":
                text = value.strip()
                if not _QVALUE.match(text):
                    valid = False
                else:
                    q = float(text)
                break  # parameters after ``q`` are accept-extensions
        if valid:
            ranges.append(MediaRange(type_, subtype, q, index))
    return tuple(ranges)


def _quality(ranges: Sequence[MediaRange], media: str) -> tuple[int, float, int] | None:
    """``(specificity, q, index)`` of the most specific range matching ``media``."""
    best: tuple[int, float, int] | None = None
    for r in ranges:
        if not r.matches(media):
            continue
        spec = r.specificity
        if best is None or spec > best[0] or (spec == best[0] and r.q > best[1]):
            best = (spec, r.q, r.index)
    return best


def select(ranges: Sequence[MediaRange], offers: Sequence[str], *, client_order: bool = False) -> str | None:
    """Choose the best offer for already-parsed ``ranges`` (``None``: nothing acceptable).

    ``offers`` are concrete, lower-cased media types in server preference
    order. With no ranges every offer is acceptable and the first one wins.
    """
    if not offers:
        return None
    if not ranges:
        return offers[0]
    chosen: str | None = None
    chosen_key: tuple[float, int, int, int] | None = None
    for position, offer in enumerate(offers):
        found = _quality(ranges, offer)
        if found is None:
            continue
        spec, q, index = found
        if q <= 0:
            continue
        key = (q, spec, -index if client_order else 0, -position)
        if chosen_key is None or key > chosen_key:
            chosen, chosen_key = offer, key
    return chosen


def negotiate(header: str | None, offers: Sequence[str], *, client_order: bool = False) -> str | None:
    """Pick the offer the ``Accept`` header prefers (``None``: nothing acceptable)."""
    return select(parse_accept(header), offers, client_order=client_order)


__all__ = ["MediaRange", "negotiate", "parse_accept", "select"]
