"""Metadata readers — read cross-cutting concern annotations from any
decorated class without hard-coding internal attribute name strings.
"""

from __future__ import annotations

from typing import NamedTuple

__all__ = [
    "reflect_guards",
    "reflect_interceptors",
    "reflect_middlewares",
    "reflect_all",
    "ReflectedMeta",
]

# Mirror the constants from lauren/decorators.py so reflect/_reader.py has
# no circular import dependency on the rest of the framework.  These strings
# are stable — they form part of Lauren's public decorator contract.
_USE_GUARDS = "__lauren_use_guards__"
_USE_INTERCEPTORS = "__lauren_use_interceptors__"
_USE_MIDDLEWARES = "__lauren_use_middlewares__"


class ReflectedMeta(NamedTuple):
    """All cross-cutting metadata declared on a class via Lauren decorators."""

    guards: tuple[type, ...]
    interceptors: tuple[type, ...]
    middlewares: tuple[type, ...]


def reflect_guards(cls: type) -> tuple[type, ...]:
    """Return the guard classes declared on *cls* via ``@use_guards``.

    Reads from ``cls.__dict__`` only — matches Lauren's "metadata is never
    inherited" rule.  Returns an empty tuple when no guards are declared.

    Example::

        @use_guards(AuthGuard, RateLimitGuard)
        @ws_controller("/chat")
        class ChatGateway: ...

        reflect_guards(ChatGateway)   # → (AuthGuard, RateLimitGuard)
    """
    return tuple(cls.__dict__.get(_USE_GUARDS, ()))


def reflect_interceptors(cls: type) -> tuple[type, ...]:
    """Return the interceptor classes declared on *cls* via ``@use_interceptors``.

    Reads from ``cls.__dict__`` only.  Returns an empty tuple when none are
    declared.
    """
    return tuple(cls.__dict__.get(_USE_INTERCEPTORS, ()))


def reflect_middlewares(cls: type) -> tuple[type, ...]:
    """Return the middleware classes declared on *cls* via ``@use_middlewares``.

    Reads from ``cls.__dict__`` only.  Returns an empty tuple when none are
    declared.
    """
    return tuple(cls.__dict__.get(_USE_MIDDLEWARES, ()))


def reflect_all(cls: type) -> ReflectedMeta:
    """Return all cross-cutting metadata declared on *cls*.

    Equivalent to calling all three individual readers and bundling the
    results into a :class:`ReflectedMeta` named tuple.

    Example::

        meta = reflect_all(MyGateway)
        meta.guards          # tuple[type, ...]
        meta.interceptors    # tuple[type, ...]
        meta.middlewares     # tuple[type, ...]
    """
    return ReflectedMeta(
        guards=reflect_guards(cls),
        interceptors=reflect_interceptors(cls),
        middlewares=reflect_middlewares(cls),
    )
