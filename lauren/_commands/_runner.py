"""``python -m lauren``: run commands, list them, or open a console (private module).

The runner is stdlib-only and stateless: each invocation resolves one TARGET,
builds exactly one :class:`~lauren.context.ApplicationContext`, and exits.
``lauren-cli`` (``lauren exec`` / ``commands`` / ``console``) calls
:func:`main` in-process, or :func:`load_context` plus the context API when it
renders its own output.
"""

from __future__ import annotations

import ast
import asyncio
import builtins
import code
import concurrent.futures
import contextlib
import importlib
import inspect
import json
import os
import signal
import sys
import threading
import traceback
import types
from collections.abc import Coroutine, Iterator, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TextIO, TypeVar

from ..exceptions import StartupError
from ._compile import CommandOutcome

if TYPE_CHECKING:
    from ..context import ApplicationContext

T = TypeVar("T")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_STARTUP = 3
TARGET_ENV = "LAUREN_TARGET"
PROG = "python -m lauren"

USAGE = f"""\
usage: {PROG} exec [-t TARGET] [-y] [-v] [TARGET] COMMAND [ARGS...]
       {PROG} commands [-t TARGET] [--all] [TARGET]
       {PROG} console [-t TARGET] [-v] [TARGET]
"""

HELP = (
    USAGE
    + """
Run the application's code with its DI graph and lifecycle, without serving HTTP.

actions:
  exec      start the app, run one @command, shut the app down
            (ARGS go to the command; `COMMAND --help` shows its help)
  commands  list the app's commands (the app is built, not started)
  console   an async Python REPL with ctx, app, container, get(), resolve()
            and scope() preloaded; top-level await works

TARGET is pkg.module:attr: a @module class, a LaurenApp, a Lauren app, an
ApplicationContext, or a zero-argument (sync or async) factory returning one.
It defaults to $LAUREN_TARGET.

options:
  -t, --target TARGET  the application (instead of the positional TARGET)
  -y, --yes            answer yes to confirmation prompts (exec; also accepted
                       after COMMAND)
  -v, --verbose        print full tracebacks (also accepted after COMMAND)
  --all                include hidden commands (commands)
  -h, --help           show this help and exit

exit codes: 0 success, 1 uncaught error, 2 usage error, 3 the application could
not be built or started, 130 / 143 interrupted by SIGINT / SIGTERM; a command
that returns an int exits with it.
"""
)


class _TargetError(Exception):
    """TARGET is malformed, cannot be imported, or names the wrong kind of object."""

    def __init__(self, message: str, exit_code: int = EXIT_STARTUP) -> None:
        super().__init__(message)
        self.exit_code = exit_code


@dataclass(slots=True)
class _Invocation:
    action: str
    target: str
    rest: list[str] = field(default_factory=list)
    yes: bool = False
    verbose: bool = False
    show_all: bool = False


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    stream.flush()


# ---------------------------------------------------------------------------
# Target resolution
# ---------------------------------------------------------------------------


def split_target(spec: str) -> tuple[str, str]:
    """``"pkg.module:attr.sub"`` → ``("pkg.module", "attr.sub")``; raises for bad syntax."""
    module_name, sep, attr = spec.partition(":")
    valid = bool(sep and module_name and attr) and all(
        part.isidentifier() for part in (*module_name.split("."), *attr.split("."))
    )
    if not valid:
        raise _TargetError(
            f"invalid TARGET {spec!r}: use pkg.module:attr, for example app.main:AppModule", EXIT_USAGE
        )
    return module_name, attr


def import_target(spec: str) -> Any:
    """Import the module of ``spec`` and return the named attribute."""
    module_name, attr = split_target(spec)
    try:
        obj: Any = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing = exc.name or ""
        if missing and (module_name == missing or module_name.startswith(missing + ".")):
            raise _TargetError(
                f"cannot import {module_name!r} from TARGET {spec!r}: {exc} "
                "(run from the project directory, or put it on PYTHONPATH)"
            ) from exc
        raise
    for part in attr.split("."):
        try:
            obj = getattr(obj, part)
        except AttributeError:
            raise _TargetError(f"TARGET {spec!r}: {module_name!r} has no attribute {attr!r}") from None
    return obj


