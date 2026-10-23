"""In-process tests for ``python -m lauren`` (``lauren._commands._runner.main``).

The targets live in ``tests/integration/cli_app.py``; the subprocess checks
are in ``tests/e2e/test_cli_exec_e2e.py``.
"""

from __future__ import annotations

import io
import os
import signal
import sys
from collections.abc import Iterator
from typing import Any

import pytest

from lauren._commands import _runner
from lauren._commands._runner import HELP, TARGET_ENV, _AsyncConsole, _Interrupts, _parse_cli, main

from . import cli_app

TARGET = "tests.integration.cli_app:AppModule"
posix_only = pytest.mark.skipif(sys.platform == "win32", reason="delivers real signals to the test process")


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch) -> None:
    cli_app.EVENTS.clear()
    monkeypatch.delenv(TARGET_ENV, raising=False)


class Run:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code, self.stdout, self.stderr = code, stdout, stderr


def run(*argv: str, stdin: str = "") -> Run:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err, stdin=io.StringIO(stdin))
    return Run(code, out.getvalue(), err.getvalue())


# ---------------------------------------------------------------------------
# Command line parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["exec", TARGET, "greet", "--shout"], ("exec", TARGET, ["greet", "--shout"], False, False)),
        (["exec", "-y", "-v", TARGET, "greet"], ("exec", TARGET, ["greet"], True, True)),
        (["exec", TARGET, "--yes", "greet", "-v"], ("exec", TARGET, ["greet", "-v"], True, False)),
        (["exec", "-t", TARGET, "greet", "ada"], ("exec", TARGET, ["greet", "ada"], False, False)),
        (["exec", f"--target={TARGET}", "greet"], ("exec", TARGET, ["greet"], False, False)),
        (["exec", TARGET, "--", "greet"], ("exec", TARGET, ["greet"], False, False)),
        (["commands", TARGET, "--all"], ("commands", TARGET, [], False, False)),
        (["console", "-v", TARGET], ("console", TARGET, [], False, True)),
    ],
)
def test_parse_cli(argv: list[str], expected: tuple[Any, ...]) -> None:
    parsed = _parse_cli(argv, io.StringIO(), io.StringIO())

    assert not isinstance(parsed, int)
    assert (parsed.action, parsed.target, parsed.rest, parsed.yes, parsed.verbose) == expected


def test_parse_cli_reads_the_target_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TARGET_ENV, TARGET)

    parsed = _parse_cli(["exec", "greet", "--shout"], io.StringIO(), io.StringIO())

    assert not isinstance(parsed, int)
    assert (parsed.target, parsed.rest) == (TARGET, ["greet", "--shout"])


def test_all_flag_is_parsed_for_commands() -> None:
    parsed = _parse_cli(["commands", "--all", TARGET], io.StringIO(), io.StringIO())

    assert not isinstance(parsed, int) and parsed.show_all


@pytest.mark.parametrize("argv", [["--help"], ["-h"], ["exec", "--help"], ["commands", TARGET, "-h"]])
def test_help_prints_usage_and_exit_codes(argv: list[str]) -> None:
    result = run(*argv)

    assert (result.code, result.stdout) == (0, HELP)
    assert "exit codes" in result.stdout


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        ([], "missing action"),
        (["serve"], "unknown action 'serve'"),
        (["exec"], "missing TARGET"),
        (["exec", TARGET], "missing COMMAND"),
        (["exec", "-t"], "-t needs a value"),
        (["exec", "--bogus", TARGET, "greet"], "unknown option '--bogus' for exec"),
        (["commands", TARGET, "extra"], "unexpected arguments: extra"),
        (["commands", "--yes", TARGET], "unknown option '--yes' for commands"),
        (["console", TARGET, "--all"], "unknown option '--all' for console"),
    ],
)
def test_usage_errors_exit_2(argv: list[str], message: str) -> None:
    result = run(*argv)

    assert result.code == 2
    assert result.stderr.startswith("usage: python -m lauren exec")
    assert message in result.stderr


def test_the_working_directory_is_put_on_sys_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(tmp_path))])

    _runner._ensure_cwd_on_path()

    assert sys.path[0] == str(tmp_path)


# ---------------------------------------------------------------------------
# exec
# ---------------------------------------------------------------------------


