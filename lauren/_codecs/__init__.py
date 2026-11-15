"""Private body-codec runtime; the public surface is :mod:`lauren.codecs`.

* ``_media`` — media-type parsing, validation and matching.
* ``_accept`` — the RFC 9110 ``Accept`` parser and negotiation, shared with
  typed-stream format selection.
* ``_codec`` — the :class:`Codec` protocol, :class:`JsonCodec`, :class:`MsgpackCodec`.
* ``_plan`` — ``ContentConfig`` resolution and the per-route content rules
  compiled by the factory (gate, body decoding, negotiation, body limit).
"""

from __future__ import annotations

from ._accept import MediaRange, negotiate, parse_accept
from ._codec import JSON_BACKENDS, Codec, JsonCodec, MsgpackCodec
from ._media import is_json_media, matches, normalize_media_range

__all__ = [
    "JSON_BACKENDS",
    "Codec",
    "JsonCodec",
    "MediaRange",
    "MsgpackCodec",
    "is_json_media",
    "matches",
    "negotiate",
    "normalize_media_range",
    "parse_accept",
]
