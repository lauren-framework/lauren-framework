"""Session payload serialisation.

The serialiser turns a session ``dict`` into ``bytes`` for storage (the
server-side stores) or for the cookie envelope (the stateless cookie
store). The default is compact JSON; users who need a denser format
(msgpack, cbor, …) implement :class:`SessionSerializer` and pass an
instance via :class:`~lauren.SessionConfig`.
"""

from __future__ import annotations

import json as _json
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class SessionSerializer(Protocol):
    """Encode/decode a session ``dict`` to/from ``bytes``.

    Implementations must round-trip: ``loads(dumps(d)) == d`` for any
    JSON-compatible ``d``. A failed ``loads`` should raise (the engine
    treats a raising ``loads`` as a tampered/garbage cookie and starts a
    fresh session).
    """

    def dumps(self, data: dict[str, Any]) -> bytes: ...

    def loads(self, raw: bytes) -> dict[str, Any]: ...


class JSONSessionSerializer:
    """Compact-JSON serialiser — the default backend.

    Uses ``(",", ":")`` separators so the cookie envelope stays as small
    as possible, and ``sort_keys`` so equal payloads encode identically.
    """

    def dumps(self, data: dict[str, Any]) -> bytes:
        return _json.dumps(data, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def loads(self, raw: bytes) -> dict[str, Any]:
        out = _json.loads(raw.decode("utf-8"))
        if not isinstance(out, dict):
            raise ValueError("session payload did not decode to a dict")
        return out