async def context_for(obj: Any, spec: str = "TARGET", *, call_factory: bool = True) -> ApplicationContext:
    """The :class:`~lauren.context.ApplicationContext` for a resolved TARGET object.

    A ``@module`` class goes through :meth:`LaurenFactory.create_context`, a
    ``LaurenApp`` through :meth:`LaurenApp.context`, a ``Lauren`` app through
    :meth:`ApplicationContext.from_lauren`; a context is returned as is; a
    zero-argument callable (sync or async) is called once and its result
    resolved the same way.
    """
    from .._app import Lauren
    from .._asgi import LaurenApp, LaurenFactory
    from ..context import ApplicationContext
    from ..decorators import MODULE_META

    if isinstance(obj, ApplicationContext):
        return obj
    if isinstance(obj, LaurenApp):
        return obj.context()
    if isinstance(obj, Lauren):
        return ApplicationContext.from_lauren(obj)
    if isinstance(obj, type):
        if MODULE_META in obj.__dict__:
            return LaurenFactory.create_context(obj)
        raise _TargetError(f"TARGET {spec!r} is the class {obj.__name__}, which is not a @module")
    if call_factory and callable(obj):
        try:
            inspect.signature(obj).bind()
        except TypeError:
            raise _TargetError(
                f"TARGET {spec!r} is a callable that needs arguments; a factory must take none"
            ) from None
        except ValueError:  # pragma: no cover - builtins without a signature
            pass
        result = obj()
        if inspect.isawaitable(result):
            result = await result
        return await context_for(result, spec, call_factory=False)
    raise _TargetError(
        f"TARGET {spec!r} is a {type(obj).__name__}; expected a @module class, a LaurenApp, a Lauren app, "
        "an ApplicationContext, or a zero-argument factory returning one"
    )


async def load_context(spec: str) -> ApplicationContext:
    """Import ``spec`` (``pkg.module:attr``) and return its context (built, not started)."""
    return await context_for(import_target(spec), spec)


# ---------------------------------------------------------------------------
# Error rendering
# ---------------------------------------------------------------------------


def render_startup_error(exc: BaseException, *, verbose: bool = False, action: str = "build") -> str:
    """A ``StartupError`` (or any exception raised while building/starting) as text."""
    if isinstance(exc, StartupError):
        lines = [f"error: the application failed to {action}", f"{type(exc).__name__}: {exc.message or exc}"]
        for key, value in exc.detail.items():
            lines.append(f"  {key}: {json.dumps(value, default=str, ensure_ascii=False)}")
    else:
        lines = [f"error: the application failed to {action}: {type(exc).__name__}: {exc}"]
    text = "\n".join(lines) + "\n"
    if verbose:
        text += "".join(traceback.format_exception(exc))
    elif not isinstance(exc, StartupError):
        text += "(run with --verbose for the traceback)\n"
    return text


async def _load(spec: str, stderr: TextIO, *, verbose: bool) -> ApplicationContext | int:
    try:
        return await load_context(spec)
    except _TargetError as exc:
        _write(stderr, f"error: {exc}\n")
        return exc.exit_code
    except Exception as exc:
        _write(stderr, render_startup_error(exc, verbose=verbose))
        return EXIT_STARTUP


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def _parse_cli(argv: Sequence[str], stdout: TextIO, stderr: TextIO) -> _Invocation | int:
    def usage_error(message: str) -> int:
        _write(stderr, f"{USAGE}{PROG}: error: {message}\n")
        return EXIT_USAGE

    if not argv:
        return usage_error("missing action (exec, commands or console)")
    if argv[0] in ("-h", "--help"):
        _write(stdout, HELP)
        return EXIT_OK
    action, tokens = argv[0], list(argv[1:])
    if action not in ("exec", "commands", "console"):
        return usage_error(f"unknown action {action!r} (use exec, commands or console)")
    target: str | None = None
    target_from_env = os.environ.get(TARGET_ENV) or None
    positionals: list[str] = []
    yes = verbose = show_all = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if (
            action == "exec"
            and positionals
            and (target is not None or target_from_env or len(positionals) == 2)
        ):
            break  # COMMAND reached: everything from here belongs to the command
        if token in ("-h", "--help"):
            _write(stdout, HELP)
            return EXIT_OK
        if token in ("-t", "--target"):
            if index + 1 >= len(tokens):
                return usage_error(f"{token} needs a value")
            target = tokens[index + 1]
            index += 2
            continue
        if token == "--":  # end of runner options
            index += 1
            break
        if token.startswith("--target="):
            target = token.partition("=")[2]
        elif token in ("-v", "--verbose"):
            verbose = True
        elif token in ("-y", "--yes") and action == "exec":
            yes = True
        elif token == "--all" and action == "commands":
            show_all = True
        elif token.startswith("-") and token != "-":
            return usage_error(f"unknown option {token!r} for {action}")
        else:
            positionals.append(token)
        index += 1
    rest = positionals + tokens[index:]
    if target is None:
        target = target_from_env
    if target is None:
        if not rest:
            return usage_error(f"missing TARGET (pkg.module:attr, or set ${TARGET_ENV})")
        target, rest = rest[0], rest[1:]
    if action == "exec" and not rest:
        return usage_error("missing COMMAND (list them with `python -m lauren commands TARGET`)")
    if action != "exec" and rest:
        return usage_error(f"unexpected arguments: {' '.join(rest)}")
    return _Invocation(action=action, target=target, rest=rest, yes=yes, verbose=verbose, show_all=show_all)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    stdin: TextIO | None = None,
) -> int:
    """Run ``python -m lauren`` with ``argv`` (default ``sys.argv[1:]``); return the exit code."""
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    inp = stdin if stdin is not None else sys.stdin
    parsed = _parse_cli(list(sys.argv[1:] if argv is None else argv), out, err)
    if isinstance(parsed, int):
        return parsed
    _ensure_cwd_on_path()
    if parsed.action == "exec":
        return asyncio.run(_exec(parsed, out, err, inp))
    if parsed.action == "commands":
        return asyncio.run(_list(parsed, out, err))
    return _console(parsed, out, err)


