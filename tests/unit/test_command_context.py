"""Unit tests for ``CommandContext`` and ``CommandResult``."""

from __future__ import annotations

import dataclasses
import io
from typing import Any

import pytest

from lauren.commands import CommandContext, CommandResult
from lauren.logging import NullLogger


def _ctx(*, stdin: str | None = None, interactive: bool = False, assume_yes: bool = False) -> CommandContext:
    return CommandContext(
        name="db:seed",
        argv=("--count", "3"),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
        stdin=io.StringIO(stdin) if stdin is not None else None,
        interactive=interactive,
        assume_yes=assume_yes,
        log=NullLogger(),
    )


def _out(ctx: CommandContext) -> str:
    stream: Any = ctx.stdout
    return str(stream.getvalue())


def _err(ctx: CommandContext) -> str:
    stream: Any = ctx.stderr
    return str(stream.getvalue())


def test_properties_expose_the_invocation() -> None:
    ctx = _ctx(interactive=True, assume_yes=True)

    assert (ctx.name, ctx.argv, ctx.is_interactive, ctx.assume_yes) == (
        "db:seed",
        ("--count", "3"),
        True,
        True,
    )
    assert isinstance(ctx.log, NullLogger)
    assert "db:seed" in repr(ctx)


def test_echo_writes_to_stdout_like_print() -> None:
    ctx = _ctx()

    ctx.echo("seeded", 3, "rows")
    ctx.echo("a", "b", sep="-", end="!")

    assert _out(ctx) == "seeded 3 rows\na-b!"
    assert _err(ctx) == ""


def test_error_writes_to_stderr() -> None:
    ctx = _ctx()

    ctx.error("bad", "thing")

    assert _err(ctx) == "bad thing\n"


def test_confirm_with_yes_answers_without_asking() -> None:
    ctx = _ctx(assume_yes=True)

    assert ctx.confirm("Truncate?") is True
    assert _out(ctx) == ""


def test_confirm_refuses_when_not_interactive() -> None:
    ctx = _ctx(stdin="y\n", interactive=False)

    assert ctx.confirm("Truncate?") is False
    assert "Truncate? refused: not running interactively (pass --yes to confirm)" in _err(ctx)


def test_confirm_refuses_without_a_stdin() -> None:
    ctx = _ctx(interactive=True)

    assert ctx.confirm("Truncate?") is False


@pytest.mark.parametrize(
    ("answer", "default", "expected"),
    [
        ("y\n", False, True),
        ("YES\n", False, True),
        ("n\n", True, False),
        ("\n", True, True),
        ("", False, False),
    ],
)
def test_confirm_reads_the_answer_when_interactive(answer: str, default: bool, expected: bool) -> None:
    ctx = _ctx(stdin=answer, interactive=True)

    assert ctx.confirm("Truncate?", default=default) is expected
    assert _out(ctx) == f"Truncate? {'[Y/n]' if default else '[y/N]'} "


def test_fail_writes_the_message_and_raises_system_exit() -> None:
    ctx = _ctx()

    with pytest.raises(SystemExit) as info:
        ctx.fail("no database", exit_code=4)

    assert info.value.code == 4
    assert _err(ctx) == "no database\n"


@pytest.mark.parametrize("code", [True, "2", 1.0])
def test_fail_rejects_a_non_int_exit_code(code: Any) -> None:
    with pytest.raises(TypeError):
        _ctx().fail("x", exit_code=code)


def test_command_result_defaults_and_immutability() -> None:
    result = CommandResult(exit_code=0)

    assert (result.value, result.stdout, result.stderr, result.exception) == (None, "", "", None)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.exit_code = 1  # type: ignore[misc]
