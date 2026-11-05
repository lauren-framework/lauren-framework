"""The byte layout of a cache entry.

``version (1 byte) | header length (4 bytes, big-endian) | JSON header | payload``

The header carries the write time (``w``), the tag generations seen at write
time (``g``) and, for stale-while-revalidate entries, the soft expiry
(``s``). Times come from the cache's wall clock so they compare across
processes. Anything that does not parse is treated as a miss.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping

_VERSION = 1


@dataclass(frozen=True, slots=True)
class Entry:
    payload: bytes
    written_at: float
    gens: dict[str, int] = field(default_factory=dict)
    soft_expiry: float | None = None


def encode_entry(
    payload: bytes,
    *,
    written_at: float,
    gens: Mapping[str, int],
    soft_expiry: float | None = None,
) -> bytes:
    header: dict[str, Any] = {"w": written_at}
    if gens:
        header["g"] = dict(gens)
    if soft_expiry is not None:
        header["s"] = soft_expiry
    raw = json.dumps(header, separators=(",", ":")).encode("utf-8")
    return bytes((_VERSION,)) + len(raw).to_bytes(4, "big") + raw + payload


def decode_entry(raw: bytes) -> Entry | None:
    if len(raw) < 5 or raw[0] != _VERSION:
        return None
    size = int.from_bytes(raw[1:5], "big")
    try:
        header = json.loads(raw[5 : 5 + size])
        written_at = float(header["w"])
        gens = {str(k): int(v) for k, v in header.get("g", {}).items()}
        soft = header.get("s")
        soft_expiry = None if soft is None else float(soft)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    return Entry(payload=raw[5 + size :], written_at=written_at, gens=gens, soft_expiry=soft_expiry)


__all__ = ["Entry", "decode_entry", "encode_entry"]