def _ensure_cwd_on_path() -> None:
    """``python -m`` puts the working directory on ``sys.path``; console-script launchers do not."""
    cwd = os.getcwd()
    if "" not in sys.path and cwd not in sys.path:
        sys.path.insert(0, cwd)


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


class _Interrupts:
    """SIGINT / SIGTERM: the first cancels the running command, the second exits at once."""

    __slots__ = ("target", "signum")

    def __init__(self, target: asyncio.Task[Any] | None) -> None:
        self.target = target
        self.signum: int | None = None

    @property
    def exit_code(self) -> int:
        return 128 + (self.signum or 0)

    def handle(self, signum: int) -> None:
        if self.signum is not None:
            os._exit(128 + self.signum)  # second signal: no more teardown
            return
        self.signum = signum
        if self.target is not None and not self.target.done():
            self.target.cancel()

    @contextlib.contextmanager
    def installed(self) -> Iterator[None]:
        from ..context import _install_signal_handlers

        restore, _ = _install_signal_handlers(
            asyncio.get_running_loop(), (signal.SIGINT, signal.SIGTERM), self.handle
        )
        try:
            yield
        finally:
            restore()


async def _exec(inv: _Invocation, stdout: TextIO, stderr: TextIO, stdin: TextIO) -> int:
    loaded = await _load(inv.target, stderr, verbose=inv.verbose)
    if isinstance(loaded, int):
        return loaded
    ctx = loaded
    registry = ctx.app._commands
    prepared = registry.prepare(inv.rest[0], inv.rest[1:], stdout=stdout, stderr=stderr)
    if isinstance(prepared, CommandOutcome):  # unknown command, --help, usage error: never start the app
        return prepared.exit_code
    interrupts = _Interrupts(asyncio.current_task())
    with interrupts.installed():
        try:
            await ctx.__aenter__()
        except asyncio.CancelledError:
            if interrupts.signum is None:
                raise
            return interrupts.exit_code  # the context already shut down what had started
        except Exception as exc:
            _write(stderr, render_startup_error(exc, verbose=inv.verbose or prepared.verbose, action="start"))
            return EXIT_STARTUP
        code = EXIT_ERROR
        try:
            task = asyncio.ensure_future(
                registry.execute(
                    ctx.app,
                    prepared,
                    stdout=stdout,
                    stderr=stderr,
                    stdin=stdin,
                    interactive=_isatty(stdin),
                    assume_yes=inv.yes,
                    verbose=inv.verbose,
                )
            )
            interrupts.target = task
            try:
                code = (await task).exit_code
            except asyncio.CancelledError:
                if interrupts.signum is None:
                    raise
                _write(stderr, f"interrupted by {signal.Signals(interrupts.signum).name}; shutting down\n")
        finally:
            interrupts.target = None  # a first signal during shutdown only sets the exit code
            await ctx.__aexit__(None, None, None)
    return interrupts.exit_code if interrupts.signum is not None else code


