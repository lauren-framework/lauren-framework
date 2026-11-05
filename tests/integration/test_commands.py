"""Integration tests for ``@command`` discovery and execution (PRD F19 §5.4-5.8).

Commands run through ``ApplicationContext.run_command`` on a real app built by
``LaurenFactory.create_context``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import io
import json
import sys
import threading
from typing import Any

import pytest

from lauren import (
    LaurenFactory,
    Scope,
    StdlibJSONEncoder,
    injectable,
    module,
    post_construct,
    pre_destruct,
)
from lauren.commands import CommandContext, command
from lauren.exceptions import DuplicateBindingError, UnresolvableParameterError

EVENTS: list[str] = []


@pytest.fixture(autouse=True)
def _reset() -> None:
    EVENTS.clear()


# ---------------------------------------------------------------------------
# A two-module app
# ---------------------------------------------------------------------------


@injectable()
class Database:
    def __init__(self) -> None:
        self.rows: list[str] = []

    @post_construct
    def connect(self) -> None:
        EVENTS.append("Database.connect")

    @pre_destruct
    def close(self) -> None:
        EVENTS.append("Database.close")


@injectable()
class Secrets:
    """Not exported from UsersModule."""

    token = "s3cret"


@injectable(scope=Scope.REQUEST)
class UnitOfWork:
    def __init__(self, db: Database, ctx: CommandContext) -> None:
        self.db = db
        self.command = ctx.name
        EVENTS.append("UnitOfWork.open")

    @pre_destruct
    async def commit(self) -> None:
        EVENTS.append("UnitOfWork.pre_destruct")

    async def aclose(self) -> None:
        EVENTS.append("UnitOfWork.aclose")


@dataclasses.dataclass
class Summary:
    tables: int
    rows: int


@injectable()
class UserCommands:
    def __init__(self, db: Database) -> None:
        self._db = db

    @command("users:add", aliases=("add-user",))
    async def add(self, ctx: CommandContext, uow: UnitOfWork, name: str, *, admin: bool = False) -> None:
        """Add a user."""
        self._db.rows.append(name)
        ctx.echo(f"added {name}{' (admin)' if admin else ''} in {uow.command}")

    @command("users:count")
    def count(self) -> int:
        return len(self._db.rows)

    @command("users:secret")
    def secret(self, secrets: Secrets) -> str:
        return secrets.token

    @command("users:purge")
    async def purge(self, ctx: CommandContext) -> int:
        if not ctx.confirm("Delete every user?"):
            ctx.error("aborted")
            return 1
        self._db.rows.clear()
        ctx.echo("purged")
        return 0


@module(providers=[Database, Secrets, UnitOfWork, UserCommands], exports=[Database, UnitOfWork])
class UsersModule:
    pass


@injectable()
class OutcomeCommands:
    @command("ok-none")
    async def returns_none(self) -> None:
        return None

    @command("ok-int")
    async def returns_int(self, code: int) -> int:
        return code

    @command("ok-bool")
    async def returns_bool(self, flag: bool = False) -> bool:
        return flag

    @command("ok-json")
    def returns_json(self) -> dict[str, Any]:
        return {"tables": 2, "rows": [1, 2]}

    @command("ok-dataclass")
    async def returns_dataclass(self) -> Summary:
        return Summary(tables=2, rows=1830)

    @command("bad-json")
    async def returns_unencodable(self) -> Any:
        return threading.Lock()

    @command("fail")
    async def fails(self, ctx: CommandContext, *, code: int = 1) -> None:
        ctx.fail("cannot continue", exit_code=code)

    @command("exit-message")
    def exits_with_message(self) -> None:
        sys.exit("plain message")

    @command("exit-none")
    def exits_with_none(self) -> None:
        sys.exit()

    @command("boom")
    async def raises(self, uow: UnitOfWork) -> None:
        raise RuntimeError("kaboom")

    @command("thread")
    def thread_name(self) -> str:
        return threading.current_thread().name

    @command("static")
    @staticmethod
    def static_command(value: int) -> int:
        return value + 1

    @command("cls")
    @classmethod
    def class_command(cls, value: int) -> str:
        return f"{cls.__name__}:{value}"

    @command("sleep")
    async def sleeps(self, uow: UnitOfWork) -> None:
        try:
            EVENTS.append("sleep.start")
            await asyncio.sleep(30)
        finally:
            EVENTS.append("sleep.finally")

    @command("hidden", hidden=True)
    async def hidden(self) -> str:
        return "found"


@module(imports=[UsersModule], providers=[OutcomeCommands])
class AppModule:
    pass


async def _run(*argv: str, **kwargs: Any) -> Any:
    async with LaurenFactory.create_context(AppModule) as ctx:
        return await ctx.run_command(*argv, **kwargs)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_commands_are_discovered_across_modules_without_starting() -> None:
    ctx = LaurenFactory.create_context(AppModule)

    infos = {info.name: info for info in ctx.commands()}

    assert {"users:add", "users:count", "ok-json", "static", "cls"} <= set(infos)
    assert "hidden" not in infos
    assert infos["users:add"].module is UsersModule
    assert infos["ok-json"].module is AppModule
    assert infos["users:add"].help == "Add a user."
    assert EVENTS == []  # listing does not start the app


def test_hidden_commands_are_listed_on_request() -> None:
    ctx = LaurenFactory.create_context(AppModule)

    assert "hidden" in {info.name for info in ctx.commands(include_hidden=True)}


async def test_hidden_commands_still_run() -> None:
    result = await _run("hidden")

    assert (result.exit_code, result.value) == (0, "found")


async def test_an_alias_runs_the_command() -> None:
    result = await _run("add-user", "ada")

    assert result.exit_code == 0
    assert result.stdout == "added ada in users:add\n"


def test_dependencies_resolve_in_the_providers_own_module() -> None:
    @injectable()
    class Leaky:
        @command("leak")
        def leak(self, secrets: Secrets) -> str:  # pragma: no cover - startup fails
            return secrets.token

    @module(imports=[UsersModule], providers=[Leaky])
    class LeakyModule:
        pass

    with pytest.raises(UnresolvableParameterError) as info:
        LaurenFactory.create(LeakyModule)

    assert info.value.detail["param"] == "secrets"
    assert info.value.detail["command"] == "leak"


async def test_a_private_dependency_is_visible_to_its_own_modules_command() -> None:
    result = await _run("users:secret")

    assert result.stdout == '"s3cret"\n'


async def test_an_exported_dependency_is_visible_from_another_module() -> None:
    @injectable()
    class Reader:
        @command("read")
        def read(self, db: Database) -> int:
            return len(db.rows) + 7

    @module(imports=[UsersModule], providers=[Reader])
    class ReaderModule:
        pass

    async with LaurenFactory.create_context(ReaderModule) as ctx:
        assert (await ctx.run_command("read")).exit_code == 7


@pytest.mark.parametrize(
    ("first", "second", "name"),
    [
        (("dup",), ("dup",), "dup"),
        (("one", ("dup",)), ("dup",), "dup"),
        (("one", ("dup",)), ("two", ("dup",)), "dup"),
    ],
)
def test_duplicate_names_and_aliases_fail_startup(
    first: tuple[Any, ...], second: tuple[Any, ...], name: str
) -> None:
    def build(spec: tuple[Any, ...]) -> Any:
        aliases = spec[1] if len(spec) > 1 else ()
        return command(spec[0], aliases=aliases)(lambda self: None)

    a = injectable()(type("A", (), {"run": build(first)}))
    b = injectable()(type("B", (), {"run": build(second)}))
    root = module(providers=[a, b])(type("DupModule", (), {}))

    with pytest.raises(DuplicateBindingError) as info:
        LaurenFactory.create(root)

    assert info.value.detail["name"] == name
    assert info.value.detail["targets"] == ["A.run", "B.run"]
    assert info.value.detail["reason"] == "duplicate_command_name"


def test_an_app_without_commands_lists_none() -> None:
    @module(providers=[Database])
    class Plain:
        pass

    assert LaurenFactory.create_context(Plain).commands() == ()


async def test_an_app_without_commands_reports_unknown_names() -> None:
    @module(providers=[Database])
    class Plain:
        pass

    async with LaurenFactory.create_context(Plain) as ctx:
        result = await ctx.run_command("anything")

    assert result.exit_code == 2
    assert "none declared" in result.stderr


# ---------------------------------------------------------------------------
# Results and exit codes (§5.6)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "exit_code", "value", "stdout"),
    [
        (("ok-none",), 0, None, ""),
        (("ok-int", "5"), 5, 5, ""),
        (("ok-bool", "--flag"), 0, True, ""),
        (("ok-bool",), 1, False, ""),
        (("ok-json",), 0, {"tables": 2, "rows": [1, 2]}, '{"tables": 2, "rows": [1, 2]}\n'),
        (("static", "1"), 2, 2, ""),
        (("cls", "3"), 0, "OutcomeCommands:3", '"OutcomeCommands:3"\n'),
    ],
)
async def test_return_values_map_to_exit_codes(
    argv: tuple[str, ...], exit_code: int, value: Any, stdout: str
) -> None:
    result = await _run(*argv)

    assert (result.exit_code, result.value, result.stdout, result.stderr) == (exit_code, value, stdout, "")


async def test_other_return_values_use_the_apps_encoder() -> None:
    class Upper(StdlibJSONEncoder):
        def encode(self, value: Any) -> bytes:
            return super().encode(value).upper()

    async with LaurenFactory.create_context(AppModule, json_encoder=Upper()) as ctx:
        result = await ctx.run_command("ok-dataclass")

    assert json.loads(result.stdout) == {"TABLES": 2, "ROWS": 1830}


async def test_an_unencodable_return_value_exits_1() -> None:
    result = await _run("bad-json")

    assert result.exit_code == 1
    assert "cannot be encoded as JSON" in result.stderr
    assert result.exception is not None


@pytest.mark.parametrize(("argv", "exit_code"), [(("fail",), 1), (("fail", "--code", "4"), 4)])
async def test_fail_exits_with_its_code_and_message(argv: tuple[str, ...], exit_code: int) -> None:
    result = await _run(*argv)

    assert (result.exit_code, result.stderr, result.exception) == (exit_code, "cannot continue\n", None)


async def test_sys_exit_with_a_message_exits_1() -> None:
    result = await _run("exit-message")

    assert (result.exit_code, result.stderr) == (1, "plain message\n")


async def test_sys_exit_without_a_code_exits_0() -> None:
    assert (await _run("exit-none")).exit_code == 0


async def test_an_uncaught_exception_exits_1_with_a_summary() -> None:
    result = await _run("boom")

    assert result.exit_code == 1
    assert result.stderr == "error: RuntimeError: kaboom (run with --verbose for the traceback)\n"
    assert isinstance(result.exception, RuntimeError)
    assert "UnitOfWork.aclose" in EVENTS  # the scope is torn down after the failure


@pytest.mark.parametrize("how", ["flag", "kwarg"])
async def test_verbose_prints_the_traceback(how: str) -> None:
    result = await (_run("boom", "--verbose") if how == "flag" else _run("boom", verbose=True))

    assert "Traceback (most recent call last)" in result.stderr
    assert "RuntimeError: kaboom" in result.stderr


async def test_argument_errors_exit_2_with_usage() -> None:
    result = await _run("ok-int", "five")

    assert result.exit_code == 2
    assert result.stderr.startswith("usage: ok-int [-h] code\n")
    assert "ok-int: error: argument code: invalid value 'five'" in result.stderr


async def test_help_exits_0_with_the_help_on_stdout() -> None:
    result = await _run("users:add", "--help")

    assert result.exit_code == 0
    assert "usage: users:add [-h] [--admin | --no-admin] name" in result.stdout
    assert "Add a user." in result.stdout


async def test_unknown_commands_exit_2_with_suggestions() -> None:
    result = await _run("users:ad")

    assert result.exit_code == 2
    assert "did you mean 'users:add'" in result.stderr


async def test_non_string_arguments_are_stringified() -> None:
    result = await _run("ok-int", 9)  # type: ignore[arg-type]

    assert result.exit_code == 9


# ---------------------------------------------------------------------------
# Scopes, DI and threads
# ---------------------------------------------------------------------------


async def test_each_invocation_gets_its_own_scope_torn_down_after() -> None:
    async with LaurenFactory.create_context(AppModule) as ctx:
        await ctx.run_command("users:add", "ada")
        await ctx.run_command("users:add", "grace", "--admin")
        count = await ctx.run_command("users:count")

    assert count.exit_code == 2
    assert EVENTS.count("UnitOfWork.open") == 2
    assert EVENTS.count("UnitOfWork.pre_destruct") == 2
    assert EVENTS.count("UnitOfWork.aclose") == 2
    first_close = EVENTS.index("UnitOfWork.pre_destruct")
    assert EVENTS.index("UnitOfWork.aclose") == first_close + 1  # @pre_destruct, then aclose()
    assert EVENTS[0] == "Database.connect" and EVENTS[-1] == "Database.close"


async def test_command_context_is_injectable_into_request_scoped_providers() -> None:
    result = await _run("users:add", "ada")

    assert result.stdout == "added ada in users:add\n"


async def test_a_request_scoped_command_class_is_built_per_invocation() -> None:
    @injectable(scope=Scope.REQUEST)
    class PerRun:
        def __init__(self) -> None:
            EVENTS.append("PerRun.init")

        @command("per-run")
        async def run(self) -> int:
            return id(self) % 100 + 1

        async def aclose(self) -> None:
            EVENTS.append("PerRun.aclose")

    @module(providers=[PerRun])
    class PerRunModule:
        pass

    async with LaurenFactory.create_context(PerRunModule) as ctx:
        await ctx.run_command("per-run")
        await ctx.run_command("per-run")

    assert EVENTS == ["PerRun.init", "PerRun.aclose", "PerRun.init", "PerRun.aclose"]


async def test_sync_commands_run_in_a_worker_thread() -> None:
    result = await _run("thread")

    assert result.value != threading.current_thread().name


async def test_cancellation_tears_the_scope_down_and_propagates() -> None:
    async with LaurenFactory.create_context(AppModule) as ctx:
        task = asyncio.ensure_future(ctx.run_command("sleep"))
        while "sleep.start" not in EVENTS:
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert EVENTS[-4:-1] == ["sleep.finally", "UnitOfWork.pre_destruct", "UnitOfWork.aclose"]


# ---------------------------------------------------------------------------
# Confirmation prompts
# ---------------------------------------------------------------------------


async def test_confirm_refuses_when_not_interactive() -> None:
    result = await _run("users:purge")

    assert result.exit_code == 1
    assert "refused: not running interactively" in result.stderr
    assert "aborted" in result.stderr


@pytest.mark.parametrize("how", ["flag", "kwarg"])
async def test_yes_confirms(how: str) -> None:
    result = await (_run("users:purge", "--yes") if how == "flag" else _run("users:purge", assume_yes=True))

    assert (result.exit_code, result.stdout) == (0, "purged\n")


async def test_an_interactive_answer_is_read_from_stdin() -> None:
    result = await _run("users:purge", interactive=True, stdin=io.StringIO("y\n"))

    assert result.exit_code == 0
    assert result.stdout == "Delete every user? [y/N] purged\n"
