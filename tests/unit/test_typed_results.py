"""Unit: the typed-result wrappers and their generic coercion."""

from __future__ import annotations

import dataclasses
import json
from typing import Any, get_args, get_origin

import pytest

from lauren import Accepted, Created, NoContent, Ok, Status
from lauren._asgi import _coerce_to_response
from lauren.responses import RESULT_TYPES


class _RecordingEncoder:
    """A JSONEncoder that records every value it encodes."""

    name = "recording"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def encode(self, value: Any) -> bytes:
        return self.encode_compact(value)

    def encode_compact(self, value: Any) -> bytes:
        self.calls.append(value)
        return json.dumps(value, separators=(",", ":")).encode("utf-8")


@pytest.mark.parametrize(
    ("cls", "status"),
    [(Ok, 200), (Created, 201), (Accepted, 202), (NoContent, 204)],
)
def test_each_wrapper_class_declares_its_status(cls: type, status: int) -> None:
    assert cls.status == status  # type: ignore[attr-defined]


@pytest.mark.parametrize("instance", [Ok(1), Created(1), Accepted(), NoContent()])
def test_wrappers_are_frozen(instance: Any) -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        instance.headers = {"x": "y"}


@pytest.mark.parametrize("cls", [Ok, Created, Accepted])
def test_wrappers_are_generic(cls: Any) -> None:
    alias = cls[int]

    assert (get_origin(alias), get_args(alias)) == (cls, (int,))


def test_wrappers_use_slots() -> None:
    assert not hasattr(Ok(1), "__dict__")


def test_result_types_lists_the_four_wrappers() -> None:
    assert RESULT_TYPES == (Ok, Created, Accepted, NoContent)


def test_status_marker_holds_code_and_description() -> None:
    marker = Status(206, description="Partial")

    assert (marker.code, marker.description) == (206, "Partial")


def test_created_sends_201_with_location_header() -> None:
    response = _coerce_to_response(Created({"id": 1}, location="/users/1"))

    assert response.status == 201
    assert response.headers.get("location") == "/users/1"
    assert json.loads(response.body) == {"id": 1}


def test_wrapper_headers_are_added_to_the_response() -> None:
    response = _coerce_to_response(Ok({"a": 1}, headers={"X-Trace": "abc"}))

    assert (response.status, response.headers.get("x-trace")) == (200, "abc")


def test_location_wins_over_a_location_entry_in_headers() -> None:
    response = _coerce_to_response(Created(1, location="/new", headers={"Location": "/old"}))

    assert response.headers.get("location") == "/new"


@pytest.mark.parametrize(
    ("value", "status"),
    [(NoContent(), 204), (Accepted(), 202), (Created(None, location="/x"), 201), (Ok(None), 200)],
)
def test_wrappers_without_a_value_send_an_empty_body(value: Any, status: int) -> None:
    response = _coerce_to_response(value)

    assert (response.status, response.body) == (status, b"")


def test_wrapper_text_value_keeps_its_text_media_type() -> None:
    response = _coerce_to_response(Accepted("queued"))

    assert response.status == 202
    assert response.headers.get("content-type", "").startswith("text/plain")


def test_wrapper_bodies_use_the_passed_encoder() -> None:
    encoder = _RecordingEncoder()

    response = _coerce_to_response(Created({"id": 7}), encoder=encoder)

    assert encoder.calls == [{"id": 7}]
    assert response.body == b'{"id":7}'


def test_wrapper_dataclass_value_uses_the_passed_encoder() -> None:
    @dataclasses.dataclass
    class Job:
        id: int

    encoder = _RecordingEncoder()

    response = _coerce_to_response(Accepted(Job(3)), encoder=encoder)

    assert response.status == 202
    assert encoder.calls == [{"id": 3}]
