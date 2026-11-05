"""DI-aware commands: admin and maintenance tasks that run with the app's DI graph.

A command is a method on any provider, marked with :func:`command`. It is
discovered and validated in ``LaurenFactory.create`` like a route, and runs
inside its own :class:`~lauren.context.OperationScope`, so request-scoped
dependencies are created per invocation and torn down afterwards::

    from typing import Annotated

    from lauren import injectable
    from lauren.commands import CommandContext, Opt, command


    @injectable()
    class SeedCommands:
        def __init__(self, db: Database) -> None:
            self._db = db

        @command("db:seed", help="Insert fixture data.")
        async def seed(
            self,
            ctx: CommandContext,
            fixture: str = "demo",                                         # positional, optional
            *,
            count: Annotated[int, Opt(short="-n", help="rows per table")] = 50,  # --count / -n
            reset: bool = False,                                           # --reset / --no-reset
        ) -> int:
            if reset and not ctx.confirm("Truncate every table?"):
                return 1
            ctx.echo(f"seeded {await self._db.seed(fixture, count)} rows")
            return 0

Run it with ``python -m lauren exec app.main:AppModule db:seed --count 3``, or
from a test with ``await ctx.run_command("db:seed", "--count", "3")`` on an
:class:`~lauren.context.ApplicationContext`. Arguments are parsed with the
standard library only (``argparse``); the exit code comes from the return
value (``None`` → 0, an ``int`` → itself, a ``bool`` → 0 / 1, anything else is
printed as JSON → 0).
"""

from __future__ import annotations

from ._commands._compile import CommandInfo, CommandParameter
from ._commands._context import CommandContext, CommandResult
from ._commands._meta import CommandMeta, Opt, command

__all__ = [
    "command",
    "Opt",
    "CommandContext",
    "CommandResult",
    "CommandInfo",
    "CommandParameter",
    "CommandMeta",
]
