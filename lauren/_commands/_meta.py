"""``@command`` and ``Opt`` (private module, public through :mod:`lauren.commands`).

The decorator only validates its arguments and appends a :class:`CommandMeta`
to ``__lauren_command__`` on the function — a list, so stacking registers the
same method under several names. Commands are found once, in
``LaurenFactory.create`` Phase 6a, by the shared discovery pass
(``lauren/_discovery.py``); the marker is registered here, at import.
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, TypeVar

from .._di._scope import register_framework_value
from .._discovery import MarkerSpec, register_marker
from ..decorators import _reject_bare_usage
from ..exceptions import DecoratorUsageError
from ._context import CommandContext

F = TypeVar("F")

#: Marker attribute: a list of :class:`CommandMeta`, one per ``@command``.
COMMAND_META = "__lauren_command__"

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]*$")
_SHORT_RE = re.compile(r"^-[A-Za-z0-9]$")
_ENV_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MISSING: Any = object()


@dataclass(frozen=True, slots=True)
class CommandMeta:
    """One ``@command(...)`` declaration."""

    name: str
    help: str | None = None
    aliases: tuple[str, ...] = ()
    hidden: bool = False


@dataclass(frozen=True, slots=True)
class Opt:
    """CLI details for one command parameter, given as ``Annotated[T, Opt(...)]``.

    ``short`` adds a one-letter flag (``"-n"``) to an option, ``env`` names an
    environment variable read when the option is not on the command line, and
    ``help`` is the parameter's help text. ``short`` and ``env`` apply to
    options (keyword-only parameters and ``bool`` flags); ``help`` applies to
    every parameter.
    """

    short: str | None = None
    env: str | None = None
    help: str | None = None

    def __post_init__(self) -> None:
        if self.short is not None and (not isinstance(self.short, str) or not _SHORT_RE.match(self.short)):
            raise DecoratorUsageError(
                f"Opt(short={self.short!r}) must be a dash and one letter or digit, for example short='-n'",
                detail={"helper": "Opt", "option": "short", "got": repr(self.short)},
            )
        if self.env is not None and (not isinstance(self.env, str) or not _ENV_RE.match(self.env)):
            raise DecoratorUsageError(
                f"Opt(env={self.env!r}) must be an environment variable name, for example env='SEED_COUNT'",
                detail={"helper": "Opt", "option": "env", "got": repr(self.env)},
            )
        if self.help is not None and not isinstance(self.help, str):
            raise DecoratorUsageError(
                f"Opt(help={self.help!r}) must be a string",
                detail={"helper": "Opt", "option": "help", "got": repr(self.help)},
            )


def _check_name(value: Any, *, option: str) -> str:
    if not isinstance(value, str) or not _NAME_RE.match(value):
        raise DecoratorUsageError(
            f"@command: {option} {value!r} is invalid; use lowercase letters, digits and '.', '_', ':', '-', "
            "starting with a letter or digit (for example 'db:seed')",
            detail={"decorator": "command", "option": option, "got": repr(value)},
        )
    return value


def command(
    name: Any = _MISSING,
    *,
    help: str | None = None,
    aliases: Iterable[str] = (),
    hidden: bool = False,
) -> Callable[[F], F]:
    """Mark a provider method as a command run with ``python -m lauren exec`` or ``ctx.run_command``.

    ``name`` (``"db:seed"``) must match ``^[a-z0-9][a-z0-9._:-]*$``; ``aliases``
    are extra names; ``hidden=True`` keeps the command runnable but out of
    listings. ``help`` defaults to the first line of the method's docstring.

    The method's parameters become the command's CLI: a :class:`CommandContext`
    is injected, dependencies the provider's module can resolve are injected,
    and the rest are arguments (positional parameters) and options
    (keyword-only parameters, ``bool`` flags). Everything is validated in
    ``LaurenFactory.create``. The decorator only records metadata and returns
    the method unchanged; stacking it registers several names.
    """
    if name is _MISSING:
        raise DecoratorUsageError(
            "@command needs a name: write @command('db:seed')",
            detail={"decorator": "command", "reason": "missing_name"},
        )
    if not isinstance(name, str):
        _reject_bare_usage("command", name)
    _check_name(name, option="name")
    if isinstance(aliases, str) or not isinstance(aliases, Iterable):
        raise DecoratorUsageError(
            f"@command(aliases={aliases!r}) must be a tuple or list of names, for example aliases=('seed',)",
            detail={"decorator": "command", "option": "aliases", "got": repr(aliases)},
        )
    alias_names = tuple(_check_name(alias, option="alias") for alias in aliases)
    if help is not None and not isinstance(help, str):
        raise DecoratorUsageError(
            f"@command(help={help!r}) must be a string",
            detail={"decorator": "command", "option": "help", "got": repr(help)},
        )
    if not isinstance(hidden, bool):
        raise DecoratorUsageError(
            f"@command(hidden={hidden!r}) must be True or False",
            detail={"decorator": "command", "option": "hidden", "got": repr(hidden)},
        )
    meta = CommandMeta(name=name, help=help, aliases=alias_names, hidden=hidden)

    def decorator(fn: F) -> F:
        # Above @staticmethod / @classmethod the marker goes on the function
        # itself, the place discovery reads first.
        target: Any = fn.__func__ if isinstance(fn, (staticmethod, classmethod)) else fn
        if isinstance(target, type) or not callable(target):
            raise DecoratorUsageError(
                f"@command decorates a method of an @injectable() class, got {target!r}",
                detail={"decorator": "command", "target": repr(target)},
            )
        if inspect.isgeneratorfunction(target) or inspect.isasyncgenfunction(target):
            raise DecoratorUsageError(
                f"{getattr(target, '__qualname__', target)} is a generator; a command must be a plain or async method",
                detail={"decorator": "command", "target": getattr(target, "__qualname__", repr(target))},
            )
        existing = list(getattr(target, COMMAND_META, None) or ())
        existing.append(meta)
        setattr(target, COMMAND_META, existing)
        return fn

    return decorator


# Command methods are found by the shared startup discovery pass. Parameters
# that are neither a CommandContext nor a resolvable dependency are collected
# as CLI arguments (``extra_params="collect"``) and mapped by ``_compile``.
register_marker(MarkerSpec(COMMAND_META, framework_types=(CommandContext,), extra_params="collect"))
register_framework_value(CommandContext)


__all__ = ["COMMAND_META", "CommandMeta", "Opt", "command"]
