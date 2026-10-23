"""``CommandContext`` and ``CommandResult`` (private module, public through :mod:`lauren.commands`)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, NoReturn, TextIO

from ..logging import Logger


class CommandContext:
    """What a running command gets besides its arguments and dependencies.

    Annotate a command parameter (or a request-scoped provider's constructor
    parameter) with ``CommandContext`` to receive it. Write output with
    :meth:`echo` / :meth:`error` rather than ``print()`` so the runner and
    ``ctx.run_command`` (tests) can capture it.
    """

    __slots__ = ("_name", "_argv", "_stdout", "_stderr", "_stdin", "_interactive", "_assume_yes", "_log")

    def __init__(
        self,
        *,
        name: str,
        argv: tuple[str, ...],
        stdout: TextIO,
        stderr: TextIO,
        stdin: TextIO | None,
        interactive: bool,
        assume_yes: bool,
        log: Logger,
    ) -> None:
        self._name = name
        self._argv = argv
        self._stdout = stdout
        self._stderr = stderr
        self._stdin = stdin
        self._interactive = interactive
        self._assume_yes = assume_yes
        self._log = log

    @property
    def name(self) -> str:
        """The command's name (its canonical name, also when invoked through an alias)."""
        return self._name

    @property
    def argv(self) -> tuple[str, ...]:
        """The arguments the command was invoked with (runner flags removed)."""
        return self._argv

    @property
    def stdout(self) -> TextIO:
        """The output stream (a ``StringIO`` under ``ctx.run_command``)."""
        return self._stdout

    @property
    def stderr(self) -> TextIO:
        """The error stream (a ``StringIO`` under ``ctx.run_command``)."""
        return self._stderr

    @property
    def is_interactive(self) -> bool:
        """``True`` when a person can answer prompts (the runner's stdin is a terminal)."""
        return self._interactive

    @property
    def assume_yes(self) -> bool:
        """``True`` when the command runs with ``--yes``: :meth:`confirm` answers yes."""
        return self._assume_yes

    @property
    def log(self) -> Logger:
        """The application logger, bound to ``context="Command:<name>"``."""
        return self._log

    def echo(self, *parts: Any, sep: str = " ", end: str = "\n") -> None:
        """Write ``parts`` to :attr:`stdout`, like ``print()``."""
        _write(self._stdout, sep.join(str(part) for part in parts) + end)

    def error(self, *parts: Any, sep: str = " ", end: str = "\n") -> None:
        """Write ``parts`` to :attr:`stderr`, like ``print(..., file=sys.stderr)``."""
        _write(self._stderr, sep.join(str(part) for part in parts) + end)

    def confirm(self, prompt: str, *, default: bool = False) -> bool:
        """Ask a yes/no question and return the answer.

        ``--yes`` answers ``True`` without asking. When nobody can answer (not
        interactive) it refuses — writes a note to :attr:`stderr` and returns
        ``False`` — instead of waiting forever. An empty answer (or end of
        input) returns ``default``.
        """
        if self._assume_yes:
            return True
        if not self._interactive or self._stdin is None:
            self.error(f"{prompt} refused: not running interactively (pass --yes to confirm)")
            return False
        _write(self._stdout, f"{prompt} {'[Y/n]' if default else '[y/N]'} ")
        answer = self._stdin.readline().strip().lower()
        if not answer:
            return default
        return answer in ("y", "yes")

    def fail(self, message: str, exit_code: int = 1) -> NoReturn:
        """Stop the command: write ``message`` to :attr:`stderr` and exit with ``exit_code``.

        Raises :class:`SystemExit`, so ``finally`` blocks run and the command's
        scope is torn down before the runner exits.
        """
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            raise TypeError(f"fail(exit_code=...) must be an int, got {exit_code!r}")
        self.error(message)
        raise SystemExit(exit_code)

    def __repr__(self) -> str:
        return f"<CommandContext {self._name!r} argv={list(self._argv)!r}>"


def _write(stream: TextIO, text: str) -> None:
    stream.write(text)
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


@dataclass(frozen=True, slots=True)
class CommandResult:
    """The outcome of :meth:`ApplicationContext.run_command <lauren.ApplicationContext.run_command>`.

    ``exit_code`` follows the runner's table (0 success, 1 uncaught error,
    2 usage error, the returned ``int``, …); ``value`` is what the command
    returned (``None`` when it did not finish); ``stdout`` / ``stderr`` hold
    the captured output; ``exception`` is the uncaught exception, if any.
    """

    exit_code: int
    value: Any = None
    stdout: str = ""
    stderr: str = ""
    exception: BaseException | None = None


__all__ = ["CommandContext", "CommandResult"]
