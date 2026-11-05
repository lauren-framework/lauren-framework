"""Signed, versioned pagination cursors: round-trip, tampering, rotation, expiry (F29)."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pytest

from lauren._pagination._cursor import MAX_CURSOR_LENGTH, CursorCodec, InvalidCursor
from lauren._signing import Signer
from lauren.validation import TypeCompiler


@dataclass(frozen=True)
class EventCursor:
    created_at: datetime
    id: int


_KEY = EventCursor(created_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc), id=7)


class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _codec(secrets: tuple[bytes, ...] = (b"key-a",), **kwargs: Any) -> CursorCodec:
    compiled = TypeCompiler().compile(EventCursor)
    return CursorCodec(secrets, dump=compiled.project, load=compiled.validate, **kwargs)


def _reason(codec: CursorCodec, token: str) -> str:
    with pytest.raises(InvalidCursor) as info:
        codec.decode(token)
    return info.value.reason


def _forge(payload: Any, secrets: tuple[bytes, ...] = (b"key-a",)) -> str:
    """A correctly signed cursor around an arbitrary payload."""
    raw = json.dumps(payload).encode("utf-8") if not isinstance(payload, bytes) else payload
    value = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return Signer(secrets, namespace="lauren.cursor").sign(value)


@pytest.mark.parametrize("direction", ["next", "prev"])
def test_keyset_round_trips_with_direction(direction: str) -> None:
    codec = _codec()

    assert codec.decode(codec.encode(_KEY, direction)) == (_KEY, direction)


def test_cursor_is_opaque_url_safe_text() -> None:
    token = _codec().encode(_KEY)

    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_.")


def test_wire_format_is_versioned_json() -> None:
    value = _codec().encode(_KEY, "next").rsplit(".", 1)[0]

    payload = json.loads(base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)))

    assert payload == {"v": 1, "k": {"created_at": "2026-01-02T03:04:05+00:00", "id": 7}, "d": "next"}


def test_every_single_character_mutation_is_rejected() -> None:
    codec = _codec()
    token = codec.encode(_KEY)

    for index, char in enumerate(token):
        mutated = token[:index] + ("A" if char != "A" else "B") + token[index + 1 :]
        with pytest.raises(InvalidCursor):
            codec.decode(mutated)


def test_truncated_cursor_is_rejected() -> None:
    codec = _codec()

    assert _reason(codec, codec.encode(_KEY)[:-1]) == "signature"


def test_cursor_without_separator_is_rejected() -> None:
    assert _reason(_codec(), "no-signature-here") == "signature"


def test_rotation_new_key_signs_old_key_still_verifies() -> None:
    old_token = _codec((b"key-a",)).encode(_KEY)
    rotated = _codec((b"key-b", b"key-a"))

    assert rotated.decode(old_token) == (_KEY, "next")


def test_rotation_signs_with_the_first_key() -> None:
    token = _codec((b"key-b", b"key-a")).encode(_KEY)

    assert _codec((b"key-b",)).decode(token) == (_KEY, "next")


def test_dropping_the_old_key_invalidates_its_cursors() -> None:
    old_token = _codec((b"key-a",)).encode(_KEY)

    assert _reason(_codec((b"key-b",)), old_token) == "signature"


def test_version_mismatch_is_rejected() -> None:
    token = _forge({"v": 2, "k": {"created_at": "2026-01-02T03:04:05Z", "id": 1}, "d": "next"})

    assert _reason(_codec(), token) == "version"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        [1, 2],
        {"v": 1, "d": "next"},
        {"v": 1, "k": {}, "d": "sideways"},
        {"v": 1, "k": {}, "d": "next", "x": "tomorrow"},
        {"v": 1, "k": {}, "d": "next", "x": True},
    ],
)
def test_malformed_payload_is_rejected(payload: Any) -> None:
    assert _reason(_codec(), _forge(payload)) == "malformed"


def test_invalid_base64_is_rejected() -> None:
    token = Signer((b"key-a",), namespace="lauren.cursor").sign("a")

    assert _reason(_codec(), token) == "malformed"


def test_keyset_that_no_longer_validates_is_rejected() -> None:
    token = _forge({"v": 1, "k": {"id": "seven"}, "d": "next"})

    assert _reason(_codec(), token) == "payload"


def test_oversized_cursor_is_rejected_before_verification() -> None:
    assert _reason(_codec(), "a" * (MAX_CURSOR_LENGTH + 1)) == "malformed"


def test_cursor_expires_after_max_age() -> None:
    clock = FakeClock()
    codec = _codec(max_age=60, clock=clock)
    token = codec.encode(_KEY)

    clock.now += 61

    assert _reason(codec, token) == "expired"


def test_cursor_is_valid_before_max_age() -> None:
    clock = FakeClock()
    codec = _codec(max_age=60, clock=clock)
    token = codec.encode(_KEY)

    clock.now += 59

    assert codec.decode(token) == (_KEY, "next")


def test_cursor_without_expiry_is_rejected_once_max_age_is_configured() -> None:
    token = _codec().encode(_KEY)

    assert _reason(_codec(max_age=60), token) == "expired"


def test_session_signed_value_never_verifies_as_cursor() -> None:
    raw = json.dumps({"v": 1, "k": {"created_at": "2026-01-02T03:04:05Z", "id": 1}, "d": "next"})
    value = base64.urlsafe_b64encode(raw.encode()).rstrip(b"=").decode("ascii")
    session_token = Signer((b"key-a",)).sign(value)

    assert _reason(_codec(), session_token) == "signature"


def test_unknown_direction_cannot_be_encoded() -> None:
    with pytest.raises(ValueError):
        _codec().encode(_KEY, "sideways")


def test_codec_without_compiled_type_projects_permissively() -> None:
    codec = CursorCodec((b"k",))

    assert codec.decode(codec.encode(_KEY)) == ({"created_at": "2026-01-02T03:04:05+00:00", "id": 7}, "next")
