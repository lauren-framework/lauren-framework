"""Command compilation (``LaurenFactory.create`` Phase 6a) and execution (private).

:func:`compile_commands` turns every discovered ``@command`` method into a
:class:`CompiledCommand`: its CLI arguments are classified from the call plan
the discovery pass compiled, their annotations are checked against the
coercion table, and a stdlib ``argparse`` parser is built — all once, at
startup. At run time :meth:`CommandRegistry.run` only parses ``argv`` with the
prebuilt parser, opens an ``OperationScope`` seeded with the
:class:`~lauren.commands.CommandContext` and calls the method.

Parameter mapping (positions follow the method signature):

* ``CommandContext`` — injected; dependencies the provider's module can
  resolve (or ``Annotated[T, Inject(token)]``) — injected from the scope;
* ``bool`` — a ``--name / --no-name`` flag, wherever it is declared;
* keyword-only — an ``--kebab-case`` option (``list[T]`` / ``tuple[T, ...]``
  repeat: ``--tag a --tag b``);
* other parameters — positional arguments, optional when they have a default.
"""

from __future__ import annotations

import argparse
import datetime
import decimal
import difflib
import enum
import inspect
import os
import pathlib
import traceback
import types
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Any, Literal, NoReturn, TextIO, Union, get_args, get_origin

from .._config._coerce import CoercionError, coerce, type_name
from .._discovery import ARGUMENT, DiscoveredMethod, DiscoveryIndex, PlanParam
from .._scheduling._context import bound_logger
from ..exceptions import DecoratorUsageError, DuplicateBindingError, UnresolvableParameterError
from ._context import CommandContext, _write
from ._meta import COMMAND_META, CommandMeta, Opt

if TYPE_CHECKING:
    from .._asgi import LaurenApp

_EMPTY = inspect.Parameter.empty
_UNION_TYPES: tuple[Any, ...] = (Union, types.UnionType)


class _Sentinel:
    __slots__ = ("_name",)

    def __init__(self, name: str) -> None:
        self._name = name

    def __repr__(self) -> str:
        return self._name


_UNSET: Any = _Sentinel("<unset>")  # argparse default: "not on the command line"
_REQUIRED: Any = _Sentinel("<required>")  # spec default: no fallback value

#: Scalar annotations a CLI token can be coerced to (``bool`` is a flag).
_SCALAR_TYPES: tuple[type, ...] = (
    str,
    int,
    float,
    decimal.Decimal,
    pathlib.PurePath,
    uuid.UUID,
    datetime.datetime,
    datetime.date,
    datetime.time,
    datetime.timedelta,
    enum.Enum,
)
_SUPPORTED = (
    "str, int, float, Decimal, bool, Path, UUID, datetime, date, time, timedelta, an Enum or a Literal "
    "(optionally `| None`, or in list[T] / tuple[T, ...])"
)

POSITIONAL = "positional"
OPTION = "option"
FLAG = "flag"


# ---------------------------------------------------------------------------
# Public info records
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommandParameter:
    """One CLI parameter of a command, as listed by ``ctx.commands()``.

    ``kind`` is ``"positional"``, ``"option"`` or ``"flag"``; ``flags`` holds
    the option strings (``("-n", "--count")``, ``("--reset", "--no-reset")``;
    empty for positionals); ``type`` is a readable annotation; ``default`` is
    ``None`` for a required parameter; ``choices`` lists the accepted values
    of an ``Enum`` / ``Literal``.
    """

    name: str
    kind: str
    flags: tuple[str, ...]
    type: str
    required: bool
    default: Any
    repeatable: bool
    choices: tuple[str, ...]
    env: str | None
    help: str | None


@dataclass(frozen=True, slots=True)
class CommandInfo:
    """A discovered command, as listed by ``ctx.commands()``.

    ``module`` is the module that owns the provider (``None`` for global
    providers) and ``target`` names the method (``"SeedCommands.seed"``).
    """

    name: str
    aliases: tuple[str, ...]
    help: str | None
    hidden: bool
    parameters: tuple[CommandParameter, ...]
    module: type | None
    target: str