def test_exec_runs_the_command_with_the_full_lifecycle() -> None:
    result = run("exec", TARGET, "greet", "ada", "--shout")

    assert (result.code, result.stdout, result.stderr) == (0, "HELLO ADA\n", "")
    assert cli_app.EVENTS == ["db.open", "db.close"]


def test_exec_exit_code_comes_from_the_return_value() -> None:
    assert run("exec", TARGET, "add", "2", "3").code == 5


def test_exec_prints_other_return_values_as_json() -> None:
    result = run("exec", TARGET, "info")

    assert (result.code, result.stdout) == (0, '{"ok": true}\n')


def test_exec_reports_an_uncaught_exception() -> None:
    result = run("exec", TARGET, "boom")

    assert result.code == 1
    assert "error: RuntimeError: kaboom" in result.stderr
    assert cli_app.EVENTS == ["db.open", "session.close", "db.close"]


@pytest.mark.parametrize("where", ["before", "after"])
def test_exec_verbose_prints_the_traceback(where: str) -> None:
    argv = ["exec", "-v", TARGET, "boom"] if where == "before" else ["exec", TARGET, "boom", "--verbose"]

    result = run(*argv)

    assert "Traceback (most recent call last)" in result.stderr


@pytest.mark.parametrize("argv", [["nope"], ["greet", "--help"], ["add", "1"]])
def test_unknown_commands_help_and_usage_errors_never_start_the_app(argv: list[str]) -> None:
    result = run("exec", TARGET, *argv)

    assert result.code == (0 if "--help" in argv else 2)
    assert cli_app.EVENTS == []


def test_exec_help_shows_the_command_help() -> None:
    result = run("exec", TARGET, "hello", "--help")

    assert "usage: greet [-h] [--shout | --no-shout] [name]" in result.stdout
    assert "Say hello." in result.stdout


@pytest.mark.parametrize("flag", ["-y", "--yes"])
def test_exec_yes_confirms(flag: str) -> None:
    argv = ["exec", flag, TARGET, "confirm"] if flag == "-y" else ["exec", TARGET, "confirm", flag]

    assert run(*argv).code == 0


def test_exec_refuses_a_confirmation_without_a_terminal() -> None:
    result = run("exec", TARGET, "confirm", stdin="y\n")

    assert result.code == 1
    assert "refused" in result.stderr


def test_exec_with_target_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(TARGET_ENV, TARGET)

    assert run("exec", "add", "1", "1").code == 2  # 1 + 1


@pytest.mark.parametrize(
    "target",
    [
        "tests.integration.cli_app:create_app",
        "tests.integration.cli_app:acreate_app",
        "tests.integration.cli_app:make_lauren",
    ],
)
def test_exec_accepts_factories_and_imperative_apps(target: str) -> None:
    result = run("exec", target, "greet")

    assert (result.code, result.stdout) == (0, "hello world\n")


def test_a_startup_failure_exits_3() -> None:
    result = run("exec", "tests.integration.cli_app:BrokenStartupModule", "noop")

    assert result.code == 3
    assert "error: the application failed to start: RuntimeError: cannot connect" in result.stderr


@pytest.mark.parametrize(
    "target", ["tests.integration.cli_app:BrokenGraphModule", "tests.integration.cli_app:broken_graph_app"]
)
def test_a_startup_error_while_building_exits_3_with_its_detail(target: str) -> None:
    result = run("exec", target, "noop")

    assert result.code == 3
    assert "error: the application failed to build" in result.stderr
    assert "MissingProviderError" in result.stderr
    assert "Unregistered" in result.stderr


@pytest.mark.parametrize(
    ("target", "code", "message"),
    [
        ("cli_app", 2, "invalid TARGET"),
        ("tests.integration.cli_app:Missing", 3, "has no attribute 'Missing'"),
        ("tests.integration.cli_app:NotAModule", 3, "not a @module"),
        ("tests.integration.no_such_module:App", 3, "cannot import"),
    ],
)
def test_target_errors(target: str, code: int, message: str) -> None:
    result = run("exec", target, "greet")

    assert result.code == code
    assert message in result.stderr


def test_an_exception_while_importing_the_target_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(spec: str) -> Any:
        raise ZeroDivisionError("division by zero at import")

    monkeypatch.setattr(_runner, "import_target", broken)

    result = run("exec", TARGET, "greet")

    assert result.code == 3
    assert "ZeroDivisionError" in result.stderr


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------


