"""``Secret[T]`` — a value that never shows itself.

A :class:`Secret` wraps a configuration value (an API key, a signing
secret, a database password) so that ``repr``, ``str``, f-strings, log
lines and JSON encoding print a fixed mask instead of the value. The only
way to read the value is the explicit :meth:`Secret.get_secret_value`.

This module has no lauren imports so the JSON encoder in
:mod:`lauren.types` and the session signer can depend on it without an
import cycle.
"""

from __future__ import annotations

from typing import Any, Generic, TypeVar

T = TypeVar("T")

#: The mask printed in place of every secret value.
REDACTED = "**********"


class Secret(Generic[T]):
    """Wrap ``value`` so that it is redacted everywhere it could be printed.

    Equality and hashing compare the underlying values (pydantic
    ``SecretStr`` parity), and truthiness follows the underlying value, so
    ``if settings.api_key:`` still works. Annotate a settings field as
    ``Secret[str]`` (or ``Secret[bytes]``, ``Secret[int]``, ...) and the
    configuration loader coerces the raw value as ``T`` before wrapping it.
    """

    __slots__ = ("_value",)

    def __init__(self, value: T) -> None:
        self._value = value

    def get_secret_value(self) -> T:
        """Return the wrapped value — the only way to read it."""
        return self._value

    def __repr__(self) -> str:
        return f"Secret('{REDACTED}')"

    __str__ = __repr__

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return bool(self._value == other._value)
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)

    def __bool__(self) -> bool:
        return bool(self._value)

    @classmethod
    def __get_pydantic_core_schema__(cls, source: Any, handler: Any) -> Any:
        """Let pydantic models declare ``Secret[T]`` fields.

        The inner value is validated as ``T`` and then wrapped; JSON
        serialisation prints the mask. ``pydantic_core`` is imported here,
        so pydantic stays optional for everything else.
        """
        from typing import get_args

        from pydantic_core import core_schema

        args = get_args(source)
        inner = handler.generate_schema(args[0]) if args else core_schema.any_schema()
        return core_schema.no_info_before_validator_function(
            _unwrap,
            core_schema.no_info_after_validator_function(_wrap, inner),
            serialization=core_schema.plain_serializer_function_ser_schema(_serialize, info_arg=True),
        )


def _unwrap(value: Any) -> Any:
    return value.get_secret_value() if isinstance(value, Secret) else value


def _wrap(value: Any) -> Secret[Any]:
    return value if isinstance(value, Secret) else Secret(value)


def _serialize(value: Any, info: Any) -> Any:
    """Mask in JSON mode; keep the :class:`Secret` object in Python mode (``model_dump()``)."""
    return REDACTED if info.mode_is_json() else value


#: pydantic's own secret types, recognised by name so pydantic is never imported.
_PYDANTIC_SECRET_TYPES = frozenset({"SecretStr", "SecretBytes", "Secret"})


def is_secret(value: Any) -> bool:
    """Return True for a :class:`Secret` or a pydantic ``SecretStr`` / ``SecretBytes``."""
    if isinstance(value, Secret):
        return True
    for klass in type(value).__mro__:
        if klass.__name__ in _PYDANTIC_SECRET_TYPES and klass.__module__.startswith("pydantic"):
            return True
    return False


def is_secret_type(tp: Any) -> bool:
    """Return True when the annotation ``tp`` declares a secret value."""
    from typing import get_origin

    target = get_origin(tp) or tp
    if target is Secret:
        return True
    if isinstance(target, type):
        for klass in target.__mro__:
            if klass.__name__ in _PYDANTIC_SECRET_TYPES and klass.__module__.startswith("pydantic"):
                return True
    return False


__all__ = ["REDACTED", "Secret", "is_secret", "is_secret_type"]