# ---------------------------------------------------------------------------
# Annotation analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Shape:
    """A CLI-mappable annotation: scalar element, optional container, ``| None``."""

    scalar: Any
    container: type | None  # list | tuple | None
    nullable: bool
    opt: Opt | None

    @property
    def full_type(self) -> Any:
        """The annotation to coerce a whole value (an environment variable) to."""
        if self.container is tuple:
            return types.GenericAlias(tuple, (self.scalar, Ellipsis))
        if self.container is list:
            return types.GenericAlias(list, (self.scalar,))
        return self.scalar

    @property
    def choices(self) -> tuple[str, ...]:
        if get_origin(self.scalar) is Literal:
            return tuple(str(value) for value in get_args(self.scalar))
        if isinstance(self.scalar, type) and issubclass(self.scalar, enum.Enum):
            return tuple(str(member.value) for member in self.scalar)
        return ()


def _strip_annotated(ann: Any, opts: list[Opt]) -> Any:
    while get_origin(ann) is Annotated:
        base, *extras = get_args(ann)
        opts.extend(extra for extra in extras if isinstance(extra, Opt))
        ann = base
    return ann


def _is_scalar(tp: Any) -> bool:
    if tp is bool or tp is Any:
        return True
    if get_origin(tp) is Literal:
        return all(isinstance(value, (str, int, float)) for value in get_args(tp))
    return isinstance(tp, type) and issubclass(tp, _SCALAR_TYPES)


def _shape(ann: Any) -> _Shape | None:
    """Analyse an annotation; ``None`` when it cannot be a CLI parameter."""
    opts: list[Opt] = []
    ann = _strip_annotated(ann, opts)
    nullable = False
    if get_origin(ann) in _UNION_TYPES:
        members = get_args(ann)
        others = [m for m in members if m is not type(None)]
        if len(others) != 1 or len(others) == len(members):
            return None
        nullable = True
        ann = _strip_annotated(others[0], opts)
    container: type | None = None
    origin = get_origin(ann)
    if ann in (list, tuple) or origin in (list, tuple):
        args = get_args(ann)
        container = list if (origin or ann) is list else tuple
        if container is tuple and args and not (len(args) == 2 and args[1] is Ellipsis):
            return None  # fixed-length tuples are not repeatable options
        ann = _strip_annotated(args[0], opts) if args else str
    if not _is_scalar(ann):
        return None
    if ann is Any:
        ann = str
    return _Shape(scalar=ann, container=container, nullable=nullable, opt=opts[0] if opts else None)


def _infer_annotation(default: Any) -> Any:
    """An unannotated parameter is a ``str`` unless its default says otherwise."""
    if default is _EMPTY or default is None:
        return str
    kind = type(default)
    return kind if _is_scalar(kind) else str


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------


class _HelpRequested(Exception):
    """``-h`` / ``--help`` was given (the runner prints the help, exit 0)."""


class _UsageError(Exception):
    """The arguments do not match the command (the runner prints usage, exit 2)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _Parser(argparse.ArgumentParser):
    """An ``ArgumentParser`` that raises instead of printing and exiting."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(message)

    def exit(self, status: int = 0, message: str | None = None) -> NoReturn:
        raise _UsageError(message or f"exited with status {status}")


class _HelpAction(argparse.Action):
    def __init__(self, option_strings: Sequence[str], dest: str = argparse.SUPPRESS, **_: Any) -> None:
        super().__init__(
            option_strings=option_strings,
            dest=dest,
            default=argparse.SUPPRESS,
            nargs=0,
            help="show this help message and exit",
        )

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
        raise _HelpRequested()


