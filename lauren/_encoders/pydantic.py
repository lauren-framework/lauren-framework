"""Pydantic-optimised JSON encoder.

Only usable when pydantic is installed (``pip install 'lauren[pydantic]'``).
The public import path ``from lauren.serialization import PydanticEncoder``
is preserved via a ``__getattr__`` shim in ``lauren/serialization.py``.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PydanticEncoder"]


class PydanticEncoder:
    """Encoder that uses Pydantic v2's Rust-backed ``pydantic-core`` serializer.

    For non-Pydantic values (plain ``dict``, ``list``, ``str``, dataclasses,
    ``msgspec.Struct``, …) it falls back to :class:`~lauren.serialization.StdlibJSONEncoder`.

    :raises RuntimeError: If Pydantic v2 is not installed.
    """

    name = "pydantic"

    __slots__ = ("_fallback",)

    def __init__(self, default: Any = None) -> None:
        try:
            import pydantic  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "PydanticEncoder requires the 'pydantic' package (v2); "
                "install it with `pip install pydantic`."
            ) from exc
        from lauren.serialization import StdlibJSONEncoder  # noqa: PLC0415

        self._fallback = StdlibJSONEncoder(default=default)

    def encode(self, value: Any) -> bytes:
        """Serialize *value* to pretty-printed JSON bytes."""
        return self._encode(value, compact=False)

    def encode_compact(self, value: Any) -> bytes:
        """Serialize *value* to compact JSON bytes (no whitespace)."""
        return self._encode(value, compact=True)

    def _encode(self, value: Any, *, compact: bool) -> bytes:
        from pydantic import BaseModel  # noqa: PLC0415

        if isinstance(value, BaseModel):
            result: str | bytes = (
                value.model_dump_json() if compact else value.model_dump_json(indent=2).encode()
            )
            return result if isinstance(result, bytes) else result.encode("utf-8")

        if isinstance(value, list) and value and all(isinstance(item, BaseModel) for item in value):
            from pydantic import TypeAdapter  # noqa: PLC0415

            adapter: TypeAdapter[list] = TypeAdapter(list[type(value[0])])  # type: ignore[misc]
            return adapter.dump_json(value) if compact else adapter.dump_json(value, indent=2)

        return self._fallback.encode_compact(value) if compact else self._fallback.encode(value)