@posix_only
@pytest.mark.parametrize(("which", "code"), [("SIGINT", 130), ("SIGTERM", 143)])
def test_a_signal_cancels_the_command_tears_down_and_exits_128_plus_n(which: str, code: int) -> None:
    previous = signal.getsignal(getattr(signal, which))

    result = run("exec", TARGET, "interrupt", "--which", which)

    assert result.code == code
    assert f"interrupted by {which}; shutting down" in result.stderr
    assert cli_app.EVENTS == ["db.open", "interrupt.raise", "interrupt.finally", "session.close", "db.close"]
    assert signal.getsignal(getattr(signal, which)) == previous


@posix_only
def test_a_command_swallowing_the_cancellation_still_exits_130() -> None:
    assert run("exec", TARGET, "survive").code == 130


@posix_only
def test_a_signal_during_startup_exits_130_after_cleanup() -> None:
    result = run("exec", "tests.integration.cli_app:InterruptedStartupModule", "noop")

    assert result.code == 130
    assert "slow.open" in cli_app.EVENTS


def test_a_second_signal_exits_immediately(monkeypatch: pytest.MonkeyPatch) -> None:
    exits: list[int] = []
    monkeypatch.setattr(os, "_exit", exits.append)
    interrupts = _Interrupts(None)

    interrupts.handle(signal.SIGTERM)
    interrupts.handle(signal.SIGINT)

    assert interrupts.exit_code == 143
    assert exits == [143]


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def test_commands_lists_visible_commands_without_starting_the_app() -> None:
    result = run("commands", TARGET)

    assert result.code == 0
    assert result.stdout.splitlines() == [
        "commands:",
        "  add",
        "  boom",
        "  confirm",
        "  greet (hello)  Say hello.",
        "  info",
        "  interrupt",
        "  survive",
    ]
    assert cli_app.EVENTS == []


def test_commands_all_includes_hidden_ones() -> None:
    result = run("commands", "--all", TARGET)

    assert "  secret [hidden]" in result.stdout


def test_commands_on_an_app_without_commands() -> None:
    result = run("commands", "tests.integration.cli_app:EmptyModule")

    assert (result.code, result.stdout) == (
        0,
        "no commands declared in tests.integration.cli_app:EmptyModule\n",
    )


def test_commands_reports_target_errors() -> None:
    assert run("commands", "tests.integration.cli_app:Missing").code == 3


# ---------------------------------------------------------------------------
# console
# ---------------------------------------------------------------------------


@pytest.fixture
def console_input(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    lines: list[str] = []

    def fake_input(self: Any, prompt: str = "") -> str:
        if not lines:
            raise EOFError
        return lines.pop(0)

    monkeypatch.setattr(_AsyncConsole, "raw_input", fake_input)
    yield lines


def test_console_preloads_the_context_and_supports_top_level_await(
    console_input: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    console_input.extend(
        [
            "print(type(ctx).__name__, app is ctx.app, container is ctx.container)",
            "from tests.integration.cli_app import Database",
            "print(get(Database) is await resolve(Database))",
            "async with scope('repl') as s:",
            "    print(s.name)",
            "",
            "result = await ctx.run_command('greet', 'repl')",
            "print(result.stdout.strip())",
        ]
    )

    code = main(["console", TARGET])

    out = capsys.readouterr()
    assert code == 0
    assert out.out.splitlines() == ["ApplicationContext True True", "True", "repl", "hello repl"]
    assert "lauren console: tests.integration.cli_app:AppModule" in out.err
    assert cli_app.EVENTS == ["db.open", "db.close"]


def test_console_shows_tracebacks_and_continues(
    console_input: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    console_input.extend(["1 / 0", "await asyncio.sleep(0) or missing_name", "print('still here')"])

    assert main(["console", TARGET]) == 0

    out = capsys.readouterr()
    assert "ZeroDivisionError" in out.err
    assert "NameError" in out.err
    assert out.out.strip() == "still here"


def test_console_exit_shuts_the_app_down(console_input: list[str]) -> None:
    console_input.extend(["exit()", "print('never')"])

    assert main(["console", TARGET]) == 0
    assert cli_app.EVENTS == ["db.open", "db.close"]


def test_console_startup_failure_exits_3(
    console_input: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["console", "tests.integration.cli_app:BrokenStartupModule"])

    assert code == 3
    assert "cannot connect" in capsys.readouterr().err


def test_console_target_error(console_input: list[str]) -> None:
    assert main(["console", "nope"]) == 2