class _FlagAction(argparse.Action):
    """``--name`` sets ``True``, ``--no-name`` sets ``False`` (a short flag sets ``True``)."""

    def __init__(
        self,
        option_strings: Sequence[str],
        dest: str,
        default: Any = _UNSET,
        help: str | None = None,
        **_: Any,
    ) -> None:
        super().__init__(option_strings=option_strings, dest=dest, nargs=0, default=default, help=help)

    def __call__(self, parser: Any, namespace: Any, values: Any, option_string: str | None = None) -> None:
        setattr(namespace, self.dest, not (option_string or "").startswith("--no-"))

    def format_usage(self) -> str:
        return " | ".join(self.option_strings)


class _Converter:
    """argparse ``type=``: coerce one token with the configuration coercion table."""

    def __init__(self, tp: Any) -> None:
        self._tp = tp
        self.__name__ = type_name(tp)  # argparse names the type in some messages

    def __call__(self, raw: str) -> Any:
        try:
            return coerce(raw, self._tp)
        except CoercionError as exc:
            raise argparse.ArgumentTypeError(f"invalid value {raw!r} ({exc})") from None


def _new_parser(prog: str, description: str | None) -> _Parser:
    options: dict[str, Any] = {
        "prog": prog,
        "description": description,
        "add_help": False,
        "allow_abbrev": False,
    }
    try:
        # Python 3.14+ colours help by default; keep it deterministic and plain.
        parser = _Parser(**{**options, "color": False})
    except TypeError:
        parser = _Parser(**options)
    parser.add_argument("-h", "--help", action=_HelpAction)
    return parser


# ---------------------------------------------------------------------------
# Argument specs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ArgSpec:
    """How one ARGUMENT parameter maps to the CLI (compiled at startup)."""

    name: str
    kind: str  # POSITIONAL | OPTION | FLAG
    shape: _Shape
    flags: tuple[str, ...]
    default: Any  # _REQUIRED when there is no fallback
    env: str | None
    annotation: Any

    @property
    def required(self) -> bool:
        return self.default is _REQUIRED

    @property
    def display(self) -> str:
        return "/".join(self.flags[-2:] if self.kind == FLAG else self.flags[-1:]) or self.name

    def help_text(self) -> str | None:
        notes: list[str] = []
        if self.kind == POSITIONAL and self.shape.choices:
            notes.append("one of: " + ", ".join(self.shape.choices))
        if self.kind == OPTION and self.shape.container is not None:
            notes.append("repeatable")
        if (
            not self.required
            and self.default is not None
            and not (self.kind == FLAG and self.default is False)
        ):
            notes.append(f"default: {_render_default(self.default)}")
        if self.env is not None:
            notes.append(f"env: {self.env}")
        base = self.shape.opt.help if self.shape.opt is not None and self.shape.opt.help else ""
        if base and notes:
            text = f"{base} ({'; '.join(notes)})"
        else:
            text = base or "; ".join(notes)
        return text.replace("%", "%%") if text else None

    def info(self) -> CommandParameter:
        return CommandParameter(
            name=self.name,
            kind=self.kind,
            flags=self.flags,
            type=type_name(self.annotation),
            required=self.required,
            default=None if self.required else self.default,
            repeatable=self.shape.container is not None,
            choices=self.shape.choices,
            env=self.env,
            help=self.shape.opt.help if self.shape.opt is not None else None,
        )


def _render_default(value: Any) -> str:
    if isinstance(value, enum.Enum):
        return str(value.value)
    if isinstance(value, bool):
        return "on" if value else "off"
    if isinstance(value, (list, tuple)):
        return ", ".join(_render_default(item) for item in value) or "none"
    return str(value)


def _kebab(name: str) -> str:
    return "--" + name.strip("_").replace("_", "-")


