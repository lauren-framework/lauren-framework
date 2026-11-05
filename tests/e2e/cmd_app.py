"""Target module for ``tests/e2e/test_cli_exec_e2e.py`` (run in a subprocess).

Lifecycle events are appended to the file named by ``$CMD_APP_MARKER`` so the
test can prove teardown ran after the process exited.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

from lauren import Scope, injectable, module, post_construct, pre_destruct
from lauren.commands import CommandContext, Opt, command


def _mark(event: str) -> None:
    marker = os.environ.get("CMD_APP_MARKER")
    if marker:
        with Path(marker).open("a", encoding="utf-8") as handle:
            handle.write(event + "\n")


@injectable()
class Database:
    @post_construct
    def connect(self) -> None:
        _mark("post_construct")

    @pre_destruct
    def disconnect(self) -> None:
        _mark("pre_destruct")


@injectable(scope=Scope.REQUEST)
class DbSession:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def aclose(self) -> None:
        _mark("session.aclose")


@injectable()
class SeedCommands:
    @command("db:seed", help="Insert fixture data.")
    async def seed(
        self,
        ctx: CommandContext,
        session: DbSession,
        *,
        count: Annotated[int, Opt(short="-n", help="rows per table")] = 50,
    ) -> None:
        ctx.echo(f"seeded {count} rows")

    @command("db:fail")
    async def fail(self) -> None:
        raise RuntimeError("database is gone")

    @command("db:wait")
    async def wait(self, ctx: CommandContext, session: DbSession) -> None:
        try:
            ctx.echo("sleeping")
            await asyncio.sleep(60)
        finally:
            _mark("finally")


@module(providers=[Database, DbSession, SeedCommands])
class AppModule:
    pass


class Unregistered:
    pass


@injectable()
class NeedsMissing:
    def __init__(self, dep: Unregistered) -> None:
        self.dep = dep


@module(providers=[NeedsMissing, SeedCommands, DbSession, Database])
class BrokenModule:
    pass
