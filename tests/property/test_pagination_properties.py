"""Hypothesis properties of pagination: partitions, link inverses, URL fidelity, cursors (F29).

Skipped automatically when hypothesis is not installed.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest

hypothesis = pytest.importorskip("hypothesis")

from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from lauren import OffsetPagination, Page, PageNumberPagination, paginate_sequence  # noqa: E402
from lauren._pagination._config import resolve  # noqa: E402
from lauren._pagination._cursor import CursorCodec  # noqa: E402
from lauren._pagination._links import RequestSnapshot, build_url  # noqa: E402
from lauren._pagination._page import page_links  # noqa: E402
from lauren._pagination._params import _Bound  # noqa: E402
from lauren.pagination import PaginationConfig  # noqa: E402

# Signing does real HMAC work; on a busy machine examples trip the deadline.
_TOLERANT = settings(deadline=None, suppress_health_check=[HealthCheck.too_slow], max_examples=150)

_BOUND = _Bound(resolve((PaginationConfig(),)), RequestSnapshot("/items", b""))


def _offset_of(url: str, name: str = "offset") -> int:
    return int(dict(parse_qsl(urlsplit(url).query))[name])


@_TOLERANT
@given(total=st.integers(min_value=0, max_value=300), limit=st.integers(min_value=1, max_value=60))
def test_walking_next_partitions_the_collection(total: int, limit: int) -> None:
    items = list(range(total))
    seen: list[int] = []
    offset: int | None = 0
    while offset is not None:
        paging = OffsetPagination(limit=limit, offset=offset)
        page = paginate_sequence(items, paging)
        seen.extend(page.items)
        rels = dict(page_links(page, paging, _BOUND))
        offset = _offset_of(rels["next"]) if "next" in rels else None

    assert seen == items


@_TOLERANT
@given(
    total=st.integers(min_value=1, max_value=300),
    limit=st.integers(min_value=1, max_value=60),
    index=st.integers(min_value=0, max_value=300),
)
def test_next_and_prev_links_are_inverses(total: int, limit: int, index: int) -> None:
    pages = (total + limit - 1) // limit
    offset = (index % pages) * limit
    paging = OffsetPagination(limit=limit, offset=offset)
    page = Page.of(list(range(offset, min(offset + limit, total))), paging, total=total)
    rels = dict(page_links(page, paging, _BOUND))

    if "next" in rels:
        following = OffsetPagination(limit=limit, offset=_offset_of(rels["next"]))
        next_page = Page.of(
            list(range(following.offset, min(following.offset + limit, total))), following, total=total
        )
        assert _offset_of(dict(page_links(next_page, following, _BOUND))["prev"]) == offset
    if "prev" in rels:
        before = OffsetPagination(limit=limit, offset=_offset_of(rels["prev"]))
        prev_page = Page.of(list(range(before.offset, before.offset + limit)), before, total=total)
        assert _offset_of(dict(page_links(prev_page, before, _BOUND))["next"]) == offset


@_TOLERANT
@given(
    total=st.integers(min_value=0, max_value=500),
    limit=st.integers(min_value=1, max_value=50),
    offset=st.integers(min_value=0, max_value=600),
)
def test_has_next_agrees_with_total(total: int, limit: int, offset: int) -> None:
    page = paginate_sequence(range(total), OffsetPagination(limit=limit, offset=offset))

    assert page.has_next == (offset + limit < total)
    assert len(page.items) == max(0, min(limit, total - offset))


@_TOLERANT
@given(total=st.integers(min_value=0, max_value=300), per_page=st.integers(min_value=1, max_value=40))
def test_page_numbers_partition_the_collection(total: int, per_page: int) -> None:
    items = list(range(total))
    seen: list[int] = []
    number = 1
    while True:
        page = paginate_sequence(items, PageNumberPagination(page=number, per_page=per_page))
        seen.extend(page.items)
        if not page.has_next:
            break
        number += 1

    assert seen == items


_query_text = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",), blacklist_characters="\x00"), max_size=12
)


@_TOLERANT
@given(
    pairs=st.lists(st.tuples(_query_text, _query_text), max_size=6),
    new_offset=st.integers(min_value=0, max_value=10**6),
)
def test_link_urls_preserve_unrelated_parameters(pairs: list[tuple[str, str]], new_offset: int) -> None:
    from urllib.parse import quote

    unrelated = [(k, v) for k, v in pairs if k and k not in ("limit", "offset")]
    raw = "&".join(f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in unrelated)
    snap = RequestSnapshot("/items", raw.encode("ascii"))

    url = build_url(snap, ("limit", "offset"), {"limit": "10", "offset": str(new_offset)})

    decoded = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    assert decoded == [*unrelated, ("limit", "10"), ("offset", str(new_offset))]


_keys = st.one_of(
    st.integers(min_value=-(10**15), max_value=10**15),
    st.text(max_size=40),
    st.lists(st.integers(min_value=0, max_value=1000), max_size=5),
    st.dictionaries(st.text(max_size=8), st.integers(min_value=0, max_value=1000), max_size=4),
)


@_TOLERANT
@given(key=_keys, direction=st.sampled_from(["next", "prev"]), secret=st.binary(min_size=1, max_size=32))
def test_cursor_round_trips_any_json_keyset(key: Any, direction: str, secret: bytes) -> None:
    codec = CursorCodec((secret,))

    assert codec.decode(codec.encode(key, direction)) == (key, direction)