def _compile_spec(param: PlanParam, *, command: str, target: str) -> _ArgSpec:
    annotation = param.token if param.token is not _EMPTY else _infer_annotation(param.default)
    shape = _shape(annotation)
    if shape is None:
        raise UnresolvableParameterError(
            f"command {command!r} ({target}): cannot map parameter {param.name!r} "
            f"({type_name(annotation)}); it is not a CLI type and no provider for it is visible from the "
            f"provider's module. CLI parameters are {_SUPPORTED}; dependencies need a provider the module "
            "can see (declare or import it)",
            detail={
                "command": command,
                "target": target,
                "param": param.name,
                "annotation": type_name(annotation),
                "reason": "unsupported_command_parameter",
            },
        )
    opt = shape.opt
    has_default = param.has_default
    if shape.scalar is bool and shape.container is None:
        kind = FLAG
        default = param.default if has_default else (None if shape.nullable else False)
        flags = ((opt.short,) if opt is not None and opt.short else ()) + (
            _kebab(param.name),
            "--no-" + _kebab(param.name)[2:],
        )
    elif param.keyword_only:
        kind = OPTION
        default = param.default if has_default else (None if shape.nullable else _REQUIRED)
        flags = ((opt.short,) if opt is not None and opt.short else ()) + (_kebab(param.name),)
    else:
        if opt is not None and (opt.short or opt.env):
            raise DecoratorUsageError(
                f"command {command!r} ({target}): parameter {param.name!r} is positional, so Opt(short=..., "
                "env=...) does not apply; make it keyword-only (after `*`) to turn it into an option",
                detail={
                    "command": command,
                    "target": target,
                    "param": param.name,
                    "reason": "opt_on_positional",
                },
            )
        kind = POSITIONAL
        default = param.default if has_default else (None if shape.nullable else _REQUIRED)
        flags = ()
    return _ArgSpec(
        name=param.name,
        kind=kind,
        shape=shape,
        flags=flags,
        default=default,
        env=opt.env if opt is not None else None,
        annotation=annotation,
    )


def _add_to_parser(parser: _Parser, spec: _ArgSpec, *, command: str, target: str) -> None:
    help_text = spec.help_text()
    shape = spec.shape
    metavar = "{" + ",".join(shape.choices) + "}" if shape.choices else None
    try:
        if spec.kind == FLAG:
            parser.add_argument(
                *spec.flags, dest=spec.name, action=_FlagAction, default=_UNSET, help=help_text
            )
        elif spec.kind == OPTION:
            parser.add_argument(
                *spec.flags,
                dest=spec.name,
                type=_Converter(shape.scalar),
                action="append" if shape.container is not None else "store",
                default=None if shape.container is not None else _UNSET,
                required=spec.required and spec.env is None,
                metavar=metavar or spec.name.strip("_").upper(),
                help=help_text,
            )
        else:
            if shape.container is not None:
                nargs: Any = "+" if spec.required else "*"
            else:
                nargs = None if spec.required else "?"
            parser.add_argument(
                spec.name,
                type=_Converter(shape.scalar),
                nargs=nargs,
                default=_UNSET,
                help=help_text,
            )
    except (argparse.ArgumentError, ValueError) as exc:
        raise DecoratorUsageError(
            f"command {command!r} ({target}): parameter {spec.name!r} cannot be added to the CLI: {exc}",
            detail={"command": command, "target": target, "param": spec.name, "reason": "conflicting_option"},
        ) from exc


