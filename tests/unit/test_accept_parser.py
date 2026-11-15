"""The shared RFC 9110 ``Accept`` parser and negotiation (F03 §5.6)."""

from __future__ import annotations

import pytest

from lauren._codecs._accept import MediaRange, negotiate, parse_accept, select
from lauren.streaming import negotiate_stream_format

JSON = "application/json"
MSGPACK = "application/msgpack"
OFFERS = (JSON, MSGPACK)


def test_parse_reads_type_subtype_q_and_position() -> None:
    ranges = parse_accept("Application/JSON;q=0.5, */*;q=0.1, text/*")

    assert ranges == (
        MediaRange("application", "json", 0.5, 0),
        MediaRange("*", "*", 0.1, 1),
        MediaRange("text", "*", 1.0, 2),
    )
    assert [r.specificity for r in ranges] == [2, 0, 1]


@pytest.mark.parametrize(
    "header",
    [
        "",
        "garbage",
        "*/json",
        "application/",
        "/json",
        "a b/c",
        "text/html;q=2",
        "text/html;q=.5",
        "x/y;q=abc",
    ],
)
def test_malformed_members_are_ignored(header: str) -> None:
    assert parse_accept(header) == ()


def test_parameters_after_q_are_accept_extensions() -> None:
    (only,) = parse_accept("text/html;level=1;q=0.4;ext=whatever")

    assert (only.type, only.subtype, only.q) == ("text", "html", 0.4)


def test_quoted_commas_semicolons_and_escapes_do_not_break_quality_values() -> None:
    header = 'application/json; profile="a,b;c\\"d"; q=0.3, application/msgpack;q=0.9'

    ranges = parse_accept(header)

    assert [(r.type, r.subtype, r.q) for r in ranges] == [
        ("application", "json", 0.3),
        ("application", "msgpack", 0.9),
    ]
    assert negotiate(header, OFFERS) == MSGPACK


@pytest.mark.parametrize("header", [None, "", "nonsense"])
def test_missing_or_unusable_header_selects_the_first_offer(header: str | None) -> None:
    assert negotiate(header, OFFERS) == JSON


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("application/msgpack", MSGPACK),
        ("application/msgpack;q=0.9, application/json;q=0.8", MSGPACK),
        ("application/json;q=0.2, application/msgpack", MSGPACK),
        ("*/*", JSON),
        ("application/*", JSON),
        ("application/*;q=0.5, application/msgpack", MSGPACK),
        ("text/html, */*;q=0.1", JSON),
        ("application/json; charset=utf-8", JSON),
    ],
)
def test_quality_then_specificity_then_server_order(header: str, expected: str) -> None:
    assert negotiate(header, OFFERS) == expected


def test_equal_quality_ties_follow_the_server_order() -> None:
    header = "application/msgpack, application/json"

    assert negotiate(header, OFFERS) == JSON
    assert negotiate(header, OFFERS, client_order=True) == MSGPACK


def test_the_most_specific_range_decides_an_offers_quality() -> None:
    # ``*/*`` would accept JSON, but the exact range says q=0.
    assert negotiate("application/json;q=0, */*", OFFERS) == MSGPACK


def test_q_zero_is_never_chosen() -> None:
    assert negotiate("application/json;q=0, application/msgpack;q=0", OFFERS) is None
    assert negotiate("text/html", OFFERS) is None


def test_equally_specific_duplicates_take_the_highest_q() -> None:
    assert negotiate("application/msgpack;q=0, application/msgpack;q=1", (MSGPACK,)) == MSGPACK


def test_select_without_offers_is_none() -> None:
    assert select(parse_accept("*/*"), ()) is None


# --- typed-stream negotiation now reads q-values --------------------------


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("text/event-stream;q=0, */*", "jsonlines"),
        ("text/event-stream;q=0.1, application/x-ndjson", "ndjson"),
        ("*/*, text/event-stream", "sse"),
        ("text/*", "sse"),
        ("text/event-stream;q=0", "jsonlines"),
    ],
)
def test_stream_negotiation_honours_quality_values(header: str, expected: str) -> None:
    assert negotiate_stream_format(header) == expected


def test_stream_negotiation_excluding_the_default_picks_another_format() -> None:
    assert negotiate_stream_format("text/event-stream;q=0, */*", default="sse") == "ndjson"
