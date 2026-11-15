"""Hypothesis properties of ``Accept`` negotiation (PRD F03 §10.8).

The chosen media type is always one of the offers (or ``None``), never one
the header rules out with ``q=0``; raising the quality of the chosen type's
exact range never changes the choice; the parser never raises.
"""

from __future__ import annotations

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren._codecs._accept import MediaRange, negotiate, parse_accept, select  # noqa: E402

_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow])

TYPES = ["application/json", "application/msgpack", "text/html", "text/plain", "image/png"]
_media = st.sampled_from(TYPES)
_offers = st.lists(_media, min_size=1, max_size=4, unique=True)
_q = st.sampled_from(["0", "0.1", "0.5", "0.999", "1", "1.000"])
_range = st.one_of(_media, st.just("*/*"), st.sampled_from(["application/*", "text/*", "image/*"]))
_member = st.builds(lambda r, q: f"{r};q={q}" if q is not None else r, _range, st.one_of(st.none(), _q))
_header = st.lists(_member, min_size=1, max_size=6).map(", ".join)


def _quality(ranges: tuple[MediaRange, ...], media: str) -> float | None:
    matching = [r for r in ranges if r.matches(media)]
    if not matching:
        return None
    best = max(r.specificity for r in matching)
    return max(r.q for r in matching if r.specificity == best)


@_TOLERANT
@given(header=_header, offers=_offers)
def test_the_choice_is_an_acceptable_offer(header: str, offers: list[str]) -> None:
    chosen = negotiate(header, offers)

    if chosen is not None:
        assert chosen in offers
        quality = _quality(parse_accept(header), chosen)
        assert quality is not None and quality > 0


@_TOLERANT
@given(header=_header, offers=_offers)
def test_nothing_is_chosen_only_when_every_offer_is_excluded(header: str, offers: list[str]) -> None:
    ranges = parse_accept(header)
    acceptable = [o for o in offers if (_quality(ranges, o) or 0) > 0]

    assert (negotiate(header, offers) is None) == (not acceptable)


@_TOLERANT
@given(header=_header, offers=_offers)
def test_raising_the_chosen_types_quality_keeps_the_choice(header: str, offers: list[str]) -> None:
    chosen = negotiate(header, offers)
    hypothesis.assume(chosen is not None)

    raised = parse_accept(f"{header}, {chosen};q=1")

    assert select(raised, offers) == chosen


@_TOLERANT
@given(header=st.text(max_size=200))
def test_the_parser_never_raises(header: str) -> None:
    ranges = parse_accept(header)

    assert all(0.0 <= r.q <= 1.0 for r in ranges)
    negotiate(header, ["application/json"])