def _parse(parser: _Parser, specs: tuple[_ArgSpec, ...], argv: Sequence[str]) -> dict[str, Any]:
    """Parse ``argv`` with a prebuilt parser and apply env fallbacks and defaults."""
    namespace = parser.parse_args(list(argv))
    arguments: dict[str, Any] = {}
    missing: list[str] = []
    for spec in specs:
        value = getattr(namespace, spec.name, _UNSET)
        given = (
            value is not None
            if spec.kind == OPTION and spec.shape.container is not None
            else value is not _UNSET
        )
        if given:
            arguments[spec.name] = tuple(value) if spec.shape.container is tuple else value
            continue
        if spec.env is not None and spec.env in os.environ:
            try:
                arguments[spec.name] = coerce(os.environ[spec.env], spec.shape.full_type)
            except CoercionError as exc:
                raise _UsageError(f"environment variable {spec.env} (for {spec.display}): {exc}") from None
            continue
        if spec.required:
            missing.append(spec.display + (f" (or set {spec.env})" if spec.env else ""))
        else:
            arguments[spec.name] = spec.default
    if missing:
        raise _UsageError("the following arguments are required: " + ", ".join(missing))
    return arguments


# ---------------------------------------------------------------------------
# Compiled commands
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompiledCommand:
    """One command: metadata, discovered method, argument specs and parser."""

    meta: CommandMeta
    entry: DiscoveredMethod
    specs: tuple[_ArgSpec, ...]
    parser: _Parser
    info: CommandInfo
    option_strings: frozenset[str]

    def parse(self, argv: Sequence[str]) -> dict[str, Any]:
        """Parse ``argv``; raises ``_HelpRequested`` / ``_UsageError``."""
        return _parse(self.parser, self.specs, argv)

    def format_help(self) -> str:
        return self.parser.format_help()


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """What :meth:`CommandRegistry.run` reports back to a runner."""

    exit_code: int
    value: Any = None
    exception: BaseException | None = None


class CommandRegistry:
    """The commands of one app, by name and alias (built once per app)."""

    __slots__ = ("_commands", "_aliases")

    def __init__(
        self, commands: dict[str, CompiledCommand] | None = None, aliases: dict[str, str] | None = None
    ) -> None:
        self._commands: dict[str, CompiledCommand] = dict(commands or {})
        self._aliases: dict[str, str] = dict(aliases or {})

    def __len__(self) -> int:
        return len(self._commands)

    def lookup(self, name: str) -> CompiledCommand | None:
        """The command called ``name`` (or aliased so), else ``None``."""
        found = self._commands.get(name)
        if found is None and name in self._aliases:
            found = self._commands[self._aliases[name]]
        return found

    def infos(self, *, include_hidden: bool = False) -> tuple[CommandInfo, ...]:
        """Every command's :class:`CommandInfo`, sorted by name."""
        return tuple(
            command.info
            for name, command in sorted(self._commands.items())
            if include_hidden or not command.meta.hidden
        )

    def unknown(self, name: str) -> str:
        """The message for an unknown command name (with close matches)."""
        visible = [n for n, c in self._commands.items() if not c.meta.hidden]
        visible += [a for a, n in self._aliases.items() if not self._commands[n].meta.hidden]
        close = difflib.get_close_matches(name, visible, n=3)
        if close:
            return f"unknown command {name!r}; did you mean {', '.join(repr(c) for c in close)}?"
        known = (
            ", ".join(sorted(n for n, c in self._commands.items() if not c.meta.hidden)) or "none declared"
        )
        return f"unknown command {name!r} (known commands: {known})"

    def prepare(
        self, name: str, argv: Sequence[str], *, stdout: TextIO, stderr: TextIO
    ) -> PreparedCommand | CommandOutcome:
        """Look up ``name`` and parse ``argv`` without touching the app.

        Returns a :class:`PreparedCommand`, or the final :class:`CommandOutcome`
        for an unknown command (2), ``--help`` (0, help on ``stdout``) and a
        usage error (2, usage on ``stderr``). ``--yes`` / ``--verbose`` in
        ``argv`` are runner flags unless the command declares those options.
        """
        command = self.lookup(name)
        if command is None:
            _write(stderr, self.unknown(name) + "\n")
            return CommandOutcome(2)
        args, assume_yes, verbose = _split_runner_flags(command, argv)
        try:
            arguments = command.parse(args)
        except _HelpRequested:
            _write(stdout, command.format_help())
            return CommandOutcome(0)
        except _UsageError as exc:
            _write(stderr, f"{command.parser.format_usage()}{command.parser.prog}: error: {exc.message}\n")
            return CommandOutcome(2)
        return PreparedCommand(command, tuple(args), arguments, assume_yes, verbose)

    async def execute(
        self,
        app: LaurenApp,
        prepared: PreparedCommand,
        *,
        stdout: TextIO,
        stderr: TextIO,
        stdin: TextIO | None = None,
        interactive: bool = False,
        assume_yes: bool = False,
        verbose: bool = False,
    ) -> CommandOutcome:
        """Run a prepared command in a fresh operation scope (the app must be started).

        Command failures become exit codes and are never raised;
        ``asyncio.CancelledError`` propagates after the scope is torn down.
        """
        command = prepared.command
        verbose = verbose or prepared.verbose
        ctx = CommandContext(
            name=command.meta.name,
            argv=prepared.argv,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            interactive=interactive,
            assume_yes=assume_yes or prepared.assume_yes,
            log=bound_logger(app.logger, f"Command:{command.meta.name}", command=command.meta.name),
        )
        try:
            value = await _invoke(app, command, ctx, prepared.arguments)
        except SystemExit as exc:
            return CommandOutcome(_exit_code(exc.code, stderr))
        except Exception as exc:
            if verbose:
                _write(stderr, "".join(traceback.format_exception(exc)))
            else:
                _write(stderr, f"error: {type(exc).__name__}: {exc} (run with --verbose for the traceback)\n")
            return CommandOutcome(1, None, exc)
        return _render_value(value, app, stdout, stderr)

    async def run(
        self,
        app: LaurenApp,
        name: str,
        argv: Sequence[str],
        *,
        stdout: TextIO,
        stderr: TextIO,
        stdin: TextIO | None = None,
        interactive: bool = False,
        assume_yes: bool = False,
        verbose: bool = False,
    ) -> CommandOutcome:
        """:meth:`prepare` then :meth:`execute`."""
        prepared = self.prepare(name, argv, stdout=stdout, stderr=stderr)
        if isinstance(prepared, CommandOutcome):
            return prepared
        return await self.execute(
            app,
            prepared,
            stdout=stdout,
            stderr=stderr,
            stdin=stdin,
            interactive=interactive,
            assume_yes=assume_yes,
            verbose=verbose,
        )