def _isatty(stream: Any) -> bool:
    isatty = getattr(stream, "isatty", None)
    try:
        return bool(isatty()) if callable(isatty) else False
    except (ValueError, OSError):  # closed stream
        return False


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


async def _list(inv: _Invocation, stdout: TextIO, stderr: TextIO) -> int:
    loaded = await _load(inv.target, stderr, verbose=inv.verbose)
    if isinstance(loaded, int):
        return loaded
    infos = loaded.commands(include_hidden=inv.show_all)
    if not infos:
        _write(stdout, f"no commands declared in {inv.target}\n")
        return EXIT_OK
    labels = []
    for info in infos:
        label = info.name + (f" ({', '.join(info.aliases)})" if info.aliases else "")
        labels.append(label + (" [hidden]" if info.hidden else ""))
    width = max(len(label) for label in labels)
    lines = [f"  {label:<{width}}  {info.help or ''}".rstrip() for label, info in zip(labels, infos)]
    _write(stdout, "commands:\n" + "\n".join(lines) + "\n")
    return EXIT_OK


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------


class _AsyncConsole(code.InteractiveConsole):
    """An interactive console whose top-level ``await`` runs on the app's event loop.

    Input is read on the calling (main) thread, so Ctrl-C behaves as in the
    normal REPL; coroutine code runs on ``loop`` (a background thread) and
    the console waits for it.
    """

    def __init__(self, namespace: dict[str, Any], loop: asyncio.AbstractEventLoop) -> None:
        super().__init__(namespace, filename="<lauren console>")
        self.compile.compiler.flags |= ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        self._loop = loop

    def runcode(self, code_obj: types.CodeType) -> None:  # type: ignore[override]
        future: concurrent.futures.Future[Any] | None = None
        try:
            result = types.FunctionType(code_obj, self.locals)()  # type: ignore[arg-type]
            if inspect.iscoroutine(result):
                future = asyncio.run_coroutine_threadsafe(result, self._loop)
                future.result()
        except SystemExit:
            raise
        except BaseException:
            if future is not None and not future.done():
                future.cancel()
            self.showtraceback()


def _console(inv: _Invocation, stdout: TextIO, stderr: TextIO) -> int:
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, name="lauren-console-loop", daemon=True)
    thread.start()

    def call(coro: Coroutine[Any, Any, T]) -> T:
        return asyncio.run_coroutine_threadsafe(coro, loop).result()

    try:
        loaded = call(_load(inv.target, stderr, verbose=inv.verbose))
        if isinstance(loaded, int):
            return loaded
        ctx = loaded
        try:
            call(ctx.__aenter__())
        except Exception as exc:
            _write(stderr, render_startup_error(exc, verbose=inv.verbose, action="start"))
            return EXIT_STARTUP
        try:
            namespace: dict[str, Any] = {
                "__name__": "__console__",
                "__builtins__": builtins,
                "asyncio": asyncio,
                "ctx": ctx,
                "app": ctx.app,
                "container": ctx.container,
                "get": ctx.get,
                "resolve": ctx.resolve,
                "scope": ctx.scope,
            }
            banner = (
                f"lauren console: {inv.target} (started; hosted services and the scheduler are off)\n"
                "Preloaded: ctx, app, container, get(token), await resolve(token), scope(name), asyncio.\n"
                "Top-level await works. exit() or Ctrl-D shuts the app down."
            )
            try:
                _AsyncConsole(namespace, loop).interact(banner=banner, exitmsg="")
            except SystemExit:
                pass
        finally:
            call(ctx.__aexit__(None, None, None))
        return EXIT_OK
    finally:
        with contextlib.suppress(Exception):
            call(_drain_loop())
        loop.call_soon_threadsafe(loop.stop)
        thread.join()
        loop.close()


async def _drain_loop() -> None:
    """What ``asyncio.run`` does before closing a loop: cancel leftovers, close generators and executor."""
    current = asyncio.current_task()
    leftovers = [task for task in asyncio.all_tasks() if task is not current]
    for task in leftovers:
        task.cancel()
    await asyncio.gather(*leftovers, return_exceptions=True)
    loop = asyncio.get_running_loop()
    await loop.shutdown_asyncgens()
    await loop.shutdown_default_executor()


__all__ = [
    "EXIT_ERROR",
    "EXIT_OK",
    "EXIT_STARTUP",
    "EXIT_USAGE",
    "TARGET_ENV",
    "context_for",
    "import_target",
    "load_context",
    "main",
    "render_startup_error",
    "split_target",
]
