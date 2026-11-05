"""Opaque, signed, versioned pagination cursors (private).

Wire format::

    base64url(json({"v": 1, "k": <keyset>, "d": "next" | "prev", "x": <expiry>?})) + "." + hex HMAC-SHA256

The keyset is projected to JSON by the cursor type's compiled validator and
validated again when the cursor comes back, so a handler always receives a
typed keyset (``EventCursor(created_at=datetime(...), id=7)``). Signing uses
the shared :class:`lauren._signing.Signer` in its own namespace: the first
secret signs, every secret verifies (rotation), and a token signed for another
purpose (a session cookie) never verifies as a cursor.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import time
from collections.abc import Callable
from typing import Any

from .._signing import Signer

#: Version of the cursor envelope; a cursor of another version is rejected.
CURSOR_VERSION = 1

#: Signing namespace — cursors and other signed tokens never verify as each other.
CURSOR_NAMESPACE = "lauren.cursor"

#: Longest cursor accepted before any HMAC work (bounds the cost of junk input).
MAX_CURSOR_LENGTH = 4096

DIRECTIONS: tuple[str, ...] = ("next", "prev")


class InvalidCursor(Exception):
    """A cursor failed verification; ``reason`` says why (internal)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _default_dump(key: Any) -> Any:
    """Project a keyset with the framework's permissive JSON rules (no compiled type)."""
    from ..types import _json_default

    return json.loads(json.dumps(key, default=_json_default))


def _identity(value: Any) -> Any:
    return value


class CursorCodec:
    """Encodes keysets into signed cursors and decodes them back (internal).

    ``dump`` projects a keyset to JSON-compatible builtins and ``load``
    validates the decoded value back into the keyset type (both come from the
    cursor type's ``CompiledType``). ``clock`` is injectable for tests.
    """

    __slots__ = ("_signer", "_max_age", "_clock", "_dump", "_load")

    def __init__(
        self,
        secrets: tuple[bytes, ...],
        *,
        max_age: float | None = None,
        dump: Callable[[Any], Any] | None = None,
        load: Callable[[Any], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._signer = Signer(secrets, namespace=CURSOR_NAMESPACE)
        self._max_age = max_age
        self._clock = clock
        self._dump = dump or _default_dump
        self._load = load or _identity

    def encode(self, key: Any, direction: str = "next") -> str:
        """Return the signed cursor for ``key`` travelling in ``direction``."""
        if direction not in DIRECTIONS:
            raise ValueError(f"direction must be 'next' or 'prev', got {direction!r}")
        payload: dict[str, Any] = {"v": CURSOR_VERSION, "k": self._dump(key), "d": direction}
        if self._max_age is not None:
            payload["x"] = math.ceil(self._clock() + self._max_age)
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        return self._signer.sign(_b64encode(raw.encode("ascii")))

    def decode(self, token: str) -> tuple[Any, str]:
        """Return ``(keyset, direction)`` or raise :class:`InvalidCursor`."""
        if len(token) > MAX_CURSOR_LENGTH:
            raise InvalidCursor("malformed")
        value = self._signer.unsign(token)
        if value is None:
            raise InvalidCursor("signature")
        try:
            payload = json.loads(_b64decode(value))
        except (binascii.Error, ValueError):
            raise InvalidCursor("malformed") from None
        if not isinstance(payload, dict) or "k" not in payload:
            raise InvalidCursor("malformed")
        if payload.get("v") != CURSOR_VERSION:
            raise InvalidCursor("version")
        direction = payload.get("d")
        if direction not in DIRECTIONS:
            raise InvalidCursor("malformed")
        expires = payload.get("x")
        if expires is not None:
            if isinstance(expires, bool) or not isinstance(expires, (int, float)):
                raise InvalidCursor("malformed")
            if self._clock() >= expires:
                raise InvalidCursor("expired")
        elif self._max_age is not None:
            # Issued before expiry was configured: it would never expire.
            raise InvalidCursor("expired")
        try:
            key = self._load(payload["k"])
        except (ValueError, TypeError):
            # A signed keyset that no longer validates (the cursor type changed
            # since it was issued) is an invalid cursor, not a server error.
            raise InvalidCursor("payload") from None
        return key, direction