@dataclass(frozen=True, slots=True)
class PreparedCommand:
    """A command whose ``argv`` parsed cleanly (``CommandRegistry.prepare``)."""

    command: CompiledCommand
    argv: tuple[str, ...]
    arguments: dict[str, Any]
    assume_yes: bool
    verbose: bool


def _split_runner_flags(command: CompiledCommand, argv: Sequence[str]) -> tuple[list[str], bool, bool]:
    args: list[str] = []
    assume_yes = verbose = False
    for index, token in enumerate(argv):
        if token == "--":
            args.extend(argv[index:])
            break
        if token == "--yes" and token not in command.option_strings:
            assume_yes = True
        elif token == "--verbose" and token not in command.option_strings:
            verbose = True
        else:
            args.append(token)
    return args, assume_yes, verbose


async def _invoke(
    app: LaurenApp, command: CompiledCommand, ctx: CommandContext, arguments: dict[str, Any]
) -> Any:
    entry = command.entry
    values: dict[Any, Any] = {CommandContext: ctx}
    async with app.scope(f"command:{command.meta.name}", values=values) as scope:
        instance = None
        if entry.plan.binding == "instance":
            # The exact provider (multi-binding slot, use_class target), cached in
            # the scope, so a request-scoped command class is torn down on exit.
            instance = await app.container._instantiate(entry.provider, scope._cache, values)  # noqa: SLF001
        kwargs = await entry.plan.build_kwargs(scope, values=values, arguments=arguments)
        return await entry.plan.call(entry.bind(instance), kwargs)


def _exit_code(code: Any, stderr: TextIO) -> int:
    """``SystemExit.code`` → exit status, the way the interpreter maps it."""
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    _write(stderr, f"{code}\n")
    return 1


def _render_value(value: Any, app: LaurenApp, stdout: TextIO, stderr: TextIO) -> CommandOutcome:
    if value is None:
        return CommandOutcome(0)
    if isinstance(value, bool):
        return CommandOutcome(0 if value else 1, value)
    if isinstance(value, int):
        return CommandOutcome(value, value)
    try:
        text = app.json_encoder.encode(value).decode("utf-8")
    except Exception as exc:
        _write(
            stderr,
            f"error: the command returned a {type(value).__name__} that cannot be encoded as JSON: {exc}\n",
        )
        return CommandOutcome(1, value, exc)
    _write(stdout, text + "\n")
    return CommandOutcome(0, value)


# ---------------------------------------------------------------------------
# Phase 6a
# ---------------------------------------------------------------------------


def _class_label(cls: type) -> str:
    return cls.__qualname__.rsplit("<locals>.", 1)[-1]


def _doc_line(fn: Any) -> str | None:
    doc = inspect.getdoc(fn)
    if not doc:
        return None
    first = doc.strip().splitlines()[0].strip()
    return first or None


def compile_commands(discovery: DiscoveryIndex) -> CommandRegistry:
    """Phase 6a: validate every ``@command`` method and build its parser.

    Raises :class:`~lauren.exceptions.DuplicateBindingError` when two
    commands share a name or alias,
    :class:`~lauren.exceptions.UnresolvableParameterError` for a parameter
    that is neither injectable nor a CLI type, and
    :class:`~lauren.exceptions.DecoratorUsageError` for a misplaced ``Opt``.
    """
    entries = discovery.methods_for(COMMAND_META)
    if not entries:
        return CommandRegistry()
    commands: dict[str, CompiledCommand] = {}
    aliases: dict[str, str] = {}
    claimed: dict[str, str] = {}
    for entry in entries:
        target = f"{_class_label(entry.owner)}.{entry.attr_name}"
        metas = [m for m in _as_list(entry.meta) if isinstance(m, CommandMeta)]
        params = tuple(p for p in entry.plan.params if p.kind == ARGUMENT)
        for meta in metas:
            for name in (meta.name, *meta.aliases):
                other = claimed.get(name)
                if other is not None:
                    raise DuplicateBindingError(
                        f"two commands are named {name!r} ({other} and {target}); rename one or drop the alias",
                        detail={"name": name, "targets": [other, target], "reason": "duplicate_command_name"},
                    )
                claimed[name] = target
            specs = tuple(_compile_spec(p, command=meta.name, target=target) for p in params)
            help_line = meta.help or _doc_line(entry.function)
            parser = _new_parser(meta.name, help_line)
            for spec in specs:
                _add_to_parser(parser, spec, command=meta.name, target=target)
            commands[meta.name] = CompiledCommand(
                meta=meta,
                entry=entry,
                specs=specs,
                parser=parser,
                info=CommandInfo(
                    name=meta.name,
                    aliases=meta.aliases,
                    help=help_line,
                    hidden=meta.hidden,
                    parameters=tuple(spec.info() for spec in specs),
                    module=entry.provider.owning_module,
                    target=target,
                ),
                option_strings=frozenset(flag for spec in specs for flag in spec.flags),
            )
            aliases.update({alias: meta.name for alias in meta.aliases})
    return CommandRegistry(commands, aliases)


def _as_list(meta: Any) -> Iterable[Any]:
    return meta if isinstance(meta, list) else [meta]


__all__ = [
    "CommandInfo",
    "CommandOutcome",
    "CommandParameter",
    "CommandRegistry",
    "CompiledCommand",
    "PreparedCommand",
    "compile_commands",
]
